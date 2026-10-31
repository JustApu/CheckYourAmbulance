"""Audited deficiency commands: independent report, add link, remove link (SPEC 8.1-8.3).

report_deficiency is the server boundary for POST /api/v1/deficiencies. Like record_check it
takes IDs and intent, never role snapshots, and runs through run_command, which owns the
outermost transaction, the operation-id receipt and the audit. Shape errors are raised before
any transaction; everything that reads the database runs in prepare, so a rejected write rolls
back whole and the operation id stays reusable.
"""

from uuid import UUID

from django.utils import timezone

from cya.accounts.fields import new_id
from cya.accounts.models import User
from cya.authorization.backend import canonical_id, load_state
from cya.authorization.guard import PermissionDenied, Principal, require_permission
from cya.authorization.locking import locked_identities
from cya.authorization.persistence import DatabaseRoleBackend, digest
from cya.authorization.policy import ResourceContext, Role, permitted
from cya.checklists.models import Vehicle
from cya.core.audit_commands import Actor, CommandDenied, Plan, run_command
from cya.deficiencies import service
from cya.deficiencies.models import (
    Deficiency,
    DeficiencyEvent,
    DeficiencyProjection,
    VerificationScopeRow,
)
from cya.deficiencies.service import DeficiencyError
from cya.deficiencies.transitions import (
    IN_PROGRESS,
    OPEN,
    PENDING_VERIFICATION,
    TransitionRejected,
    validate_transition,
)
from cya.operations.models import MAX_TEXT

REPORT_ACTION = "create_deficiency"
ASSIGN_ACTION = "assign_deficiency_deadline"
LINK_ACTION = "link_unlink_deficiencies"
REGRADE_ACTION = "regrade_deficiency_severity"
REPORT_COMMAND_TYPE = "DEFICIENCY_REPORT"
LINK_ADD_COMMAND_TYPE = "DEFICIENCY_LINK_ADD"
LINK_REMOVE_COMMAND_TYPE = "DEFICIENCY_LINK_REMOVE"
REGRADE_COMMAND_TYPE = "DEFICIENCY_REGRADE"
ASSIGN_COMMAND_TYPE = "DEFICIENCY_ASSIGN"
DEADLINE_COMMAND_TYPE = "DEFICIENCY_DEADLINE"
START_WORK_COMMAND_TYPE = "DEFICIENCY_START_WORK"
CORRECT_ACTION = "record_deficiency_correction"
CORRECT_COMMAND_TYPE = "DEFICIENCY_CORRECTION_RECORDED"
# Roles that may be given a deficiency to work (owner decision 2026-10-27, DEF-4): an account that
# holds only Administrator cannot be an assignee. The legacy EMT grant counts as Crew.
ASSIGNEE_ROLES = frozenset({Role.CREW, Role.SUPERVISOR, Role.SUPPLY_OFFICER})


def _operation_id(value):
    service.canonical(value, "operation_id")
    if UUID(value).version != 4:
        raise service.malformed("operation_id", "operation_id must be a version 4 UUID")


