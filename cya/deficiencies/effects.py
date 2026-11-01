"""What correcting or invalidating a check does to the deficiencies it supports (issue 81 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/81)).

COR-3/COR-4: an author's Failed to Passed Correct-entry marks a supporting deficiency Reported in
Error only when no other effective failed (or unable) check supports it, no repair or
verification work is recorded for it, and its status allows the transition. Otherwise the
correction stands, the deficiency stays open and a CORRECTION_NOTICE event records the intent to
notify a supervisor. INV-3: invalidating a Failed chain never closes or cancels a deficiency.
INV-7: invalidating the chain whose correction cancelled a deficiency reopens it.
INV-4 (issue 155 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/155)): invalidating a verification chain, or its author correcting its Pass to Failed
or Unable to Inspect, reopens the Closed Verified deficiency it verified when the closing scope
revision is no longer fully satisfied by the remaining effective Passes (VERIFICATION_REOPENED). A
Pending Verification deficiency needs no event: its scope is always recomputed from effective
checks, so the lost Pass simply stops counting.

A Reported in Error cancellation ends every check suspension still in force and lists it in
suspensions_ended (SPEC SUS-4, issue 13 (https://github.com/JustApu/CheckYourAmbulance/issues/13));
the INV-7 reopening resumes none.

Both commands call plan_* in prepare, under the item lock they already hold, which takes the
locks of every deficiency the chain supports or verifies once, as one sorted set (the contract's
last lock group), and then append_effect in apply. The decision is a pure function so each branch is tested on its own. Everything here
fails toward open: unreadable or oversized evidence blocks the cancellation.
"""

from dataclasses import dataclass

from django.db.models import Exists, OuterRef
from django.utils import timezone

from cya.accounts.fields import new_id
from cya.checks.models import CheckEvent, CheckRecord
from cya.checks.state import CheckDecision, CheckSnapshot, effective_checks
from cya.deficiencies.models import (
    DeficiencyEvent,
    DeficiencyEvidence,
    DeficiencyProjection,
    VerificationScopeRow,
)
from cya.deficiencies.service import (
    DeficiencyError,
    end_suspensions,
    lock_deficiencies,
    stamp,
    suspension_ids_in_force,
)
from cya.deficiencies.transitions import (
    ACTIVE_STATUSES,
    ALLOWED_TRANSITIONS,
    CLOSED_VERIFIED,
    OPEN,
    REPORTED_IN_ERROR,
    TransitionRejected,
    validate_transition,
)

# Event types that are neither repair nor verification (a re-grade only informs people, DEF-8,
# OPS-2, and assigning, setting a deadline or starting work records no repair, DEF-4, and waiting
# on a third party records none either, SPEC 8.5, nor does asking for or granting more time, DEF-5,
# so none of them may block a cancellation). An escalation acknowledgement records intent and
# pauses escalation, not performed repair (DEF-7). Any
# other type present counts as recorded repair work and blocks a cancellation: the M4 corrective
# action and verification events, and a progress note, whose free text may describe work done
# (SPEC 8.1). Each later lifecycle issue states its own classification the same way. A supervisor
# check suspension pauses routine checks for a known defect and records no repair (SPEC 8.9).
NEUTRAL_EVENT_TYPES = frozenset(
    {
        "REPORTED",
        "REPORTED_IN_ERROR",
        "CORRECTION_NOTICE",
        "REOPENED",
        "SEVERITY_CHANGED",
        "ASSIGNED",
        "DEADLINE_SET",
        "WORK_STARTED",
        "DEPENDENCY_ENTERED",
        "DEPENDENCY_REVISED",
        "DEPENDENCY_ENDED",
        "EXTENSION_REQUESTED",
        "EXTENSION_DECIDED",
        "ESCALATION_ACKNOWLEDGED",
        "ADMINISTRATOR_CLOSED",
        "ADMINISTRATOR_REOPENED",
        "EMPTY_SCOPE_CLOSED",
        # Reopening after a lost verification Pass records no repair (INV-4, issue 155 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/155)).
        "VERIFICATION_REOPENED",
        "CHECKS_SUSPENDED",
    }
)
# Evidence that keeps a deficiency open (SPEC COR-4, OQ-57): a Failed check, or an Unable to
# Inspect one, because an unverified item is not a resolved defect.
BLOCKING_OUTCOMES = frozenset({"FAILED", "UNABLE_TO_INSPECT"})
# Bounds on the work and the lock set of one correction. Over a bound the answer fails toward open
# (a notice, never a cancellation) or, for the lock set, the command is refused before any lock.
MAX_EVIDENCE = 1000  # other chains of one deficiency
MAX_CHAIN_ROWS = 10_000  # check and event rows read across those chains
MAX_SUPPORTING = 100  # deficiencies one chain may support and a correction will lock
MAX_HISTORY_EVENTS = 1000  # events of one deficiency inspected for recorded repair work
MAX_SCOPE_ROWS = 2000  # rows of one scope revision a verification decision will evaluate
MAX_VERIFICATION_CHECKS = 20_000  # qualifying checks read to decide one closure or reopening
NO_KIT_KEY = "00000000-0000-0000-0000-000000000000"  # the scope instance key of a no-kit item

