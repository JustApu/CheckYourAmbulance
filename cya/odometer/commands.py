"""Audited odometer commands (SPEC 6.4, ODO-1..ODO-4; docs/odometer.md).

record_odometer is the server boundary for POST /api/v1/odometer-entries and
set_odometer_threshold the administrator's way to change the plausibility threshold. Both run
through cya.core.audit_commands.run_command, which owns the outermost transaction, the
operation-id receipt (a replay returns the original result and writes nothing) and the
success/failure audit. Do not wrap them in a transaction.

Shape errors are raised before any transaction. Everything that needs the database is checked
in prepare, after authorization, so a rejected write rolls back whole: no entry, no receipt,
and the operation id stays reusable.

Locking: record_odometer holds the vehicle row (select_for_update), the same row obligation
generation and session commands take first. Two entries for one vehicle therefore serialize,
which keeps "the previous reading" and "one entry per obligation" exact.
"""

import re
from datetime import UTC, timedelta
from decimal import Decimal

from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from cya.accounts.fields import new_id
from cya.accounts.models import Organization
from cya.authorization.backend import canonical_id, load_state
from cya.authorization.guard import PermissionDenied, Principal, require_permission
from cya.authorization.locking import locked_identities
from cya.authorization.persistence import DatabaseRoleBackend, digest
from cya.authorization.policy import Role
from cya.checks.commands import (
    CheckError,
    _explanation,
    _id,
    _operation_id,
    _optional_id,
    _paper_action,
    _paper_batch,
    _vehicle,
)
from cya.core.audit_commands import Actor, CommandDenied, Plan, run_command
from cya.obligations.queries import odometer_obligations
from cya.odometer.models import (
    DEFAULT_MAX_INCREASE,
    UNITS,
    EngineHoursEntry,
    OdometerEntry,
    OdometerObligationLink,
    OdometerThreshold,
    ReadingEvent,
)

# Odometer readings are recorded like a check (an item of kind VEHICLE_READING, SPEC 4.4) and any
# crew member may satisfy the obligation (ODO-2), so the record_check permission applies; the
# threshold is "administrator configurable" (SPEC 3.3 row Configure schedules, thresholds).
RECORD_ACTION = "record_check"
CONFIGURE_ACTION = "configure_schedules_thresholds_notifications"
RECORD_COMMAND = "ODOMETER_RECORD"
THRESHOLD_COMMAND = "ODOMETER_THRESHOLD_SET"
LOOKBACK = timedelta(days=31)  # due instants read for a reading; late ones then need w_late
LOOKAHEAD = timedelta(hours=48)  # due instants whose early window may already have opened
READING = re.compile(r"\d{1,7}(\.\d)?")


def _malformed(path, message):
    return CheckError("MALFORMED_REQUEST", message, status=400, path=path)


def _reading(value, path):
    """A whole or one-decimal number below ten million, from a decimal string or an integer."""
    bad = CheckError(
        "INVALID_OBSERVATION",
        f"{path} must be a number from 0 with at most one decimal place",
        path=path,
    )
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or READING.fullmatch(value) is None:
        raise bad
    return Decimal(value)


def _shape(*, vehicle_id, value, unit, engine_hours, confirm_anomaly, explanation, session_id):
    """Validate everything that needs no database; returns the normalized fields."""
    if unit not in UNITS:
        raise CheckError("INVALID_OBSERVATION", f"unit must be one of {list(UNITS)}", path="unit")
    if type(confirm_anomaly) is not bool:
        raise _malformed("confirm_anomaly", "confirm_anomaly must be true or false")
    if session_id is not None:
        raise CheckError(
            "NOT_YET_SUPPORTED",
            "session_id is part of the contract but is not available yet",
            path="session_id",
        )
    return {
        "vehicle_id": _id(vehicle_id, "vehicle_id"),
        "value": _reading(value, "value"),
        "unit": unit,
        "engine_hours": None if engine_hours is None else _reading(engine_hours, "engine_hours"),
        "confirm": confirm_anomaly,
        "explanation": _explanation(explanation),
    }


