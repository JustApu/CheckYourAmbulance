"""Vehicle crew assignment commands and reads (SPEC 15.4 ASG-1 to ASG-8, issue #202).

An assignment says that a person is working on a vehicle from the moment it is recorded until
an end time that whoever makes it enters (ASG-4). It never restricts who may record a check
and is not the effective roster for conditional obligations (ASG-6): nothing here touches
checks, sessions or determinations.

Each command runs through cya.core.audit_commands.run_command (receipt, replay, audit). Do
not wrap them in a transaction. Who may make a change (ASG-2, SPEC 3.3) depends on facts read
under the identity locks: the person themselves (assign_own_vehicle, source SELF); anyone
currently on the target vehicle (correct_vehicle_crew, source CREW); or a Supervisor or
Administrator (manage_vehicle_assignments, source SUPERVISOR). For an assignment or move the
target vehicle is the destination; for an extension or end it is the person's current one.

The person's identity row is locked with the actor's, so changes to one person's chain are
serialized; the vehicle rows are then locked in ID order. The database guard in migration
0007 repeats the chain rules, so a row written any other way is refused.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id, load_state
from cya.authorization.guard import PermissionDenied, Principal, require_permission
from cya.authorization.locking import locked_identities
from cya.authorization.persistence import DatabaseRoleBackend, digest
from cya.authorization.policy import ResourceContext, Role, permitted
from cya.checklists.models import Vehicle
from cya.core.audit_commands import Actor, CommandDenied, Plan, run_command
from cya.operations.models import (
    DEFAULT_ASSIGNMENT_HOURS,
    MAX_ASSIGNMENT_HOURS,
    VehicleAssignmentEvent,
    VehicleAssignmentLimit,
)

ASSIGN_COMMAND = "VEHICLE_ASSIGN"
EXTEND_COMMAND = "VEHICLE_ASSIGNMENT_EXTEND"
END_COMMAND = "VEHICLE_ASSIGNMENT_END"
LIMIT_COMMAND = "VEHICLE_ASSIGNMENT_LIMIT_SET"
OWN = "assign_own_vehicle"
CREW = "correct_vehicle_crew"
ANY = "manage_vehicle_assignments"
HISTORY = "view_vehicle_assignment_history"
LIMIT_ACTION = "configure_schedules_thresholds_notifications"


class AssignmentError(ValueError):
    """An assignment rule was violated; the command rolls back and is audited as failed.

    code is the stable wire code, status the HTTP status, path a safe schema path.
    """

    def __init__(self, message, *, code="INVALID_ASSIGNMENT", status=422, path=None):
        super().__init__(message)
        self.code, self.status, self.path = code, status, path


@dataclass(frozen=True)
class Assignment:
    """A person's current assignment: the newest event of their chain, while it applies."""

    event_id: str
    person_id: str
    vehicle_id: str
    ends_at: datetime


# --- reads -----------------------------------------------------------------------------------


def _applies(event, at):
    return event is not None and event.action != "END" and event.ends_at > at


def _as_assignment(event):
    return Assignment(event.pk, event.person_id, event.vehicle_id, event.ends_at)


def _newest(organization_id, person_id, *, lock=False):
    rows = VehicleAssignmentEvent.objects.filter(
        organization_id=organization_id, person_id=person_id
    )
    if lock:
        rows = rows.select_for_update()
    return rows.order_by("-sequence").first()


def current_assignment(organization_id, person_id, at=None):
    """The person's assignment at `at` (default now), or None (ASG-3, ASG-4)."""
    event = _newest(organization_id, person_id)
    return _as_assignment(event) if _applies(event, at or timezone.now()) else None


def current_crew(organization_id, vehicle_id=None, at=None):
    """Current assignments, for one vehicle or all, ordered by vehicle then end time (ASG-8).

    Rebuilt on every read from the newest event of each person's chain and the clock, so it
    can never disagree with the event history (AC-39 (6)).
    """
    at = at or timezone.now()
    later = VehicleAssignmentEvent.objects.filter(
        person_id=OuterRef("person_id"), sequence__gt=OuterRef("sequence")
    )
    rows = VehicleAssignmentEvent.objects.filter(
        ~Exists(later),
        ~Q(action="END"),
        organization_id=organization_id,
        ends_at__gt=at,
        person__active=True,  # a deactivated user is not shown as crew (ASG-8)
    )
    if vehicle_id is not None:
        rows = rows.filter(vehicle_id=vehicle_id)
    return [_as_assignment(e) for e in rows.order_by("vehicle_id", "ends_at", "person_id")]