ERROR, NOTICE, NONE = "ERROR", "NOTICE", "NONE"
ERROR_TEXT = "Reported in error: the author corrected the Failed check to Passed."
NOTICE_TEXT = (
    "The author corrected a Failed check to Passed, but this deficiency stays open: "
    "other evidence, recorded repair work or its status needs a supervisor to resolve it."
)
REOPEN_TEXT = (
    "Reopened: the correction chain that reported this deficiency in error was invalidated."
)
VERIFICATION_REOPEN_TEXT = {
    "INVALIDATION": "Reopened: a verification check its closure relied on was invalidated.",
    "CORRECTION": "Reopened: a verification check its closure relied on was corrected to a non-Pass.",
}


@dataclass(frozen=True)
class Effect:
    deficiency_id: str
    event_id: str
    event_type: str
    from_status: str
    to_status: str
    explanation: str
    # Set only on VERIFICATION_REOPENED: the root of the verification chain that lost its Pass.
    verification_check_id: str | None = None


def decide_correction_effect(status, other_support, repair_recorded):
    """ERROR (cancel), NOTICE (stay open, notify) or NONE for one supporting deficiency."""
    if status not in ACTIVE_STATUSES:
        return NONE  # already closed or cancelled: nothing for a correction to change
    if other_support or repair_recorded:
        return NOTICE
    if (status, REPORTED_IN_ERROR) not in ALLOWED_TRANSITIONS:
        return NOTICE
    return ERROR


def supporting_deficiency_ids(organization_id, root_id, verification_target=None):
    """Deficiencies the chain supports or verifies, sorted once (the lock order).

    Evidence from any row of the chain, plus the deficiency a verification chain verifies, which
    need not hold the chain as evidence (a Pass is never evidence). Both sets are locked together in
    one sorted pass: locking them one after the other could take two locks in opposite orders.
    """
    ids = {
        str(i)
        for i in DeficiencyEvidence.objects.filter(
            organization_id=organization_id, check_record__root_check_id=root_id
        )
        .values_list("deficiency_id", flat=True)
        .distinct()[: MAX_SUPPORTING + 1]
    }
    if verification_target is not None:
        ids.add(str(verification_target))
    if len(ids) > MAX_SUPPORTING:
        raise DeficiencyError(
            "CONFIGURATION_UNAVAILABLE",
            "the check supports too many deficiencies to act on at once",
            status=503,
        )
    return sorted(ids)