def _authorize(organization_id, actor_id, action, paper_batch_id=None):
    backend = DatabaseRoleBackend()

    def authorize(_org):
        with locked_identities(organization_id, [actor_id], lock_organization=False) as (
            current_org,
            users,
        ):
            user = users[actor_id]
            roles = frozenset(
                Role.CREW if r == Role.EMT else r for r in load_state(backend, user).grants
            )
            actor = Actor(user=user, roles=tuple(sorted(r.value for r in roles)))
            principal = Principal(
                organization_id, actor_id, roles, user.active and current_org.active
            )
            actions = [action]
            if paper_batch_id is not None:
                # SPEC 3.3 paper-entry roles, checked again for every paper record (LATE-3).
                actions.append(
                    _paper_action(
                        organization_id,
                        actor_id,
                        paper_batch_id,
                        roles,
                        user.active and current_org.active,
                    )
                )
            try:
                for each in actions:
                    require_permission(principal, organization_id, each)
            except PermissionDenied:
                raise CommandDenied(actor) from None
            return actor

    return authorize


def sequence_key(root):
    """A root reading's place in its vehicle's sequence (SPEC 8.0.0 ODO-6, LATE-5).

    (qualifying instant, recorded instant, id): a paper reading sits at its performance instant,
    a live one at its recorded instant, as before.
    """
    return (root.performed_at or root.recorded_at, root.recorded_at, str(root.pk))


def _effective_tips(model, organization_id, vehicle_id):
    root_field = "root_odometer_id" if model is OdometerEntry else "root_hours_id"
    invalidated = ReadingEvent.objects.filter(
        organization_id=organization_id,
        event_type="INVALIDATION",
        **{f"{root_field}__isnull": False},
    ).values(root_field)
    superseded = model.objects.filter(
        organization_id=organization_id, predecessor_entry__isnull=False
    ).values("predecessor_entry_id")
    return (
        model.objects.filter(organization_id=organization_id, vehicle_id=vehicle_id)
        .exclude(root_entry_id__in=invalidated)
        .exclude(pk__in=superseded)
        .annotate(seq_at=Coalesce("root_entry__performed_at", "root_entry__recorded_at"))
    )


def _relative(key, before):
    at, recorded, pk = key
    if before:
        return (
            Q(seq_at__lt=at)
            | Q(seq_at=at, root_entry__recorded_at__lt=recorded)
            | Q(seq_at=at, root_entry__recorded_at=recorded, root_entry_id__lt=pk)
        )
    return (
        Q(seq_at__gt=at)
        | Q(seq_at=at, root_entry__recorded_at__gt=recorded)
        | Q(seq_at=at, root_entry__recorded_at=recorded, root_entry_id__gt=pk)
    )


def previous_reading(model, organization_id, vehicle_id, *, before_root=None, before=None):
    """The newest effective reading in sequence order, optionally before one root or key.

    Corrections are recorded later than the reading they replace, but do not move that reading's
    place in the odometer sequence: order by the root's sequence_key, not the current chain
    tip's timestamp.
    """
    rows = _effective_tips(model, organization_id, vehicle_id)
    key = sequence_key(before_root) if before_root is not None else before
    if key is not None:
        rows = rows.filter(_relative(key, before=True))
    return rows.order_by("-seq_at", "-root_entry__recorded_at", "-root_entry_id").first()


def next_reading(model, organization_id, vehicle_id, *, after_root=None, after=None):
    """The oldest effective reading in sequence order strictly after one root or key."""
    key = sequence_key(after_root) if after_root is not None else after
    rows = _effective_tips(model, organization_id, vehicle_id).filter(_relative(key, before=False))
    return rows.order_by("seq_at", "root_entry__recorded_at", "root_entry_id").first()


def threshold_for(organization_id, unit):
    """The latest administrator-set revision for the unit, else the default (ODO-3, OQ-55)."""
    latest = (
        OdometerThreshold.objects.filter(organization_id=organization_id, unit=unit)
        .order_by("-revision")
        .first()
    )
    return latest.max_increase if latest else Decimal(DEFAULT_MAX_INCREASE[unit])


def anomaly(value, prior, limit):
    """LOWER, JUMP or None for a reading against the previous one (limit None: no jump rule)."""
    if prior is None:
        return None
    if value < prior.value:
        return "LOWER"
    if limit is not None and value - prior.value > limit:
        return "JUMP"
    return None


