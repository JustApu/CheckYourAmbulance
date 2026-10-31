"""Network adapter for Google's OpenID Connect endpoints (production only).

Endpoints are taken from Google's discovery document
(https://accounts.google.com/.well-known/openid-configuration, read
2026-10-20). Configuration comes from CYA_GOOGLE_* settings; nothing is
committed. Tests never use this module's network paths: they configure
settings.CYA_OIDC_PROVIDER to an offline stub, and the test runner blocks
Python-level network access.

The code exchange is never retried automatically: an authorization code is
single-use, and a timeout leaves it unknown whether Google accepted it. The
user simply starts sign-in again.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from functools import lru_cache

import jwt
from django.conf import settings

from cya.integrations import (
    IdentityProviderUnavailable,
    IdentityRejected,
    IntegrationNotConfigured,
)

AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 64 * 1024
LOCAL_DEV_PREFIXES = ("http://localhost:", "http://127.0.0.1:")


@dataclass(frozen=True)
class GoogleConfig:
    client_id: str
    client_secret: str
    hosted_domain: str
    redirect_uri: str


def google_config():
    """Read and validate CYA_GOOGLE_*; raise IntegrationNotConfigured if incomplete."""
    values = {
        "client_id": getattr(settings, "CYA_GOOGLE_CLIENT_ID", ""),
        "client_secret": getattr(settings, "CYA_GOOGLE_CLIENT_SECRET", ""),
        "hosted_domain": getattr(settings, "CYA_GOOGLE_HOSTED_DOMAIN", ""),
        "redirect_uri": getattr(settings, "CYA_GOOGLE_REDIRECT_URI", ""),
    }
    missing = sorted(name for name, value in values.items() if not value)
    if missing:
        raise IntegrationNotConfigured(
            "Google sign-in settings missing: "
            + ", ".join(f"CYA_GOOGLE_{m.upper()}" for m in missing)
        )
    uri = values["redirect_uri"]
    if not (uri.startswith("https://") or uri.startswith(LOCAL_DEV_PREFIXES)):
        raise IntegrationNotConfigured(
            "CYA_GOOGLE_REDIRECT_URI must use https (or http://localhost)"
        )
    return GoogleConfig(**values)


def authorization_url(config, *, state, nonce, code_challenge):
    """The URL that starts sign-in at Google (authorization code flow with PKCE)."""
    query = urllib.parse.urlencode(
        {
            "client_id": config.client_id,
            "response_type": "code",
            "scope": "openid email profile",
            "redirect_uri": config.redirect_uri,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            # Only a hint for Google's account chooser; the hd claim is what is enforced.
            "hd": config.hosted_domain,
        }
    )
    return f"{AUTHORIZATION_ENDPOINT}?{query}"


@lru_cache(maxsize=1)
def _jwk_client():
    # One shared client so Google's keys are cached between sign-ins.
    return jwt.PyJWKClient(JWKS_URI, cache_keys=True, timeout=TIMEOUT_SECONDS)


class GoogleOidcProvider:
    def __init__(self, config, urlopen=urllib.request.urlopen, jwk_client=None):
        self._config = config
        self._urlopen = urlopen
        self._jwk_client = jwk_client

    def exchange_code(self, *, code, redirect_uri, code_verifier):
        body = urllib.parse.urlencode(
            {
                "code": code,
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            }
        ).encode("ascii")
        request = urllib.request.Request(
            TOKEN_ENDPOINT,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with self._urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401):
                raise IdentityRejected("code_rejected") from exc
            raise IdentityProviderUnavailable(f"token endpoint HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise IdentityProviderUnavailable("token endpoint unreachable") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise IdentityProviderUnavailable("token endpoint response too large")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IdentityProviderUnavailable("token endpoint returned malformed JSON") from exc
        id_token = payload.get("id_token") if isinstance(payload, dict) else None
        if not isinstance(id_token, str) or not id_token:
            raise IdentityProviderUnavailable("token endpoint response has no id_token")
        return id_token

    def signing_key(self, id_token):
        client = self._jwk_client or _jwk_client()
        try:
            return client.get_signing_key_from_jwt(id_token).key
        except jwt.PyJWKClientConnectionError as exc:
            raise IdentityProviderUnavailable("signing keys unreachable") from exc
        except (jwt.PyJWKClientError, jwt.DecodeError) as exc:
            raise IdentityRejected("unknown_signing_key") from exc


def google_provider():
    """Settings factory for CYA_OIDC_PROVIDER in production."""
    return GoogleOidcProvider(google_config())
