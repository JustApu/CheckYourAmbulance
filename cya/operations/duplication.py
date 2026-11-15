"""Duplicating a checklist template or a vehicle (SPEC 4.8 CFG-3, CFG-4; issue #130 part 4).

Supervisors and Administrators (policy action duplicate_configuration, SPEC 3.3). The creator
enters the new record's key (SPEC 4.0.0 IMP-6), so both commands take the organization lock
first: keys are unique across the organization. See docs/configuration-management.md.
"""

from dataclasses import dataclass, field

from django.utils import timezone

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id
from cya.authorization.persistence import digest
from cya.checklists.models import (
    ChecklistItemDef,
    ChecklistTemplate,
    ChecklistVersion,
    Compartment,
    Vehicle,
    VehicleOverrideItem,
    VehicleOverrideVersion,
    VehicleSystem,
)
from cya.checklists.overlay import (
    NoEffectiveVersionError,
    StaleOverrideError,
    governing_versions,
)
from cya.checklists.validation import validate_definitions
from cya.kits import keys as key_rules
from cya.kits.kit_lists import kit_list_effective_at
from cya.kits.models import (
    ChecklistKitPlacement,
    VehicleKitListVersion,
    VehicleKitRequirement,
)
from cya.kits.placements import placements_of, validate_placements
from cya.obligations.models import RequirementBinding
from cya.operations.vehicles import (
    MAX_NAME,
    VehicleError,
    _locked,
    _name,
    _number,
    _plan,
    _run,
    append_activity,
)

ACTION = "duplicate_configuration"
TEMPLATE_COMMAND = "TEMPLATE_DUPLICATE"
VEHICLE_COMMAND = "VEHICLE_DUPLICATE"
SLOT_MODELS = (("COMPARTMENT", Compartment), ("SYSTEM", VehicleSystem))
# Never copied (SPEC 4.8 CFG-4); the preview lists these with anything the plan had to leave out.
NEVER_COPIED = (
    "the VIN",
    "kit instances and equipment",
    "checks, obligations and deficiencies",
    "seals",
    "operational status",
    "odometer and engine hours",
    "every history",
)
# The item definition fields a draft is saved with (cya.checklists.validation.FIELDS).
DEFINITION_FIELDS = (
    "item_id",
    "display_order",
    "kind",
    "instructions",
    "expected_quantity",
    "unit",
    "allowed_outcomes",
    "expiration_tracked",
    "compartment_id",
    "system_id",
    "kit_id",
)

assert MAX_NAME >= 200  # a template label fits the same bound


class DuplicationError(VehicleError):
    """A duplication rule was violated; the command rolls back and is audited as failed."""


def _specs(version):
    """The source version's item definitions as draft specs, in display order."""
    specs = []
    for definition in ChecklistItemDef.objects.filter(checklist_version=version).order_by(
        "display_order", "id"
    ):
        spec = {name: getattr(definition, name) for name in DEFINITION_FIELDS}
        for name in ("item_id", "compartment_id", "system_id", "kit_id"):
            spec[name] = str(spec[name]) if spec[name] is not None else None
        spec["allowed_outcomes"] = list(spec["allowed_outcomes"])
        specs.append(spec)
    return specs