def _authorizer(organization_id, actor_id, *actions, assignee_id=None, context=None):
    """Lock the actor (and any assignee) in id order and require every named action.

    The assignee must be an active user of the organization who holds Crew, Supervisor or Supply
    Officer; the answer is the same whether the id is unknown, of another organization, inactive
    or an Administrator-only account, so nothing is revealed about users. context is passed to the
    permission check: the assignment commands pass the most permissive supply fact here and
    decide the real one from the deficiency's category once they hold its lock.
    """
    backend = DatabaseRoleBackend()
    unavailable = DeficiencyError(
        "INVALID_ASSIGNEE", "the assignee is not available", path="assignee_id"
    )

    def authorize(_org):
        wanted = [actor_id] if assignee_id is None else [actor_id, assignee_id]
        try:
            with locked_identities(organization_id, wanted, lock_organization=False) as (
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
                try:
                    for action in actions:
                        require_permission(principal, organization_id, action, context=context)
                except PermissionDenied:
                    raise CommandDenied(actor) from None
                if assignee_id is not None:
                    assignee = users[assignee_id]
                    held = {
                        Role.CREW if r == Role.EMT else r
                        for r in load_state(backend, assignee).grants
                    }
                    if assignee.active is not True or not held & ASSIGNEE_ROLES:
                        raise unavailable
                return actor
        except User.DoesNotExist:
            # The signed-in actor exists, so a missing identity is the named assignee.
            if assignee_id is None:
                raise CommandDenied() from None
            raise unavailable from None

    return authorize


def _related_ids(value, own_id=None):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > service.MAX_RELATED:
        raise service.malformed(
            "related_deficiency_ids",
            f"related_deficiency_ids is a list of at most {service.MAX_RELATED}",
        )
    ids = [service.canonical(v, "related_deficiency_ids") for v in value]
    if len(set(ids)) != len(ids):
        raise service.malformed("related_deficiency_ids", "related_deficiency_ids must not repeat")
    return ids


def report_deficiency(
    *,
    vehicle_id,
    target,
    category_id,
    severity,
    summary,
    description,
    related_deficiency_ids=None,
    assignee_id=None,
    deadline=None,
    session_id=None,
    organization_id,
    actor_id,
    operation_id,
    correlation_id,
    request_attempt_id,
):
    """Report a deficiency independently of any check; idempotent on operation_id (REC-4).

    Exactly one target; scope and category revision are resolved on the server. Related links are
    informational and explicitly chosen. Assignment and deadline need the SPEC 3.3 privilege,
    judged on the category revision's supply flag; a crew report stays unassigned.
    """
    for value in (organization_id, actor_id):
        canonical_id(value)
    _operation_id(operation_id)
    vehicle_id = service.canonical(vehicle_id, "vehicle_id")
    if session_id is not None:
        raise service.DeficiencyError(
            "NOT_YET_SUPPORTED", "session_id is not available yet", path="session_id"
        )
    parsed = service.parse_target(target)
    fields = service.report_fields(
        {
            "category_id": category_id,
            "severity": severity,
            "summary": summary,
            "description": description,
        },
        "$",
    )
    related = _related_ids(related_deficiency_ids)
    if assignee_id is not None:
        assignee_id = service.canonical(assignee_id, "assignee_id")
    deadline = service.instant(deadline, "deadline")
    request = {
        "vehicle_id": vehicle_id,
        "target": target,
        "category_id": category_id,
        "severity": severity,
        "summary": summary,
        "description": description,
        "related_deficiency_ids": related,
        "assignee_id": assignee_id,
        "deadline": deadline.isoformat() if deadline else None,
    }

    def build(actor):
        vehicle = (
            Vehicle.objects.select_for_update()
            .filter(organization_id=organization_id, pk=vehicle_id)
            .first()
        )
        if vehicle is None:
            raise DeficiencyError(
                "NOT_FOUND", "vehicle was not found", status=404, path="vehicle_id"
            )
        if not vehicle.active:
            raise DeficiencyError("VEHICLE_INACTIVE", "vehicle is inactive", path="vehicle_id")
        service.lock_checklist_templates(organization_id)
        # Deficiencies are the last lock group; related ones are held sorted for the whole command.
        service.lock_deficiencies(organization_id, related)
        now = timezone.now()
        category = service.resolve_category(organization_id, fields["category_id"], now)
        severity = service.resolve_severity_level(
            organization_id, fields["severity"], now, path="severity"
        )
        if assignee_id is not None or deadline is not None:
            context = ResourceContext(supply_related=category.supply_related)
            if not permitted(ASSIGN_ACTION, actor_id, actor.roles, context=context):
                raise CommandDenied(actor)
        if deadline is not None and deadline <= now:
            raise DeficiencyError(
                "INVALID_DEADLINE", "the deadline must be in the future", path="deadline"
            )
        header, affected = service.resolve_scope(organization_id, vehicle_id, parsed, now)
        deficiency_id, event_id = new_id(), new_id()
        link_ids = [new_id() for _ in related]

        def apply(receipt, current):
            service.insert_deficiency(
                receipt,
                current,
                organization_id=organization_id,
                deficiency_id=deficiency_id,
                event_id=event_id,
                operation_id=operation_id,
                vehicle_id=vehicle_id,
                category=category,
                severity=severity,
                fields=fields,
                affected=affected,
                item_id=header["item_id"],
                kit_id=header["kit_id"],
                compartment_id=header["compartment_id"],
                assignee_id=assignee_id,
                deadline=deadline,
            )
            for link_id, other in zip(link_ids, related, strict=True):
                service.insert_link(
                    receipt,
                    current,
                    organization_id=organization_id,
                    first_id=deficiency_id,
                    second_id=other,
                    link_type="ADD",
                    link_id=link_id,
                )
            from cya.live_updates.publish import publish_vehicle_update

            publish_vehicle_update(
                organization_id=organization_id,
                vehicle_id=vehicle_id,
                event_id=event_id,
                kind="deficiency.changed",
            )

        return Plan(
            response_body={
                "code": "DEFICIENCY_REPORTED",
                "deficiency_id": deficiency_id,
                "creation_event_id": event_id,
                "related_link_event_ids": link_ids,
            },
            apply=apply,
            response_status=201,
            target_type="ORGANIZATION",
            target_id=organization_id,
            new_snapshot={
                "schema_version": 1,
                "values": {
                    "deficiency_id": deficiency_id,
                    "vehicle_id": vehicle_id,
                    "category_version_id": category.pk,
                    "severity_version_id": severity.pk,
                    "affected_item_count": len(affected),
                    "related_deficiency_ids": related,
                },
            },
            recorded_at=now,
        )

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type=REPORT_COMMAND_TYPE,
        request_digest=digest(request),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=_authorizer(
            organization_id,
            actor_id,
            REPORT_ACTION,
            *((LINK_ACTION,) if related else ()),
            assignee_id=assignee_id,
        ),
        prepare=build,
    )


