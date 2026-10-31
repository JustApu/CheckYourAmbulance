"""Synthetic, offline stand-ins for Google Workspace, Humanity and the
collaborators sign-in needs from other issues.

Inject them through settings (django.test.override_settings), e.g.
override_settings(**stub_google_settings()). They make no network calls and
read only tests/fixtures/. Keys are generated at test time; none is committed.
"""

import secrets
import time
from datetime import UTC, datetime

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from cya.integrations import (
    IdentityProviderUnavailable,
    IdentityRejected,
    RosterShift,
    RosterUnavailable,
)
from cya.testing.fixtures import FIXTURE_DIR, FixtureError, load_fixture

STUB_CLIENT_ID = "stub-client-id.apps.example.invalid"
STUB_CLIENT_SECRET = "stub-client-secret-not-a-secret"
STUB_HOSTED_DOMAIN = "agency.example"
STUB_REDIRECT_URI = "https://cya.example/auth/callback/"
STUB_KID = "stub-key-1"


class StubGoogle:
    """Offline Google: mints RS256 ID tokens with a synthetic key, accepts the
    codes it issued exactly once, and implements the OidcProvider protocol."""

    def __init__(self):
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._codes = {}
        self.exchanges = []
        self.unavailable = False  # True simulates Google being unreachable

    def id_token(
        self, *, sub, nonce, omit=(), key=None, kid=STUB_KID, algorithm="RS256", **overrides
    ):
        now = int(time.time())
        claims = {
            "iss": "https://accounts.google.com",
            "aud": STUB_CLIENT_ID,
            "sub": sub,
            "nonce": nonce,
            "hd": STUB_HOSTED_DOMAIN,
            "email": f"{sub}@{STUB_HOSTED_DOMAIN}",
            "email_verified": True,
            "name": f"Synthetic {sub}",
            "iat": now,
            "exp": now + 3600,
        }
        claims.update(overrides)
        for name in omit:
            claims.pop(name, None)
        signing_key = key if key is not None else self.private_key
        return jwt.encode(claims, signing_key, algorithm=algorithm, headers={"kid": kid})

    def issue_code(self, id_token):
        code = secrets.token_urlsafe(24)
        self._codes[code] = id_token
        return code

    def exchange_code(self, *, code, redirect_uri, code_verifier):
        self.exchanges.append({"redirect_uri": redirect_uri, "code_verifier": code_verifier})
        if self.unavailable:
            raise IdentityProviderUnavailable("stub Google configured as unreachable")
        if code not in self._codes:
            raise IdentityRejected("code_rejected")
        return self._codes.pop(code)  # single use, like Google

    def signing_key(self, id_token):
        try:
            kid = jwt.get_unverified_header(id_token).get("kid")
        except jwt.DecodeError as exc:
            raise IdentityRejected("unknown_signing_key") from exc
        if kid != STUB_KID:
            raise IdentityRejected("unknown_signing_key")
        return self.private_key.public_key()


_ACTIVE_GOOGLE = None


def activate_stub_google():
    """Create a fresh StubGoogle and make it the configured provider."""
    global _ACTIVE_GOOGLE
    _ACTIVE_GOOGLE = StubGoogle()
    return _ACTIVE_GOOGLE


def active_stub_google():
    """Settings factory for CYA_OIDC_PROVIDER in tests."""
    if _ACTIVE_GOOGLE is None:
        raise RuntimeError("call activate_stub_google() first")
    return _ACTIVE_GOOGLE


class RecordingAuditSink:
    """Collects AuthAuditEvents; set fail=True (or fail_with=an exception) to
    simulate an audit outage."""

    def __init__(self):
        self.events = []
        self.fail = False
        self.fail_with = None

    def __call__(self, event):
        if self.fail_with is not None:
            raise self.fail_with
        if self.fail:
            raise RuntimeError("stub audit sink unavailable")
        self.events.append(event)


class RecordingRoleGranter:
    """Stands in for issue 22 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/22)'s Crew grant; set fail=True (or fail_with=an
    exception) to force a rollback."""

    def __init__(self, role="CREW"):
        self.grants = []
        self.fail = False
        self.fail_with = None
        self.role = role

    def __call__(self, *, user, command_id):
        if self.fail_with is not None:
            raise self.fail_with
        if self.fail:
            raise RuntimeError("stub role granter failed")
        self.grants.append((str(user.id), self.role, command_id))


AUDIT = RecordingAuditSink()
ROLE_GRANTER = RecordingRoleGranter()