def other_effective_support(organization_id, deficiency_id, own_root_id, now):
    """True if another, non-invalidated chain of this deficiency still ends Failed or Unable."""
    roots = set(
        DeficiencyEvidence.objects.filter(
            organization_id=organization_id, deficiency_id=deficiency_id, check_record__isnull=False
        )
        .exclude(check_record__root_check_id=own_root_id)
        .values_list("check_record__root_check_id", flat=True)[: MAX_EVIDENCE + 1]
    )
    if len(roots) > MAX_EVIDENCE:
        return True
    if not roots:
        return False
    rows = list(
        CheckRecord.objects.filter(organization_id=organization_id, root_check_id__in=roots)[
            : MAX_CHAIN_ROWS + 1
        ]
    )
    events = list(
        CheckEvent.objects.filter(organization_id=organization_id, root_check_id__in=roots)[
            : MAX_CHAIN_ROWS + 1
        ]
    )
    if len(rows) > MAX_CHAIN_ROWS or len(events) > MAX_CHAIN_ROWS:
        return True  # too much history to replay here: keep the deficiency open
    try:
        effective = effective_checks(
            (
                CheckSnapshot(
                    str(r.pk),
                    str(r.root_check_id),
                    _text(r.predecessor_check_id),
                    r.recorded_at,
                    r.outcome,
                )
                for r in rows
            ),
            (
                CheckDecision(
                    str(e.root_check_id),
                    str(e.selected_check_id),
                    e.event_type,
                    e.recorded_at,
                    _text(e.replacement_check_id),
                )
                for e in events
            ),
            as_of=timezone.now(),
        )
    except ValueError:
        raise DeficiencyError(
            "CONFIGURATION_UNAVAILABLE", "check history is unavailable", status=503
        ) from None
    return any(not e.invalidated and e.outcome in BLOCKING_OUTCOMES for e in effective)


def _text(value):
    return None if value is None else str(value)


def _repair_recorded(deficiency_id):
    """True if the history may hold repair work: a non-neutral event type, or too long to inspect.

    At most MAX_HISTORY_EVENTS + 1 event types are read, newest first, so the work per deficiency
    is bounded whatever the history length. Past the bound the answer is "yes" (the deficiency
    stays open with a notice), because an uninspected history cannot prove that no repair exists.
    """
    types = list(
        DeficiencyEvent.objects.filter(deficiency_id=deficiency_id)
        .order_by("-sequence")
        .values_list("event_type", flat=True)[: MAX_HISTORY_EVENTS + 1]
    )
    return len(types) > MAX_HISTORY_EVENTS or any(t not in NEUTRAL_EVENT_TYPES for t in types)


def plan_correction_effects(organization_id, root_id):
    """Lock the chain's deficiencies (sorted) and decide each; call under the item lock."""
    ids = supporting_deficiency_ids(organization_id, root_id)
    projections = lock_deficiencies(organization_id, ids)
    now = timezone.now()
    plans = []
    for deficiency_id in ids:
        status = projections[deficiency_id].status
        if status not in ACTIVE_STATUSES:
            continue
        decision = decide_correction_effect(
            status,
            other_effective_support(organization_id, deficiency_id, root_id, now),
            _repair_recorded(deficiency_id),
        )
        if decision == ERROR:
            plans.append(
                Effect(
                    deficiency_id,
                    new_id(),
                    "REPORTED_IN_ERROR",
                    status,
                    REPORTED_IN_ERROR,
                    ERROR_TEXT,
                )
            )
        elif decision == NOTICE:
            plans.append(
                Effect(deficiency_id, new_id(), "CORRECTION_NOTICE", status, status, NOTICE_TEXT)
            )
    return plans


def scope_lineage(organization_id, deficiency_id, rows):
    """The revisions whose Passes may count for these rows of one revision (VER-4, 1.22.0).

    That is the rows' own revision and every earlier revision of the same correction: a supervisor
    expansion repeats the rows of the revision it expands, so a Pass of an expanded revision still
    satisfies its repeated row, while a new correction starts a lineage no earlier Pass reaches.
    """
    revisions = {row.scope_revision for row in rows}
    corrections = {row.correction_event_id for row in rows}
    if len(revisions) != 1 or len(corrections) != 1:
        raise DeficiencyError(
            "CONFIGURATION_UNAVAILABLE", "verification scope is unavailable", status=503
        )
    return set(
        VerificationScopeRow.objects.filter(
            organization_id=organization_id,
            deficiency_id=deficiency_id,
            correction_event_id=corrections.pop(),
            scope_revision__lte=revisions.pop(),
        )
        .values_list("scope_revision", flat=True)
        .distinct()
    )