def _change_link(link_type, command_type, *, deficiency_id, related_deficiency_id, **context):
    """Add or remove an informational link (DEF-2): the audited rows are the whole effect.

    Both deficiencies are locked sorted by id. A link never merges responsibility, moves an
    assignee or closes anything, so no status, projection or event of either side is written.
    """
    organization_id, actor_id = context["organization_id"], context["actor_id"]
    for value in (organization_id, actor_id):
        canonical_id(value)
    operation_id = context["operation_id"]
    _operation_id(operation_id)
    first = service.canonical(deficiency_id, "deficiency_id")
    second = service.canonical(related_deficiency_id, "related_deficiency_id")
    if first == second:
        raise DeficiencyError("SELF_LINK", "a deficiency cannot be linked to itself")

    def build(actor):
        service.lock_deficiencies(organization_id, [first, second])
        service.check_link_change(organization_id, first, second, link_type)
        link_id = new_id()
        lower, upper = service.canonical_pair(first, second)

        def apply(receipt, current):
            service.insert_link(
                receipt,
                current,
                organization_id=organization_id,
                first_id=first,
                second_id=second,
                link_type=link_type,
                link_id=link_id,
            )
            vehicles = Deficiency.objects.filter(
                organization_id=organization_id, pk__in=[first, second]
            ).values_list("vehicle_id", flat=True)
            from cya.live_updates.publish import publish_vehicle_update

            for vehicle_id in sorted(set(vehicles)):
                publish_vehicle_update(
                    organization_id=organization_id,
                    vehicle_id=str(vehicle_id),
                    event_id=link_id,
                    kind="deficiency.changed",
                )

        return Plan(
            response_body={
                "code": "DEFICIENCY_LINK_" + link_type,
                "link_event_id": link_id,
                "lower_deficiency_id": lower,
                "upper_deficiency_id": upper,
            },
            apply=apply,
            response_status=201,
            target_type="ORGANIZATION",
            target_id=organization_id,
            new_snapshot={
                "schema_version": 1,
                "values": {
                    "link_event_id": link_id,
                    "lower_deficiency_id": lower,
                    "upper_deficiency_id": upper,
                    "link_type": link_type,
                },
            },
        )

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type=command_type,
        request_digest=digest({"deficiency_id": first, "related_deficiency_id": second}),
        correlation_id=context["correlation_id"],
        request_attempt_id=context["request_attempt_id"],
        authorize=_authorizer(organization_id, actor_id, LINK_ACTION),
        prepare=build,
    )


