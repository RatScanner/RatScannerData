"""Offline checks for the builder's outbound source policy."""

import unittest
import urllib.request
from unittest.mock import patch

from scripts import build_data


class DownloadPolicyTests(unittest.TestCase):
    def test_configured_https_sources_are_allowed(self) -> None:
        for url in (
            build_data.CATALOG_URL,
            build_data.MAP_CATALOG_URL,
            build_data.MAPS_URL,
            build_data.UNKNOWN_IMAGE_URL,
            build_data.TESSDATA_LICENSE_URL,
            build_data.TARKOV_DEV_LICENSE_URL,
        ):
            with self.subTest(url=url):
                build_data.require_allowed_source(url)

    def test_unapproved_initial_url_is_rejected_before_open(self) -> None:
        with patch.object(build_data.SOURCE_OPENER, "open") as open_source:
            with self.assertRaisesRegex(ValueError, "allowed hosts"):
                build_data.fetch_bytes("https://example.invalid/catalog")
        open_source.assert_not_called()

    def test_unapproved_redirect_is_rejected_before_request_creation(self) -> None:
        handler = build_data.SourceRedirectHandler()
        request = urllib.request.Request(build_data.MAPS_URL)
        for url in ("https://example.invalid/map", "http://assets.tarkov.dev/map"):
            with (
                self.subTest(url=url),
                patch.object(urllib.request.HTTPRedirectHandler, "redirect_request") as redirect,
                self.assertRaisesRegex(ValueError, "allowed hosts"),
            ):
                handler.redirect_request(request, None, 302, "Found", {}, url)
                redirect.assert_not_called()

    def test_approved_redirect_keeps_standard_request_behavior(self) -> None:
        handler = build_data.SourceRedirectHandler()
        request = urllib.request.Request(
            build_data.MAPS_URL, headers={"Accept": "application/json"}
        )
        redirected = handler.redirect_request(request, None, 302, "Found", {}, build_data.MAPS_URL)
        self.assertEqual(redirected.full_url, build_data.MAPS_URL)
        self.assertEqual(redirected.get_header("Accept"), "application/json")