def satisfied_scope_rows(organization_id, deficiency_id, rows, excluding_root_id=None):
    """Ids of scope rows an effective verification Pass satisfies (VER-4).

    A Pass counts for a row only if it is a verification check for this deficiency recorded
    against a revision of the row's lineage (scope_lineage), on the row's item and physical kit
    instance (an item outside a kit has the all-zero key), was recorded strictly after the row's
    instant, and is effective: nothing replaced it and no administrator invalidated its chain.
    excluding_root_id leaves out a chain the calling command is about to invalidate or correct to a
    non-Pass. Call under the deficiency lock, so every committed verification check is visible.
    """
    lineage = scope_lineage(organization_id, deficiency_id, rows)
    checks = (
        CheckRecord.objects.filter(
            organization_id=organization_id,
            purpose="VERIFICATION",
            verification_for_deficiency_id=deficiency_id,
            outcome="PASSED",
            verification_scope_revision__in=lineage,
            recorded_at__gt=min(row.required_after for row in rows),
        )
        .filter(~Exists(CheckRecord.objects.filter(predecessor_check_id=OuterRef("pk"))))
        .filter(
            ~Exists(
                CheckEvent.objects.filter(
                    root_check_id=OuterRef("root_check_id"), event_type="INVALIDATION"
                )
            )
        )
    )
    if excluding_root_id is not None:
        checks = checks.exclude(root_check_id=excluding_root_id)
    seen = {}
    values = checks.values_list("item_id", "kit_id", "recorded_at")
    for count, (item_id, kit_id, recorded_at) in enumerate(
        values[: MAX_VERIFICATION_CHECKS + 1], start=1
    ):
        if count > MAX_VERIFICATION_CHECKS:
            raise DeficiencyError(
                "CONFIGURATION_UNAVAILABLE", "verification history is too large", status=503
            )
        seen.setdefault((str(item_id), str(kit_id or NO_KIT_KEY)), []).append(recorded_at)
    return {
        row.pk
        for row in rows
        if any(at > row.required_after for at in seen.get((str(row.item_id), row.instance_key), ()))
    }


def decide_verification_reopen(status, chain_counted, still_satisfied):
    """True when losing this chain's Pass must reopen the deficiency (INV-4), as a pure function.

    Only a Closed Verified deficiency reopens, and only when the chain was recorded against a
    revision whose Passes counted toward its closure (the closing revision's lineage) and the
    remaining effective Passes no longer satisfy every row of it. A Pass of another correction
    never counted; an independent Pass of the same row keeps the closure supported. A Pending
    Verification deficiency stays as it is.
    """
    return status == CLOSED_VERIFIED and chain_counted and not still_satisfied


def _closure_revision(deficiency_id):
    """The scope revision the deficiency's newest automatic closure satisfied, or None."""
    return (
        DeficiencyEvent.objects.filter(
            deficiency_id=deficiency_id, event_type="VERIFICATION_CLOSED"
        )
        .order_by("-sequence")
        .values_list("verification_check__verification_scope_revision", flat=True)
        .first()
    )


def _verification_reopen(organization_id, root, status, kind):
    """The VERIFICATION_REOPENED effect for the deficiency root verifies, or None (INV-4)."""
    deficiency_id = str(root.verification_for_deficiency_id)
    if status != CLOSED_VERIFIED:
        return None
    revision = _closure_revision(deficiency_id)
    if revision is None:
        # Closed Verified is reached only by VERIFICATION_CLOSED: its absence is corrupt state.
        raise DeficiencyError(
            "CONFIGURATION_UNAVAILABLE", "deficiency closure is unavailable", status=503
        )
    rows = list(
        VerificationScopeRow.objects.filter(
            organization_id=organization_id,
            deficiency_id=deficiency_id,
            scope_revision=revision,
        )[: MAX_SCOPE_ROWS + 1]
    )
    if not rows or len(rows) > MAX_SCOPE_ROWS:
        raise DeficiencyError(
            "CONFIGURATION_UNAVAILABLE", "verification scope is unavailable", status=503
        )
    counted = root.verification_scope_revision in scope_lineage(
        organization_id, deficiency_id, rows
    )
    still_satisfied = True
    if counted:
        satisfied = satisfied_scope_rows(
            organization_id, deficiency_id, rows, excluding_root_id=root.pk
        )
        still_satisfied = len(satisfied) == len(rows)
    if not decide_verification_reopen(status, counted, still_satisfied):
        return None
    return Effect(
        deficiency_id,
        new_id(),
        "VERIFICATION_REOPENED",
        CLOSED_VERIFIED,
        OPEN,
        VERIFICATION_REOPEN_TEXT[kind],
        verification_check_id=str(root.pk),
    )


