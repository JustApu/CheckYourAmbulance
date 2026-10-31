"""Google Workspace ID-token verification (SPEC AUTH-1, AUTH-3; OQ-13).

Checked against Google's OpenID Connect documentation (page last updated
2026-06-15, read 2026-10-20) and its discovery document:

* signature: RS256 only, key from Google's JWKS (jwks_uri);
* iss is "https://accounts.google.com" or "accounts.google.com";
* aud equals our client ID; exp has not passed;
* nonce equals the value we sent (replay protection);
* sub is the identity key; email is an attribute and is never trusted for
  identity or organization membership;
* hd ("the domain associated with the Google Workspace or Cloud organization
  of the user") must be present and equal the configured domain. Its absence
  means the account is not in a Google-hosted domain, so personal accounts
  and accounts that merely use an address in our domain are refused.

Network access (token exchange and key download) lives in cya.accounts.oidc;
this module only verifies, so it is tested offline with synthetic keys.
"""

import hmac
from dataclasses import dataclass
from datetime import UTC, datetime

import jwt

from cya.integrations import IdentityRejected, VerifiedIdentity

GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
CANONICAL_ISSUER = "https://accounts.google.com"
ALGORITHMS = ["RS256"]
REQUIRED_CLAIMS = ["exp", "iat", "iss", "aud", "sub"]
# Allowance for clock differences between Google and this server.
LEEWAY_SECONDS = 60


@dataclass(frozen=True)
class GoogleIdTokenVerifier:
    """Verifies one ID token. key_provider.signing_key(token) returns the
    public key for the token's kid (PyJWKClient in production, a synthetic
    key in tests)."""

    client_id: str
    hosted_domain: str
    key_provider: object

    def verify(self, id_token, *, nonce, now=None):
        if not isinstance(id_token, str) or not id_token:
            raise IdentityRejected("malformed_token")
        if not isinstance(nonce, str) or not nonce:
            raise IdentityRejected("missing_nonce")
        try:
            key = self.key_provider.signing_key(id_token)
            claims = jwt.decode(
                id_token,
                key,
                algorithms=ALGORITHMS,
                audience=self.client_id,
                issuer=GOOGLE_ISSUERS,
                leeway=LEEWAY_SECONDS,
                options={"require": REQUIRED_CLAIMS, "strict_aud": True},
            )
        except IdentityRejected:
            raise
        except jwt.ExpiredSignatureError as exc:
            raise IdentityRejected("token_expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise IdentityRejected("wrong_audience") from exc
        except jwt.InvalidIssuerError as exc:
            raise IdentityRejected("wrong_issuer") from exc
        except jwt.ImmatureSignatureError as exc:
            raise IdentityRejected("issued_in_future") from exc
        except jwt.InvalidTokenError as exc:
            raise IdentityRejected("invalid_token") from exc
        return self._identity_from_claims(claims, nonce, now)

    def _identity_from_claims(self, claims, nonce, now):
        now_ts = (now or datetime.now(UTC)).timestamp()
        iat = claims.get("iat")
        if not isinstance(iat, int | float) or iat > now_ts + LEEWAY_SECONDS:
            raise IdentityRejected("issued_in_future")
        token_nonce = claims.get("nonce")
        if not isinstance(token_nonce, str) or not hmac.compare_digest(
            token_nonce.encode(), nonce.encode()
        ):
            raise IdentityRejected("nonce_mismatch")
        azp = claims.get("azp")
        if azp is not None and azp != self.client_id:
            raise IdentityRejected("wrong_authorized_party")
        hosted_domain = claims.get("hd")
        if not isinstance(hosted_domain, str) or not hosted_domain:
            raise IdentityRejected("not_workspace_account")
        if hosted_domain.lower() != self.hosted_domain.lower():
            raise IdentityRejected("wrong_workspace_domain")
        subject = claims.get("sub")
        email = claims.get("email")
        if not isinstance(subject, str) or not subject or len(subject) > 255:
            raise IdentityRejected("invalid_subject")
        if claims.get("email_verified") is not True or not isinstance(email, str) or not email:
            raise IdentityRejected("email_not_verified")
        name = claims.get("name")
        name = name if isinstance(name, str) else ""
        # Oversized attributes are refused, never silently truncated (contracts section 1).
        if len(email) > 254 or len(name) > 200:
            raise IdentityRejected("attribute_too_long")
        return VerifiedIdentity(
            # Both documented issuer spellings name the same provider; store one
            # so the identity key cannot split into two users.
            issuer=CANONICAL_ISSUER,
            subject=subject,
            email=email,
            display_name=name,
            hosted_domain=hosted_domain.lower(),
        )
