"""THROWAWAY spike (Python candidate) criterion 6a: ID-token validation mechanics with PyJWT, synthetic RSA keys.
Google's real claim semantics are NOT verified here (documentation unreachable)."""
import base64
import json
import time
import unittest

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ISS, AUD, DOMAIN = "https://idp.example.invalid", "synthetic-client-id", "agency.example.invalid"


def keypair():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return k, k.public_key()


K1, K1P = keypair()
K2, K2P = keypair()
EVIL, _ = keypair()
KEYS = {"k1": K1P, "k2": K2P}
ALGS = ["RS256"]


def validate(token):
    """Pinned algorithm, required claims, kid lookup, organization checks. Returns (identity, error)."""
    try:
        kid = jwt.get_unverified_header(token).get("kid")
        key = KEYS[kid]
        claims = jwt.decode(token, key=key, algorithms=ALGS, audience=AUD, issuer=ISS, leeway=30,
                            options={"require": ["exp", "iat", "iss", "aud", "sub"], "verify_signature": True,
                                     "verify_exp": True, "verify_iat": True, "verify_aud": True, "verify_iss": True})
    except (jwt.PyJWTError, KeyError) as exc:
        return None, f"rejected: {type(exc).__name__}"
    if not claims.get("email_verified"):
        return None, "email not verified"
    if claims.get("hd") != DOMAIN:
        return None, "not an organization account"
    return {"subject": claims["sub"]}, None


NOW = int(time.time())
GOOD = dict(iss=ISS, aud=AUD, sub="synthetic-sub-1", email="emt1@agency.example.invalid", email_verified=True,
            hd=DOMAIN, iat=NOW - 5, exp=NOW + 600)


def tok(claims, key=K1, kid="k1", alg="RS256"):
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


def b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


class Token(unittest.TestCase):
    def rej(self, name, token):
        ident, err = validate(token)
        self.assertIsNone(ident, name)

    def test_accepts_valid_and_rotation(self):
        self.assertEqual(validate(tok(GOOD))[0], {"subject": "synthetic-sub-1"})
        self.assertIsNotNone(validate(tok(GOOD, key=K2, kid="k2"))[0])

    def test_rejections(self):
        without = lambda *names: {k: v for k, v in GOOD.items() if k not in names}
        self.rej("wrong key", tok(GOOD, key=EVIL))
        self.rej("unknown kid", tok(GOOD, kid="nope"))
        self.rej("expired", tok({**GOOD, "exp": NOW - 3600, "iat": NOW - 7200}))
        self.rej("missing exp", tok(without("exp")))
        self.rej("future iat", tok({**GOOD, "iat": NOW + 3600, "exp": NOW + 7200}))
        self.rej("wrong issuer", tok({**GOOD, "iss": "https://evil.example.invalid"}))
        self.rej("wrong audience", tok({**GOOD, "aud": "someone-elses-client"}))
        self.rej("missing audience", tok(without("aud")))
        self.rej("no hd (personal)", tok(without("hd")))
        self.rej("other org", tok({**GOOD, "hd": "other.example.invalid"}))
        self.rej("email_verified false", tok({**GOOD, "email_verified": False}))
        self.rej("missing sub", tok(without("sub")))
        self.rej("garbage", "not.a.token")
        self.rej("empty", "")

    def test_tamper_none_and_confusion(self):
        h, p, s = tok(GOOD).split(".")
        payload = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
        payload["sub"] = "synthetic-admin"
        self.rej("tampered payload", ".".join([h, b64(payload), s]))
        self.rej("alg none", f"{b64({'alg': 'none', 'typ': 'JWT', 'kid': 'k1'})}.{b64(GOOD)}.")
        pub_pem = K1P.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        # PyJWT itself refuses to SIGN with an asymmetric key as an HMAC secret, so an attacker would craft it by hand.
        with self.assertRaises(jwt.InvalidKeyError):
            jwt.encode(GOOD, pub_pem, algorithm="HS256")
        import hashlib, hmac
        signing_input = f"{b64({'alg': 'HS256', 'typ': 'JWT', 'kid': 'k1'})}.{b64(GOOD)}".encode()
        sig = base64.urlsafe_b64encode(hmac.new(pub_pem, signing_input, hashlib.sha256).digest()).rstrip(b"=")
        self.rej("HS256 with public key as secret", (signing_input + b"." + sig).decode())

    def test_library_default_is_unsafe_without_required_claims(self):
        # Evidence: without options={"require": [...]}, PyJWT accepts a token that has no exp at all.
        t = tok({k: v for k, v in GOOD.items() if k != "exp"})
        claims = jwt.decode(t, key=K1P, algorithms=ALGS, audience=AUD, issuer=ISS)
        self.assertEqual(claims["sub"], "synthetic-sub-1")


if __name__ == "__main__":
    unittest.main()