def plan_invalidation_effects(organization_id, root):
    """Lock the chain's deficiencies once and decide what invalidating the chain does to each.

    INV-7: a deficiency this chain cancelled, whose last event is still that cancellation, reopens.
    INV-4: the deficiency a verification chain verified reopens when its closure loses support.
    Anything else, including a deficiency closed independently, is left exactly as it is (INV-3).
    """
    target = root.verification_for_deficiency_id
    ids = supporting_deficiency_ids(organization_id, root.pk, target)
    projections = lock_deficiencies(organization_id, ids)
    plans = []
    for deficiency_id in ids:
        status = projections[deficiency_id].status
        if target is not None and deficiency_id == str(target):
            effect = _verification_reopen(organization_id, root, status, "INVALIDATION")
            if effect is not None:
                plans.append(effect)
                continue
        if status != REPORTED_IN_ERROR:
            continue
        last = (
            DeficiencyEvent.objects.filter(deficiency_id=deficiency_id)
            .order_by("-sequence")
            .first()
        )
        # Reopen only a deficiency whose very last event is this chain's cancellation.
        if (
            last is None
            or last.event_type != "REPORTED_IN_ERROR"
            or last.correction_ref != str(root.pk)
        ):
            continue
        plans.append(
            Effect(deficiency_id, new_id(), "REOPENED", REPORTED_IN_ERROR, "OPEN", REOPEN_TEXT)
        )
    return plans


def plan_verification_correction_effects(organization_id, root):
    """Lock the chain's deficiencies once; reopen the verified one if the lost Pass closed it.

    For an author's correction of a verification Pass to Failed or Unable to Inspect: the same
    scope effect as invalidation (INV-4, VER-4 "superseded to a non-Pass"), and it never closes.
    """
    target = root.verification_for_deficiency_id
    ids = supporting_deficiency_ids(organization_id, root.pk, target)
    projections = lock_deficiencies(organization_id, ids)
    effect = _verification_reopen(
        organization_id, root, projections[str(target)].status, "CORRECTION"
    )
    return [] if effect is None else [effect]


def append_effect(receipt, current, *, organization_id, effect, root_id):
    """Append the planned event after the deficiency's last one and move its projection."""
    last = (
        DeficiencyEvent.objects.filter(deficiency_id=effect.deficiency_id)
        .order_by("-sequence")
        .first()
    )
    if last is None or last.to_status != effect.from_status:
        raise DeficiencyError(
            "CONFIGURATION_UNAVAILABLE", "deficiency state changed under its lock", status=503
        )
    if effect.from_status != effect.to_status:
        try:
            validate_transition(effect.from_status, effect.to_status)
        except TransitionRejected as error:
            raise DeficiencyError("CONFIGURATION_UNAVAILABLE", str(error), status=503) from None
    event = DeficiencyEvent.objects.create(
        id=effect.event_id,
        organization_id=organization_id,
        deficiency_id=effect.deficiency_id,
        sequence=last.sequence + 1,
        predecessor_event=last,
        event_type=effect.event_type,
        from_status=effect.from_status,
        to_status=effect.to_status,
        explanation=effect.explanation,
        # A verification reopening names its chain by foreign key; the others by correction_ref.
        correction_ref=None if effect.verification_check_id else str(root_id),
        verification_check_id=effect.verification_check_id,
        # Cancelling ends the suspensions in force (SUS-4); the deficiency lock is held since prepare.
        suspensions_ended=(
            suspension_ids_in_force(organization_id, effect.deficiency_id)
            if effect.event_type == "REPORTED_IN_ERROR"
            else []
        ),
        **stamp(receipt, current),
    )
    end_suspensions(receipt, current, organization_id=organization_id, event=event)
    DeficiencyProjection.objects.filter(
        organization_id=organization_id, deficiency_id=effect.deficiency_id
    ).update(status=effect.to_status, last_applied_event=event)
    return event
