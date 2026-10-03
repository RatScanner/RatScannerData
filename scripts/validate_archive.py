#!/usr/bin/env python3
"""Validate a generated RatScanner Data.zip without extracting it."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import PurePosixPath
from xml.etree import ElementTree

SVG_NAMESPACE = "http://www.w3.org/2000/svg"

SAFE_ICON = re.compile(r"^icons/[A-Za-z0-9_-]+\.png$")
SAFE_MAP_SVG = re.compile(r"^maps/[0-9a-f]{24}\.svg$")
SAFE_BANNER = re.compile(r"^banner/([0-9a-f]{24}|default)\.png$")

REQUIRED = {
    "maps.json",
    "unknown.png",
    "traineddata/eng.traineddata",
    "banner/default.png",
    "manifest.json",
    "THIRD_PARTY_NOTICES.md",
    "licenses/tarkov-dev-MIT.txt",
    "licenses/tessdata-Apache-2.0.txt",
}

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def validate_map_svg(name: str, content: bytes) -> None:
    """A map SVG must still be parseable XML with a usable viewBox.

    RatScanner's map viewer derives the canvas aspect ratio from the viewBox, so a
    document without one renders as an empty map.
    """

    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as error:
        raise ValueError(f"Map SVG is not parseable XML: {name}") from error

    tag = root.tag
    if tag not in ("svg", f"{{{SVG_NAMESPACE}}}svg"):
        raise ValueError(f"Map SVG has no root <svg> element: {name}")

    view_box = root.get("viewBox")
    if not view_box:
        raise ValueError(f"Map SVG has no viewBox: {name}")
    parts = view_box.split()
    if len(parts) != 4:
        raise ValueError(f"Map SVG has a malformed viewBox: {name}")
    try:
        width, height = float(parts[2]), float(parts[3])
    except ValueError as error:
        raise ValueError(f"Map SVG has a non-numeric viewBox: {name}") from error
    if width <= 0 or height <= 0:
        raise ValueError(f"Map SVG has an empty viewBox: {name}")


def validate_maps_json_svg_paths(
    archive: zipfile.ZipFile, map_names: list[str]
) -> None:
    """Every SVG in the bundle must correspond to a map id maps.json can serve.

    The reverse is not required: a map id can be bundled for a variant that shares
    its base map's artwork.
    """

    maps = json.loads(archive.read("maps.json"))
    if not isinstance(maps, list) or not maps:
        raise ValueError("maps.json must be a non-empty array")

    normalized_names = {
        group.get("normalizedName")
        for group in maps
        if isinstance(group, dict) and isinstance(group.get("normalizedName"), str)
    }

    manifest = json.loads(archive.read("manifest.json"))
    entries = {
        entry["path"]: entry
        for entry in manifest.get("files", [])
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }

    for name in map_names:
        entry = entries.get(name)
        if not entry:
            continue
        normalized_name = entry.get("normalizedName")
        if normalized_name is None:
            # Carried forward without a maps.json reference; nothing to cross-check.
            continue
        if normalized_name not in normalized_names:
            raise ValueError(
                f"{name} names map {normalized_name!r}, which maps.json no longer lists"
            )


def validate_archive(path: str, minimum_icons: int) -> None:
    with zipfile.ZipFile(path, "r") as archive:
        invalid = archive.testzip()
        if invalid:
            raise ValueError(f"Corrupt ZIP entry: {invalid}")

        names = archive.namelist()
        for name in names:
            archive_path = PurePosixPath(name)
            if archive_path.is_absolute() or ".." in archive_path.parts:
                raise ValueError(f"Unsafe ZIP entry: {name}")

        missing = REQUIRED.difference(names)
        if missing:
            raise ValueError(f"Missing required entries: {sorted(missing)}")

        icon_names = sorted(name for name in names if SAFE_ICON.fullmatch(name))
        if len(icon_names) < minimum_icons:
            raise ValueError(
                f"Only {len(icon_names)} icons; expected at least {minimum_icons}"
            )
        unexpected_icon_entries = [
            name
            for name in names
            if name.startswith("icons/") and not SAFE_ICON.fullmatch(name)
        ]
        if unexpected_icon_entries:
            raise ValueError(f"Unexpected icon paths: {unexpected_icon_entries[:5]}")

        # The map SVGs are part of the payload, so they get the same structural
        # checks the icons do rather than being taken on trust.
        map_names = sorted(name for name in names if name.startswith("maps/"))
        if not map_names:
            raise ValueError("No map SVGs were carried into the archive")
        unexpected_map_entries = [
            name for name in map_names if not SAFE_MAP_SVG.fullmatch(name)
        ]
        if unexpected_map_entries:
            raise ValueError(f"Unexpected map SVG paths: {unexpected_map_entries[:5]}")
        for name in map_names:
            validate_map_svg(name, archive.read(name))

        banner_names = sorted(name for name in names if name.startswith("banner/"))
        if not banner_names:
            raise ValueError("No map banners were carried into the archive")
        unexpected_banner_entries = [
            name for name in banner_names if not SAFE_BANNER.fullmatch(name)
        ]
        if unexpected_banner_entries:
            raise ValueError(
                f"Unexpected banner paths: {unexpected_banner_entries[:5]}"
            )
        for name in banner_names:
            if not archive.read(name).startswith(PNG_SIGNATURE):
                raise ValueError(f"Banner is not PNG: {name}")

        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("schemaVersion") != 1:
            raise ValueError("Unsupported or missing manifest schemaVersion")
        if manifest.get("iconCount") != len(icon_names):
            raise ValueError(
                "Manifest iconCount does not match the archive: "
                f"{manifest.get('iconCount')} != {len(icon_names)}"
            )
        skipped_items = manifest.get("skippedItems")
        if not isinstance(skipped_items, list):
            raise ValueError("Manifest is missing skippedItems")
        if manifest.get("skippedItemCount") != len(skipped_items):
            raise ValueError("Manifest skippedItemCount is inconsistent")
        if manifest.get("catalogItemCount") != len(icon_names) + len(skipped_items):
            raise ValueError("Manifest catalogItemCount is inconsistent")
        if manifest.get("mapSvgCount") != len(map_names):
            raise ValueError(
                "Manifest mapSvgCount does not match the archive: "
                f"{manifest.get('mapSvgCount')} != {len(map_names)}"
            )
        banner_count = sum(name.startswith("banner/") for name in names)
        if manifest.get("bannerCount") != banner_count:
            raise ValueError(
                "Manifest bannerCount does not match the archive: "
                f"{manifest.get('bannerCount')} != {banner_count}"
            )

        entries = {
            entry["path"]: entry
            for entry in manifest.get("files", [])
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        }
        for name in icon_names + map_names + banner_names:
            content = archive.read(name)
            if not content.startswith(PNG_SIGNATURE) and name.endswith(".png"):
                raise ValueError(f"Icon is not PNG: {name}")
            entry = entries.get(name)
            if not entry:
                raise ValueError(f"Manifest is missing an entry for: {name}")
            if sha256_bytes(content) != entry.get("sha256"):
                raise ValueError(f"Checksum mismatch: {name}")

        for name in icon_names:
            entry = entries[name]
            width = entry.get("width")
            height = entry.get("height")
            if not isinstance(width, int) or width % 63 != 1:
                raise ValueError(f"Icon width is incompatible with RatEye: {name}")
            if not isinstance(height, int) or height % 63 != 1:
                raise ValueError(f"Icon height is incompatible with RatEye: {name}")

        maps = json.loads(archive.read("maps.json"))
        if not isinstance(maps, list) or not maps:
            raise ValueError("maps.json must be a non-empty array")
        validate_maps_json_svg_paths(archive, map_names)

    print(
        f"Validated {path}: {len(icon_names)} icons, "
        f"{len(map_names)} map SVGs, {len(banner_names)} banners, "
        f"content {manifest['contentSha256']}"
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", help="Path to Data.zip")
    parser.add_argument(
        "--minimum-icons",
        type=int,
        default=4_000,
        help="Minimum accepted icon count (default: 4000)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    try:
        arguments = parse_arguments()
        validate_archive(arguments.archive, arguments.minimum_icons)
    except Exception as error:
        print(f"Validation failed: {error}", file=sys.stderr)
        raise
