"""Interfaces to external services, and how the application obtains them.

The application never constructs a Google or Humanity client directly. It asks
get_oidc_provider() / get_roster_provider(), which build the adapter named by a
setting. Tests point those settings at the synthetic stubs in cya.testing.stubs,
so no test can reach a real service (AGENTS.md rule 6, SPEC ARC-8). An unset
setting raises IntegrationNotConfigured, so a missing adapter fails closed.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from django.conf import settings
from django.utils.module_loading import import_string


class IntegrationNotConfigured(Exception):
    """No adapter is configured for a required external service."""


class IdentityRejected(Exception):
    """A sign-in failed verification. args[0] is a short reason code, never a token."""

    @property
    def reason(self):
        return self.args[0] if self.args else "rejected"


class IdentityProviderUnavailable(Exception):
    """The identity provider could not be reached or answered unusably."""


class RosterUnavailable(Exception):
    """The roster source could not supply an authoritative answer."""


@dataclass(frozen=True)
class VerifiedIdentity:
    """An identity whose ID token passed every check in cya.accounts.google."""

    issuer: str
    subject: str
    email: str
    display_name: str
    hosted_domain: str


@dataclass(frozen=True)
class RosterShift:
    """One scheduled shift: who is rostered between two aware UTC instants.

    Provisional: SPEC HUM-2 also names a vehicle assignment and fetched_at;
    the roster integration issue adds them when it implements the adapter.
    """

    shift_id: str
    user_ids: tuple[str, ...]
    start: datetime
    end: datetime
    vehicle_id: str | None = None


class OidcProvider(Protocol):
    """The network side of OpenID Connect sign-in (Google in production)."""

    def exchange_code(self, *, code: str, redirect_uri: str, code_verifier: str) -> str:
        """Exchange an authorization code for an ID token (a JWT string).

        Raises IdentityRejected if the provider refuses the code, or
        IdentityProviderUnavailable if it cannot be reached."""

    def signing_key(self, id_token: str):
        """Return the public key that should have signed id_token (by its kid)."""


class RosterProvider(Protocol):
    def shifts_overlapping(self, start: datetime, end: datetime) -> list[RosterShift]:
        """Return shifts overlapping [start, end) or raise RosterUnavailable."""


def _build(setting_name):
    path = getattr(settings, setting_name, None)
    if not path:
        raise IntegrationNotConfigured(f"settings.{setting_name} is not set.")
    return import_string(path)()


def get_oidc_provider() -> OidcProvider:
    return _build("CYA_OIDC_PROVIDER")


def get_roster_provider() -> RosterProvider:
    return _build("CYA_ROSTER_PROVIDER")