def duplicate_template(*, source_version_id, label, key, **context):
    """A new template with its creator's label and key, and one draft copying a version (CFG-3).

    The draft copies the chosen version's items, placements and settings through the same
    validation as save_template_draft. Nothing else is copied: no publication, override,
    binding, obligation or history. The source is unchanged.
    """
    canonical_id(source_version_id)
    name = _name(label)
    key = key_rules.check_key(key, "key")
    org = context["organization_id"]

    def build(actor):
        source = (
            ChecklistVersion.objects.filter(organization_id=org, pk=source_version_id)
            .select_related("template")
            .first()
        )
        if source is None:
            raise DuplicationError(f"checklist version {source_version_id} does not exist")
        if ChecklistTemplate.objects.filter(organization_id=org, key=key).exists():
            raise DuplicationError("another template has that key")
        definitions = validate_definitions(org, _specs(source))
        placed = validate_placements(
            org,
            [
                {"definition_id": str(p.definition_id), "display_order": p.display_order}
                for p in placements_of(source.pk)
            ],
        )
        template_id, version_id = new_id(), new_id()

        def apply(receipt, _current):
            ChecklistTemplate.objects.create(
                id=template_id, organization_id=org, label=name, active=True, key=key
            )
            ChecklistVersion.objects.create(
                id=version_id,
                organization_id=org,
                template_id=template_id,
                revision=1,
                created_at=receipt.recorded_at,
            )
            ChecklistItemDef.objects.bulk_create(
                ChecklistItemDef(
                    id=new_id(), organization_id=org, checklist_version_id=version_id, **fields
                )
                for fields in definitions
            )
            ChecklistKitPlacement.objects.bulk_create(
                ChecklistKitPlacement(
                    id=new_id(), organization_id=org, checklist_version_id=version_id, **fields
                )
                for fields in placed
            )

        body = {
            "code": TEMPLATE_COMMAND,
            "template_id": template_id,
            "version_id": version_id,
            "key": key,
            "label": name,
            "source_template_id": str(source.template_id),
            "source_version_id": source_version_id,
            "item_count": len(definitions),
            "placement_count": len(placed),
        }
        return _plan(org, body, apply)

    request = {"source_version_id": source_version_id, "label": name, "key": key}
    # Template keys are unique across the organization: take its lock first.
    return _run(context, TEMPLATE_COMMAND, request, build, lock_organization=True, action=ACTION)


# --- Vehicles (CFG-4) ------------------------------------------------------------------------


@dataclass
class VehicleCopyPlan:
    """What duplicating a vehicle copies, read without writing. Ids are the source's."""

    source: Vehicle
    positions: list = field(default_factory=list)  # (kind, row in effect now, not retired)
    kit_lines: list = field(default_factory=list)  # VehicleKitRequirement rows in force
    overrides: list = field(default_factory=list)  # (template_id, override, [(entry, def)])
    bindings: list = field(default_factory=list)  # (template_id, slot)
    excluded: list = field(default_factory=list)  # what the copy leaves out, and why

    def shown(self):
        """The preview, and the basis of its token (CFG-6): every row the copy would take."""
        return {
            "source_vehicle_id": str(self.source.pk),
            "positions": [
                {
                    "kind": kind,
                    "stable_slot_id": str(row.stable_slot_id),
                    "label": row.label,
                    "display_order": row.display_order,
                    "sealable": bool(getattr(row, "sealable", False)),
                }
                for kind, row in self.positions
            ],
            "kit_lines": [
                {
                    "definition_id": str(line.definition_id),
                    "stable_slot_id": str(line.compartment.stable_slot_id),
                    "required_count": line.required_count,
                    "display_order": line.display_order,
                }
                for line in self.kit_lines
            ],
            "overrides": [
                {
                    "template_id": template_id,
                    "override_version_id": str(override.pk),
                    "template_version_id": str(override.template_version_id),
                    "entries": [str(entry.pk) for entry, _definition in entries],
                }
                for template_id, override, entries in self.overrides
            ],
            "bindings": [{"template_id": t, "slot": s} for t, s in self.bindings],
            "excluded": list(self.excluded),
            "never_copied": list(NEVER_COPIED),
        }

    def token(self):
        return digest(self.shown())


def _positions_now(vehicle, now):
    """(kind, row) for each position of the vehicle in effect now, retired ones left out."""
    found = []
    for kind, model in SLOT_MODELS:
        latest = {}
        for row in model.objects.filter(vehicle=vehicle, effective_at__lte=now).order_by(
            "stable_slot_id", "revision"
        ):
            latest[row.stable_slot_id] = row
        for row in sorted(
            latest.values(), key=lambda r: (r.display_order is None, r.display_order or 0, r.label)
        ):
            if not row.retired:
                found.append((kind, row))
    return found


def _bindings_now(org, vehicle_id, now):
    """(template_id, slot) of each binding in force now (its revision in force is a BIND)."""
    in_force = {}
    for row in RequirementBinding.objects.filter(
        organization_id=org, vehicle_id=vehicle_id, effective_at__lte=now
    ).order_by("template_id", "slot", "revision"):
        in_force[(str(row.template_id), row.slot)] = row.action
    return sorted(key for key, action in in_force.items() if action == "BIND")