def assignment_history(organization_id, *, person_id=None, vehicle_id=None, limit=200):
    """Newest-first events for Supervisors and Administrators (ASG-8); the caller checks HISTORY."""
    rows = VehicleAssignmentEvent.objects.filter(organization_id=organization_id)
    if person_id is not None:
        rows = rows.filter(person_id=person_id)
    if vehicle_id is not None:
        rows = rows.filter(Q(vehicle_id=vehicle_id) | Q(from_vehicle_id=vehicle_id))
    return list(rows.order_by("-recorded_at", "-sequence")[:limit])


def assignment_limit_hours(organization_id):
    """The longest an assignment or extension may run (ASG-4, OQ-79); 24 h by default."""
    latest = (
        VehicleAssignmentLimit.objects.filter(organization_id=organization_id)
        .order_by("-revision")
        .first()
    )
    return latest.max_hours if latest else DEFAULT_ASSIGNMENT_HOURS


# --- command plumbing ------------------------------------------------------------------------


def _ends_at(value):
    """An aware datetime or an ISO 8601 instant with an offset, normalized to UTC."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            value = None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AssignmentError(
            "ends_at must be an instant with a time zone",
            code="MALFORMED_REQUEST",
            status=400,
            path="ends_at",
        )
    return value.astimezone(UTC)


def _check_window(organization_id, ends_at, now):
    """The end time is ahead and within the maximum after now (ASG-4, ASG-5)."""
    limit = assignment_limit_hours(organization_id)
    if ends_at <= now:
        raise AssignmentError("the end time has already passed", code="END_IN_PAST", path="ends_at")
    if ends_at > now + timedelta(hours=limit):
        raise AssignmentError(
            f"the end time is more than {limit} hours away", code="END_TOO_LATE", path="ends_at"
        )


def _locked_vehicles(organization_id, vehicle_ids):
    rows = {
        v.pk: v
        for v in Vehicle.objects.select_for_update()
        .filter(organization_id=organization_id, pk__in=sorted(set(vehicle_ids)))
        .order_by("pk")
    }
    for vehicle_id in vehicle_ids:
        if vehicle_id not in rows:
            raise AssignmentError(
                f"vehicle {vehicle_id} does not exist in the organization",
                code="NOT_FOUND",
                status=404,
            )
    return rows


def _authorizer(organization_id, actor_id, person_id, target_vehicle, decision, *, ending=False):
    """Lock the actor and the person, then decide who may act and with which source (ASG-2).

    target_vehicle(person_current) returns the vehicle the change is on. decision receives the
    source, so the plan can record it.
    """
    backend = DatabaseRoleBackend()

    def roles_of(user):
        return frozenset(
            Role.CREW if r == Role.EMT else r for r in load_state(backend, user).grants
        )

    def authorize(_org):
        with locked_identities(organization_id, [actor_id, person_id], lock_organization=False) as (
            current_org,
            users,
        ):
            user, person = users[actor_id], users[person_id]
            roles = roles_of(user)
            actor = Actor(user=user, roles=tuple(sorted(r.value for r in roles)))
            active = user.active and current_org.active
            principal = Principal(organization_id, actor_id, roles, active)
            now = timezone.now()
            person_now = _newest(organization_id, person_id, lock=True)
            person_now = person_now if _applies(person_now, now) else None
            vehicle_id = target_vehicle(person_now)
            if actor_id == person_id:
                action, context, source = OWN, ResourceContext(author_id=person_id), "SELF"
            elif permitted(ANY, actor_id, roles, active=active):
                action, context, source = ANY, None, "SUPERVISOR"
            else:
                mine = _newest(organization_id, actor_id)
                on_vehicle = _applies(mine, now) and vehicle_id is not None
                on_vehicle = on_vehicle and mine.vehicle_id == vehicle_id
                action, context, source = CREW, ResourceContext(on_vehicle=on_vehicle), "CREW"
            try:
                require_permission(principal, organization_id, action, context=context)
            except PermissionDenied:
                raise CommandDenied(actor) from None
            # ASG-2: only an active user with a role is put on, or kept on, a vehicle. Ending is
            # always allowed, so a deactivated person's assignment can still be cleared.
            if not ending and (not person.active or not roles_of(person)):
                raise AssignmentError(
                    "the person is not an active user with a role", code="PERSON_INACTIVE"
                )
            decision.update(source=source, person_now=person_now)
            return actor

    return authorize


def _run(context, command_type, request, authorize, build):
    return run_command(
        organization_id=context["organization_id"],
        operation_id=context["operation_id"],
        command_type=command_type,
        request_digest=digest(request),
        correlation_id=context["correlation_id"],
        request_attempt_id=context["request_attempt_id"],
        authorize=authorize,
        prepare=build,
    )


def _evidence(receipt, actor):
    return {
        "command": receipt,
        "recorded_at": receipt.recorded_at,
        "actor_user": actor.user,
        "actor_roles_at_time": list(actor.roles),
    }


def _snapshot(assignment):
    if assignment is None:
        return None
    return {
        "schema_version": 1,
        "values": {
            "person_id": str(assignment.person_id),
            "vehicle_id": str(assignment.vehicle_id),
            "ends_at": assignment.ends_at.isoformat() if assignment.ends_at else None,
        },
    }


def _append(receipt, current, *, event_id, org, person_id, prior, action, vehicle_id, **fields):
    return VehicleAssignmentEvent.objects.create(
        id=event_id,
        organization_id=org,
        person_id=person_id,
        sequence=prior.sequence + 1 if prior else 1,
        prior_event=prior,
        action=action,
        vehicle_id=vehicle_id,
        **fields,
        **_evidence(receipt, current),
    )


def _plan(org, body, apply, prior, new):
    return Plan(
        response_body=body,
        apply=apply,
        target_type="ORGANIZATION",
        target_id=org,
        prior_snapshot=_snapshot(prior),
        new_snapshot=new,
    )


def _ids(*values):
    for value in values:
        canonical_id(value)


# --- commands --------------------------------------------------------------------------------


def assign_vehicle(*, person_id, vehicle_id, ends_at, **context):
    """Put a person on a vehicle until ends_at (ASSIGN), or move them there (MOVE, ASG-3).

    A person already on another vehicle is moved in one event, so nobody is on two vehicles.
    Refused when they are already on this vehicle (use extend_assignment).
    """
    _ids(context["organization_id"], context["actor_id"], person_id, vehicle_id)
    org, ends = context["organization_id"], _ends_at(ends_at)
    decision = {}

    def build(actor):
        now = timezone.now()
        current = decision["person_now"]
        vehicles = _locked_vehicles(org, [vehicle_id] + ([current.vehicle_id] if current else []))
        if not vehicles[vehicle_id].active:
            raise AssignmentError("vehicle is inactive", code="VEHICLE_INACTIVE", status=409)
        if current is not None and current.vehicle_id == vehicle_id:
            raise AssignmentError(
                "the person is already on this vehicle; extend the assignment instead",
                code="ALREADY_ASSIGNED",
                status=409,
            )
        _check_window(org, ends, now)
        prior = _newest(org, person_id, lock=True)
        action = "MOVE" if current is not None else "ASSIGN"
        event_id = new_id()

        def apply(receipt, current_actor):
            _append(
                receipt,
                current_actor,
                event_id=event_id,
                org=org,
                person_id=person_id,
                prior=prior,
                action=action,
                vehicle_id=vehicle_id,
                from_vehicle_id=current.vehicle_id if current else None,
                ends_at=ends,
                source=decision["source"],
            )

        body = {
            "code": action,
            "event_id": event_id,
            "person_id": person_id,
            "vehicle_id": vehicle_id,
            "from_vehicle_id": str(current.vehicle_id) if current else None,
            "ends_at": ends.isoformat(),
            "source": decision["source"],
        }
        new = _snapshot(Assignment(event_id, person_id, vehicle_id, ends))
        return _plan(org, body, apply, current, new)

    authorize = _authorizer(
        org, context["actor_id"], person_id, lambda _current: vehicle_id, decision
    )
    request = {"person_id": person_id, "vehicle_id": vehicle_id, "ends_at": ends.isoformat()}
    return _run(context, ASSIGN_COMMAND, request, authorize, build)


def _on_current(person_id, command_type, request, context, make, *, ending=False):
    """Shared shape of extend and end: both act on the person's current assignment."""
    _ids(context["organization_id"], context["actor_id"], person_id)
    org = context["organization_id"]
    decision = {}

    def build(actor):
        current = decision["person_now"]
        if current is None:
            raise AssignmentError(
                "the person has no current assignment", code="NOT_ASSIGNED", status=409
            )
        _locked_vehicles(org, [current.vehicle_id])
        prior = _newest(org, person_id, lock=True)
        return make(org, current, prior, decision["source"], timezone.now())

    authorize = _authorizer(
        org,
        context["actor_id"],
        person_id,
        lambda current: current.vehicle_id if current else None,
        decision,
        ending=ending,
    )
    return _run(context, command_type, request, authorize, build)