def add_link(*, deficiency_id, related_deficiency_id, **context):
    """Link two deficiencies; the crew member chose both (DEF-1, DEF-2)."""
    return _change_link(
        "ADD",
        LINK_ADD_COMMAND_TYPE,
        deficiency_id=deficiency_id,
        related_deficiency_id=related_deficiency_id,
        **context,
    )


def remove_link(*, deficiency_id, related_deficiency_id, **context):
    """Remove a link; the history keeps both the add and the removal."""
    return _change_link(
        "REMOVE",
        LINK_REMOVE_COMMAND_TYPE,
        deficiency_id=deficiency_id,
        related_deficiency_id=related_deficiency_id,
        **context,
    )


def regrade_severity(
    *,
    deficiency_id,
    severity,
    explanation,
    expected_event_id,
    organization_id,
    actor_id,
    operation_id,
    correlation_id,
    request_attempt_id,
):
    """Re-grade a deficiency's severity as a logged, append-only event (SPEC DEF-8, issue 123 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/123)).

    Needs the regrade_deficiency_severity permission. The caller names the event it last saw; if
    another has been appended since, the re-grade is refused (409) rather than applied to a
    deficiency the person has not seen. Only an active deficiency can be re-graded: a closed or
    cancelled one is finished, and a re-grade after a cancellation would stop its reopening
    (INV-7). The new level must be an active level in force and differ from the current one. The
    event and the projection's level are the whole effect: no status, assignee, deadline, link or
    evidence changes, and severity never changes the vehicle's status (OPS-2).
    """
    for value in (organization_id, actor_id):
        canonical_id(value)
    _operation_id(operation_id)
    deficiency_id = service.canonical(deficiency_id, "deficiency_id")
    expected_event_id = service.canonical(expected_event_id, "expected_event_id")
    severity = service.severity_code(severity, "severity")
    explanation = service.text(explanation, "explanation", MAX_TEXT)

    def build(actor):
        projection = service.lock_deficiencies(organization_id, [deficiency_id])[deficiency_id]
        last = service.last_event(deficiency_id)
        if projection.last_applied_event_id != last.pk:
            raise DeficiencyError(
                "CONFIGURATION_UNAVAILABLE", "deficiency state is unavailable", status=503
            )
        if str(last.pk) != expected_event_id:
            raise DeficiencyError(
                "DEFICIENCY_CHANGED",
                "the deficiency changed since it was read; reload it and decide again",
                status=409,
                path="expected_event_id",
            )
        service.require_active(projection)
        now = timezone.now()
        level = service.resolve_severity_level(organization_id, severity, now, path="severity")
        previous = projection.severity_version
        if level.code == previous.code:
            raise DeficiencyError(
                "SEVERITY_UNCHANGED",
                "the deficiency already has this severity level",
                path="severity",
            )
        event_id = new_id()

        def apply(receipt, current):
            service.insert_regrade(
                receipt,
                current,
                organization_id=organization_id,
                event_id=event_id,
                last=last,
                previous=previous,
                severity=level,
                explanation=explanation,
            )
            from cya.live_updates.publish import publish_vehicle_update

            publish_vehicle_update(
                organization_id=organization_id,
                vehicle_id=str(
                    Deficiency.objects.values_list("vehicle_id", flat=True).get(
                        organization_id=organization_id, pk=deficiency_id
                    )
                ),
                event_id=event_id,
                kind="deficiency.changed",
            )

        return Plan(
            response_body={
                "code": "DEFICIENCY_SEVERITY_CHANGED",
                "deficiency_id": deficiency_id,
                "event_id": event_id,
                "previous_severity": previous.code,
                "severity": level.code,
            },
            apply=apply,
            response_status=201,
            target_type="ORGANIZATION",
            target_id=organization_id,
            new_snapshot={
                "schema_version": 1,
                "values": {
                    "deficiency_id": deficiency_id,
                    "event_id": event_id,
                    "previous_severity_version_id": previous.pk,
                    "severity_version_id": level.pk,
                },
            },
            recorded_at=now,
        )

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type=REGRADE_COMMAND_TYPE,
        request_digest=digest(
            {
                "deficiency_id": deficiency_id,
                "severity": severity,
                "explanation": explanation,
                "expected_event_id": expected_event_id,
            }
        ),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=_authorizer(organization_id, actor_id, REGRADE_ACTION),
        prepare=build,
    )


