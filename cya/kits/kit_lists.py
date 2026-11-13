"""Audited commands for vehicle kit lists (SPEC 4.6 KDEF-9, issue #129).

A vehicle's kit list states the exact number of instances of each kit definition the vehicle
should carry in each compartment slot. It is versioned like a checklist (CHK-2, CHK-3): every
save inserts the next draft revision with its complete set of lines, a publication freezes one
revision at an effective instant never earlier than the publication, and one vehicle's
publications are strictly ordered by effective instant. An empty list is allowed: it says the
vehicle should carry no kits.

All commands need the policy action manage_vehicle_kit_lists (Supervisors and Administrators,
SPEC 3.3) and run through cya.core.audit_commands.run_command; do not wrap them in a
transaction. The vehicle row is locked FOR UPDATE first, so concurrent saves get distinct
revisions and publishes are ordered; publishing then locks the listed definitions in id order so
a concurrent deactivation cannot slip between the check and the publication.
"""

from datetime import datetime

from django.db.models import Max

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id
from cya.authorization.persistence import digest
from cya.checklists.models import Compartment, Vehicle
from cya.checklists.validation import is_count
from cya.core.audit_commands import Plan, run_command
from cya.kits import definitions
from cya.kits.models import (
    MAX_REQUIRED_COUNT,
    KitDefinition,
    VehicleKitListPublication,
    VehicleKitListVersion,
    VehicleKitRequirement,
)

ACTION = "manage_vehicle_kit_lists"
DRAFT_COMMAND = "VEHICLE_KIT_LIST_DRAFT"
PUBLISH_COMMAND = "VEHICLE_KIT_LIST_PUBLISH"
MAX_LINES = 200
LINE_FIELDS = frozenset({"definition_id", "compartment_id", "required_count", "display_order"})


class KitListError(ValueError):
    """A rule was violated; the command rolls back and is audited as failed."""


def _run(context, command_type, request, build):
    canonical_id(context["actor_id"])
    return run_command(
        organization_id=context["organization_id"],
        operation_id=context["operation_id"],
        command_type=command_type,
        request_digest=digest(request),
        correlation_id=context["correlation_id"],
        request_attempt_id=context["request_attempt_id"],
        authorize=definitions.authorizer(context["organization_id"], context["actor_id"], ACTION),
        prepare=build,
    )


def _plan(organization_id, body, apply, **values):
    return Plan(
        response_body=body,
        apply=apply,
        target_type="ORGANIZATION",
        target_id=organization_id,
        new_snapshot={"schema_version": 1, "values": values},
    )


def _locked_vehicle(org, vehicle_id):
    vehicle = Vehicle.objects.select_for_update().filter(organization_id=org, pk=vehicle_id).first()
    if vehicle is None:
        raise KitListError(f"vehicle {vehicle_id} does not exist in the organization")
    return vehicle


def _line(org, vehicle, index, entry):
    where = f"lines[{index}]"
    if not isinstance(entry, dict) or set(entry) != LINE_FIELDS:
        raise KitListError(f"{where} must have exactly {sorted(LINE_FIELDS)}")
    for field in ("definition_id", "compartment_id"):
        try:
            canonical_id(entry[field])
        except (TypeError, ValueError):
            raise KitListError(f"{where}.{field} is not an identifier") from None
    count = entry["required_count"]
    if not is_count(count) or not 1 <= count <= MAX_REQUIRED_COUNT:
        raise KitListError(
            f"{where}.required_count must be a whole number 1 to {MAX_REQUIRED_COUNT}"
        )
    if not is_count(entry["display_order"]):
        raise KitListError(f"{where}.display_order must be a whole number")
    definition = KitDefinition.objects.filter(
        organization_id=org, pk=entry["definition_id"]
    ).first()
    if definition is None:
        raise KitListError(f"{where}.definition_id does not exist in the organization")
    if not definition.active:
        raise KitListError(f"{where}: kit definition {definition.key!r} is inactive")
    compartment = Compartment.objects.filter(
        organization_id=org, pk=entry["compartment_id"], vehicle=vehicle
    ).first()
    if compartment is None:
        raise KitListError(f"{where}.compartment_id is not a compartment of this vehicle")
    return {
        "definition_id": str(definition.pk),
        "compartment": compartment,
        "required_count": count,
        "display_order": entry["display_order"],
    }


def validate_lines(org, vehicle, lines):
    """The validated lines of a kit list draft; at most one line per slot and definition."""
    if not isinstance(lines, list):
        raise KitListError("lines must be a list")
    if len(lines) > MAX_LINES:
        raise KitListError(f"a kit list has at most {MAX_LINES} lines")
    validated, seen = [], set()
    for index, entry in enumerate(lines):
        line = _line(org, vehicle, index, entry)
        key = (str(line["compartment"].stable_slot_id), line["definition_id"])
        if key in seen:
            raise KitListError(f"lines[{index}] repeats a definition in the same compartment")
        seen.add(key)
        validated.append(line)
    return validated


