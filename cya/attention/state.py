"""Pure item attention rules (SPEC 7.5, 19.4): no queries, no clock.

An item is red (Attention) when an unresolved deficiency applies to it, or when its latest
effective check is Failed or Unable to Inspect; otherwise it is clear. The two causes are
independent, so a later Pass clears the check cause but never an unresolved deficiency. An item
inside a kit is evaluated per kit instance (SPEC 19.4 as amended, OQ-53): a failure in one kit is
red for that kit only. An item outside a kit uses the NO_KIT key.

A deficiency applies to the items in its scope snapshot. When it names a kit instance it applies
to that instance only; when it names none (an independent report on an item that sits in several
kits) it applies to every instance of the item, so the state errs towards red.
"""

from dataclasses import dataclass

from cya.deficiencies.transitions import ACTIVE_STATUSES
from cya.obligations.state import NO_KIT

RED_OUTCOMES = ("FAILED", "UNABLE_TO_INSPECT")


@dataclass(frozen=True)
class CheckFact:
    """The effective, non-invalidated outcome of one correction chain."""

    check_id: str
    item_id: str
    kit_key: str
    recorded_at: object  # aware datetime of the effective row
    outcome: str


@dataclass(frozen=True)
class DeficiencyFact:
    """A deficiency's status at the evaluated instant and the items it concerns."""

    deficiency_id: str
    status: str
    item_ids: frozenset
    kit_key: str = NO_KIT  # NO_KIT: not tied to one kit instance


@dataclass(frozen=True)
class Attention:
    red: bool
    # Unresolved deficiencies that apply, sorted; the latest effective check when it is red.
    deficiency_ids: tuple = ()
    check_id: str | None = None
    check_outcome: str | None = None

    @property
    def state(self):
        return "ATTENTION" if self.red else "CLEAR"


def applies(deficiency, item_id, kit_key):
    if item_id not in deficiency.item_ids:
        return False
    return deficiency.kit_key == NO_KIT or deficiency.kit_key == kit_key


def latest_checks(checks):
    """{(item_id, kit_key): CheckFact}: the greatest (recorded_at, check_id) of each key."""
    latest = {}
    for check in checks:
        key = (check.item_id, check.kit_key)
        if key not in latest or (check.recorded_at, check.check_id) > (
            latest[key].recorded_at,
            latest[key].check_id,
        ):
            latest[key] = check
    return latest


def attention_states(keys, checks, deficiencies):
    """Return {(item_id, kit_key): Attention} for each requested key.

    Input order has no effect: the latest check is the greatest (recorded_at, check_id), and
    deficiency ids are sorted.
    """
    unresolved = [d for d in deficiencies if d.status in ACTIVE_STATUSES]
    latest = latest_checks(checks)
    result = {}
    for item_id, kit_key in keys:
        applying = tuple(
            sorted(d.deficiency_id for d in unresolved if applies(d, item_id, kit_key))
        )
        check = latest.get((item_id, kit_key))
        red_check = check if check is not None and check.outcome in RED_OUTCOMES else None
        result[(item_id, kit_key)] = Attention(
            red=bool(applying) or red_check is not None,
            deficiency_ids=applying,
            check_id=red_check.check_id if red_check else None,
            check_outcome=red_check.outcome if red_check else None,
        )
    return result