def _lock_for_change(actor, action, organization_id, actor_id, deficiency_id, expected_event_id):
    """Lock one deficiency for a lifecycle event and return its projection and newest event.

    The action's permission is decided here from the deficiency's own category revision, read from
    the database and never from the request (and the assignee from the locked projection), because
    the authorize step could only assume the most permissive facts before the lock. A denial for a
    person who may act on only some deficiencies looks the same whether the deficiency is out of
    reach or absent. The caller then names the event it last saw: another appended since is 409
    DEFICIENCY_CHANGED.
    """
    unconditional = permitted(
        action, actor_id, actor.roles, context=ResourceContext(supply_related=False)
    )
    try:
        projection = service.lock_deficiencies(organization_id, [deficiency_id])[deficiency_id]
    except DeficiencyError as error:
        if error.code == "NOT_FOUND" and not unconditional:
            raise CommandDenied(actor) from None
        raise
    supply_related = Deficiency.objects.values_list(
        "category_version__supply_related", flat=True
    ).get(organization_id=organization_id, pk=deficiency_id)
    assignee_id = None if projection.assignee_id is None else str(projection.assignee_id)
    context = ResourceContext(supply_related=supply_related, assignee_id=assignee_id)
    if not permitted(action, actor_id, actor.roles, context=context):
        raise CommandDenied(actor)
    last = service.last_event(deficiency_id)
    if projection.last_applied_event_id != last.pk:
        raise DeficiencyError(
            "CONFIGURATION_UNAVAILABLE", "deficiency state is unavailable", status=503
        )
    if str(last.pk) != expected_event_id:
        raise DeficiencyError(
            "DEFICIENCY_CHANGED",
            "the deficiency changed since it was read; reload it and decide again",
            status=409,
            path="expected_event_id",
        )
    return projection, last


def publish_changed(organization_id, deficiency_id, event_id):
    """Publish the identifier-only live hint once the event has committed."""
    from cya.live_updates.publish import publish_vehicle_update

    publish_vehicle_update(
        organization_id=organization_id,
        vehicle_id=str(
            Deficiency.objects.values_list("vehicle_id", flat=True).get(
                organization_id=organization_id, pk=deficiency_id
            )
        ),
        event_id=event_id,
        kind="deficiency.changed",
    )


