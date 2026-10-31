"""ASGI entry point for HTTP and authenticated vehicle live updates."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cya.settings")


def _application():
    http_application = get_asgi_application()

    # Initialize Django before consumers import ORM-backed collaborators.
    from channels.routing import ProtocolTypeRouter, URLRouter
    from channels.security.websocket import AllowedHostsOriginValidator
    from channels.sessions import SessionMiddlewareStack

    from cya.live_updates.routing import websocket_urlpatterns

    return ProtocolTypeRouter(
        {
            "http": http_application,
            "websocket": AllowedHostsOriginValidator(
                SessionMiddlewareStack(URLRouter(websocket_urlpatterns))
            ),
        }
    )


application = _application()
