"""App icons and the web app manifest, so phones can install the app (issue #194 item 1).

The app inlines its CSS and scripts and has no static-file pipeline; nginx sends every path to
the application. These two views serve the few brand files that browsers must fetch by URL:
the icons named in the page head and the manifest. Both answer anonymously, because browsers
fetch the manifest without credentials, and both send long cache lifetimes; a changed icon
gets a new file name.

Only the files in ICONS are served. Their paths are built once, at import, from that fixed
table; a request's name is only a key to look one up and is never joined to a path, so no
request can reach any other file. There is no service worker and no offline mode;
the app stays online-only (SPEC).
"""

import json
from functools import cache
from pathlib import Path

from django.http import Http404, HttpResponse
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET

BRAND_DIR = Path(__file__).resolve().parent / "brand"
ONE_YEAR = 365 * 24 * 3600
ONE_DAY = 24 * 3600
ICONS = {
    "favicon.svg": "image/svg+xml",
    "favicon-32.png": "image/png",
    "apple-touch-icon.png": "image/png",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "maskable-512.png": "image/png",
    "monochrome.svg": "image/svg+xml",
}
THEME_COLOR = "#14305C"  # navy, the approved brand color (issue #194)
MANIFEST = {
    "id": "/",
    "name": "Check Your Ambulance",
    "short_name": "CYA",
    "description": "Ambulance and equipment inspections, problems and readiness.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#FFFFFF",
    "theme_color": THEME_COLOR,
    "icons": [
        {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {
            "src": "/icons/maskable-512.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "maskable",
        },
        {
            "src": "/icons/monochrome.svg",
            "sizes": "any",
            "type": "image/svg+xml",
            "purpose": "monochrome",
        },
    ],
}


# name -> (file path, media type), fixed at import. A request selects an entry; it never
# contributes to a path (CodeQL py/path-injection).
ICON_FILES = {name: (BRAND_DIR / name, content_type) for name, content_type in ICONS.items()}


@cache
def _read(path):
    return path.read_bytes()


@require_GET
@cache_control(public=True, max_age=ONE_YEAR, immutable=True)
def icon(request, name):
    entry = ICON_FILES.get(name)
    if entry is None:
        raise Http404("no such icon")
    path, content_type = entry
    response = HttpResponse(_read(path), content_type=content_type)
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_GET
@cache_control(public=True, max_age=ONE_DAY)
def manifest(request):
    return HttpResponse(
        json.dumps(MANIFEST, indent=2), content_type="application/manifest+json; charset=utf-8"
    )
