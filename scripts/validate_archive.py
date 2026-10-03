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
    """A carried map SVG must still be well-formed XML with a usable viewBox."""

    text = content.decode("utf-8", errors="replace")
    if "<svg" not in text[:2048]:
        raise ValueError(f"Map SVG has no root <svg> element: {name}")
    match = re.search(r'viewBox\s*=\s*"([^"]+)"', text)
    if not match:
        raise ValueError(f"Map SVG has no viewBox: {name}")
    parts = match.group(1).split()
    if len(parts) != 4:
        raise ValueError(f"Map SVG has a malformed viewBox: {name}")
    try:
        width, height = float(parts[2]), float(parts[3])
    except ValueError as error:
        raise ValueError(f"Map SVG has a non-numeric viewBox: {name}") from error
    if width <= 0 or height <= 0:
        raise ValueError(f"Map SVG has an empty viewBox: {name}")


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
            raise ValueError(f"Only {len(icon_names)} icons; expected at least {minimum_icons}")
        unexpected_icon_entries = [
            name for name in names if name.startswith("icons/") and not SAFE_ICON.fullmatch(name)
        ]
        if unexpected_icon_entries:
            raise ValueError(f"Unexpected icon paths: {unexpected_icon_entries[:5]}")

        # The carried map assets are part of the payload, so they get the same
        # structural checks the icons do rather than being taken on trust.
        map_names = sorted(name for name in names if name.startswith("maps/"))
        if not map_names:
            raise ValueError("No map SVGs were carried into the archive")
        unexpected_map_entries = [name for name in map_names if not SAFE_MAP_SVG.fullmatch(name)]
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
            raise ValueError(f"Unexpected banner paths: {unexpected_banner_entries[:5]}")
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
