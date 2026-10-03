#!/usr/bin/env python3
"""Build the deterministic RatScanner Data.zip release payload.

Item icons, OCR models, map metadata and the unknown-item placeholder are fetched
from their upstream sources on every run. Map SVGs are driven by maps.json: each
interactive entry's `svgPath` is downloaded for the map id that artwork belongs to.
Artwork maps.json references but upstream does not serve, and the per-map banner
art (which has no upstream endpoint at all), are copied forward from the tracked
`maps/` and `banner/` folders in this repository.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import io
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from PIL import Image, UnidentifiedImageError

SVG_NAMESPACE = "http://www.w3.org/2000/svg"

CATALOG_URL = "https://json.tarkov.dev/regular/items"
MAP_CATALOG_URL = "https://json.tarkov.dev/regular/maps"
MAPS_URL = "https://raw.githubusercontent.com/the-hideout/tarkov-dev/main/src/data/maps.json"
TARKOV_DEV_LICENSE_URL = "https://raw.githubusercontent.com/the-hideout/tarkov-dev/main/LICENSE"
TESSDATA_REF = "4.1.0"
TESSDATA_BASE_URL = f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/{TESSDATA_REF}"
TESSDATA_LICENSE_URL = f"{TESSDATA_BASE_URL}/LICENSE"
UNKNOWN_IMAGE_URL = "https://assets.tarkov.dev/unknown-item-base-image.webp"
USER_AGENT = "RatScannerDataBuilder/1.0 (+https://github.com/TarkovTracker-org/RatScannerData)"

SAFE_ITEM_ID = re.compile(r"^[A-Za-z0-9_-]+$")
# Map ids are 24-character hex strings; both carried folders are keyed by them.
SAFE_MAP_ID = re.compile(r"^[0-9a-f]{24}$")
DEFAULT_BANNER = "default.png"

# Map ids are opaque strings upstream, but every id observed is Mongo ObjectId-shaped.
# The pattern guards the archive entry path built from it, not the id itself.
MIN_PREFIX_LENGTH = 4
REQUIRED_ARCHIVE_FILES = (
    "maps.json",
    "unknown.png",
    "traineddata/eng.traineddata",
    "banner/default.png",
    "manifest.json",
    "THIRD_PARTY_NOTICES.md",
)
# Map artwork that maps.json does not reference, or that upstream does not serve,
# must still reach the client, so a bundle with too few SVGs is treated as a failure.
MINIMUM_MAP_SVGS = 8

# RatScanner addresses OCR models by ISO-639-3 code. Tesseract names Simplified
# Chinese "chi_sim"; the rest line up directly.
OCR_LANGUAGES = {
    "ces": "ces",
    "deu": "deu",
    "eng": "eng",
    "fra": "fra",
    "hun": "hun",
    "ita": "ita",
    "jpn": "jpn",
    "kor": "kor",
    "pol": "pol",
    "por": "por",
    "rus": "rus",
    "slk": "slk",
    "spa": "spa",
    "tur": "tur",
    "zho": "chi_sim",
}

_progress_lock = threading.Lock()
_completed_icons = 0


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def fetch_bytes(
    url: str,
    *,
    accept: str = "*/*",
    attempts: int = 5,
    timeout_seconds: int = 60,
) -> bytes:
    """Download a URL with bounded retry/backoff for transient failures."""

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"Only HTTPS sources are allowed: {url}")

    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": accept},
    )
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            retryable = error.code == 429 or 500 <= error.code < 600
            if not retryable or attempt == attempts:
                raise
            retry_after = error.headers.get("Retry-After")
            delay = (
                float(retry_after) if retry_after and retry_after.isdigit() else 2 ** (attempt - 1)
            )
        except (TimeoutError, urllib.error.URLError):
            if attempt == attempts:
                raise
            delay = 2 ** (attempt - 1)
        time.sleep(min(delay, 30))

    raise RuntimeError(f"Download attempts exhausted: {url}")


def write_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def parse_json(content: bytes, source: str) -> Any:
    try:
        return json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid JSON from {source}") from error


def load_catalog(
    catalog_url: str,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], bytes]:
    content = fetch_bytes(catalog_url, accept="application/json")
    document = parse_json(content, catalog_url)
    raw_items = document.get("data", {}).get("items")
    if not isinstance(raw_items, (dict, list)):
        raise ValueError("Catalog does not contain data.items")

    values = raw_items.values() if isinstance(raw_items, dict) else raw_items
    items: list[dict[str, Any]] = []
    skipped_items: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for raw_item in values:
        if not isinstance(raw_item, dict):
            raise ValueError("Catalog contains a non-object item")
        item_id = raw_item.get("id")
        image_url = raw_item.get("baseImageLink")
        if not isinstance(item_id, str) or not SAFE_ITEM_ID.fullmatch(item_id):
            raise ValueError(f"Unsafe or missing item id: {item_id!r}")
        if item_id in seen_ids:
            raise ValueError(f"Duplicate item id: {item_id}")
        if not isinstance(image_url, str):
            raise ValueError(f"Item {item_id} has no baseImageLink")
        image_host = urllib.parse.urlparse(image_url).hostname
        if image_host != "assets.tarkov.dev":
            raise ValueError(f"Item {item_id} has an unexpected image host: {image_host}")

        seen_ids.add(item_id)
        if image_url == UNKNOWN_IMAGE_URL:
            skipped_items.append(
                {
                    "id": item_id,
                    "reason": "generic unknown-item placeholder",
                    "source": image_url,
                }
            )
            continue

        width = raw_item.get("width")
        height = raw_item.get("height")
        if not isinstance(width, int) or width <= 0:
            raise ValueError(f"Item {item_id} has an invalid width: {width!r}")
        if not isinstance(height, int) or height <= 0:
            raise ValueError(f"Item {item_id} has an invalid height: {height!r}")
        items.append(
            {
                "id": item_id,
                "baseImageLink": image_url,
                "declaredWidth": width,
                "declaredHeight": height,
            }
        )

    items.sort(key=lambda item: item["id"])
    skipped_items.sort(key=lambda item: item["id"])
    return items, skipped_items, content


def png_from_image(content: bytes, source: str) -> tuple[bytes, int, int, bool]:
    """Normalise an upstream image to RGBA PNG, as RatEye expects."""

    try:
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            if image.width <= 0 or image.height <= 0:
                raise ValueError(f"Image has invalid dimensions: {source}")
            if image.width > 4096 or image.height > 4096:
                raise ValueError(f"Image dimensions are unexpectedly large: {source}")
            rgba = image.convert("RGBA")
            alpha_minimum, alpha_maximum = rgba.getchannel("A").getextrema()
            output = io.BytesIO()
            rgba.save(output, format="PNG", compress_level=6)
            return (
                output.getvalue(),
                rgba.width,
                rgba.height,
                alpha_minimum < 255 or alpha_maximum < 255,
            )
    except (UnidentifiedImageError, OSError) as error:
        raise ValueError(f"Could not decode image from {source}") from error


def build_icon_group(
    source_url: str,
    items: list[dict[str, Any]],
    icons_directory: Path,
    total_item_count: int,
) -> list[dict[str, Any]]:
    """Download one base image and write it under every item id that shares it."""

    global _completed_icons

    source_content = fetch_bytes(source_url, accept="image/webp,image/png,image/*")
    png, width, height, has_transparency = png_from_image(source_content, source_url)
    if width % 63 != 1 or height % 63 != 1:
        raise ValueError(
            "Image dimensions are incompatible with RatEye's 63-pixel slot "
            f"geometry: {width}x{height} from {source_url}"
        )
    digest = sha256_bytes(png)
    entries: list[dict[str, Any]] = []
    for item in items:
        item_id = item["id"]
        relative_path = f"icons/{item_id}.png"
        write_bytes_atomic(icons_directory / f"{item_id}.png", png)

        rendered_slots = {"width": (width - 1) // 63, "height": (height - 1) // 63}
        declared_slots = {
            "width": item["declaredWidth"],
            "height": item["declaredHeight"],
        }
        entries.append(
            {
                "path": relative_path,
                "sha256": digest,
                "size": len(png),
                "width": width,
                "height": height,
                "transparent": has_transparency,
                "renderedSlots": rendered_slots,
                "declaredSlots": declared_slots,
                "slotDimensionsMatch": rendered_slots == declared_slots,
                "source": source_url,
            }
        )

    with _progress_lock:
        _completed_icons += len(items)
        completed = _completed_icons
    if completed == total_item_count or completed % 100 == 0:
        print(f"Generated {completed}/{total_item_count} icons", flush=True)

    return entries


def install_icons(
    items: list[dict[str, Any]],
    icons_directory: Path,
    workers: int,
) -> list[dict[str, Any]]:
    # Many items share a single base image, so group first and download each
    # distinct image exactly once.
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(item["baseImageLink"], []).append(item)

    entries: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                build_icon_group,
                source_url,
                grouped_items,
                icons_directory,
                len(items),
            )
            for source_url, grouped_items in grouped.items()
        ]
        for future in concurrent.futures.as_completed(futures):
            entries.extend(future.result())

    entries.sort(key=lambda entry: entry["path"])
    return entries


def install_maps(data_directory: Path) -> tuple[dict[str, Any], list[Any]]:
    """Download and validate maps.json, returning it for SVG target resolution."""

    content = fetch_bytes(MAPS_URL, accept="application/json")
    document = parse_json(content, MAPS_URL)
    if not isinstance(document, list) or not document:
        raise ValueError("maps.json must be a non-empty array")
    for map_group in document:
        if not isinstance(map_group, dict):
            raise ValueError("maps.json contains a non-object entry")
        if not isinstance(map_group.get("normalizedName"), str):
            raise ValueError("maps.json entry is missing normalizedName")
        if not isinstance(map_group.get("maps"), list):
            raise ValueError("maps.json entry is missing maps")
    write_bytes_atomic(data_directory / "maps.json", content)
    entry = {
        "path": "maps.json",
        "sha256": sha256_bytes(content),
        "size": len(content),
        "source": MAPS_URL,
    }
    return entry, document


def install_unknown_icon(data_directory: Path) -> dict[str, Any]:
    source = fetch_bytes(UNKNOWN_IMAGE_URL, accept="image/webp,image/png,image/*")
    png, width, height, has_transparency = png_from_image(source, UNKNOWN_IMAGE_URL)
    write_bytes_atomic(data_directory / "unknown.png", png)
    return {
        "path": "unknown.png",
        "sha256": sha256_bytes(png),
        "size": len(png),
        "width": width,
        "height": height,
        "transparent": has_transparency,
        "source": UNKNOWN_IMAGE_URL,
    }


def install_ocr_models(data_directory: Path, workers: int) -> list[dict[str, Any]]:
    traineddata_directory = data_directory / "traineddata"

    def install(output_code: str, source_code: str) -> dict[str, Any]:
        source_url = f"{TESSDATA_BASE_URL}/{source_code}.traineddata"
        content = fetch_bytes(source_url)
        if len(content) < 100_000:
            raise ValueError(f"OCR model is unexpectedly small: {source_url}")
        write_bytes_atomic(traineddata_directory / f"{output_code}.traineddata", content)
        return {
            "path": f"traineddata/{output_code}.traineddata",
            "sha256": sha256_bytes(content),
            "size": len(content),
            "source": source_url,
        }

    entries: list[dict[str, Any]] = []
    max_workers = min(workers, len(OCR_LANGUAGES))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(install, output_code, source_code)
            for output_code, source_code in OCR_LANGUAGES.items()
        ]
        for future in concurrent.futures.as_completed(futures):
            entries.append(future.result())

    entries.sort(key=lambda entry: entry["path"])
    return entries


def load_map_catalog() -> dict[str, dict[str, Any]]:
    """normalizedName -> map id, from the tarkov.dev map catalog."""

    content = fetch_bytes(MAP_CATALOG_URL, accept="application/json")
    document = parse_json(content, MAP_CATALOG_URL)
    raw_maps = document.get("data", {}).get("maps")
    if not isinstance(raw_maps, dict):
        raise ValueError("Map catalog does not contain data.maps")

    catalog: dict[str, dict[str, Any]] = {}
    for map_id, raw_map in raw_maps.items():
        if not isinstance(raw_map, dict):
            continue
        normalized_name = raw_map.get("normalizedName")
        if isinstance(map_id, str) and isinstance(normalized_name, str):
            catalog[normalized_name] = {"id": map_id, "name": raw_map.get("name")}

    if not catalog:
        raise ValueError("Map catalog is empty")
    return catalog


def resolve_map_ids(map_catalog: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Map a maps.json normalizedName to the map id(s) its artwork serves.

    maps.json has one entry per artwork while the catalog has a separate map per
    variant, so "ground-zero" is the artwork behind "ground-zero",
    "ground-zero-21" and "ground-zero-tutorial". Exact matches win; otherwise the
    shortest longer name sharing the prefix is used, mirroring how RatScanner
    resolves maps at runtime.
    """

    resolved: dict[str, str] = {}
    for normalized_name in map_catalog:
        if normalized_name in map_catalog:
            resolved[normalized_name] = map_catalog[normalized_name]["id"]
            continue
        candidates = [
            name
            for name in map_catalog
            if name.startswith(normalized_name) and len(normalized_name) >= MIN_PREFIX_LENGTH
        ]
        if candidates:
            shortest = min(candidates, key=lambda name: (len(name), name))
            resolved[normalized_name] = map_catalog[shortest]["id"]

    return resolved


