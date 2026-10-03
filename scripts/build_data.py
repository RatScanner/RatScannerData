#!/usr/bin/env python3
"""Build the deterministic RatScanner Data.zip release payload.

Item icons, OCR models, map metadata and the unknown-item placeholder are fetched
from their upstream sources on every run. The interactive map SVGs and the per-map
banner art have no stable public upstream endpoint, so they are copied forward from
the tracked `maps/` and `banner/` folders in this repository.
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

from PIL import Image, UnidentifiedImageError

CATALOG_URL = "https://json.tarkov.dev/regular/items"
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

REQUIRED_ARCHIVE_FILES = (
    "maps.json",
    "unknown.png",
    "traineddata/eng.traineddata",
    "banner/default.png",
    "manifest.json",
    "THIRD_PARTY_NOTICES.md",
)

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


def load_catalog(catalog_url: str) -> tuple[list[dict[str, Any]], list[dict[str, str]], bytes]:
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


def install_maps(data_directory: Path) -> dict[str, Any]:
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
    return {
        "path": "maps.json",
        "sha256": sha256_bytes(content),
        "size": len(content),
        "source": MAPS_URL,
    }


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


def install_carried_folder(
    data_directory: Path,
    repository_root: Path,
    folder: str,
    suffix: str,
) -> list[dict[str, Any]]:
    """Copy a tracked asset folder into the bundle.

    `maps/` and `banner/` have no stable public upstream endpoint, so their files
    are carried forward verbatim. Names are validated because they end up as
    archive entry paths, and the tarkov.dev map ids they are keyed by must be
    recognised before anything is written.
    """

    source_directory = repository_root / folder
    if not source_directory.is_dir():
        raise ValueError(f"Required tracked folder is missing: {folder}/")

    entries: list[dict[str, Any]] = []
    for path in sorted(source_directory.iterdir()):
        if not path.is_file():
            continue
        if path.suffix != suffix:
            raise ValueError(f"Unexpected file in {folder}/: {path.name}")

        stem = path.stem
        if stem != DEFAULT_BANNER.removesuffix(".png") and not SAFE_MAP_ID.fullmatch(stem):
            raise ValueError(f"Unexpected name in {folder}/: {path.name}")

        content = path.read_bytes()
        relative_path = f"{folder}/{path.name}"
        write_bytes_atomic(data_directory / relative_path, content)
        entries.append(
            {
                "path": relative_path,
                "sha256": sha256_bytes(content),
                "size": len(content),
                "source": f"repository/{folder}/{path.name}",
            }
        )

    if folder == "banner" and not any(entry["path"].endswith(DEFAULT_BANNER) for entry in entries):
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
        "fileCount": len(content_entries),
        "sources": {
            "catalog": catalog_url,
            "maps": MAPS_URL,
            "unknownImage": UNKNOWN_IMAGE_URL,
            "ocr": f"{TESSDATA_BASE_URL}/",
        },
        "carriedFolders": dict(sorted(carried_counts.items())),
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
) -> None:
    for relative_path in REQUIRED_ARCHIVE_FILES:
        if not (data_directory / relative_path).is_file():
            raise ValueError(f"Required output is missing: {relative_path}")

    icon_count = len(list((data_directory / "icons").glob("*.png")))
    if icon_count != expected_icon_count:
        raise ValueError(f"Expected {expected_icon_count} icons but generated {icon_count}")

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
        unknown_future = executor.submit(install_unknown_icon, data_directory)
        ocr_future = executor.submit(install_ocr_models, data_directory, arguments.workers)
        notices_future = executor.submit(install_notices, data_directory, repository_root)
        other_entries = [
            maps_future.result(),
            unknown_future.result(),
            *ocr_future.result(),
            *notices_future.result(),
        ]

    # The interactive map SVGs and banner art have no stable public upstream, so
    # they are carried forward from the tracked folders rather than downloaded.
    print("Carrying forward tracked maps/ and banner/ assets", flush=True)
    carried_entries = [
        *install_carried_folder(data_directory, repository_root, "maps", ".svg"),
        *install_carried_folder(data_directory, repository_root, "banner", ".png"),
    ]

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
        skipped_items=skipped_items,
    )
    archive_path = release_directory / "Data.zip"
    print("Creating deterministic Data.zip", flush=True)
    deterministic_zip(data_directory, archive_path)
    archive_digest = sha256_bytes(archive_path.read_bytes())
    checksum = f"{archive_digest}  Data.zip\n".encode("ascii")
    write_bytes_atomic(release_directory / "Data.zip.sha256", checksum)
    validate_output(data_directory, archive_path, len(items))

    print(
        "Build complete: "
        f"{len(items)} icons ({len(skipped_items)} placeholders skipped), "
        f"{len(carried_entries)} carried map assets, "
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