def plan_vehicle_copy(organization_id, source_vehicle_id, now=None):
    """The copy's plan (CFG-4), reading only. Rows tied to the source alone are left out."""
    org = str(organization_id)
    now = now or timezone.now()
    source = Vehicle.objects.filter(organization_id=org, pk=source_vehicle_id).first()
    if source is None:
        raise DuplicationError(f"vehicle {source_vehicle_id} does not exist in the organization")
    plan = VehicleCopyPlan(source=source)
    plan.positions = _positions_now(source, now)
    copied_slots = {(kind, str(row.stable_slot_id)) for kind, row in plan.positions}
    slot_of = {}  # any revision row id of the source -> (kind, stable slot)
    for kind, model in SLOT_MODELS:
        for pk, slot in model.objects.filter(vehicle=source).values_list("pk", "stable_slot_id"):
            slot_of[str(pk)] = (kind, str(slot))

    in_force = kit_list_effective_at(org, str(source.pk), now)
    if in_force is not None:
        for line in (
            VehicleKitRequirement.objects.filter(version=in_force)
            .select_related("compartment", "definition")
            .order_by("display_order", "id")
        ):
            if slot_of.get(str(line.compartment_id)) in copied_slots:
                plan.kit_lines.append(line)
            else:
                plan.excluded.append(
                    f"kit list line {line.definition.key}: its compartment is retired"
                )

    plan.bindings = _bindings_now(org, str(source.pk), now)
    for template_id in sorted({t for t, _slot in plan.bindings}):
        try:
            _version, override = governing_versions(org, str(source.pk), template_id, now)
        except NoEffectiveVersionError:
            continue  # no version in force, so no override in force either
        except StaleOverrideError:
            plan.excluded.append(
                f"the override of template {template_id}: it overlays an older template version"
            )
            continue
        if override is None:
            continue
        entries = []
        for entry in (
            VehicleOverrideItem.objects.filter(override_version=override)
            .select_related("definition", "item")
            .order_by("id")
        ):
            definition = entry.definition
            if definition is not None and definition.kit_id is not None:
                plan.excluded.append(
                    f"override entry for {entry.item.label}: it names one of the source's kits"
                )
                continue
            row = definition and (definition.compartment_id or definition.system_id)
            if row is not None and slot_of.get(str(row)) not in copied_slots:
                plan.excluded.append(
                    f"override entry for {entry.item.label}: its position is retired"
                )
                continue
            entries.append((entry, definition))
        plan.overrides.append((template_id, override, entries))
    return plan


def preview_vehicle_duplicate(organization_id, source_vehicle_id):
    """What duplicating the vehicle would copy and leave out, with the token to confirm (CFG-6)."""
    canonical_id(str(organization_id))
    canonical_id(source_vehicle_id)
    plan = plan_vehicle_copy(organization_id, source_vehicle_id)
    return {**plan.shown(), "base_token": plan.token()}


def _copy_definition(org, definition, override_version_id, row_for):
    """The source override definition, owned by the copy's override and placed in its rows."""
    values = {name: getattr(definition, name) for name in DEFINITION_FIELDS}
    for name in ("compartment_id", "system_id"):
        if values[name] is not None:
            values[name] = row_for[str(values[name])]
    return ChecklistItemDef.objects.create(
        id=new_id(),
        organization_id=org,
        override_version_id=override_version_id,
        bulk_eligible=definition.bulk_eligible,
        **values,
    )


