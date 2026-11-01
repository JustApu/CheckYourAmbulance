"""Supervisor suspension of routine checks for a known deficiency (SPEC 8.9, AC-18, issue 13
(https://github.com/JustApu/CheckYourAmbulance/issues/13)).

suspend_checks lets a Supervisor or Administrator (the SPEC 3.3 suspend_routine_checks permission)
name exactly which items, and physical kit instances for kit-defined items, of the vehicle's
current checklist stop needing their routine check while the deficiency is being worked (SUS-1). It
appends CHECKS_SUSPENDED (status unchanged) and one check_suspension START per item. A suspension
is not a Pass, leaves every other check required and never changes operational status (SUS-2): the
obligation reader shows the item's routine obligations as Suspended instead.

Nothing here ends a suspension. The event that closes or cancels the deficiency (verified closure,
administrator or empty-scope closure, Reported in Error) ends every suspension in force and lists it
in suspensions_ended (SUS-3, SUS-4; service.end_suspensions). Reopening resumes none: a new
suspension needs this command again.

Locks follow the correction and expansion commands (vehicle, checklist templates, deficiency), and
expected_event_id refuses a decision made on a state that has since changed (409), so a suspension
and a concurrent closure serialize and no suspension outlives its open deficiency.
"""

from django.utils import timezone

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id
from cya.authorization.persistence import digest
from cya.core.audit_commands import Plan, run_command
from cya.deficiencies import service
from cya.deficiencies.commands import _authorizer, _operation_id, publish_changed
from cya.deficiencies.effects import NO_KIT_KEY
from cya.deficiencies.expansion import _lock, parse_items, resolve_items
from cya.deficiencies.models import CheckSuspension, DeficiencyEvent, DeficiencyProjection
from cya.deficiencies.service import DeficiencyError
from cya.operations.models import MAX_TEXT

SUSPEND_ACTION = "suspend_routine_checks"
SUSPEND_COMMAND = "DEFICIENCY_CHECKS_SUSPEND"
MAX_SUSPENSION_ROWS = 5000  # START and END rows one history read will return


def _not_suspended_yet(in_force, resolved):
    """Refuse an item and kit this deficiency already suspends; the refusal names the entry."""
    held = {(str(row.item_id), row.instance_key) for row in in_force}
    for index, (item_id, _definition, kit_id) in enumerate(resolved):
        if (item_id, kit_id or NO_KIT_KEY) in held:
            raise DeficiencyError(
                "ALREADY_SUSPENDED",
                "this deficiency already suspends the check of that item and kit",
                path=f"items[{index}]",
            )
    if len(in_force) + len(resolved) > service.MAX_SUSPENSIONS_IN_FORCE:
        raise DeficiencyError(
            "TOO_MANY_SUSPENSIONS",
            f"a deficiency may suspend at most {service.MAX_SUSPENSIONS_IN_FORCE} checks at once",
            path="items",
        )


def _start_rows(resolved, *, organization_id, deficiency_id, vehicle_id, event, ids, stamp):
    rows = [
        CheckSuspension(
            id=suspension_id,
            organization_id=organization_id,
            deficiency_id=deficiency_id,
            vehicle_id=vehicle_id,
            item_id=item_id,
            definition_id=definition_id,
            kit_instance_id=kit_id,
            instance_key=kit_id or NO_KIT_KEY,
            action="START",
            deficiency_event=event,
            **stamp,
        )
        for suspension_id, (item_id, definition_id, kit_id) in zip(ids, resolved, strict=True)
    ]
    CheckSuspension.objects.bulk_create(rows)
    return rows


