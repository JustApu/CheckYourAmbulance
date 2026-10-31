"""Supervisor scope expansion and added verification after closure (issue 156 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/156)).

SPEC VER-3, VER-4 and 8.7, amendment 1.22.0. Supervisors and Administrators (the SPEC 3.3
expand_verification permission) name items, and physical kit instances for kit-defined items, from
the vehicle's current checklist:

- expand_scope, while the deficiency is Pending Verification, appends SCOPE_EXPANDED and a new scope
  revision of the same correction that repeats every row of the newest revision with its own
  instant and adds the named rows with the expansion instant. A Pass that satisfied a repeated row
  still does (effects.satisfied_scope_rows), and an added row needs a Pass recorded after now, so
  the deficiency cannot close until the added rows are verified too.
- add_verification, while it is Closed Verified, appends VERIFICATION_ADDED, which reopens it to Open
  with a new resolution deadline (the former stays in history) and keeps the assignee.

Both keep the named items as deficiency_scope_expansion rows, which every later correction's scope
includes (service.correction_scope). Locks follow the correction command: vehicle, checklist
templates, then the deficiency, so expansion and a concurrent last verification Pass serialize, and
expected_event_id refuses a decision made on a state that has since changed (409). Notification of
the current assignee is issue 14 (https://github.com/JustApu/CheckYourAmbulance/issues/14).
"""

from django.db.models import Max
from django.utils import timezone

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id
from cya.authorization.persistence import digest
from cya.checklists.models import Vehicle
from cya.core.audit_commands import Plan, run_command
from cya.deficiencies import service
from cya.deficiencies.commands import _authorizer, _lock_for_change, _operation_id, publish_changed
from cya.deficiencies.effects import MAX_SCOPE_ROWS, NO_KIT_KEY
from cya.deficiencies.models import (
    Deficiency,
    DeficiencyEvent,
    DeficiencyProjection,
    DeficiencyScopeExpansion,
    VerificationScopeRow,
)
from cya.deficiencies.service import DeficiencyError
from cya.deficiencies.transitions import (
    CLOSED_VERIFIED,
    OPEN,
    PENDING_VERIFICATION,
    TransitionRejected,
    validate_transition,
)
from cya.operations.models import MAX_TEXT

EXPAND_ACTION = "expand_verification"
EXPAND_COMMAND = "DEFICIENCY_SCOPE_EXPAND"
ADD_COMMAND = "DEFICIENCY_VERIFICATION_ADD"
MAX_ITEMS = 50  # items one expansion may name


def parse_items(value):
    """[(item_id, kit_instance_id or None)] from 1 to MAX_ITEMS distinct {item_id, kit_instance_id?}."""
    if not isinstance(value, list) or not 0 < len(value) <= MAX_ITEMS:
        raise service.malformed("items", f"items is a list of 1 to {MAX_ITEMS} entries")
    parsed = []
    for index, entry in enumerate(value):
        path = f"items[{index}]"
        if not isinstance(entry, dict) or not {"item_id"} <= set(entry) <= {
            "item_id",
            "kit_instance_id",
        }:
            raise service.malformed(path, "an entry is {item_id, kit_instance_id?}")
        item_id = service.canonical(entry["item_id"], f"{path}.item_id")
        kit = entry.get("kit_instance_id")
        kit = None if kit is None else service.canonical(kit, f"{path}.kit_instance_id")
        parsed.append((item_id, kit))
    if len(set(parsed)) != len(parsed):
        raise service.malformed("items", "items must not repeat")
    return parsed


def resolve_items(organization_id, vehicle_id, items, now):
    """(item, definition, kit or None) for each named item, checked against today's checklist.

    The item must be required by the vehicle's current checklists; a kit-defined item names one of
    its active physical kit instances on the vehicle, and any other item names none.
    """
    current = service._effective_items(organization_id, vehicle_id, now)
    resolved = []
    for index, (item_id, kit_id) in enumerate(items):
        entry = current.get(item_id)
        if entry is None:
            raise DeficiencyError(
                "ITEM_NOT_IN_CHECKLIST",
                "the item is not on the vehicle's current checklist",
                path=f"items[{index}].item_id",
            )
        kits = {str(k) for k, _label in entry.kits}
        if entry.requirement.kit_id and kit_id not in kits:
            raise DeficiencyError(
                "KIT_REQUIRED",
                "a kit item names one of its active kit instances on the vehicle",
                path=f"items[{index}].kit_instance_id",
            )
        if not entry.requirement.kit_id and kit_id is not None:
            raise DeficiencyError(
                "KIT_NOT_ALLOWED",
                "only a kit item names a kit instance",
                path=f"items[{index}].kit_instance_id",
            )
        resolved.append((item_id, str(entry.requirement.definition_id), kit_id))
    return resolved


