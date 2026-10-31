"""The Google network adapter, exercised with a fake opener (no network)."""

import io
import json
import urllib.error
import urllib.parse

import jwt
from django.test import SimpleTestCase, override_settings

from cya.accounts.oidc import (
    MAX_RESPONSE_BYTES,
    TOKEN_ENDPOINT,
    GoogleConfig,
    GoogleOidcProvider,
    authorization_url,
    google_config,
)
from cya.integrations import IdentityProviderUnavailable, IdentityRejected, IntegrationNotConfigured

CONFIG = GoogleConfig(
    "client-id", "client-secret", "agency.example", "https://cya.example/auth/callback/"
)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeOpener:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        if isinstance(self.result, Exception):
            raise self.result
        return FakeResponse(self.result)


def http_error(code):
    return urllib.error.HTTPError(TOKEN_ENDPOINT, code, "error", {}, io.BytesIO(b"{}"))


class ExchangeTests(SimpleTestCase):
    def exchange(self, result):
        opener = FakeOpener(result)
        provider = GoogleOidcProvider(CONFIG, urlopen=opener)
        token = provider.exchange_code(
            code="c0de", redirect_uri=CONFIG.redirect_uri, code_verifier="v" * 43
        )
        return token, opener

    def test_successful_exchange_posts_the_documented_parameters(self):
        token, opener = self.exchange(json.dumps({"id_token": "header.payload.sig"}).encode())
        self.assertEqual(token, "header.payload.sig")
        ((request, timeout),) = opener.requests
        self.assertEqual(
            (request.full_url, request.get_method(), timeout), (TOKEN_ENDPOINT, "POST", 10)
        )
        body = urllib.parse.parse_qs(request.data.decode())
        self.assertEqual(
            {k: v[0] for k, v in body.items()},
            {
                "code": "c0de",
                "client_id": "client-id",
                "client_secret": "client-secret",
                "redirect_uri": CONFIG.redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": "v" * 43,
            },
        )

    def test_rejected_code_is_a_refusal_and_server_trouble_is_unavailable(self):
        with self.assertRaises(IdentityRejected):
            self.exchange(http_error(400))
        for result in (
            http_error(500),
            urllib.error.URLError("down"),
            TimeoutError(),
            b"not json",
            json.dumps({"access_token": "x"}).encode(),
            b"[]",
            b"x" * (MAX_RESPONSE_BYTES + 1),
        ):
            with (
                self.subTest(result=repr(result)[:40]),
                self.assertRaises(IdentityProviderUnavailable),
            ):
                self.exchange(result)


class FakeJwkClient:
    def __init__(self, error):
        self.error = error

    def get_signing_key_from_jwt(self, token):
        raise self.error


class SigningKeyTests(SimpleTestCase):
    def test_key_download_failure_is_unavailable_and_unknown_key_is_refused(self):
        cases = [
            (jwt.PyJWKClientConnectionError("down"), IdentityProviderUnavailable),
            (jwt.PyJWKClientError("no matching kid"), IdentityRejected),
            (jwt.DecodeError("bad header"), IdentityRejected),
        ]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__), self.assertRaises(expected):
                GoogleOidcProvider(CONFIG, jwk_client=FakeJwkClient(error)).signing_key("t")


class ConfigTests(SimpleTestCase):
    @override_settings(
        CYA_GOOGLE_CLIENT_ID="",
        CYA_GOOGLE_CLIENT_SECRET="s",
        CYA_GOOGLE_HOSTED_DOMAIN="",
        CYA_GOOGLE_REDIRECT_URI="https://cya.example/auth/callback/",
    )
    def test_missing_settings_are_named(self):
        with self.assertRaisesRegex(
            IntegrationNotConfigured, "missing: CYA_GOOGLE_CLIENT_ID, CYA_GOOGLE_HOSTED_DOMAIN$"
        ):
            google_config()

    @override_settings(
        CYA_GOOGLE_CLIENT_ID="c",
        CYA_GOOGLE_CLIENT_SECRET="s",
        CYA_GOOGLE_HOSTED_DOMAIN="agency.example",
        CYA_GOOGLE_REDIRECT_URI="http://cya.example/auth/callback/",
    )
    def test_plain_http_redirect_is_refused_except_localhost(self):
        with self.assertRaisesRegex(IntegrationNotConfigured, "https"):
            google_config()
        with self.settings(CYA_GOOGLE_REDIRECT_URI="http://localhost:8000/auth/callback/"):
            self.assertEqual(google_config().redirect_uri, "http://localhost:8000/auth/callback/")

    def test_authorization_url_escapes_values(self):
        url = authorization_url(CONFIG, state="a&b", nonce="n", code_challenge="c")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(query["state"], ["a&b"])
        self.assertEqual(query["redirect_uri"], [CONFIG.redirect_uri])
