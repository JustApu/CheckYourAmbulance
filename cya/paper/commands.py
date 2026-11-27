"""Record a paper-entry batch header (SPEC 8.0.0 LATE-1 to LATE-4, LATE-6; issue #9 Part B1).

record_paper_entry_batch is the audited command behind POST /api/v1/paper-entry-batches. It
saves only the header: what the paper form says once for the whole form. It never records an
item result; each check, reading or seal record of the form is saved afterwards on its own,
naming the batch (LATE-6: "A paper batch header is recorded first ... It does not submit or
commit the batch's item results").

What the header holds and how each part is checked:

- vehicle: a vehicle of the organization, locked for the command, and active.
- checklist version: the vehicle-checklist version printed on the form (LATE-4). It must be a
  version of one of this vehicle's own checklists, published by now, and in force by the
  performance instant (its publication's effective_at is not after it). A later version may
  have replaced it since; the printed version still governs (owner: "use the version printed on
  the paper").
- kit-definition versions: those printed on the kit reference used (LATE-4), each published and
  in force by the performance instant, of a kit definition the printed checklist version places,
  and at most one per definition. None is required: a form with no kit rows names none.
- performance date and time: the wall-clock date and time written on the form, read in the
  organization's station zone (TIME-2) and resolved under TIME-3 (a time in a spring-forward gap
  moves to the first valid instant after it; a repeated fall-back time takes its first
  occurrence). The instant, the zone, the offset and how it was resolved are stored. A time
  after the server's now is refused; there is no age limit and no outage declaration (LATE-2).
- performers: one or more users of the organization, active or not (LATE-3). A Crew member may
  name only themself (enter_own_paper_record); a Supervisor or Administrator may name any users,
  themself included (enter_paper_record_for_others).

The server entry instant and actor come from the command receipt, as for every other record;
they are never taken from the request. The command runs through
cya.core.audit_commands.run_command: one transaction, replay of the stored result for a repeated
operation id, and a success or failure audit event. Do not wrap it in a transaction.
"""

from datetime import date, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from django.utils import timezone

from cya.accounts.fields import new_id
from cya.accounts.models import User
from cya.authorization.backend import canonical_id, load_state
from cya.authorization.guard import PermissionDenied, Principal, require_permission
from cya.authorization.locking import locked_identities
from cya.authorization.persistence import DatabaseRoleBackend, digest
from cya.authorization.policy import Role, permitted
from cya.checklists.models import ChecklistPublication, ChecklistVersion, Vehicle
from cya.core.audit_commands import Actor, CommandDenied, Plan, run_command
from cya.kits.models import ChecklistKitPlacement, KitDefinitionPublication, KitDefinitionVersion
from cya.paper.models import PaperEntryBatch, PaperEntryKitVersion, PaperEntryPerformer
from cya.schedules.engine import resolve_local

COMMAND_TYPE = "PAPER_ENTRY_BATCH_RECORD"
OWN_ACTION = "enter_own_paper_record"
OTHERS_ACTION = "enter_paper_record_for_others"
MAX_PERFORMERS = 20
MAX_KIT_VERSIONS = 50


class PaperEntryError(ValueError):
    """A rule was violated; the command rolls back and is audited as failed.

    code is the stable wire code, status the HTTP status it maps to, path a safe schema path.
    """

    def __init__(self, code, message, *, status=422, path=None):
        super().__init__(message)
        self.code, self.status, self.path = code, status, path


def _malformed(path, message):
    return PaperEntryError("MALFORMED_REQUEST", message, status=400, path=path)


def _id(value, path):
    try:
        return canonical_id(value)
    except (TypeError, ValueError):
        raise _malformed(path, f"{path} must be a canonical lowercase UUID") from None


def _operation_id(value):
    _id(value, "operation_id")
    if UUID(value).version != 4:
        raise _malformed("operation_id", "operation_id must be a version 4 UUID")


def _ids(values, path, *, minimum, maximum):
    if not isinstance(values, list) or not minimum <= len(values) <= maximum:
        raise _malformed(path, f"{path} must list {minimum} to {maximum} ids")
    ids = [_id(value, path) for value in values]
    if len(set(ids)) != len(ids):
        raise _malformed(path, f"{path} names an id more than once")
    return ids


