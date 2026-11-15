"""Vehicle lifecycle: create, edit, activate and deactivate (SPEC 4.8 CFG-1, issue #130 part 2).

Supervisors and Administrators (policy action manage_vehicles, SPEC 3.3) create a vehicle
with its VIN (IMP-6, SPEC 4.0.0), edit its name and number, and deactivate or reactivate it.
A vehicle is never deleted. Each activity change appends a vehicle_activity_event in the same
command as the vehicle row's flag, so the flag always has a history: obligations due while a
vehicle is inactive are exempt (cya.obligations.state), and reactivation never backfills.

Vehicles from before this history get one BASELINE event from record_activity_baseline, run
once per organization at deploy time as a service identity. set_vehicle_active refuses a
vehicle without events, so a baseline is never written after a change.

Locks follow docs/configuration-management.md: the organization row when a rule spans it
(VIN uniqueness, the baseline), then the vehicle row.
"""

import logging

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id
from cya.authorization.persistence import digest
from cya.checklists.models import MAX_KEY, Vehicle
from cya.core.audit_commands import Actor, CommandDenied, Plan, run_command
from cya.kits import definitions
from cya.kits import keys as key_rules
from cya.operations.models import VehicleActivityEvent
from tools.command_identity import worker_operation_id

logger = logging.getLogger(__name__)

ACTION = "manage_vehicles"
CREATE_COMMAND = "VEHICLE_CREATE"
UPDATE_COMMAND = "VEHICLE_UPDATE"
ACTIVE_COMMAND = "VEHICLE_ACTIVE_SET"
BASELINE_COMMAND = "VEHICLE_ACTIVITY_BASELINE"
BASELINE_SERVICE = "deploy.vehicle_activity_baseline"
BASELINE_JOB = '["vehicle-activity-baseline"]'
MAX_NAME = 200
MAX_NUMBER = 50
UNSET = object()

assert MAX_KEY >= 17  # a VIN fits the key column


class VehicleError(ValueError):
    """A rule was violated; the command rolls back and is audited as failed."""


