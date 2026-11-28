"""Offline checks for the data used by RatScanner and RatEye."""

import argparse
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from scripts import build_data, validate_archive

ROOT = Path(__file__).resolve().parent.parent


def image_bytes(
    mode: str,
    color: tuple[int, ...],
    image_format: str = "PNG",
    size: tuple[int, int] = (64, 64),
) -> bytes:
    output = io.BytesIO()
    Image.new(mode, size, color).save(output, format=image_format, lossless=True)
    return output.getvalue()


class BundleTests(unittest.TestCase):
    def test_png_and_webp_preserve_pixels_dimensions_and_transparency(self) -> None:
        for image_format in ("PNG", "WEBP"):
            with self.subTest(image_format=image_format):
                png, width, height, transparent = build_data.png_from_image(
                    image_bytes("RGBA", (10, 20, 30, 128), image_format), "fixture"
                )
                self.assertEqual((width, height, transparent), (64, 64, True))
                with Image.open(io.BytesIO(png)) as rendered:
                    self.assertEqual((rendered.format, rendered.mode), ("PNG", "RGBA"))
                    self.assertEqual(rendered.getpixel((0, 0)), (10, 20, 30, 128))

    def test_opaque_rgb_is_not_reported_as_transparent(self) -> None:
        _, width, height, transparent = build_data.png_from_image(
            image_bytes("RGB", (10, 20, 30)), "fixture"
        )
        self.assertEqual((width, height, transparent), (64, 64, False))

    def test_invalid_image_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Could not decode image"):
            build_data.png_from_image(b"not an image", "fixture")

    def test_independent_validator_requires_svg_viewbox(self) -> None:
        with self.assertRaisesRegex(ValueError, "no viewBox"):
            validate_archive.validate_map_svg("fixture.svg", b"<svg/>")

    def test_bundle_keeps_slot_geometry_map_artwork_and_stable_zip_metadata(self) -> None:
        icon_url = "https://assets.tarkov.dev/test-item.webp"
        svg_url = "https://assets.tarkov.dev/test-map.svg"
        missing_svg_url = "https://assets.tarkov.dev/test-missing-map.svg"
        downloaded_id = "0" * 24
        fallback_id = "5704e4dad2720bb55b8b4567"
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"/>'
        catalog = {
            "data": {
                "items": [
                    {"id": "test-item", "baseImageLink": icon_url, "width": 2, "height": 3},
                    {"id": "placeholder", "baseImageLink": build_data.UNKNOWN_IMAGE_URL},
                ]
            }
        }
        maps = {
            "test-map": (downloaded_id, svg_url),
            "fallback-map": (fallback_id, missing_svg_url),
        }
        maps_document = [
            {"normalizedName": name, "maps": [{"projection": "interactive", "svgPath": url}]}
            for name, (_, url) in maps.items()
        ]
        map_catalog = {
            "data": {"maps": {map_id: {"normalizedName": n} for n, (map_id, _) in maps.items()}}
        }
        sources = {
            build_data.CATALOG_URL: json.dumps(catalog).encode(),
            build_data.MAP_CATALOG_URL: json.dumps(map_catalog).encode(),
            build_data.MAPS_URL: json.dumps(maps_document).encode(),
            icon_url: image_bytes("RGBA", (10, 20, 30, 128), "WEBP", size=(127, 190)),
            build_data.UNKNOWN_IMAGE_URL: image_bytes("RGBA", (0, 0, 0, 0)),
            svg_url: svg,
            f"{build_data.TESSDATA_BASE_URL}/eng.traineddata": b"x" * 100_000,
            build_data.TARKOV_DEV_LICENSE_URL: b"MIT fixture",
            build_data.TESSDATA_LICENSE_URL: b"Apache fixture",
        }

        def fetch(url: str, **_kwargs) -> bytes:
            if url == missing_svg_url:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return sources[url]

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(build_data, "fetch_bytes", side_effect=fetch),
            patch.object(build_data, "OCR_LANGUAGES", {"eng": "eng"}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            arguments = argparse.Namespace(
                output=Path(temporary) / "bundle",
                catalog_url=build_data.CATALOG_URL,
                workers=1,
                minimum_icons=1,
            )
            archive = build_data.build(arguments)
            first = archive.read_bytes()
            validate_archive.validate_archive(str(archive), minimum_icons=1)
            self.assertEqual(
                archive.with_suffix(".zip.sha256").read_text().split()[0],
                hashlib.sha256(first).hexdigest(),
            )
            with zipfile.ZipFile(archive) as bundle:
                manifest = json.loads(bundle.read("manifest.json"))
                self.assertEqual(manifest["skippedItemCount"], 1)
                self.assertEqual(manifest["mapSvgDownloadedCount"], 1)
                self.assertEqual(bundle.read(f"maps/{downloaded_id}.svg"), svg)
                for name in (f"maps/{fallback_id}.svg", "banner/default.png"):
                    self.assertEqual(bundle.read(name), (ROOT / name).read_bytes())
                icon = next(
                    entry for entry in manifest["files"] if entry["path"] == "icons/test-item.png"
                )
                self.assertEqual((icon["width"], icon["height"]), (127, 190))
                self.assertEqual(icon["renderedSlots"], {"width": 2, "height": 3})
                self.assertEqual(icon["declaredSlots"], {"width": 2, "height": 3})
                self.assertTrue(icon["slotDimensionsMatch"])
                with Image.open(io.BytesIO(bundle.read(icon["path"]))) as rendered:
                    self.assertEqual(rendered.size, (127, 190))
                for entry in bundle.infolist():
                    self.assertEqual(entry.date_time, (1980, 1, 1, 0, 0, 0))
            self.assertEqual(build_data.build(arguments).read_bytes(), first)
