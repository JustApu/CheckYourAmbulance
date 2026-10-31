"""THROWAWAY spike: minimal standalone Django configuration + unmanaged models mapped onto the spike
schema (no migrations: the application account has no DDL rights, which is the point under test)."""
import os

import django
from django.conf import settings


def configure(db_name):
    if settings.configured:
        settings.DATABASES["default"]["NAME"] = db_name
        from django.db import connections
        connections.close_all()
        return
    settings.configure(
        DEBUG=False,
        SECRET_KEY="spike-only-synthetic-secret",
        USE_TZ=True,
        TIME_ZONE="UTC",
        ALLOWED_HOSTS=["testserver", "127.0.0.1", "localhost"],
        INSTALLED_APPS=["django.contrib.contenttypes", "django.contrib.auth", "django.contrib.sessions", "channels", "spikepy"],
        DATABASES={"default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": db_name, "USER": "app", "PASSWORD": "spike-local-only",
            "HOST": os.environ["SPIKE_SOCK"],      # a path starting with / is treated as a unix socket
            "OPTIONS": {},                         # deliberately NO init_command: report Django's own defaults
        }},
        MIDDLEWARE=["django.middleware.security.SecurityMiddleware",
                    "django.contrib.sessions.middleware.SessionMiddleware",
                    "django.middleware.csrf.CsrfViewMiddleware"],
        SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies",   # no DB table needed
        SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
        CSRF_COOKIE_SECURE=True, CSRF_COOKIE_SAMESITE="Strict",
        ROOT_URLCONF="spikepy.urls",
        ASGI_APPLICATION="spikepy.asgi.application",
        CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        SILENCED_SYSTEM_CHECKS=[],
    )
    django.setup()
