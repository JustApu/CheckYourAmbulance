"""Validate fresh full-inspection observations against server-resolved scope."""

from dataclasses import dataclass
from datetime import date, datetime
from types import SimpleNamespace

from cya.checks.commands import (
    CheckError,
    _check_observation,
    _check_outcome,
    _measurement,
    _quantity,
)
from cya.kits.seal_rules import InspectionEntry, InspectionRequirement, inspection_summary
from cya.seals.scope import MAX_SCOPE


@dataclass(frozen=True)
class InspectedItem:
    """A normalized observation and the source facts it was checked against."""

    scope: object
    outcome: str
    count: int | None
    expiration: date | None
    measurement: tuple | None


def validate_inspection(scope, observations, *, today):
    """Validate every physical item once; return observations, expiration and mismatches.

    Scope comes from the server, including physical kit identity and current target. An
    inability to inspect is not a full inspection; applicability determinations require their
    own evidence and are rejected here until that path is supplied by the caller.
    """
    if not isinstance(scope, (tuple, list)) or len(scope) > MAX_SCOPE:
        raise ValueError("invalid inspection scope")
    if not isinstance(observations, (tuple, list)) or len(observations) > MAX_SCOPE:
        raise ValueError("invalid inspection observations")
    if not isinstance(today, date) or isinstance(today, datetime):
        raise ValueError("today must be a date")
    expected = {item.key: item for item in scope}
    if len(expected) != len(scope):
        raise ValueError("duplicate physical item in inspection scope")
    seen = set()
    inspected = []
    requirements = []
    entries = []
    for row in observations:
        if not isinstance(row, dict) or set(row) != {
            "item_id",
            "kit_id",
            "outcome",
            "count",
            "expiration",
            "measurement",
        }:
            raise ValueError("invalid inspection item fields")
        item_id, kit_id = row["item_id"], row["kit_id"]
        if (
            not isinstance(item_id, str)
            or not item_id
            or (kit_id is not None and (not isinstance(kit_id, str) or not kit_id))
        ):
            raise ValueError("invalid inspection item identity")
        key = (item_id, kit_id)
        if key in seen or key not in expected:
            raise ValueError("inspection contains a duplicate or unlisted physical item")
        seen.add(key)
        item = expected[key]
        try:
            count = _quantity(row["count"])
            measurement = _measurement(row["measurement"])
        except CheckError as error:
            raise ValueError(str(error)) from None
        outcome = row["outcome"]
        if outcome is None and item.kind == "QUANTITY" and count is not None:
            outcome = "PASSED" if count == item.expected_quantity else "FAILED"
        if outcome not in ("PASSED", "FAILED"):
            raise ValueError("a full inspection requires a passed or failed observation")
        shape = {"outcome": outcome, "quantity": count, "measurement": measurement}
        resolved = SimpleNamespace(requirement=item, effective_quantity=item.expected_quantity)
        try:
            _check_outcome(resolved, shape)
            _check_observation(resolved, shape)
        except CheckError as error:
            raise ValueError(str(error)) from None
        expiration = row["expiration"]
        # A tuple key identifies the physical item in the authoritative scope; using a
        # generated local ordinal avoids treating any client-supplied concatenation as an ID.
        ordinal = str(len(inspected))
        requirements.append(
            InspectionRequirement(ordinal, item.expected_quantity, item.expiration_tracked)
        )
        entries.append(InspectionEntry(ordinal, count, expiration))
        inspected.append(InspectedItem(item, outcome, count, expiration, measurement))
    if seen != set(expected):
        raise ValueError("inspection must include every covered physical item")
    expiration, mismatch_ordinals = inspection_summary(requirements, entries, today=today)
    mismatches = tuple(inspected[int(ordinal)].scope.key for ordinal in mismatch_ordinals)
    return tuple(inspected), expiration, mismatches