def _lock(actor, organization_id, actor_id, deficiency_id, expected_event_id):
    """Vehicle, checklist templates, then the deficiency: the correction command's order."""
    vehicle_id = (
        Deficiency.objects.filter(organization_id=organization_id, pk=deficiency_id)
        .values_list("vehicle_id", flat=True)
        .first()
    )
    if vehicle_id is not None:
        vehicle = (
            Vehicle.objects.select_for_update()
            .filter(organization_id=organization_id, pk=vehicle_id)
            .first()
        )
        if vehicle is None:
            raise DeficiencyError(
                "CONFIGURATION_UNAVAILABLE", "deficiency vehicle is unavailable", status=503
            )
        service.lock_checklist_templates(organization_id)
    projection, last = _lock_for_change(
        actor, EXPAND_ACTION, organization_id, actor_id, deficiency_id, expected_event_id
    )
    return str(vehicle_id), projection, last


def _require(status, wanted, target, message):
    if status != wanted:
        raise DeficiencyError("INVALID_TRANSITION", message, path="deficiency")
    if target == status:
        return  # an expansion keeps the status: no arrow of the table is taken
    try:
        validate_transition(status, target)
    except TransitionRejected:
        raise DeficiencyError("INVALID_TRANSITION", "the transition is unavailable") from None


def _newest_rows(organization_id, deficiency_id):
    """The rows of the newest scope revision, bounded; none is corrupt for Pending Verification."""
    scope = VerificationScopeRow.objects.filter(
        organization_id=organization_id, deficiency_id=deficiency_id
    )
    revision = scope.aggregate(newest=Max("scope_revision"))["newest"]
    rows = list(scope.filter(scope_revision=revision)[: MAX_SCOPE_ROWS + 1]) if revision else []
    if not rows or len(rows) > MAX_SCOPE_ROWS:
        raise DeficiencyError(
            "CONFIGURATION_UNAVAILABLE", "verification scope is unavailable", status=503
        )
    return revision, rows


def _expanded_revision(organization_id, deficiency_id, resolved):
    """(next revision, newest rows to repeat), refusing an item already in the newest revision."""
    previous, carried = _newest_rows(organization_id, deficiency_id)
    present = {(str(r.item_id), r.instance_key) for r in carried}
    for index, (item_id, _definition, kit_id) in enumerate(resolved):
        if (item_id, kit_id or NO_KIT_KEY) in present:
            raise DeficiencyError(
                "ALREADY_IN_SCOPE",
                "the item and kit are already in the verification scope",
                path=f"items[{index}]",
            )
    if len(carried) + len(resolved) > MAX_SCOPE_ROWS:
        raise DeficiencyError(
            "SCOPE_TOO_LARGE", "the expanded scope would be too large", path="items"
        )
    return previous + 1, carried


def _expansion_rows(resolved, *, organization_id, deficiency_id, event, receipt, current):
    rows = [
        DeficiencyScopeExpansion(
            id=new_id(),
            organization_id=organization_id,
            deficiency_id=deficiency_id,
            event=event,
            item_id=item_id,
            definition_id=definition_id,
            kit_instance_id=kit_id,
            instance_key=kit_id or NO_KIT_KEY,
            **service.stamp(receipt, current),
        )
        for item_id, definition_id, kit_id in resolved
    ]
    DeficiencyScopeExpansion.objects.bulk_create(rows)
    return rows


def _append_revision(
    receipt, current, *, organization_id, deficiency_id, carried, resolved, scope_ids, revision
):
    """The newest revision's rows with their own instants, then the added rows (now)."""
    correction_event_id = carried[0].correction_event_id
    specs = [
        (str(r.item_id), str(r.definition_id), r.kit_instance_id, r.required_after) for r in carried
    ] + [
        (item_id, definition_id, kit_id, receipt.recorded_at)
        for item_id, definition_id, kit_id in resolved
    ]
    VerificationScopeRow.objects.bulk_create(
        VerificationScopeRow(
            id=scope_id,
            organization_id=organization_id,
            deficiency_id=deficiency_id,
            correction_event_id=correction_event_id,
            item_id=item_id,
            definition_id=definition_id,
            kit_instance_id=kit_id,
            instance_key=str(kit_id) if kit_id else NO_KIT_KEY,
            required_after=required_after,
            scope_revision=revision,
            **service.stamp(receipt, current),
        )
        for scope_id, (item_id, definition_id, kit_id, required_after) in zip(
            scope_ids, specs, strict=True
        )
    )


def expand_scope(*, deficiency_id, expected_event_id, explanation, items, **context):
    """Add items to a Pending Verification deficiency's scope as a new revision (VER-3, VER-4)."""
    return _run(
        event_type="SCOPE_EXPANDED",
        command_type=EXPAND_COMMAND,
        deficiency_id=deficiency_id,
        expected_event_id=expected_event_id,
        explanation=explanation,
        items=items,
        deadline=None,
        **context,
    )