def _shape(vehicle_id, checklist_version_id, performed_on, performed_time, performers, kits):
    if type(performed_on) is not date:
        raise _malformed("performed_on", "performed_on must be a date")
    if type(performed_time) is not time or performed_time.tzinfo is not None:
        raise _malformed("performed_time", "performed_time must be a wall-clock time")
    if performed_time.second or performed_time.microsecond:
        # The paper records hours and minutes (Part A sheet); nothing finer is claimed.
        raise _malformed("performed_time", "performed_time is in whole minutes")
    return (
        _id(vehicle_id, "vehicle_id"),
        _id(checklist_version_id, "checklist_version_id"),
        _ids(performers, "performer_ids", minimum=1, maximum=MAX_PERFORMERS),
        _ids(kits, "kit_definition_version_ids", minimum=0, maximum=MAX_KIT_VERSIONS),
    )


def resolve_performance(performed_on, performed_time, zone):
    """(UTC instant, offset in minutes, resolution) for a wall time in zone (TIME-2, TIME-3)."""
    instant = resolve_local(performed_on, performed_time, zone)
    local = instant.astimezone(ZoneInfo(zone))
    offset = local.utcoffset() // timedelta(minutes=1)
    written = (performed_on, performed_time)
    if (local.date(), local.time().replace(tzinfo=None)) != written:
        return instant, offset, "GAP_FORWARD"
    other_fold = local.replace(fold=1 - local.fold)
    if other_fold.utcoffset() != local.utcoffset():
        return instant, offset, "AMBIGUOUS_FIRST"
    return instant, offset, "EXACT"


def _authorize(organization_id, actor_id, performer_ids):
    backend = DatabaseRoleBackend()
    action = OWN_ACTION if performer_ids == [actor_id] else OTHERS_ACTION

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
            active = user.active and current_org.active
            principal = Principal(organization_id, actor_id, roles, active)
            chosen = action
            if action == OWN_ACTION and not permitted(OWN_ACTION, actor_id, roles, active=active):
                # A Supervisor or Administrator naming only themself uses the attribution row.
                chosen = OTHERS_ACTION
            try:
                require_permission(principal, organization_id, chosen)
            except PermissionDenied:
                raise CommandDenied(actor) from None
            return actor

    return authorize


def _vehicle(org, vehicle_id):
    vehicle = Vehicle.objects.select_for_update().filter(organization_id=org, pk=vehicle_id).first()
    if vehicle is None:
        raise PaperEntryError("NOT_FOUND", "vehicle was not found", status=404, path="vehicle_id")
    if not vehicle.active:
        raise PaperEntryError("VEHICLE_INACTIVE", "vehicle is inactive", path="vehicle_id")
    return vehicle


def _checklist_version(org, vehicle_id, version_id, performed_at, now):
    version = (
        ChecklistVersion.objects.filter(organization_id=org, pk=version_id)
        .select_related("template")
        .first()
    )
    publication = ChecklistPublication.objects.filter(
        organization_id=org, checklist_version_id=version_id, recorded_at__lte=now
    ).first()
    if version is None or publication is None:
        # A draft is not revealed, as for record_check.
        raise PaperEntryError(
            "NOT_FOUND",
            "checklist version was not found or is not published",
            status=404,
            path="checklist_version_id",
        )
    if str(version.template.vehicle_id) != vehicle_id:
        raise PaperEntryError(
            "VERSION_NOT_FOR_VEHICLE",
            "the printed checklist version belongs to another vehicle's checklist",
            path="checklist_version_id",
        )
    if publication.effective_at > performed_at:
        raise PaperEntryError(
            "VERSION_NOT_IN_FORCE",
            "the printed checklist version was not yet in force at the performance time",
            path="checklist_version_id",
        )
    return version


def _kit_versions(org, checklist_version_id, kit_version_ids, performed_at, now):
    if not kit_version_ids:
        return []
    versions = {
        str(v.pk): v
        for v in KitDefinitionVersion.objects.filter(organization_id=org, pk__in=kit_version_ids)
    }
    publications = KitDefinitionPublication.objects.filter(
        organization_id=org, version_id__in=kit_version_ids, recorded_at__lte=now
    ).values_list("version_id", "effective_at")
    published = {str(v) for v, _ in publications}
    in_force = {str(v) for v, effective_at in publications if effective_at <= performed_at}
    placed = {
        str(d)
        for d in ChecklistKitPlacement.objects.filter(
            checklist_version_id=checklist_version_id
        ).values_list("definition_id", flat=True)
    }
    definitions = set()
    for version_id in kit_version_ids:
        version = versions.get(version_id)
        if version is None or version_id not in published:
            raise PaperEntryError(
                "NOT_FOUND",
                "kit definition version was not found or is not published",
                status=404,
                path="kit_definition_version_ids",
            )
        if version_id not in in_force:
            raise PaperEntryError(
                "KIT_VERSION_NOT_IN_FORCE",
                "a printed kit definition version was not yet in force at the performance time",
                path="kit_definition_version_ids",
            )
        definition = str(version.definition_id)
        if definition not in placed:
            raise PaperEntryError(
                "KIT_VERSION_NOT_IN_CHECKLIST",
                "a printed kit definition is not placed by the printed checklist version",
                path="kit_definition_version_ids",
            )
        if definition in definitions:
            raise PaperEntryError(
                "KIT_VERSION_AMBIGUOUS",
                "two printed versions of one kit definition were named",
                path="kit_definition_version_ids",
            )
        definitions.add(definition)
    return kit_version_ids


