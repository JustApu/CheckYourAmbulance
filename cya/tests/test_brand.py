"""App icons and the web app manifest (issue #194 item 1: an installable app)."""

import json
import struct

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from cya.shell import brand_views


def png_size(data):
    """(width, height) from a PNG's IHDR chunk."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return struct.unpack(">II", data[16:24])


class ManifestTests(SimpleTestCase):
    def test_the_manifest_is_public_and_names_the_app(self):
        response = self.client.get("/manifest.webmanifest")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/manifest+json; charset=utf-8")
        self.assertIn("max-age=86400", response["Cache-Control"])
        manifest = json.loads(response.content)
        self.assertEqual(manifest["name"], "Check Your Ambulance")
        self.assertEqual(manifest["short_name"], "CYA")
        self.assertEqual((manifest["display"], manifest["start_url"]), ("standalone", "/"))
        self.assertEqual(manifest["theme_color"], "#14305C")
        purposes = {icon["purpose"] for icon in manifest["icons"]}
        self.assertEqual(purposes, {"any", "maskable", "monochrome"})

    def test_every_manifest_icon_is_served_at_its_declared_size(self):
        manifest = json.loads(self.client.get("/manifest.webmanifest").content)
        for entry in manifest["icons"]:
            with self.subTest(icon=entry["src"]):
                response = self.client.get(entry["src"])
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], entry["type"])
                if entry["type"] == "image/png":
                    width, height = entry["sizes"].split("x")
                    self.assertEqual(png_size(response.content), (int(width), int(height)))

    def test_the_manifest_refuses_writes(self):
        self.assertEqual(self.client.post("/manifest.webmanifest").status_code, 405)


class IconTests(SimpleTestCase):
    def test_each_icon_is_served_with_long_caching(self):
        sizes = {"apple-touch-icon.png": (180, 180), "favicon-32.png": (32, 32)}
        for name, content_type in brand_views.ICONS.items():
            with self.subTest(name=name):
                response = self.client.get(f"/icons/{name}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], content_type)
                self.assertEqual(response["X-Content-Type-Options"], "nosniff")
                self.assertIn("max-age=31536000", response["Cache-Control"])
                self.assertIn("immutable", response["Cache-Control"])
                if name in sizes:
                    self.assertEqual(png_size(response.content), sizes[name])

    def test_only_listed_icons_are_served(self):
        for path in (
            "/icons/render.mjs",
            "/icons/icon.svg",
            "/icons/..%2Fbrand_views.py",
            "/icons/../settings.py",
            "/icons/",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)


class PageHeadTests(SimpleTestCase):
    def test_every_page_links_the_icons_and_manifest(self):
        html = render_to_string("shell/base.html", {"title": "Probe"})
        for tag in (
            '<link rel="icon" href="/icons/favicon.svg" type="image/svg+xml">',
            '<link rel="apple-touch-icon" href="/icons/apple-touch-icon.png">',
            '<link rel="manifest" href="/manifest.webmanifest">',
            '<meta name="apple-mobile-web-app-title" content="CYA">',
            '<meta name="theme-color" content="#14305C" media="(prefers-color-scheme: light)">',
        ):
            with self.subTest(tag=tag):
                self.assertIn(tag, html)
        # No service worker: the app stays online-only (SPEC).
        self.assertNotIn("serviceWorker", html)