def save_vehicle_kit_list_draft(*, vehicle_id, lines, **context):
    """Insert the next draft revision of a vehicle's kit list with its complete lines (KDEF-9)."""
    canonical_id(vehicle_id)
    org = context["organization_id"]

    def build(actor):
        vehicle = _locked_vehicle(org, vehicle_id)
        validated = validate_lines(org, vehicle, lines)
        top = VehicleKitListVersion.objects.filter(vehicle=vehicle).aggregate(m=Max("revision"))
        revision, version_id = (top["m"] or 0) + 1, new_id()

        def apply(receipt, _current):
            VehicleKitListVersion.objects.create(
                id=version_id,
                organization_id=org,
                vehicle=vehicle,
                revision=revision,
                created_at=receipt.recorded_at,
            )
            VehicleKitRequirement.objects.bulk_create(
                VehicleKitRequirement(
                    id=new_id(), organization_id=org, version_id=version_id, **line
                )
                for line in validated
            )

        body = {
            "code": "VEHICLE_KIT_LIST_DRAFT_SAVED",
            "version_id": version_id,
            "revision": revision,
            "line_count": len(validated),
        }
        return _plan(org, body, apply, vehicle_id=vehicle_id, **body)

    request = {"vehicle_id": vehicle_id, "lines": lines}
    return _run(context, DRAFT_COMMAND, request, build)


def _aware(value):
    if value is not None and (not isinstance(value, datetime) or value.utcoffset() is None):
        raise KitListError("effective_at must be a timezone-aware datetime")
    return value


def check_listed_definitions(version):
    """Every listed definition must be active; locks them in id order until the command commits.

    A concurrent set_kit_definition_active locks the same row, so it cannot deactivate one
    between this check and the publication.
    """
    listed = set(
        VehicleKitRequirement.objects.filter(version=version).values_list(
            "definition_id", flat=True
        )
    )
    for definition in (
        KitDefinition.objects.select_for_update().filter(pk__in=listed).order_by("pk")
    ):
        if not definition.active:
            raise KitListError(f"listed kit definition {definition.key!r} is inactive")


def publish_vehicle_kit_list(*, version_id, effective_at=None, **context):
    """Publish a kit list draft at effective_at (None: when recorded), never earlier (KDEF-9)."""
    canonical_id(version_id)
    org = context["organization_id"]
    requested = _aware(effective_at)

    def build(actor):
        version = VehicleKitListVersion.objects.filter(organization_id=org, pk=version_id).first()
        if version is None:
            raise KitListError(f"vehicle kit list version {version_id} does not exist")
        vehicle = _locked_vehicle(org, version.vehicle_id)
        if VehicleKitListPublication.objects.filter(version=version).exists():
            raise KitListError("vehicle kit list version is already published")
        check_listed_definitions(version)
        publication_id = new_id()

        def apply(receipt, current):
            final = requested or receipt.recorded_at
            if final < receipt.recorded_at:
                raise KitListError("effective_at must not be earlier than the publication")
            latest = (
                VehicleKitListPublication.objects.filter(version__vehicle=vehicle)
                .order_by("-effective_at")
                .values_list("effective_at", flat=True)
                .first()
            )
            if latest is not None and final <= latest:
                raise KitListError(
                    f"effective_at must be later than the latest published effective instant {latest}"
                )
            VehicleKitListPublication.objects.create(
                id=publication_id,
                organization_id=org,
                version=version,
                effective_at=final,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current.user,
                actor_roles_at_time=list(current.roles),
            )

        body = {
            "code": "VEHICLE_KIT_LIST_PUBLISHED",
            "publication_id": publication_id,
            "version_id": version_id,
        }
        return _plan(org, body, apply, vehicle_id=str(vehicle.pk), **body)

    request = {
        "version_id": version_id,
        "effective_at": requested.isoformat() if requested else None,
    }
    return _run(context, PUBLISH_COMMAND, request, build)


def kit_list_effective_at(organization_id, vehicle_id, at, recorded_before=None):
    """The vehicle's published kit list version in force at `at`, or None (KDEF-9, KDEF-10).

    With recorded_before, only publications recorded strictly before it count.
    """
    publications = VehicleKitListPublication.objects.filter(
        organization_id=organization_id, version__vehicle_id=vehicle_id, effective_at__lte=at
    )
    if recorded_before is not None:
        publications = publications.filter(recorded_at__lt=recorded_before)
    publication = publications.order_by("-effective_at").first()
    return publication.version if publication else None