def _obligation_to_satisfy(organization_id, vehicle_id, now, at=None):
    """The unsatisfied odometer obligation this reading satisfies, or None (ODO-2).

    The earliest-due one whose window contains the instant (SCH-6). Only when none does, the
    earliest-due one past grace but not past its pinned late limit L (a late completion, SCH-2);
    one further past stays missed. An obligation whose early window has not opened is never
    eligible. `at` is the qualifying instant (a paper reading's performance instant; SPEC 8.0.0
    5.3, ODO-6); what is known, and so what is already satisfied, is read at `now`.
    """
    at = now if at is None else at
    candidates = [
        o
        for o in odometer_obligations(
            organization_id, at - LOOKBACK, at + LOOKAHEAD, vehicle_id=vehicle_id, as_of=now
        )
        if o.early_at <= at
    ]
    invalidated = ReadingEvent.objects.filter(
        organization_id=organization_id,
        event_type="INVALIDATION",
        root_odometer__isnull=False,
    ).values("root_odometer_id")
    satisfied = set(
        OdometerObligationLink.objects.filter(
            organization_id=organization_id, obligation_id__in=[o.pk for o in candidates]
        )
        .exclude(entry__root_entry_id__in=invalidated)
        .values_list("obligation_id", flat=True)
    )
    open_ = [o for o in candidates if o.pk not in satisfied]
    on_time = [o for o in open_ if at <= o.grace_at]
    # SCH-2: up to the obligation's pinned late limit L, inclusive (issue #80).
    late = [o for o in open_ if o.grace_at < at <= o.late_limit_at]
    chosen = on_time or late
    return chosen[0] if chosen else None


ANOMALY_TEXT = {
    "LOWER": "lower than the previous reading",
    "JUMP": "much higher than the previous reading",
    # SPEC 8.0.0 ODO-6: a paper reading placed before later readings is compared with both.
    "HIGHER_THAN_NEXT": "higher than the next reading",
    "FAR_BELOW_NEXT": "much lower than the next reading",
}


def _confirmation(kind, shape, path):
    """The explanation and flag to store for one reading; refuse an unconfirmed anomaly."""
    if kind is None:
        return False
    if not shape["confirm"]:
        raise CheckError(
            "ANOMALY_CONFIRMATION_REQUIRED",
            f"the {path} is {ANOMALY_TEXT[kind]}; correct it or confirm it",
            status=409,
            path=path,
        )
    return True