def add_verification(*, deficiency_id, expected_event_id, explanation, items, deadline, **context):
    """Reopen a Closed Verified deficiency to verify more items, with a new deadline (SPEC 8.7)."""
    if deadline is None:
        raise service.malformed("deadline", "a new resolution deadline is required")
    return _run(
        event_type="VERIFICATION_ADDED",
        command_type=ADD_COMMAND,
        deficiency_id=deficiency_id,
        expected_event_id=expected_event_id,
        explanation=explanation,
        items=items,
        deadline=service.instant(deadline, "deadline"),
        **context,
    )


def _run(
    *,
    event_type,
    command_type,
    deficiency_id,
    expected_event_id,
    explanation,
    items,
    deadline,
    organization_id,
    actor_id,
    operation_id,
    correlation_id,
    request_attempt_id,
):
    for value in (organization_id, actor_id):
        canonical_id(value)
    _operation_id(operation_id)
    deficiency_id = service.canonical(deficiency_id, "deficiency_id")
    expected_event_id = service.canonical(expected_event_id, "expected_event_id")
    explanation = service.text(explanation, "explanation", MAX_TEXT)
    items = parse_items(items)
    expanding = event_type == "SCOPE_EXPANDED"

    def build(actor):
        vehicle_id, projection, last = _lock(
            actor, organization_id, actor_id, deficiency_id, expected_event_id
        )
        now = timezone.now()
        if expanding:
            _require(
                projection.status,
                PENDING_VERIFICATION,
                PENDING_VERIFICATION,
                "scope can be expanded only while Pending Verification",
            )
        else:
            _require(
                projection.status,
                CLOSED_VERIFIED,
                OPEN,
                "verification can be added only to a Closed Verified deficiency",
            )
            if deadline <= now:
                raise DeficiencyError(
                    "INVALID_DEADLINE", "the deadline must be in the future", path="deadline"
                )
        resolved = resolve_items(organization_id, vehicle_id, items, now)
        revision, carried = (
            _expanded_revision(organization_id, deficiency_id, resolved)
            if expanding
            else (None, [])
        )
        event_id = new_id()
        scope_ids = [new_id() for _ in range(len(carried) + len(resolved))] if expanding else []

        def apply(receipt, current):
            event = DeficiencyEvent.objects.create(
                id=event_id,
                organization_id=organization_id,
                deficiency_id=deficiency_id,
                sequence=last.sequence + 1,
                predecessor_event=last,
                event_type=event_type,
                from_status=projection.status,
                to_status=PENDING_VERIFICATION if expanding else OPEN,
                explanation=explanation,
                deadline=deadline,
                suspensions_ended=[],
                **service.stamp(receipt, current),
            )
            _expansion_rows(
                resolved,
                organization_id=organization_id,
                deficiency_id=deficiency_id,
                event=event,
                receipt=receipt,
                current=current,
            )
            if expanding:
                _append_revision(
                    receipt,
                    current,
                    organization_id=organization_id,
                    deficiency_id=deficiency_id,
                    carried=carried,
                    resolved=resolved,
                    scope_ids=scope_ids,
                    revision=revision,
                )
                changes = {"last_applied_event": event}
            else:
                changes = {"status": OPEN, "deadline": deadline, "last_applied_event": event}
            DeficiencyProjection.objects.filter(
                organization_id=organization_id, deficiency_id=deficiency_id
            ).update(**changes)
            publish_changed(organization_id, deficiency_id, event_id)

        to_status = PENDING_VERIFICATION if expanding else OPEN
        body = {
            "code": "DEFICIENCY_SCOPE_EXPANDED" if expanding else "DEFICIENCY_VERIFICATION_ADDED",
            "deficiency_id": deficiency_id,
            "event_id": event_id,
            "status": to_status,
            "items": [
                {"item_id": item_id, "kit_instance_id": kit_id}
                for item_id, _definition, kit_id in resolved
            ],
            "assignee_id": None if projection.assignee_id is None else str(projection.assignee_id),
        }
        if expanding:
            body |= {"scope_revision": revision, "scope_count": len(scope_ids)}
        else:
            body["deadline"] = deadline.isoformat()
        return Plan(
            response_body=body,
            apply=apply,
            response_status=201,
            target_type="ORGANIZATION",
            target_id=organization_id,
            prior_snapshot={"schema_version": 1, "values": {"status": projection.status}},
            new_snapshot={
                "schema_version": 1,
                "values": {
                    "event_id": event_id,
                    "event_type": event_type,
                    "status": to_status,
                    "scope_revision": revision,
                    "scope_row_ids": sorted(scope_ids),
                    "item_ids": sorted({item_id for item_id, _d, _k in resolved}),
                },
            },
            recorded_at=now,
        )

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type=command_type,
        request_digest=digest(
            {
                "deficiency_id": deficiency_id,
                "expected_event_id": expected_event_id,
                "explanation": explanation,
                "items": sorted([item_id, kit or ""] for item_id, kit in items),
                "deadline": deadline.isoformat() if deadline else None,
            }
        ),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=_authorizer(organization_id, actor_id, EXPAND_ACTION),
        prepare=build,
    )