def _name(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_NAME:
        raise VehicleError(f"display name must be nonblank text of at most {MAX_NAME} characters")
    return value.strip()


def _number(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_NUMBER:
        raise VehicleError(f"number must be empty or text of at most {MAX_NUMBER} characters")
    return value.strip()


def _locked(organization_id, vehicle_id):
    vehicle = (
        Vehicle.objects.select_for_update()
        .filter(organization_id=organization_id, pk=vehicle_id)
        .first()
    )
    if vehicle is None:
        raise VehicleError(f"vehicle {vehicle_id} does not exist in the organization")
    return vehicle


def _run(context, command_type, request, build, *, lock_organization=False):
    canonical_id(context["actor_id"])
    return run_command(
        organization_id=context["organization_id"],
        operation_id=context["operation_id"],
        command_type=command_type,
        request_digest=digest(request),
        correlation_id=context["correlation_id"],
        request_attempt_id=context["request_attempt_id"],
        authorize=definitions.authorizer(
            context["organization_id"],
            context["actor_id"],
            ACTION,
            lock_organization=lock_organization,
        ),
        prepare=build,
    )


def _plan(organization_id, body, apply, prior=None):
    return Plan(
        response_body=body,
        apply=apply,
        target_type="ORGANIZATION",
        target_id=organization_id,
        prior_snapshot={"schema_version": 1, "values": prior} if prior is not None else None,
        new_snapshot={"schema_version": 1, "values": body},
    )


def append_activity(vehicle_id, active, basis, receipt, current, event_id=None):
    """Append the vehicle's next activity event under this receipt (the caller holds its lock)."""
    prior = VehicleActivityEvent.objects.filter(vehicle_id=vehicle_id).order_by("-sequence").first()
    return VehicleActivityEvent.objects.create(
        id=event_id or new_id(),
        organization_id=receipt.organization_id,
        vehicle_id=vehicle_id,
        sequence=prior.sequence + 1 if prior else 1,
        prior_event=prior,
        active=active,
        basis=basis,
        command=receipt,
        recorded_at=receipt.recorded_at,
        actor_user=current.user,
        actor_roles_at_time=list(current.roles),
    )


def create_vehicle(*, vin, display_name, number=None, **context):
    """Create an active vehicle with its VIN (IMP-6) and its first activity event (CFG-1)."""
    vin = key_rules.check_vin(vin)
    name, number = _name(display_name), _number(number)
    org = context["organization_id"]

    def build(actor):
        if Vehicle.objects.filter(organization_id=org, key=vin).exists():
            raise VehicleError("another vehicle has that VIN")
        vehicle_id = new_id()

        def apply(receipt, current):
            Vehicle.objects.create(
                id=vehicle_id,
                organization_id=org,
                display_name=name,
                number=number,
                active=True,
                key=vin,
            )
            append_activity(vehicle_id, True, "CHANGE", receipt, current)

        body = {
            "code": CREATE_COMMAND,
            "vehicle_id": vehicle_id,
            "vin": vin,
            "display_name": name,
            "number": number,
        }
        return _plan(org, body, apply)

    request = {"vin": vin, "display_name": name, "number": number}
    # VIN uniqueness spans the organization: take its lock first.
    return _run(context, CREATE_COMMAND, request, build, lock_organization=True)


def update_vehicle(*, vehicle_id, display_name=UNSET, number=UNSET, **context):
    """Change a vehicle's display name and/or number (CFG-1); the VIN never changes."""
    canonical_id(vehicle_id)
    wanted = {}
    if display_name is not UNSET:
        wanted["display_name"] = _name(display_name)
    if number is not UNSET:
        wanted["number"] = _number(number)
    if not wanted:
        raise VehicleError("name at least one of display_name and number")
    org = context["organization_id"]

    def build(actor):
        vehicle = _locked(org, vehicle_id)
        prior = {"display_name": vehicle.display_name, "number": vehicle.number}
        if all(prior[name] == value for name, value in wanted.items()):
            raise VehicleError("the vehicle already has those values")

        def apply(_receipt, _current):
            Vehicle.objects.filter(pk=vehicle.pk).update(**wanted)

        body = {"code": UPDATE_COMMAND, "vehicle_id": vehicle_id, **prior, **wanted}
        return _plan(org, body, apply, prior={"vehicle_id": vehicle_id, **prior})

    return _run(context, UPDATE_COMMAND, {"vehicle_id": vehicle_id, **wanted}, build)


def set_vehicle_active(*, vehicle_id, active, **context):
    """Deactivate or reactivate a vehicle, effective when recorded (CFG-1).

    From a deactivation on, the vehicle owes no routine obligation; reactivation resumes
    obligations from its instant and nothing is owed for the inactive period. Refused for a
    vehicle without an activity history (run record_activity_baseline first).
    """
    canonical_id(vehicle_id)
    if type(active) is not bool:
        raise VehicleError("active must be true or false")
    org = context["organization_id"]

    def build(actor):
        vehicle = _locked(org, vehicle_id)
        if not VehicleActivityEvent.objects.filter(vehicle_id=vehicle_id).exists():
            raise VehicleError(
                "the vehicle has no activity history yet; record the activity baseline first"
            )
        if vehicle.active is active:
            raise VehicleError(f"the vehicle is already {'active' if active else 'inactive'}")
        event_id = new_id()

        def apply(receipt, current):
            # The vehicle row first: the event's guard checks that the two agree.
            Vehicle.objects.filter(pk=vehicle.pk).update(active=active)
            append_activity(vehicle_id, active, "CHANGE", receipt, current, event_id)
            # Owed obligations change: open screens refresh (delivered only if this commits).
            from cya.live_updates.publish import publish_vehicle_update

            publish_vehicle_update(
                organization_id=org,
                vehicle_id=vehicle_id,
                event_id=event_id,
                kind="obligations.changed",
            )

        body = {
            "code": ACTIVE_COMMAND,
            "vehicle_id": vehicle_id,
            "active": active,
            "event_id": event_id,
        }
        return _plan(org, body, apply, prior={"vehicle_id": vehicle_id, "active": vehicle.active})

    result = _run(context, ACTIVE_COMMAND, {"vehicle_id": vehicle_id, "active": active}, build)
    if not result.replayed:
        # Obligations reconcile after commit, as for bindings (a deactivated vehicle generates
        # nothing new; a reactivated one is generated from now on). A unit that fails is
        # logged, as binding and kit commands do; the scheduled run repairs it.
        _reconcile_after(org, vehicle_id)
    return result


def _reconcile_after(organization_id, vehicle_id):
    from cya.obligations.generation import reconcile_vehicle

    summary = reconcile_vehicle(organization_id, vehicle_id)
    for failure in summary.failures:
        logger.error(
            "obligation reconciliation failed after vehicle activity change: vehicle %s due %s: %s",
            failure.vehicle_id,
            failure.due_at.isoformat(),
            failure.error,
        )


def record_activity_baseline(organization_id):
    """Give every vehicle without activity events one BASELINE event (deploy time, idempotent).

    Runs as a service identity with a fixed operation id per organization, so a rerun replays
    the original result instead of writing again.
    """
    org = str(organization_id)
    canonical_id(org)

    def authorize(organization):
        if not organization.active:
            raise CommandDenied()
        from cya.accounts.models import Organization

        Organization.objects.select_for_update().get(pk=org)
        return Actor(service_identity=BASELINE_SERVICE, job_key=BASELINE_JOB)

    def build(actor):
        with_events = VehicleActivityEvent.objects.filter(organization_id=org).values("vehicle_id")
        pending = list(
            Vehicle.objects.select_for_update()
            .filter(organization_id=org)
            .exclude(pk__in=with_events)
            .order_by("pk")
            .values_list("pk", "active")
        )

        def apply(receipt, current):
            for vehicle_id, active in pending:
                append_activity(str(vehicle_id), active, "BASELINE", receipt, current)

        body = {
            "code": BASELINE_COMMAND,
            "vehicles": len(pending),
            "inactive": sum(1 for _pk, active in pending if not active),
        }
        return _plan(org, body, apply)

    return run_command(
        organization_id=org,
        operation_id=str(
            worker_operation_id(org, BASELINE_SERVICE, BASELINE_COMMAND, BASELINE_JOB)
        ),
        command_type=BASELINE_COMMAND,
        request_digest=digest({"organization_id": org}),
        correlation_id=new_id(),
        request_attempt_id=new_id(),
        authorize=authorize,
        prepare=build,
    )


def inactive_at(organization_id, vehicle_id, instant):
    """The activity event that made the vehicle inactive as of `instant`, else None.

    Judged by the vehicle's latest activity event recorded by then. A vehicle without events
    is judged as before this history existed: never inactive (generation still skips a vehicle
    whose flag is false).
    """
    event = (
        VehicleActivityEvent.objects.filter(
            organization_id=organization_id, vehicle_id=vehicle_id, recorded_at__lte=instant
        )
        .order_by("-sequence")
        .first()
    )
    return event if event is not None and not event.active else None