def suspend_checks(
    *,
    deficiency_id,
    expected_event_id,
    items,
    organization_id,
    actor_id,
    operation_id,
    correlation_id,
    request_attempt_id,
    explanation=None,
):
    """Suspend the routine checks of the named items for this deficiency (SUS-1, SUS-2).

    items is 1 to 50 distinct {item_id, kit_instance_id?} from the vehicle's current checklist;
    explanation is optional (at most 4,000 characters, not blank). The deficiency must still be
    worked (422 DEFICIENCY_NOT_ACTIVE) and must not already suspend one of the items
    (422 ALREADY_SUSPENDED). A replay with the same operation_id returns the stored answer.
    """
    for value in (organization_id, actor_id):
        canonical_id(value)
    _operation_id(operation_id)
    deficiency_id = service.canonical(deficiency_id, "deficiency_id")
    expected_event_id = service.canonical(expected_event_id, "expected_event_id")
    if explanation is not None:
        explanation = service.text(explanation, "explanation", MAX_TEXT)
    items = parse_items(items)

    def build(actor):
        vehicle_id, projection, last = _lock(
            actor, organization_id, actor_id, deficiency_id, expected_event_id, SUSPEND_ACTION
        )
        service.require_active(projection)
        now = timezone.now()
        resolved = resolve_items(organization_id, vehicle_id, items, now)
        _not_suspended_yet(service.suspensions_in_force(organization_id, deficiency_id), resolved)
        event_id = new_id()
        suspension_ids = [new_id() for _ in resolved]

        def apply(receipt, current):
            stamp = service.stamp(receipt, current)
            event = DeficiencyEvent.objects.create(
                id=event_id,
                organization_id=organization_id,
                deficiency_id=deficiency_id,
                sequence=last.sequence + 1,
                predecessor_event=last,
                event_type="CHECKS_SUSPENDED",
                from_status=projection.status,
                to_status=projection.status,
                explanation=explanation,
                suspensions_ended=[],
                **stamp,
            )
            _start_rows(
                resolved,
                organization_id=organization_id,
                deficiency_id=deficiency_id,
                vehicle_id=vehicle_id,
                event=event,
                ids=suspension_ids,
                stamp=stamp,
            )
            DeficiencyProjection.objects.filter(
                organization_id=organization_id, deficiency_id=deficiency_id
            ).update(last_applied_event=event)
            publish_changed(organization_id, deficiency_id, event_id)

        return Plan(
            response_body={
                "code": "DEFICIENCY_CHECKS_SUSPENDED",
                "deficiency_id": deficiency_id,
                "event_id": event_id,
                "status": projection.status,
                "items": [
                    {"item_id": item_id, "kit_instance_id": kit_id}
                    for item_id, _definition, kit_id in resolved
                ],
                "suspension_ids": suspension_ids,
            },
            apply=apply,
            response_status=201,
            target_type="ORGANIZATION",
            target_id=organization_id,
            prior_snapshot={"schema_version": 1, "values": {"status": projection.status}},
            new_snapshot={
                "schema_version": 1,
                "values": {
                    "event_id": event_id,
                    "event_type": "CHECKS_SUSPENDED",
                    "status": projection.status,
                    "suspension_ids": sorted(suspension_ids),
                    "item_ids": sorted({item_id for item_id, _d, _k in resolved}),
                },
            },
            recorded_at=now,
        )

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type=SUSPEND_COMMAND,
        request_digest=digest(
            {
                "deficiency_id": deficiency_id,
                "expected_event_id": expected_event_id,
                "explanation": explanation,
                "items": sorted([item_id, kit or ""] for item_id, kit in items),
            }
        ),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=_authorizer(organization_id, actor_id, SUSPEND_ACTION),
        prepare=build,
    )


def suspension_state(organization_id, deficiency_id, as_of=None):
    """The deficiency's check suspensions, now or as of an instant (AC-26, AC-27).

    Each START is listed with its item and kit, who started it and when, and, once ended by then,
    the closing event that ended it. in_force lists the ids not ended at that moment. Replayed from
    the append-only rows and events; a later event never changes an earlier answer. Bounded: past
    the cap the answer is refused (503), never truncated. last_event_id is what a lifecycle command
    names as expected_event_id.
    """
    newest, starts = service._events(
        organization_id,
        deficiency_id,
        as_of,
        service.MAX_ASSIGNMENT_EVENTS,
        types=("CHECKS_SUSPENDED",),
    )
    explanations = {str(event.pk): event.explanation for event in starts}
    rows = CheckSuspension.objects.filter(
        organization_id=organization_id, deficiency_id=deficiency_id
    )
    if as_of is not None:
        rows = rows.filter(recorded_at__lte=as_of)
    rows = list(
        rows.select_related("deficiency_event").order_by("recorded_at", "id")[
            : MAX_SUSPENSION_ROWS + 1
        ]
    )
    if len(rows) > MAX_SUSPENSION_ROWS:
        raise DeficiencyError(
            "CONFIGURATION_UNAVAILABLE", "the history is too long to return", status=503
        )
    ends = {str(row.prior_suspension_id): row for row in rows if row.action == "END"}
    suspensions = []
    for row in rows:
        if row.action != "START":
            continue
        end = ends.get(str(row.pk))
        suspensions.append(
            {
                "suspension_id": str(row.pk),
                "item_id": str(row.item_id),
                "kit_instance_id": None
                if row.kit_instance_id is None
                else str(row.kit_instance_id),
                "started_at": row.recorded_at,
                "started_by": str(row.actor_user_id),
                "start_event_id": str(row.deficiency_event_id),
                "explanation": explanations.get(str(row.deficiency_event_id)),
                "ended_at": None if end is None else end.recorded_at,
                "end_event_id": None if end is None else str(end.deficiency_event_id),
                "end_event_type": None if end is None else end.deficiency_event.event_type,
            }
        )
    return {
        "deficiency_status": newest.to_status,
        "last_event_id": str(newest.pk),
        "in_force": [s["suspension_id"] for s in suspensions if s["ended_at"] is None],
        "suspensions": suspensions,
    }