def duplicate_vehicle(*, source_vehicle_id, vin, display_name, number=None, base_token, **context):
    """Copy a vehicle's configuration to a new, inactive vehicle (SPEC 4.8 CFG-4, CFG-6).

    Applies exactly the plan its preview showed (the same base_token), or refuses.
    """
    canonical_id(source_vehicle_id)
    vin = key_rules.check_vin(vin)
    name, number = _name(display_name), _number(number)
    if not isinstance(base_token, str) or not base_token:
        raise DuplicationError("base_token is required: preview the duplication first")
    org = context["organization_id"]

    def build(actor):
        # The organization is locked first (by the authorizer), then the source vehicle.
        _locked(org, source_vehicle_id)
        if Vehicle.objects.filter(organization_id=org, key=vin).exists():
            raise DuplicationError("another vehicle has that VIN")
        plan = plan_vehicle_copy(org, source_vehicle_id)
        if plan.token() != base_token:
            raise DuplicationError(
                "the source's configuration has changed since the preview; preview again"
            )
        vehicle_id = new_id()
        templates = sorted({t for t, _slot in plan.bindings})

        def apply(receipt, current):
            at = receipt.recorded_at
            evidence = {
                "command": receipt,
                "recorded_at": at,
                "actor_user": current.user,
                "actor_roles_at_time": list(current.roles),
            }
            Vehicle.objects.create(
                id=vehicle_id,
                organization_id=org,
                display_name=name,
                number=number,
                active=False,
                key=vin,
            )
            # CFG-1: the copy starts inactive, and its history starts with that.
            append_activity(vehicle_id, False, "CHANGE", receipt, current)
            row_for = {}  # source row id (any revision) -> the copy's row id, by stable slot
            by_slot = {}
            for kind, row in plan.positions:
                model = dict(SLOT_MODELS)[kind]
                copy = model.objects.create(
                    id=new_id(),
                    organization_id=org,
                    vehicle_id=vehicle_id,
                    stable_slot_id=row.stable_slot_id,
                    revision=1,
                    label=row.label,
                    effective_at=at,
                    display_order=row.display_order,
                    **({"sealable": row.sealable} if model is Compartment else {}),
                )
                by_slot[(kind, str(row.stable_slot_id))] = str(copy.pk)
            for kind, model in SLOT_MODELS:
                for pk, slot in model.objects.filter(vehicle=plan.source).values_list(
                    "pk", "stable_slot_id"
                ):
                    if (kind, str(slot)) in by_slot:
                        row_for[str(pk)] = by_slot[(kind, str(slot))]
            if plan.kit_lines:
                version = VehicleKitListVersion.objects.create(
                    id=new_id(),
                    organization_id=org,
                    vehicle_id=vehicle_id,
                    revision=1,
                    created_at=at,
                )
                for line in plan.kit_lines:
                    VehicleKitRequirement.objects.create(
                        id=new_id(),
                        organization_id=org,
                        version=version,
                        definition_id=line.definition_id,
                        compartment_id=row_for[str(line.compartment_id)],
                        required_count=line.required_count,
                        display_order=line.display_order,
                    )
            for revision, (_template_id, override, entries) in enumerate(plan.overrides, 1):
                copy = VehicleOverrideVersion.objects.create(
                    id=new_id(),
                    organization_id=org,
                    vehicle_id=vehicle_id,
                    template_version_id=override.template_version_id,
                    revision=revision,
                    created_at=at,
                )
                for entry, definition in entries:
                    VehicleOverrideItem.objects.create(
                        id=new_id(),
                        organization_id=org,
                        override_version=copy,
                        item_id=entry.item_id,
                        action=entry.action,
                        definition=(
                            _copy_definition(org, definition, copy.pk, row_for)
                            if definition is not None
                            else None
                        ),
                    )
            for template_id, slot in plan.bindings:
                RequirementBinding.objects.create(
                    organization_id=org,
                    vehicle_id=vehicle_id,
                    template_id=template_id,
                    slot=slot,
                    revision=1,
                    action="BIND",
                    effective_at=at,
                    **evidence,
                )
            if templates:
                from cya.checks.locks import ensure_item_locks

                ensure_item_locks(
                    org,
                    [vehicle_id],
                    ChecklistItemDef.objects.filter(checklist_version__template_id__in=templates)
                    .values_list("item_id", flat=True)
                    .distinct(),
                )

        shown = plan.shown()
        body = {
            "code": VEHICLE_COMMAND,
            "vehicle_id": vehicle_id,
            "vin": vin,
            "display_name": name,
            "number": number,
            "active": False,
            "source_vehicle_id": source_vehicle_id,
            "positions": len(shown["positions"]),
            "kit_lines": len(shown["kit_lines"]),
            "overrides": len(shown["overrides"]),
            "override_entries": sum(len(o["entries"]) for o in shown["overrides"]),
            "bindings": shown["bindings"],
            "excluded": shown["excluded"],
        }
        return _plan(org, body, apply)

    request = {
        "source_vehicle_id": source_vehicle_id,
        "vin": vin,
        "display_name": name,
        "number": number,
        "base_token": base_token,
    }
    # VIN uniqueness spans the organization: take its lock first.
    return _run(context, VEHICLE_COMMAND, request, build, lock_organization=True, action=ACTION)