def record_odometer(
    *,
    vehicle_id,
    value,
    unit,
    engine_hours=None,
    confirm_anomaly=False,
    explanation=None,
    session_id=None,
    paper_entry_batch_id=None,
    organization_id,
    actor_id,
    operation_id,
    correlation_id,
    request_attempt_id,
):
    """Record one attributed odometer reading (ODO-1), optionally with engine hours (ODO-4).

    paper_entry_batch_id makes it a paper reading (SPEC 8.0.0 ODO-6): placed in the vehicle's
    sequence, and judged for its obligation, at the batch's performance instant, and compared
    with the readings on both sides of it.

    Idempotent on operation_id. The actor is the authenticated user; nothing here accepts a
    client-supplied actor or roles. A reading lower than the previous valid one, or above it by
    more than the plausibility threshold, is refused (409 ANOMALY_CONFIRMATION_REQUIRED) unless
    confirm_anomaly is true; the confirmation is stored with the actor who made it (ODO-3).
    """
    for item in (organization_id, actor_id):
        canonical_id(item)
    _operation_id(operation_id)
    shape = _shape(
        vehicle_id=vehicle_id,
        value=value,
        unit=unit,
        engine_hours=engine_hours,
        confirm_anomaly=confirm_anomaly,
        explanation=explanation,
        session_id=session_id,
    )
    request = {
        "vehicle_id": vehicle_id,
        "value": value,
        "unit": unit,
        "engine_hours": engine_hours,
        "confirm_anomaly": confirm_anomaly,
        "explanation": explanation,
        "session_id": session_id,
    }
    batch_id = _optional_id(paper_entry_batch_id, "paper_entry_batch_id")
    if batch_id is not None:
        request["paper_entry_batch_id"] = batch_id  # only when given: live digests unchanged

    def build(actor):
        _vehicle(organization_id, shape["vehicle_id"], lock=True)
        paper = (
            _paper_batch(
                organization_id,
                actor,
                {"paper_entry_batch_id": batch_id, "vehicle_id": shape["vehicle_id"]},
            )
            if batch_id is not None
            else None
        )
        now = timezone.now()  # read under the lock, so it never precedes a concurrent entry
        entry_id = new_id()
        hours_id = new_id() if shape["engine_hours"] is not None else None
        at = paper.performed_at if paper else None
        prior, following = _neighbours(OdometerEntry, organization_id, shape, at, now, entry_id)
        for neighbour in (prior, following):
            if neighbour is not None and neighbour.unit != shape["unit"]:
                raise CheckError(
                    "UNIT_MISMATCH",
                    f"this vehicle's readings are in {neighbour.unit}",
                    path="unit",
                )
        limit = threshold_for(organization_id, shape["unit"])
        kind = anomaly(shape["value"], prior, limit) or _against_next(
            shape["value"], following, limit
        )
        confirmed = _confirmation(kind, shape, "value")
        prior_hours = None
        hours_confirmed = False
        if shape["engine_hours"] is not None:
            prior_hours, next_hours = _neighbours(
                EngineHoursEntry, organization_id, shape, at, now, hours_id
            )
            hours_kind = anomaly(shape["engine_hours"], prior_hours, None) or _against_next(
                shape["engine_hours"], next_hours, None
            )
            hours_confirmed = _confirmation(hours_kind, shape, "engine_hours")
        if shape["explanation"] is not None and not (confirmed or hours_confirmed):
            raise CheckError(
                "INVALID_CONFIRMATION",
                "an explanation is accepted only when confirming an anomalous reading",
                path="explanation",
            )
        obligation = _obligation_to_satisfy(organization_id, shape["vehicle_id"], now, at)
        link_id = new_id() if obligation is not None else None
        explanation_text = shape["explanation"]

        def apply(receipt, current):
            common = {
                "organization_id": organization_id,
                "command": receipt,
                "recorded_at": receipt.recorded_at,
                "actor_user": current.user,
                "actor_roles_at_time": list(current.roles),
                "operation_id": operation_id,
                "vehicle_id": shape["vehicle_id"],
                "paper_entry_batch_id": paper.batch_id if paper else None,
                "performed_at": at,
            }
            OdometerEntry.objects.create(
                id=entry_id,
                root_entry_id=entry_id,
                prior_entry=prior,
                value=shape["value"],
                unit=shape["unit"],
                anomaly_confirmed=confirmed,
                confirmation_explanation=explanation_text if confirmed else None,
                **common,
            )
            if hours_id is not None:
                EngineHoursEntry.objects.create(
                    id=hours_id,
                    root_entry_id=hours_id,
                    prior_entry=prior_hours,
                    value=shape["engine_hours"],
                    unit="h",
                    anomaly_confirmed=hours_confirmed,
                    confirmation_explanation=(
                        explanation_text if hours_confirmed and not confirmed else None
                    ),
                    **common,
                )
            if link_id is not None:
                OdometerObligationLink.objects.create(
                    id=link_id,
                    organization_id=organization_id,
                    command=receipt,
                    recorded_at=receipt.recorded_at,
                    actor_user=current.user,
                    actor_roles_at_time=list(current.roles),
                    obligation=obligation,
                    entry_id=entry_id,
                )

            from cya.live_updates.publish import publish_vehicle_update

            publish_vehicle_update(
                organization_id=organization_id,
                vehicle_id=shape["vehicle_id"],
                event_id=entry_id,
                kind="odometer.recorded",
            )

        # No explanation text in the audit snapshot: it is free text from the crew member.
        snapshot = {
            "schema_version": 1,
            "values": {
                "entry_id": entry_id,
                "vehicle_id": shape["vehicle_id"],
                "value": str(shape["value"]),
                "unit": shape["unit"],
                "anomaly_confirmed": confirmed,
                "engine_hours_entry_id": hours_id,
                "obligation_id": str(obligation.pk) if obligation else None,
            },
        }
        body = {
            "code": "ODOMETER_RECORDED",
            "entry_id": entry_id,
            "engine_hours_entry_id": hours_id,
            "anomaly_confirmed": confirmed or hours_confirmed,
            "obligation_links": [str(obligation.pk)] if obligation else [],
        }
        if paper is not None:
            # Only a paper reading reports its source, so a live response is unchanged.
            body["paper_entry"] = {
                "batch_id": paper.batch_id,
                "performed_at": at.astimezone(UTC).isoformat(),
            }
            snapshot["values"]["paper_entry_batch_id"] = paper.batch_id
        return Plan(
            response_body=body,
            apply=apply,
            response_status=201,
            target_type="ORGANIZATION",
            target_id=organization_id,
            new_snapshot=snapshot,
            # The receipt, the entry and its link carry the instant eligibility was decided at,
            # so a window opening between the decision and the write cannot split them.
            recorded_at=now,
        )

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type=RECORD_COMMAND,
        request_digest=digest(request),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=_authorize(organization_id, actor_id, RECORD_ACTION, batch_id),
        prepare=build,
    )