def _lifecycle(
    event_type,
    command_type,
    *,
    deficiency_id,
    expected_event_id,
    organization_id,
    actor_id,
    operation_id,
    correlation_id,
    request_attempt_id,
    assignee_id=None,
    deadline=None,
):
    """Assign, set a deadline or start work as one append-only event (SPEC DEF-4, 3.3, 8.2).

    Needs the assign_deficiency_deadline permission: Supervisors and Administrators on any
    deficiency, a Supply Officer only where the category revision of the deficiency is supply
    related (read from the database, never from the request), never Crew. The caller names the
    event it last saw (409 DEFICIENCY_CHANGED when another was appended since). Only an active
    deficiency can change. The event and the projection's assignee, deadline or status are the
    whole effect; the vehicle's status never changes (OPS-2). A denial for a person who may act
    only on some deficiencies looks the same whether the deficiency is out of reach or absent.
    """
    for value in (organization_id, actor_id):
        canonical_id(value)
    _operation_id(operation_id)
    deficiency_id = service.canonical(deficiency_id, "deficiency_id")
    expected_event_id = service.canonical(expected_event_id, "expected_event_id")
    if event_type == "ASSIGNED":
        assignee_id = service.canonical(assignee_id, "assignee_id")
    if event_type == "DEADLINE_SET":
        if deadline is None:
            raise service.malformed("deadline", "deadline is required")
        deadline = service.instant(deadline, "deadline")

    def build(actor):
        projection, last = _lock_for_change(
            actor, ASSIGN_ACTION, organization_id, actor_id, deficiency_id, expected_event_id
        )
        service.require_active(projection)
        now = timezone.now()
        if event_type == "ASSIGNED" and str(projection.assignee_id) == assignee_id:
            raise DeficiencyError(
                "ASSIGNEE_UNCHANGED",
                "the deficiency is already assigned to this user",
                path="assignee_id",
            )
        if event_type == "DEADLINE_SET":
            if deadline <= now:
                raise DeficiencyError(
                    "INVALID_DEADLINE", "the deadline must be in the future", path="deadline"
                )
            if projection.deadline == deadline:
                raise DeficiencyError(
                    "DEADLINE_UNCHANGED",
                    "the deficiency already has this deadline",
                    path="deadline",
                )
        to_status = projection.status
        if event_type == "WORK_STARTED":
            # Only Open to In Progress is work starting; resuming from Awaiting External Dependency
            # is another event (SPEC 8.2), though the same table lists that arrow.
            to_status = IN_PROGRESS
            try:
                if projection.status != OPEN:
                    raise TransitionRejected(projection.status)
                validate_transition(projection.status, to_status)
            except TransitionRejected:
                raise DeficiencyError(
                    "INVALID_TRANSITION",
                    "work can be started only on an Open deficiency",
                    path="deficiency",
                ) from None
        event_id = new_id()

        def apply(receipt, current):
            service.insert_lifecycle(
                receipt,
                current,
                organization_id=organization_id,
                event_id=event_id,
                last=last,
                event_type=event_type,
                to_status=to_status,
                assignee_id=assignee_id,
                deadline=deadline,
            )
            publish_changed(organization_id, deficiency_id, event_id)

        return Plan(
            response_body={
                "code": LIFECYCLE_CODES[event_type],
                "deficiency_id": deficiency_id,
                "event_id": event_id,
                "status": to_status,
                "assignee_id": assignee_id,
                "deadline": deadline.isoformat() if deadline else None,
            },
            apply=apply,
            response_status=201,
            target_type="ORGANIZATION",
            target_id=organization_id,
            new_snapshot={
                "schema_version": 1,
                "values": {
                    "deficiency_id": deficiency_id,
                    "event_id": event_id,
                    "event_type": event_type,
                    "assignee_user_id": assignee_id,
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
                "assignee_id": assignee_id,
                "deadline": deadline.isoformat() if deadline else None,
            }
        ),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=_authorizer(
            organization_id,
            actor_id,
            ASSIGN_ACTION,
            assignee_id=assignee_id,
            context=ResourceContext(supply_related=True),
        ),
        prepare=build,
    )


LIFECYCLE_CODES = {
    "ASSIGNED": "DEFICIENCY_ASSIGNED",
    "DEADLINE_SET": "DEFICIENCY_DEADLINE_SET",
    "WORK_STARTED": "DEFICIENCY_WORK_STARTED",
}


def assign_deficiency(*, deficiency_id, assignee_id, expected_event_id, **context):
    """Assign (or reassign) a deficiency; every earlier assignee stays in the history (DEF-4)."""
    return _lifecycle(
        "ASSIGNED",
        ASSIGN_COMMAND_TYPE,
        deficiency_id=deficiency_id,
        assignee_id=assignee_id,
        expected_event_id=expected_event_id,
        **context,
    )


def set_deadline(*, deficiency_id, deadline, expected_event_id, **context):
    """Set or revise a deficiency's deadline; every earlier deadline stays in the history."""
    return _lifecycle(
        "DEADLINE_SET",
        DEADLINE_COMMAND_TYPE,
        deficiency_id=deficiency_id,
        deadline=deadline,
        expected_event_id=expected_event_id,
        **context,
    )


def start_work(*, deficiency_id, expected_event_id, **context):
    """Move an Open deficiency to In Progress (SPEC 8.2)."""
    return _lifecycle(
        "WORK_STARTED",
        START_WORK_COMMAND_TYPE,
        deficiency_id=deficiency_id,
        expected_event_id=expected_event_id,
        **context,
    )


