"""Offline ID-token verification tests with synthetic keys (AC-22, AUTH-1, AUTH-3)."""

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import SimpleTestCase

from cya.accounts.google import CANONICAL_ISSUER, GoogleIdTokenVerifier
from cya.integrations import IdentityRejected
from cya.testing.stubs import STUB_CLIENT_ID, STUB_HOSTED_DOMAIN, STUB_KID, StubGoogle

NONCE = "nonce-for-test"


class VerifierTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.google = StubGoogle()
        cls.verifier = GoogleIdTokenVerifier(STUB_CLIENT_ID, STUB_HOSTED_DOMAIN, cls.google)

    def token(self, **kwargs):
        return self.google.id_token(sub="1234567890", nonce=NONCE, **kwargs)

    def assert_rejected(self, token, reason, nonce=NONCE):
        with self.assertRaises(IdentityRejected) as caught:
            self.verifier.verify(token, nonce=nonce)
        self.assertEqual(caught.exception.reason, reason)

    def test_valid_workspace_token_yields_subject_keyed_identity(self):
        identity = self.verifier.verify(self.token(), nonce=NONCE)
        self.assertEqual(identity.issuer, CANONICAL_ISSUER)
        self.assertEqual(identity.subject, "1234567890")
        self.assertEqual(identity.hosted_domain, STUB_HOSTED_DOMAIN)
        self.assertEqual(identity.email, f"1234567890@{STUB_HOSTED_DOMAIN}")

    def test_both_documented_issuer_spellings_map_to_one_identity_issuer(self):
        identity = self.verifier.verify(self.token(iss="accounts.google.com"), nonce=NONCE)
        self.assertEqual(identity.issuer, CANONICAL_ISSUER)

    def test_personal_account_without_hd_is_refused(self):
        # AC-22: valid signature, personal account.
        self.assert_rejected(
            self.token(omit=("hd",), email="someone@gmail.example"), "not_workspace_account"
        )

    def test_account_merely_claiming_the_domain_by_email_is_refused(self):
        # AUTH-1: an address in our domain without the verified hd claim.
        self.assert_rejected(self.token(omit=("hd",)), "not_workspace_account")

    def test_other_workspace_domain_is_refused_case_insensitively_matched(self):
        self.assert_rejected(self.token(hd="other.example"), "wrong_workspace_domain")
        identity = self.verifier.verify(self.token(hd=STUB_HOSTED_DOMAIN.upper()), nonce=NONCE)
        self.assertEqual(identity.hosted_domain, STUB_HOSTED_DOMAIN)

    def test_audience_issuer_and_expiry_are_enforced(self):
        self.assert_rejected(self.token(aud="someone-else.apps.example.invalid"), "wrong_audience")
        self.assert_rejected(self.token(aud=[STUB_CLIENT_ID, "other"]), "wrong_audience")
        self.assert_rejected(self.token(iss="https://evil.example"), "wrong_issuer")
        old = int(time.time()) - 7200
        self.assert_rejected(self.token(iat=old, exp=old + 600), "token_expired")

    def test_required_claims_must_be_present(self):
        for claim in ("exp", "iat", "sub", "aud", "iss"):
            with self.subTest(claim=claim):
                with self.assertRaises(IdentityRejected):
                    self.verifier.verify(self.token(omit=(claim,)), nonce=NONCE)

    def test_issued_in_the_future_is_refused(self):
        future = int(time.time()) + 3600
        self.assert_rejected(self.token(iat=future, exp=future + 3600), "issued_in_future")

    def test_nonce_must_match(self):
        self.assert_rejected(self.token(), "nonce_mismatch", nonce="different")
        self.assert_rejected(self.token(omit=("nonce",)), "nonce_mismatch")
        self.assert_rejected(self.token(), "missing_nonce", nonce="")

    def test_authorized_party_must_be_us_when_present(self):
        self.verifier.verify(self.token(azp=STUB_CLIENT_ID), nonce=NONCE)
        self.assert_rejected(self.token(azp="another-client"), "wrong_authorized_party")

    def test_unverified_or_missing_email_is_refused(self):
        self.assert_rejected(self.token(email_verified=False), "email_not_verified")
        self.assert_rejected(self.token(email_verified="true"), "email_not_verified")
        self.assert_rejected(self.token(omit=("email",)), "email_not_verified")

    def test_oversized_attributes_are_refused_not_truncated(self):
        self.assert_rejected(self.token(email="a" * 255), "attribute_too_long")
        self.assert_rejected(self.token(name="n" * 201), "attribute_too_long")

    def test_token_signed_by_another_key_is_refused(self):
        attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.assert_rejected(self.token(key=attacker), "invalid_token")

    def test_unknown_key_id_is_refused(self):
        self.assert_rejected(self.token(kid="unknown-kid"), "unknown_signing_key")

    def test_algorithm_confusion_and_unsigned_tokens_are_refused(self):
        claims = jwt.decode(self.token(), options={"verify_signature": False})
        public_pem = self.google.private_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        # HS256 "signed" with the public key as an HMAC secret, built by hand
        # because PyJWT refuses to sign that way.
        header = jwt.utils.base64url_encode(
            b'{"alg":"HS256","kid":"%s","typ":"JWT"}' % STUB_KID.encode()
        )
        payload = jwt.utils.base64url_encode(json.dumps(claims).encode())
        signing_input = header + b"." + payload
        mac = hmac.new(public_pem, signing_input, hashlib.sha256).digest()
        forged = (signing_input + b"." + jwt.utils.base64url_encode(mac)).decode()
        self.assert_rejected(forged, "invalid_token")
        unsigned = jwt.encode(claims, key=None, algorithm="none", headers={"kid": STUB_KID})
        self.assert_rejected(unsigned, "invalid_token")

    def test_malformed_tokens_are_refused(self):
        for token in ("", "not-a-jwt", "a.b.c"):
            with self.subTest(token=token):
                with self.assertRaises(IdentityRejected):
                    self.verifier.verify(token, nonce=NONCE)
        with self.assertRaises(IdentityRejected):
            self.verifier.verify(None, nonce=NONCE)

    def test_clock_skew_within_leeway_is_accepted(self):
        now = datetime.now(UTC)
        token = self.token(iat=int((now + timedelta(seconds=30)).timestamp()))
        self.verifier.verify(token, nonce=NONCE, now=now)