def stub_google_settings():
    """override_settings kwargs that wire sign-in to the offline stubs."""
    return {
        "CYA_OIDC_PROVIDER": "cya.testing.stubs.active_stub_google",
        "CYA_GOOGLE_CLIENT_ID": STUB_CLIENT_ID,
        "CYA_GOOGLE_CLIENT_SECRET": STUB_CLIENT_SECRET,
        "CYA_GOOGLE_HOSTED_DOMAIN": STUB_HOSTED_DOMAIN,
        "CYA_GOOGLE_REDIRECT_URI": STUB_REDIRECT_URI,
        "CYA_AUTH_AUDIT_SINK": "cya.testing.stubs.AUDIT",
        "CYA_INITIAL_ROLE_GRANTER": "cya.testing.stubs.ROLE_GRANTER",
    }


class StubRosterProvider:
    """Serves fixed shifts; available=False simulates an unreachable roster."""

    def __init__(self, shifts, available=True):
        self._shifts = tuple(shifts)
        self._available = available

    def shifts_overlapping(self, start, end):
        for value in (start, end):
            if not isinstance(value, datetime) or value.utcoffset() is None:
                raise ValueError("start and end must be timezone-aware datetimes")
        if not start < end:
            raise ValueError("start must be before end")
        if not self._available:
            raise RosterUnavailable("stub roster configured as unavailable")
        found = [s for s in self._shifts if s.start < end and s.end > start]
        return sorted(found, key=lambda s: (s.start, s.shift_id))


def _aware_utc(text, where):
    try:
        value = datetime.fromisoformat(text)
    except (TypeError, ValueError) as exc:
        raise FixtureError(f"{where}: invalid instant {text!r}") from exc
    if value.utcoffset() is None:
        raise FixtureError(f"{where}: instant {text!r} has no UTC offset")
    return value.astimezone(UTC)


def fixture_roster_provider(fixture="base.json", available=True, fixture_dir=FIXTURE_DIR):
    """Shifts from a fixture's 'rosters' list, normalized to UTC."""
    rosters = load_fixture(fixture, fixture_dir).get("rosters")
    if not isinstance(rosters, list):
        raise FixtureError(f"{fixture}: 'rosters' must be a list")
    shifts = []
    for roster in rosters:
        users = roster.get("users") if isinstance(roster, dict) else None
        if not isinstance(users, list) or not all(isinstance(u, str) and u for u in users):
            raise FixtureError(f"{fixture}: malformed roster {roster!r}")
        where = f"{fixture} roster {roster.get('id')!r}"
        shifts.append(
            RosterShift(
                shift_id=str(roster.get("id")),
                user_ids=tuple(roster["users"]),
                start=_aware_utc(roster.get("start"), where),
                end=_aware_utc(roster.get("end"), where),
            )
        )
    return StubRosterProvider(shifts, available=available)