def _performers(org, performer_ids):
    found = {
        str(pk)
        for pk in User.objects.filter(organization_id=org, pk__in=performer_ids).values_list(
            "pk", flat=True
        )
    }
    if found != set(performer_ids):
        raise PaperEntryError(
            "NOT_FOUND", "a performer was not found", status=404, path="performer_ids"
        )


def record_paper_entry_batch(
    *,
    vehicle_id,
    checklist_version_id,
    performed_on,
    performed_time,
    performer_ids,
    kit_definition_version_ids=(),
    **context,
):
    """Save one paper-entry batch header; returns the run_command Result (status 201)."""
    org = context["organization_id"]
    actor_id = canonical_id(context["actor_id"])
    _operation_id(context["operation_id"])
    vehicle_id, version_id, performer_ids, kit_ids = _shape(
        vehicle_id,
        checklist_version_id,
        performed_on,
        performed_time,
        performer_ids,
        list(kit_definition_version_ids),
    )

    def build(actor):
        _vehicle(org, vehicle_id)
        # The entry instant is read only after the vehicle lock is held.
        now = timezone.now()
        zone = actor.user.organization.station_zone
        performed_at, offset, resolution = resolve_performance(performed_on, performed_time, zone)
        if performed_at > now:
            raise PaperEntryError(
                "PERFORMED_IN_FUTURE",
                "the performance time is later than now",
                path="performed_time",
            )
        _checklist_version(org, vehicle_id, version_id, performed_at, now)
        _kit_versions(org, version_id, kit_ids, performed_at, now)
        _performers(org, performer_ids)
        batch_id = new_id()

        def apply(receipt, current):
            evidence = {
                "organization_id": org,
                "command": receipt,
                "recorded_at": receipt.recorded_at,
                "actor_user": current.user,
                "actor_roles_at_time": list(current.roles),
            }
            PaperEntryBatch.objects.create(
                id=batch_id,
                vehicle_id=vehicle_id,
                checklist_version_id=version_id,
                performed_at=performed_at,
                performed_local_date=performed_on,
                performed_local_time=performed_time,
                performed_zone=zone,
                performed_utc_offset_minutes=offset,
                performed_resolution=resolution,
                **evidence,
            )
            PaperEntryPerformer.objects.bulk_create(
                PaperEntryPerformer(batch_id=batch_id, performer_id=user_id, **evidence)
                for user_id in performer_ids
            )
            PaperEntryKitVersion.objects.bulk_create(
                PaperEntryKitVersion(batch_id=batch_id, kit_definition_version_id=v, **evidence)
                for v in kit_ids
            )

        values = {
            "batch_id": batch_id,
            "vehicle_id": vehicle_id,
            "checklist_version_id": version_id,
            "kit_definition_version_ids": kit_ids,
            "performer_ids": performer_ids,
            "performed_at": performed_at.isoformat(),
            "performed_local": f"{performed_on.isoformat()}T{performed_time.strftime('%H:%M')}",
            "performed_zone": zone,
            "performed_utc_offset_minutes": offset,
            "performed_resolution": resolution,
        }
        return Plan(
            response_body={"code": "PAPER_ENTRY_BATCH_RECORDED", **values},
            response_status=201,
            apply=apply,
            target_type="ORGANIZATION",
            target_id=org,
            new_snapshot={"schema_version": 1, "values": values},
            recorded_at=now,
        )

    request = {
        "vehicle_id": vehicle_id,
        "checklist_version_id": version_id,
        "performed_on": performed_on.isoformat(),
        "performed_time": performed_time.strftime("%H:%M"),
        "performer_ids": performer_ids,
        "kit_definition_version_ids": kit_ids,
    }
    return run_command(
        organization_id=org,
        operation_id=context["operation_id"],
        command_type=COMMAND_TYPE,
        request_digest=digest(request),
        correlation_id=context["correlation_id"],
        request_attempt_id=context["request_attempt_id"],
        authorize=_authorize(org, actor_id, performer_ids),
        prepare=build,
    )