def extend_assignment(*, person_id, ends_at, **context):
    """Set a later end time on the person's current assignment (EXTEND, ASG-5)."""
    ends = _ends_at(ends_at)

    def make(org, current, prior, source, now):
        if ends <= current.ends_at:
            raise AssignmentError(
                "the new end time must be later than the current one",
                code="NOT_LATER",
                path="ends_at",
            )
        _check_window(org, ends, now)
        event_id = new_id()

        def apply(receipt, current_actor):
            _append(
                receipt,
                current_actor,
                event_id=event_id,
                org=org,
                person_id=person_id,
                prior=prior,
                action="EXTEND",
                vehicle_id=current.vehicle_id,
                ends_at=ends,
                source=source,
            )

        body = {
            "code": "EXTEND",
            "event_id": event_id,
            "person_id": person_id,
            "vehicle_id": str(current.vehicle_id),
            "ends_at": ends.isoformat(),
            "source": source,
        }
        new = _snapshot(Assignment(event_id, person_id, current.vehicle_id, ends))
        return _plan(org, body, apply, current, new)

    request = {"person_id": person_id, "ends_at": ends.isoformat()}
    return _on_current(person_id, EXTEND_COMMAND, request, context, make)


def end_assignment(*, person_id, **context):
    """End the person's current assignment now (END, reason ENDED)."""

    def make(org, current, prior, source, now):
        event_id = new_id()

        def apply(receipt, current_actor):
            _append(
                receipt,
                current_actor,
                event_id=event_id,
                org=org,
                person_id=person_id,
                prior=prior,
                action="END",
                vehicle_id=current.vehicle_id,
                end_reason="ENDED",
                source=source,
            )

        body = {
            "code": "END",
            "event_id": event_id,
            "person_id": person_id,
            "vehicle_id": str(current.vehicle_id),
            "source": source,
        }
        return _plan(org, body, apply, current, {"schema_version": 1, "values": body})

    return _on_current(person_id, END_COMMAND, {"person_id": person_id}, context, make, ending=True)