def record_correction(*, deficiency_id, expected_event_id, **context):
    """Record a repair and materialize the correction-time verification scope (VER-1/3/4)."""
    organization_id = canonical_id(context["organization_id"])
    actor_id = canonical_id(context["actor_id"])
    _operation_id(context["operation_id"])
    deficiency_id = service.canonical(deficiency_id, "deficiency_id")
    expected_event_id = service.canonical(expected_event_id, "expected_event_id")

    def build(actor):
        # Kit assignment and checklist publication commands serialize on the vehicle anchor;
        # acquire it before the deficiency anchor, matching the repository's global lock order.
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
            actor, CORRECT_ACTION, organization_id, actor_id, deficiency_id, expected_event_id
        )
        if projection.status != IN_PROGRESS:
            raise DeficiencyError(
                "INVALID_TRANSITION",
                "a correction can be recorded only while In Progress",
                path="deficiency",
            )
        try:
            validate_transition(IN_PROGRESS, PENDING_VERIFICATION)
        except TransitionRejected:
            raise DeficiencyError(
                "INVALID_TRANSITION", "the correction transition is unavailable"
            ) from None
        now = timezone.now()
        deficiency = Deficiency.objects.select_related("vehicle").get(
            organization_id=organization_id, pk=deficiency_id
        )
        scope = service.correction_scope(organization_id, deficiency, now)
        if not scope:
            raise DeficiencyError(
                "EMPTY_SCOPE",
                "the deficiency has no materialized verification items",
                path="deficiency",
            )
        revision = (
            VerificationScopeRow.objects.filter(
                organization_id=organization_id, deficiency_id=deficiency_id
            )
            .order_by("-scope_revision")
            .values_list("scope_revision", flat=True)
            .first()
            or 0
        ) + 1
        event_id = new_id()
        scope_ids = [new_id() for _ in scope]

        def apply(receipt, current):
            event = DeficiencyEvent.objects.create(
                id=event_id,
                organization_id=organization_id,
                deficiency=deficiency,
                sequence=last.sequence + 1,
                predecessor_event=last,
                event_type="CORRECTION_RECORDED",
                from_status=IN_PROGRESS,
                to_status=PENDING_VERIFICATION,
                **service.stamp(receipt, current),
            )
            rows = []
            for scope_id, (item_id, definition_id, kit_id) in zip(scope_ids, scope, strict=True):
                rows.append(
                    VerificationScopeRow(
                        id=scope_id,
                        organization_id=organization_id,
                        deficiency=deficiency,
                        correction_event=event,
                        item_id=item_id,
                        definition_id=definition_id,
                        kit_instance_id=kit_id,
                        instance_key=kit_id or "00000000-0000-0000-0000-000000000000",
                        required_after=receipt.recorded_at,
                        scope_revision=revision,
                        **service.stamp(receipt, current),
                    )
                )
            VerificationScopeRow.objects.bulk_create(rows)
            DeficiencyProjection.objects.filter(
                organization_id=organization_id, deficiency_id=deficiency_id
            ).update(status=PENDING_VERIFICATION, last_applied_event=event)
            publish_changed(organization_id, deficiency_id, event_id)

        return Plan(
            response_body={
                "code": "DEFICIENCY_CORRECTION_RECORDED",
                "deficiency_id": deficiency_id,
                "event_id": event_id,
                "scope_revision": revision,
                "scope_count": len(scope),
                "status": PENDING_VERIFICATION,
                "assignee_id": None
                if projection.assignee_id is None
                else str(projection.assignee_id),
                "deadline": projection.deadline.isoformat() if projection.deadline else None,
            },
            apply=apply,
            response_status=201,
            target_type="ORGANIZATION",
            target_id=organization_id,
            new_snapshot={
                "schema_version": 1,
                "values": {
                    "deficiency_id": deficiency_id,
                    "event_id": event_id,
                    "scope_revision": revision,
                    "scope_row_ids": sorted(scope_ids),
                },
            },
            recorded_at=now,
        )

    return run_command(
        organization_id=organization_id,
        operation_id=context["operation_id"],
        command_type=CORRECT_COMMAND_TYPE,
        request_digest=digest(
            {
                "deficiency_id": deficiency_id,
                "expected_event_id": expected_event_id,
            }
        ),
        correlation_id=context["correlation_id"],
        request_attempt_id=context["request_attempt_id"],
        authorize=_authorizer(organization_id, actor_id, CORRECT_ACTION),
        prepare=build,
    )
