"""Shared seal verification rules (SPEC SEAL-4, SEAL-8 and CAB-4).

Callers must supply facts read under the command's locks, including the current
designation and all covered deficiencies and suspensions.  This module does not
read the database or persist the resulting substitution.
"""

from dataclasses import dataclass
from datetime import date, datetime
from itertools import islice

MAX_INSPECTION_ITEMS = 2000


@dataclass(frozen=True)
class SealVerificationFacts:
    sealable: bool
    intact: bool
    marking: str
    expires_on: date | None
    audit_pending: bool
    open_deficiency: bool
    suspended: bool
    no_expiration: bool = False


def verification_refusal(facts, *, marking, attested, today):
    """Return a refusal code, or None if the supplied facts permit verification.

    ``today`` is the organization's local calendar date. A seal is valid on
    its expiration date; the expiration test changes on the following day.
    """
    if not isinstance(facts, SealVerificationFacts):
        raise TypeError("facts must be SealVerificationFacts")
    if not isinstance(today, date) or isinstance(today, datetime):
        raise ValueError("today must be a date")
    if facts.expires_on is not None and (
        not isinstance(facts.expires_on, date) or isinstance(facts.expires_on, datetime)
    ):
        raise ValueError("seal expiration must be a date")
    if not isinstance(facts.marking, str) or not facts.marking.strip():
        raise ValueError("stored seal marking is required")
    for value in (
        facts.sealable,
        facts.intact,
        facts.audit_pending,
        facts.open_deficiency,
        facts.suspended,
        facts.no_expiration,
    ):
        if type(value) is not bool:
            raise ValueError("seal state flags must be booleans")
    if (facts.expires_on is None) != facts.no_expiration:
        raise ValueError("seal must have an expiration or an explicit no-expiration marking")
    if facts.sealable is not True:
        return "NOT_SEALABLE"
    if facts.intact is not True:
        return "SEAL_OPEN"
    if facts.expires_on is not None and facts.expires_on < today:
        return "SEAL_EXPIRED"
    if facts.audit_pending:
        return "AUDIT_PENDING"
    if facts.open_deficiency:
        return "OPEN_DEFICIENCY"
    if facts.suspended:
        return "CHECK_SUSPENDED"
    if not isinstance(marking, str) or marking != facts.marking:
        return "MARKING_MISMATCH"
    if attested is not True:
        return "ATTESTATION_REQUIRED"
    return None


@dataclass(frozen=True)
class InspectionRequirement:
    """Server-resolved configuration for one covered physical item."""

    item_key: str
    expected_quantity: int | None
    tracks_expiration: bool


@dataclass(frozen=True)
class InspectionEntry:
    """Fresh observations for one covered physical item."""

    item_key: str
    count: int | None
    expiration: date | None


@dataclass(frozen=True)
class CertifiedKitCount:
    """A sealed cabinet's observed count and the assignments it certified."""

    line_id: str
    observed_count: int
    assignments: tuple[tuple[str, str], ...]  # (physical kit id, assignment event id)


def kit_count_refusal(certified, *, line_id, expected_count, current_assignments):
    """Fail closed if a certified line or any covered kit assignment has changed."""
    if not isinstance(certified, CertifiedKitCount):
        raise TypeError("certified count is required")
    if not isinstance(line_id, str) or not line_id or type(expected_count) is not int:
        raise ValueError("current kit line and integer target are required")
    if expected_count < 1 or type(certified.observed_count) is not int:
        raise ValueError("invalid kit count")
    if certified.observed_count < 0:
        raise ValueError("invalid kit count")

    def checked_assignments(values):
        values = tuple(islice(values, MAX_INSPECTION_ITEMS + 1))
        if len(values) > MAX_INSPECTION_ITEMS:
            raise ValueError("too many kit assignments")
        if any(
            not isinstance(value, tuple)
            or len(value) != 2
            or any(not isinstance(part, str) or not part for part in value)
            for value in values
        ):
            raise ValueError("invalid kit assignment")
        if len({kit_id for kit_id, _ in values}) != len(values):
            raise ValueError("duplicate kit instance")
        return frozenset(values)

    old = checked_assignments(certified.assignments)
    current = checked_assignments(current_assignments)
    if certified.line_id != line_id or old != current:
        return "KIT_SCOPE_CHANGED"
    if certified.observed_count != expected_count or len(old) != certified.observed_count:
        return "KIT_COUNT_MISMATCH"
    return None


def inspection_summary(requirements, entries, *, today):
    """Validate membership and count/date fields and return earliest date and mismatches.

    Keys identify the covered item *and its physical kit instance* when relevant.
    The caller resolves requirements from authoritative configuration, never from the request,
    and separately validates item outcomes, attestations and deficiency handling.
    """
    if not isinstance(today, date) or isinstance(today, datetime):
        raise ValueError("today must be a date")
    requirements = tuple(islice(requirements, MAX_INSPECTION_ITEMS + 1))
    if len(requirements) > MAX_INSPECTION_ITEMS:
        raise ValueError("inspection scope is too large")
    if any(not isinstance(r, InspectionRequirement) for r in requirements):
        raise ValueError("invalid inspection requirement")
    expected = [r.item_key for r in requirements]
    if any(not isinstance(k, str) or not k for k in expected) or len(set(expected)) != len(
        expected
    ):
        raise ValueError("expected item keys must be nonempty and unique")
    entries = tuple(islice(entries, MAX_INSPECTION_ITEMS + 1))
    if len(entries) > MAX_INSPECTION_ITEMS:
        raise ValueError("inspection contains too many entries")
    if any(not isinstance(e, InspectionEntry) for e in entries):
        raise ValueError("invalid inspection entry")
    keys = [entry.item_key for entry in entries]
    if any(not isinstance(k, str) or not k for k in keys):
        raise ValueError("inspection item keys must be nonempty strings")
    if len(keys) != len(expected) or len(set(keys)) != len(keys) or set(keys) != set(expected):
        raise ValueError("inspection must include each covered item exactly once")
    expirations = []
    mismatches = []
    settings = {r.item_key: r for r in requirements}
    for entry in entries:
        requirement = settings[entry.item_key]
        if type(requirement.tracks_expiration) is not bool:
            raise ValueError("expiration tracking must be a boolean")
        if requirement.expected_quantity is not None:
            if type(requirement.expected_quantity) is not int or requirement.expected_quantity < 0:
                raise ValueError("quantity target must be a nonnegative integer")
            if type(entry.count) is not int or entry.count < 0:
                raise ValueError("quantity items require a fresh nonnegative count")
            if entry.count != requirement.expected_quantity:
                mismatches.append(entry.item_key)
        elif entry.count is not None:
            raise ValueError("count is not applicable to this item")
        if requirement.tracks_expiration:
            if not isinstance(entry.expiration, date) or isinstance(entry.expiration, datetime):
                raise ValueError("tracked items require a fresh expiration date")
            if entry.expiration < today:
                raise ValueError("expired items cannot be resealed")
            expirations.append(entry.expiration)
        elif entry.expiration is not None:
            raise ValueError("expiration is not tracked for this item")
    return min(expirations, default=None), tuple(mismatches)