def end_assignments_for_deactivation(vehicle_id, receipt, current_actor):
    """End everyone's current assignment on a vehicle being deactivated (ASG-3).

    Called from set_vehicle_active inside its command, after the vehicle row is updated and
    while its lock is held; the events carry that command's receipt and reason VEHICLE_INACTIVE.
    """
    org = receipt.organization_id
    ended = []
    for assignment in current_crew(org, vehicle_id, at=receipt.recorded_at):
        prior = _newest(org, assignment.person_id, lock=True)
        actor_id = current_actor.user.pk
        _append(
            receipt,
            current_actor,
            event_id=new_id(),
            org=org,
            person_id=assignment.person_id,
            prior=prior,
            action="END",
            vehicle_id=vehicle_id,
            end_reason="VEHICLE_INACTIVE",
            source="SELF" if actor_id == assignment.person_id else "SUPERVISOR",
        )
        ended.append(str(assignment.person_id))
    return ended


def set_assignment_limit(*, max_hours, **context):
    """Administrator sets the longest an assignment may run (ASG-4, OQ-79)."""
    from cya.kits import definitions

    _ids(context["organization_id"], context["actor_id"])
    if type(max_hours) is not int or not 1 <= max_hours <= MAX_ASSIGNMENT_HOURS:
        raise AssignmentError(
            f"max_hours must be a whole number from 1 to {MAX_ASSIGNMENT_HOURS}",
            code="MALFORMED_REQUEST",
            status=400,
            path="max_hours",
        )
    org = context["organization_id"]

    def build(actor):
        previous = assignment_limit_hours(org)
        if previous == max_hours:
            raise AssignmentError(
                f"the limit is already {max_hours} hours", code="NO_CHANGE", status=409
            )
        revision = VehicleAssignmentLimit.objects.filter(organization_id=org).count() + 1
        limit_id = new_id()

        def apply(receipt, current):
            VehicleAssignmentLimit.objects.create(
                id=limit_id,
                organization_id=org,
                revision=revision,
                max_hours=max_hours,
                **_evidence(receipt, current),
            )

        body = {"code": LIMIT_COMMAND, "revision": revision, "max_hours": max_hours}
        return Plan(
            response_body=body,
            apply=apply,
            target_type="ORGANIZATION",
            target_id=org,
            prior_snapshot={"schema_version": 1, "values": {"max_hours": previous}},
            new_snapshot={"schema_version": 1, "values": body},
        )

    return run_command(
        organization_id=org,
        operation_id=context["operation_id"],
        command_type=LIMIT_COMMAND,
        request_digest=digest({"max_hours": max_hours}),
        correlation_id=context["correlation_id"],
        request_attempt_id=context["request_attempt_id"],
        # The organization row serializes revision numbers.
        authorize=definitions.authorizer(
            org, context["actor_id"], LIMIT_ACTION, lock_organization=True
        ),
        prepare=build,
    )