def load_svg_targets(
    maps_document: list[Any],
    map_catalog: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    """One download target per interactive maps.json entry that has an svgPath."""

    resolved = resolve_map_ids(map_catalog)
    targets: dict[str, dict[str, str]] = {}

    for map_group in maps_document:
        if not isinstance(map_group, dict):
            raise ValueError("maps.json contains a non-object entry")
        normalized_name = map_group.get("normalizedName")
        if not isinstance(normalized_name, str):
            raise ValueError("maps.json entry is missing normalizedName")
        entries = map_group.get("maps")
        if not isinstance(entries, list):
            raise ValueError("maps.json entry is missing maps")

        # Only the interactive projection carries SVG artwork; the 2D/3D entries
        # are raster tile layers and duplicate the same svgPath when present.
        svg_paths = {
            entry.get("svgPath")
            for entry in entries
            if isinstance(entry, dict)
            and entry.get("projection") == "interactive"
            and isinstance(entry.get("svgPath"), str)
        }
        if not svg_paths:
            continue

        map_id = resolved.get(normalized_name)
        if map_id is None or not SAFE_MAP_ID.fullmatch(map_id):
            # An artwork with no resolvable map id has no destination filename.
            print(f"  skipping {normalized_name}: no map id in the catalog", flush=True)
            continue

        for svg_path in sorted(svg_paths):
            if map_id in targets:
                # Several names can share one artwork; one copy is enough.
                continue
            targets[map_id] = {
                "mapId": map_id,
                "normalizedName": normalized_name,
                "svgPath": svg_path,
            }

    return [targets[map_id] for map_id in sorted(targets)]


def validate_svg(content: bytes, source: str) -> None:
    """Check an SVG is parseable and carries a usable viewBox.

    RatScanner's map viewer derives the canvas aspect ratio from the viewBox, and
    the SVG is served to a WebView, so a malformed or unsized document would render
    as an empty map. The bytes are stored exactly as upstream serves them: the file
    is inlined into the page, so re-serializing it would rewrite namespace prefixes
    and reformat the markup for no benefit.
    """

    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as error:
        raise ValueError(f"Could not parse SVG from {source}") from error

    tag = root.tag
    if tag != "svg" and tag != f"{{{SVG_NAMESPACE}}}svg":
        raise ValueError(f"SVG from {source} has no root <svg> element")

    view_box = root.get("viewBox")
    if not view_box:
        raise ValueError(f"SVG from {source} has no viewBox")
    parts = view_box.split()
    if len(parts) != 4:
        raise ValueError(f"SVG from {source} has a malformed viewBox: {view_box}")
    try:
        view_width, view_height = float(parts[2]), float(parts[3])
    except ValueError as error:
        raise ValueError(f"SVG from {source} has a non-numeric viewBox: {view_box}") from error
    if view_width <= 0 or view_height <= 0:
        raise ValueError(f"SVG from {source} has an empty viewBox: {view_box}")


def download_svg(target: dict[str, str], destination: Path) -> dict[str, Any]:
    source_url = target["svgPath"]
    parsed = urllib.parse.urlparse(source_url)
    if parsed.scheme != "https" or parsed.hostname != "assets.tarkov.dev":
        raise ValueError(f"Unexpected SVG host for {target['normalizedName']}: {source_url}")

    content = fetch_bytes(source_url, accept="image/svg+xml,image/*")
    validate_svg(content, source_url)
    relative_path = f"maps/{target['mapId']}.svg"
    write_bytes_atomic(destination / relative_path, content)
    return {
        "path": relative_path,
        "sha256": sha256_bytes(content),
        "size": len(content),
        "normalizedName": target["normalizedName"],
        "source": source_url,
    }


def install_map_svgs(
    data_directory: Path,
    targets: list[dict[str, str]],
    repository_root: Path,
    workers: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Populate maps/{mapId}.svg from maps.json's svgPath values.

    Artwork maps.json references but upstream does not serve is taken from the
    tracked `maps/` folder instead, and any tracked file maps.json no longer
    references is carried forward so maps RatScanner exposes by id do not
    disappear from the bundle.
    """

    entries: list[dict[str, Any]] = []

    def download(target: dict[str, str]) -> dict[str, Any]:
        return download_svg(target, data_directory)

    failures: list[dict[str, str]] = []
    if targets:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(workers, len(targets))
        ) as executor:
            futures = {executor.submit(download, target): target for target in targets}
            for future in concurrent.futures.as_completed(futures):
                target = futures[future]
                try:
                    entries.append(future.result())
                except Exception as error:  # noqa: BLE001 - fall back to the tracked copy
                    failures.append(
                        {
                            "mapId": target["mapId"],
                            "normalizedName": target["normalizedName"],
                            "source": target["svgPath"],
                            "reason": str(error),
                        }
                    )

    entries.sort(key=lambda entry: entry["path"])

    requested_ids = {target["mapId"] for target in targets}
    fallback_entries, missing = carry_forward_svgs(
        data_directory,
        repository_root,
        entries,
        requested_ids,
    )
    entries.extend(fallback_entries)
    entries.sort(key=lambda entry: entry["path"])

    if len(entries) < MINIMUM_MAP_SVGS:
        raise ValueError(
            f"Only {len(entries)} map SVGs were produced; expected at least {MINIMUM_MAP_SVGS}. "
            f"Download failures: {failures}; missing from maps/: {missing}"
        )

    return entries, failures + missing


def carry_forward_svgs(
    data_directory: Path,
    repository_root: Path,
    entries: list[dict[str, Any]],
    requested_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Copy tracked maps/ files that were not fetched from upstream."""

    source_directory = repository_root / "maps"
    if not source_directory.is_dir():
        return [], []

    produced = {entry["path"] for entry in entries}
    carried: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    for path in sorted(source_directory.iterdir()):
        if not path.is_file():
            continue
        if path.suffix != ".svg":
            raise ValueError(f"Unexpected file in maps/: {path.name}")
        if not SAFE_MAP_ID.fullmatch(path.stem):
            raise ValueError(f"Unexpected name in maps/: {path.name}")

        relative_path = f"maps/{path.name}"
        if relative_path in produced:
            continue

        reason = (
            "referenced by maps.json but upstream download failed"
            if path.stem in requested_ids
            else "not referenced by maps.json; carried forward for map ids RatScanner exposes"
        )
        content = path.read_bytes()
        write_bytes_atomic(data_directory / relative_path, content)
        carried.append(
            {
                "path": relative_path,
                "sha256": sha256_bytes(content),
                "size": len(content),
                "source": f"repository/maps/{path.name}",
                "carriedReason": reason,
            }
        )
        skipped.append({"mapId": path.stem, "reason": reason, "source": relative_path})

    return carried, skipped


def install_banners(
    data_directory: Path,
    repository_root: Path,
    map_ids: set[str],
) -> list[dict[str, Any]]:
    """Carry the banner art forward.

    No endpoint publishes the per-map banner PNGs, so `banner/` stays tracked.
    Every map id this build produced artwork for gets a banner when one is
    tracked, which keeps the pairing with maps/ intact as maps.json changes.
    """

    source_directory = repository_root / "banner"
    if not source_directory.is_dir():
        raise ValueError("Required tracked folder is missing: banner/")

    entries: list[dict[str, Any]] = []
    default_stem = DEFAULT_BANNER.removesuffix(".png")
    found_default = False

    for path in sorted(source_directory.iterdir()):
        if not path.is_file():
            continue
        if path.suffix != ".png":
            raise ValueError(f"Unexpected file in banner/: {path.name}")

        stem = path.stem
        if stem == default_stem:
            found_default = True
        elif not SAFE_MAP_ID.fullmatch(stem):
            raise ValueError(f"Unexpected name in banner/: {path.name}")
        elif stem not in map_ids:
            # No artwork for this map in the bundle, so its banner has no consumer.
            continue

        content = path.read_bytes()
        relative_path = f"banner/{path.name}"
        write_bytes_atomic(data_directory / relative_path, content)
        entries.append(
            {
                "path": relative_path,
                "sha256": sha256_bytes(content),
                "size": len(content),
                "source": f"repository/banner/{path.name}",
            }
        )

    if not found_default:
        raise ValueError(f"banner/{DEFAULT_BANNER} is required")

    return entries


def install_notices(data_directory: Path, repository_root: Path) -> list[dict[str, Any]]:
    notice_source = repository_root / "THIRD_PARTY_NOTICES.md"
    notice_content = notice_source.read_bytes()
    write_bytes_atomic(data_directory / notice_source.name, notice_content)

    license_sources = (
        ("licenses/tarkov-dev-MIT.txt", TARKOV_DEV_LICENSE_URL),
        ("licenses/tessdata-Apache-2.0.txt", TESSDATA_LICENSE_URL),
    )
    entries = [
        {
            "path": notice_source.name,
            "sha256": sha256_bytes(notice_content),
            "size": len(notice_content),
            "source": "repository/THIRD_PARTY_NOTICES.md",
        }
    ]
    for relative_path, source_url in license_sources:
        content = fetch_bytes(source_url, accept="text/plain")
        write_bytes_atomic(data_directory / relative_path, content)
        entries.append(
            {
                "path": relative_path,
                "sha256": sha256_bytes(content),
                "size": len(content),
                "source": source_url,
            }
        )
    return entries


def content_digest(entries: Iterable[dict[str, Any]]) -> str:
    """Hash of the bundle contents, independent of archive metadata.

    Two builds of the same bytes produce the same digest, which is what lets the
    workflow skip a release when nothing upstream actually changed.
    """

    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda value: value["path"]):
        digest.update(entry["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(entry["sha256"].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def write_manifest(
    data_directory: Path,
    release_directory: Path,
    *,
    catalog_url: str,
    catalog_content: bytes,
    icon_entries: list[dict[str, Any]],
    other_entries: list[dict[str, Any]],
    carried_entries: list[dict[str, Any]],
    map_notes: list[dict[str, str]],
    skipped_items: list[dict[str, str]],
) -> dict[str, Any]:
    content_entries = sorted(
        icon_entries + other_entries + carried_entries,
        key=lambda entry: entry["path"],
    )
    source_counts = Counter(entry["source"] for entry in icon_entries)
    # Counted by top-level folder so the manifest reports "maps" and "banner"
    # separately, independent of how each entry's source is spelled.
    carried_counts = Counter(entry["path"].split("/", 1)[0] for entry in carried_entries)
    map_entries = [entry for entry in carried_entries if entry["path"].startswith("maps/")]
    manifest = {
        "schemaVersion": 1,
        "contentSha256": content_digest(content_entries),
        "catalogSha256": sha256_bytes(catalog_content),
        "catalogItemCount": len(icon_entries) + len(skipped_items),
        "iconCount": len(icon_entries),
        "skippedItemCount": len(skipped_items),
        "uniqueIconSourceCount": len(source_counts),
        "sharedIconSourceGroupCount": sum(count > 1 for count in source_counts.values()),
        "slotDimensionMismatchCount": sum(
            not entry["slotDimensionsMatch"] for entry in icon_entries
        ),
        "mapSvgCount": len(map_entries),
        "mapSvgDownloadedCount": sum("carriedReason" not in entry for entry in map_entries),
        "bannerCount": sum(entry["path"].startswith("banner/") for entry in carried_entries),
        "fileCount": len(content_entries),
        "sources": {
            "catalog": catalog_url,
            "mapCatalog": MAP_CATALOG_URL,
            "maps": MAPS_URL,
            "unknownImage": UNKNOWN_IMAGE_URL,
            "ocr": f"{TESSDATA_BASE_URL}/",
        },
        "carriedFolders": dict(sorted(carried_counts.items())),
        "mapNotes": map_notes,
        "skippedItems": skipped_items,
        "files": content_entries,
    }
    encoded = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    write_bytes_atomic(data_directory / "manifest.json", encoded)
    write_bytes_atomic(release_directory / "manifest.json", encoded)
    return manifest


def deterministic_zip(source_directory: Path, archive_path: Path) -> None:
    """Zip with fixed timestamps and permissions so the bytes are reproducible."""

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        archive_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as archive:
        for path in sorted(source_directory.rglob("*")):
            if not path.is_file():
                continue
            relative_path = path.relative_to(source_directory).as_posix()
            info = zipfile.ZipInfo(relative_path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes(), compresslevel=6)


def validate_output(
    data_directory: Path,
    archive_path: Path,
    expected_icon_count: int,
    expected_map_svg_count: int,
) -> None:
    for relative_path in REQUIRED_ARCHIVE_FILES:
        if not (data_directory / relative_path).is_file():
            raise ValueError(f"Required output is missing: {relative_path}")

    icon_count = len(list((data_directory / "icons").glob("*.png")))
    if icon_count != expected_icon_count:
        raise ValueError(f"Expected {expected_icon_count} icons but generated {icon_count}")

    map_svg_count = len(list((data_directory / "maps").glob("*.svg")))
    if map_svg_count != expected_map_svg_count:
        raise ValueError(f"Expected {expected_map_svg_count} map SVGs but produced {map_svg_count}")

    with zipfile.ZipFile(archive_path, "r") as archive:
        invalid_file = archive.testzip()
        if invalid_file:
            raise ValueError(f"Archive contains a corrupt entry: {invalid_file}")
        names = set(archive.namelist())
        for relative_path in REQUIRED_ARCHIVE_FILES:
            if relative_path not in names:
                raise ValueError(f"Archive is missing: {relative_path}")
        archived_icon_count = sum(
            name.startswith("icons/") and name.endswith(".png") for name in names
        )
        if archived_icon_count != expected_icon_count:
            raise ValueError(
                "Archive icon count does not match generated icon count: "
                f"{archived_icon_count} != {expected_icon_count}"
            )
        archived_map_count = sum(
            name.startswith("maps/") and name.endswith(".svg") for name in names
        )
        if archived_map_count != expected_map_svg_count:
            raise ValueError(
                "Archive map SVG count does not match generated count: "
                f"{archived_map_count} != {expected_map_svg_count}"
            )


def reset_output_directory(output_directory: Path) -> None:
    output_directory = output_directory.resolve()
    protected = {
        Path.cwd().resolve(),
        Path.home().resolve(),
        Path(output_directory.anchor).resolve(),
    }
    if output_directory in protected or len(output_directory.parts) < 3:
        raise ValueError(f"Refusing to replace unsafe output directory: {output_directory}")
    if output_directory.exists():
        shutil.rmtree(output_directory)
    output_directory.mkdir(parents=True)


def build(arguments: argparse.Namespace) -> Path:
    global _completed_icons

    _completed_icons = 0
    repository_root = Path(__file__).resolve().parent.parent
    output_directory = arguments.output.resolve()
    data_directory = output_directory / "Data"
    release_directory = output_directory / "release"
    reset_output_directory(output_directory)
    data_directory.mkdir()
    release_directory.mkdir()

    print(f"Fetching item catalog from {arguments.catalog_url}", flush=True)
    items, skipped_items, catalog_content = load_catalog(arguments.catalog_url)
    if len(items) < arguments.minimum_icons:
        raise ValueError(
            f"Catalog returned only {len(items)} items; minimum is {arguments.minimum_icons}"
        )

    print("Installing maps, OCR data, unknown icon, and notices", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        maps_future = executor.submit(install_maps, data_directory)
        map_catalog_future = executor.submit(load_map_catalog)
        unknown_future = executor.submit(install_unknown_icon, data_directory)
        ocr_future = executor.submit(install_ocr_models, data_directory, arguments.workers)
        notices_future = executor.submit(install_notices, data_directory, repository_root)

        maps_entry, maps_document = maps_future.result()
        other_entries = [
            maps_entry,
            unknown_future.result(),
            *ocr_future.result(),
            *notices_future.result(),
        ]

    # Map SVGs are driven by maps.json: every interactive entry with an svgPath is
    # fetched for the map id that artwork belongs to. Anything maps.json references
    # but upstream will not serve falls back to the tracked maps/ folder.
    map_catalog = map_catalog_future.result()
    svg_targets = load_svg_targets(maps_document, map_catalog)
    print(
        f"Resolved {len(svg_targets)} map SVGs from maps.json svgPath values",
        flush=True,
    )
    map_entries, map_notes = install_map_svgs(
        data_directory,
        svg_targets,
        repository_root,
        arguments.workers,
    )

    # Banners have no upstream source at all, so they stay tracked; only those with
    # a matching map id in this bundle are copied in.
    map_ids = {entry["path"].removeprefix("maps/").removesuffix(".svg") for entry in map_entries}
    banner_entries = install_banners(data_directory, repository_root, map_ids)
    carried_entries = [*map_entries, *banner_entries]

    print(
        f"Generating {len(items)} icons from "
        f"{len({item['baseImageLink'] for item in items})} unique sources",
        flush=True,
    )
    icon_entries = install_icons(items, data_directory / "icons", arguments.workers)

    manifest = write_manifest(
        data_directory,
        release_directory,
        catalog_url=arguments.catalog_url,
        catalog_content=catalog_content,
        icon_entries=icon_entries,
        other_entries=other_entries,
        carried_entries=carried_entries,
        map_notes=map_notes,
        skipped_items=skipped_items,
    )
    archive_path = release_directory / "Data.zip"
    print("Creating deterministic Data.zip", flush=True)
    deterministic_zip(data_directory, archive_path)
    archive_digest = sha256_bytes(archive_path.read_bytes())
    checksum = f"{archive_digest}  Data.zip\n".encode("ascii")
    write_bytes_atomic(release_directory / "Data.zip.sha256", checksum)
    validate_output(data_directory, archive_path, len(items), len(map_entries))

    print(
        "Build complete: "
        f"{len(items)} icons ({len(skipped_items)} placeholders skipped), "
        f"{manifest['mapSvgCount']} map SVGs "
        f"({manifest['mapSvgDownloadedCount']} from maps.json), "
        f"{manifest['bannerCount']} banners, "
        f"content {manifest['contentSha256']}, "
        f"archive {archive_digest}",
        flush=True,
    )
    return archive_path


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("build"),
        help="Output root (default: build)",
    )
    parser.add_argument(
        "--catalog-url",
        default=CATALOG_URL,
        help="tarkov.dev item catalog URL",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=16,
        choices=range(1, 33),
        metavar="1-32",
        help="Maximum concurrent downloads (default: 16)",
    )
    parser.add_argument(
        "--minimum-icons",
        type=int,
        default=4_000,
        help="Abort if the catalog contains fewer icons (default: 4000)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    try:
        build(parse_arguments())
    except KeyboardInterrupt:
        print("Build cancelled", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f"Build failed: {error}", file=sys.stderr)
        raise