def _neighbours(model, organization_id, shape, at, now, new_root_id):
    """(previous, next) effective readings around a new reading's place in the sequence.

    A live reading's place is now, after every reading, so it has no next one and its previous
    one is the vehicle's latest, as before. A paper reading's place is its performance instant.
    """
    if at is None:
        return previous_reading(model, organization_id, shape["vehicle_id"]), None
    key = (at, now, new_root_id)
    return (
        previous_reading(model, organization_id, shape["vehicle_id"], before=key),
        next_reading(model, organization_id, shape["vehicle_id"], after=key),
    )


def _against_next(value, following, limit):
    """ODO-6: a paper reading is anomalous above the next reading, or far below it (odometer)."""
    if following is None:
        return None
    if value > following.value:
        return "HIGHER_THAN_NEXT"
    if limit is not None and following.value - value > limit:
        return "FAR_BELOW_NEXT"
    return None


def set_odometer_threshold(
    *,
    unit,
    max_increase,
    organization_id,
    actor_id,
    operation_id,
    correlation_id,
    request_attempt_id,
):
    """Append the next threshold revision for a unit (administrators only; ODO-3, AUD-4)."""
    for item in (organization_id, actor_id):
        canonical_id(item)
    _operation_id(operation_id)
    if unit not in UNITS:
        raise CheckError("INVALID_OBSERVATION", f"unit must be one of {list(UNITS)}", path="unit")
    limit = _reading(max_increase, "max_increase")
    if limit <= 0:
        raise CheckError(
            "INVALID_OBSERVATION", "max_increase must be greater than zero", path="max_increase"
        )

    def build(actor):
        # The organization row serializes administrators, so revisions count up without gaps.
        Organization.objects.select_for_update().get(pk=organization_id)
        latest = (
            OdometerThreshold.objects.filter(organization_id=organization_id, unit=unit)
            .order_by("-revision")
            .first()
        )
        revision = latest.revision + 1 if latest else 1
        threshold_id = new_id()

        def apply(receipt, current):
            OdometerThreshold.objects.create(
                id=threshold_id,
                organization_id=organization_id,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current.user,
                actor_roles_at_time=list(current.roles),
                operation_id=operation_id,
                unit=unit,
                revision=revision,
                max_increase=limit,
            )

        snapshot = {
            "schema_version": 1,
            "values": {
                "threshold_id": threshold_id,
                "unit": unit,
                "revision": revision,
                "max_increase": str(limit),
            },
        }
        return Plan(
            response_body={
                "code": "ODOMETER_THRESHOLD_SET",
                "threshold_id": threshold_id,
                "unit": unit,
                "revision": revision,
            },
            apply=apply,
            response_status=201,
            target_type="ORGANIZATION",
            target_id=organization_id,
            prior_snapshot={"schema_version": 1, "values": {"revision": revision - 1}},
            new_snapshot=snapshot,
        )

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type=THRESHOLD_COMMAND,
        request_digest=digest({"unit": unit, "max_increase": max_increase}),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=_authorize(organization_id, actor_id, CONFIGURE_ACTION),
        prepare=build,
    )