class StubPriorCheckLookup:
    """Offline synthetic check repository for conditional carry-over tests (issue 58 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/58))."""

    def __init__(self, checks=(), *, unavailable=False):
        self._checks = list(checks)
        self.unavailable = unavailable

    def add_check(
        self,
        *,
        check_id: str,
        organization_id: str,
        vehicle_id: str,
        item_id: str,
        checked_at: datetime,
        passed: bool | None = True,
        outcome: str | None = None,
        invalidated: bool = False,
        suspended: bool = False,
        unable_to_inspect: bool = False,
        na_rule_id: str | None = None,
        obligation_id: str | None = None,
        shared_rule_id: str | None = None,
        shared_obligation_ids: tuple[str, ...] | list[str] = (),
    ) -> dict:
        if outcome is not None:
            norm_outcome = str(outcome).upper()
            if norm_outcome in ("PASS", "PASSED"):
                norm_outcome = "PASSED"
                norm_passed = True if passed is None else bool(passed)
            elif norm_outcome in ("FAIL", "FAILED"):
                norm_outcome = "FAILED"
                norm_passed = False
            elif norm_outcome in ("NA", "NOT_APPLICABLE"):
                norm_outcome = "NOT_APPLICABLE"
                norm_passed = False
            elif norm_outcome in ("UNABLE", "UNABLE_TO_INSPECT"):
                norm_outcome = "UNABLE_TO_INSPECT"
                norm_passed = False
                unable_to_inspect = True
            else:
                norm_passed = bool(passed)
        elif unable_to_inspect:
            norm_outcome = "UNABLE_TO_INSPECT"
            norm_passed = False
        elif na_rule_id:
            norm_outcome = "NOT_APPLICABLE"
            norm_passed = False if passed is None else bool(passed)
        elif passed is False:
            norm_outcome = "FAILED"
            norm_passed = False
        else:
            norm_outcome = "PASSED"
            norm_passed = True

        entry = {
            "id": str(check_id),
            "organization_id": str(organization_id),
            "vehicle_id": str(vehicle_id),
            "item_id": str(item_id),
            "checked_at": checked_at if checked_at.tzinfo else checked_at.replace(tzinfo=UTC),
            "passed": norm_passed,
            "outcome": norm_outcome,
            "invalidated": bool(invalidated),
            "suspended": bool(suspended),
            "unable_to_inspect": bool(unable_to_inspect),
            "na_rule_id": str(na_rule_id) if na_rule_id else None,
            "obligation_id": str(obligation_id) if obligation_id else None,
            "shared_rule_id": str(shared_rule_id) if shared_rule_id else None,
            "shared_obligation_ids": tuple(str(x) for x in (shared_obligation_ids or ())),
        }
        self._checks.append(entry)
        return entry

    def invalidate_check(self, check_id: str) -> None:
        for entry in self._checks:
            if entry["id"] == str(check_id):
                entry["invalidated"] = True

    def set_passed(self, check_id: str, passed: bool) -> None:
        for entry in self._checks:
            if entry["id"] == str(check_id):
                entry["passed"] = bool(passed)
                entry["outcome"] = "PASSED" if passed else "FAILED"

    @staticmethod
    def _links_to_obligation(entry: dict, obligation_id: str) -> bool:
        target = str(obligation_id)
        if entry.get("obligation_id") is not None and str(entry.get("obligation_id")) == target:
            return True
        if entry.get("shared_rule_id"):
            shared = {
                str(x)
                for x in (
                    entry.get("shared_obligation_ids") or entry.get("linked_obligation_ids") or ()
                )
            }
            if target in shared:
                return True
        return False

    @staticmethod
    def _satisfies_obligation_outcome(entry: dict) -> bool:
        if entry.get("invalidated") or entry.get("suspended") or entry.get("unable_to_inspect"):
            return False
        raw_outcome = entry.get("outcome")
        if raw_outcome is not None:
            norm = str(raw_outcome).upper()
            if norm in ("PASS", "PASSED", "FAIL", "FAILED"):
                return True
            if norm in ("NA", "NOT_APPLICABLE"):
                return bool(entry.get("na_rule_id"))
            return False
        if entry.get("na_rule_id"):
            return True
        return entry.get("passed") in (True, False)

    def __call__(
        self,
        organization,
        vehicle_id,
        item_id,
        window_start,
        window_end,
        *,
        obligation_id: str | None = None,
    ):
        from cya.conditions.evaluator import PriorCheckLookupUnavailable

        if self.unavailable:
            raise PriorCheckLookupUnavailable("stub prior check lookup configured as unavailable")
        org_id = str(getattr(organization, "pk", organization))
        veh_id = str(vehicle_id)
        itm_id = str(item_id)
        w_start = window_start if window_start.tzinfo else window_start.replace(tzinfo=UTC)
        w_end = window_end if window_end.tzinfo else window_end.replace(tzinfo=UTC)
        matching = [
            dict(c)
            for c in self._checks
            if c["organization_id"] == org_id
            and c["vehicle_id"] == veh_id
            and c["item_id"] == itm_id
            and w_start <= c["checked_at"] < w_end
            and (obligation_id is None or self._links_to_obligation(c, str(obligation_id)))
        ]
        if not matching:
            return None
        if obligation_id is not None:
            # Per SCH-7, the earliest valid qualifying check linked to the obligation satisfies it.
            matching.sort(
                key=lambda c: (
                    not self._satisfies_obligation_outcome(c),
                    c["checked_at"],
                )
            )
            return matching[0]
        matching.sort(
            key=lambda c: (
                not c.get("invalidated", False) and bool(c.get("passed", False)),
                c["checked_at"],
            ),
            reverse=True,
        )
        return matching[0]


_ACTIVE_PRIOR_CHECK_LOOKUP: StubPriorCheckLookup | None = None


def activate_stub_prior_check_lookup(checks=(), *, unavailable=False) -> StubPriorCheckLookup:
    """Create a fresh StubPriorCheckLookup for CYA_PRIOR_CHECK_LOOKUP in tests."""
    global _ACTIVE_PRIOR_CHECK_LOOKUP
    _ACTIVE_PRIOR_CHECK_LOOKUP = StubPriorCheckLookup(checks=checks, unavailable=unavailable)
    return _ACTIVE_PRIOR_CHECK_LOOKUP


def active_stub_prior_check_lookup(
    organization,
    vehicle_id,
    item_id,
    window_start,
    window_end,
    *,
    obligation_id: str | None = None,
):
    """Settings callable for CYA_PRIOR_CHECK_LOOKUP in tests."""
    from cya.conditions.evaluator import PriorCheckLookupUnavailable

    if _ACTIVE_PRIOR_CHECK_LOOKUP is None:
        raise PriorCheckLookupUnavailable("call activate_stub_prior_check_lookup() first")
    return _ACTIVE_PRIOR_CHECK_LOOKUP(
        organization,
        vehicle_id,
        item_id,
        window_start,
        window_end,
        obligation_id=obligation_id,
    )
