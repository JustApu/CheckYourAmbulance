"""Vehicle file preview, import and export (SPEC 2.1.0 4.7 IMP-2 to IMP-7, issue #129).

preview_vehicle_file reads a file and builds a plan with no writes (IMP-2): what would be created
or changed, and every problem. Its base token summarises the configuration the plan read plus
the file's digest. import_vehicle_file is one audited command (VEHICLE_IMPORT, policy action
import_configuration, Administrators; SPEC 3.3) that recomputes the plan under the organization
lock and the vehicle's row lock, refuses it if the base token no longer matches, and applies it all or nothing (IMP-3):

- the vehicle is created with its VIN as key, or its display name and number updated;
- each compartment or system position is matched by its position key (IMP-6): a key the vehicle
  has keeps its row (a new label appends a revision), a key other vehicles have adds this
  vehicle's row in that position, and a new key creates the position and its key;
- items outside kits are compared, by item label, with the vehicle's checklist in force for the
  named template; an absent or different item becomes an entry of one draft override version
  over the template version in force, keeping the entries of the override in force (IMP-7);
- the kit list becomes one draft version holding the list in force plus the file's lines, and
  the missing kit instances are added; nothing is deactivated or moved (IMP-3);
- a first binding to the template is appended if the vehicle has none (IMP-5, IMP-7).

Nothing the file omits is removed, template versions are never written, and publication stays a
separate step. Obligations are reconciled for the vehicle after the command commits.
"""

import hashlib
from dataclasses import dataclass, field

from django.db.models import Max
from django.utils import timezone

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id
from cya.authorization.persistence import digest
from cya.checklists.models import (
    KINDS,
    ChecklistItemDef,
    ChecklistTemplate,
    Compartment,
    Item,
    KitInstance,
    Vehicle,
    VehicleOverrideItem,
    VehicleOverrideVersion,
    VehicleSystem,
)
from cya.checklists.overlay import (
    ResolutionError,
    effective_template_version,
    governing_versions,
    resolve_checklist,
)
from cya.checklists.validation import (
    MAX_INSTRUCTIONS,
    ChecklistValidationError,
    _outcomes,
    _quantity_and_unit,
)
from cya.core.audit_commands import Plan, run_command
from cya.kits import keys as key_rules
from cya.kits import vehicle_files
from cya.kits.commands import _reconcile_after
from cya.kits.kit_lists import kit_list_effective_at
from cya.kits.library_import import ImportRefused, _active_items_by_label, _authorize
from cya.kits.models import (
    STORED_COUNT_MAX,
    KitAssignmentEvent,
    KitDefinition,
    KitInstanceDefinition,
    PositionKey,
    VehicleKitListVersion,
    VehicleKitRequirement,
)
from cya.obligations.models import RequirementBinding
from cya.schedules.models import ScheduleRule

IMPORT_ACTION = "import_configuration"
EXPORT_ACTION = "export_configuration"
IMPORT_COMMAND = "VEHICLE_IMPORT"
EXPORT_COMMAND = "VEHICLE_EXPORT"
MAX_LABEL = 200
MAX_NUMBER = 50
# Bounds on what one file may ask for, checked while planning, before anything is allocated.
MAX_ENTRIES = 1000  # compartments, systems, items or kit lines in one file, each
MAX_NEW_KITS = 1000  # physical kit instances one import may add
KIND_OF = {"compartments": "COMPARTMENT", "systems": "SYSTEM"}
SLOT_MODEL = {"COMPARTMENT": Compartment, "SYSTEM": VehicleSystem}


@dataclass
class VehiclePlan:
    errors: list = field(default_factory=list)
    base_token: str = ""
    vehicle: Vehicle | None = None
    vehicle_fields: dict = field(default_factory=dict)
    template: ChecklistTemplate | None = None
    version_id: str | None = None
    positions: list = field(default_factory=list)  # dicts: kind, key, label, slot, row, action
    new_items: list = field(default_factory=list)  # labels
    item_changes: list = field(default_factory=list)  # dicts: item label, action, fields
    carried_entries: list = field(default_factory=list)  # VehicleOverrideItem rows kept
    kit_lines: list = field(default_factory=list)  # dicts: definition, slot, count, display_order
    kit_list_changed: bool = False
    new_kits: list = field(default_factory=list)  # (definition, slot, how many)
    binding_slot: str | None = None
    kit_lines_in_force: dict = field(default_factory=dict)  # (definition, slot) -> line

    def summary(self):
        return {
            "vehicle": "CREATE" if self.vehicle is None else "UPDATE",
            "positions_added": sum(
                p["action"] in ("NEW_POSITION", "ADD_ROW") for p in self.positions
            ),
            "positions_relabelled": sum(p["action"] == "RELABEL" for p in self.positions),
            "items_created": len(self.new_items),
            "override_entries": len(self.item_changes),
            "kit_list_changed": self.kit_list_changed,
            "kits_added": sum(n for _d, _s, n in self.new_kits),
            "binding_slot": self.binding_slot,
        }


def _tail_rows(vehicle, model):
    """{stable slot id: the vehicle's newest revision}, including one scheduled for later."""
    if vehicle is None:
        return {}
    tails = {}
    for row in model.objects.filter(vehicle=vehicle).order_by("stable_slot_id", "-revision"):
        tails.setdefault(str(row.stable_slot_id), row)
    return tails


def _latest_rows(vehicle, model, now):
    """{stable slot id: the vehicle's latest row effective by now}."""
    if vehicle is None:
        return {}
    latest = {}
    for row in model.objects.filter(vehicle=vehicle, effective_at__lte=now).order_by(
        "stable_slot_id", "-revision"
    ):
        latest.setdefault(str(row.stable_slot_id), row)
    return latest


def _text(value, where, limit, errors, required=True):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        errors.append(f"{where}: must be nonblank text of at most {limit} characters")
        return None
    return value.strip()


def _check_item(entry, where, errors):
    """The item-independent fields of one item, checked like a template definition."""
    spec = {
        "kind": entry.get("kind"),
        "expected_quantity": entry.get("expected_quantity"),
        "unit": entry.get("unit"),
        "allowed_outcomes": entry.get("allowed_outcomes"),
    }
    try:
        if spec["kind"] not in KINDS:
            raise ChecklistValidationError(f"kind must be one of {list(KINDS)}")
        quantity, unit = _quantity_and_unit(spec)
        outcomes = _outcomes(spec)
    except ChecklistValidationError as error:
        errors.append(f"{where}: {error}")
        return None
    instructions = entry.get("instructions") or ""
    if not isinstance(instructions, str) or len(instructions) > MAX_INSTRUCTIONS:
        errors.append(f"{where}.instructions: at most {MAX_INSTRUCTIONS} characters")
        return None
    tracked = entry.get("expiration_tracked", False)
    if type(tracked) is not bool:
        errors.append(f"{where}.expiration_tracked: must be true or false")
        return None
    return {
        "kind": spec["kind"],
        "expected_quantity": quantity,
        "unit": unit,
        "allowed_outcomes": list(outcomes),
        "expiration_tracked": tracked,
        "instructions": instructions,
    }


def _signature(fields, kind, slot):
    return (
        fields["kind"],
        fields["expected_quantity"],
        fields["unit"],
        tuple(fields["allowed_outcomes"]),
        fields["expiration_tracked"],
        fields["instructions"],
        kind,
        str(slot),
    )


def _requirement_signature(requirement, slot_of):
    kind = "COMPARTMENT" if requirement.compartment_id else "SYSTEM"
    row = requirement.compartment_id or requirement.system_id
    return _signature(
        {
            "kind": requirement.kind,
            "expected_quantity": requirement.expected_quantity,
            "unit": requirement.unit,
            "allowed_outcomes": list(requirement.allowed_outcomes),
            "expiration_tracked": requirement.expiration_tracked,
            "instructions": requirement.instructions,
        },
        kind,
        slot_of.get(str(row)),
    )


def build_plan(organization_id, vehicle, source=""):
    """The plan for one parsed vehicle file; reads only, never writes (IMP-2)."""
    org, now, plan = str(organization_id), timezone.now(), VehiclePlan()
    errors = plan.errors
    try:
        vin = key_rules.check_vin(vehicle.get("vin"))
    except key_rules.KeyRuleError as error:
        errors.append(f"vin: {error}")
        vin = None
    plan.vehicle_fields = {
        "key": vin,
        "display_name": _text(vehicle.get("display_name"), "display_name", MAX_LABEL, errors),
        "number": _text(vehicle.get("number"), "number", MAX_NUMBER, errors, required=False),
    }
    found = Vehicle.objects.filter(organization_id=org, key=vin).first() if vin else None
    plan.vehicle = found
    if found is not None and not found.active:
        errors.append("vin: that vehicle is inactive; a remount is a new vehicle with a new VIN")

    # Template and the version in force (IMP-7).
    template_key = vehicle.get("template")
    template = (
        ChecklistTemplate.objects.filter(organization_id=org, key=template_key).first()
        if isinstance(template_key, str)
        else None
    )
    if template is None or not template.active:
        errors.append("template: no active template has that key")
    else:
        plan.template = template
        try:
            version = effective_template_version(org, template.pk, now)
        except ResolutionError:
            version = None
        if version is None:
            errors.append("template: the template has no published version in force")
        else:
            plan.version_id = str(version.pk)

    # Positions (IMP-6).
    slots = {kind: {} for kind in KIND_OF.values()}  # kind -> key -> slot (None: new)
    for kind in KIND_OF.values():
        for slot, key in PositionKey.objects.filter(organization_id=org, kind=kind).values_list(
            "stable_slot_id", "key"
        ):
            slots[kind][key] = str(slot)
    # Each position's newest revision: a relabel appends after it and keeps its designation,
    # and a revision scheduled for later is refused rather than overtaken (as designations do).
    rows = {kind: _tail_rows(found, SLOT_MODEL[kind]) for kind in KIND_OF.values()}
    current_rows = {kind: _latest_rows(found, SLOT_MODEL[kind], now) for kind in KIND_OF.values()}
    for name in (*KIND_OF, "items", "kits"):
        if len(vehicle.get(name) or []) > MAX_ENTRIES:
            errors.append(f"{name}: more than {MAX_ENTRIES} entries")
            return plan
    for name, kind in KIND_OF.items():
        seen = set()
        for index, entry in enumerate(vehicle.get(name) or []):
            where = f"{name}[{index}]"
            try:
                key = key_rules.check_key(entry.get("key"), "key")
            except key_rules.KeyRuleError as error:
                errors.append(f"{where}.key: {error}")
                continue
            label = _text(entry.get("label"), f"{where}.label", MAX_LABEL, errors)
            if key in seen:
                errors.append(f"{where}.key: {key!r} is listed twice")
                continue
            seen.add(key)
            slot = slots[kind].get(key)
            row = rows[kind].get(slot) if slot else None
            if row is not None and row.retired:
                # SPEC 4.8 CFG-2: a file never revives or relabels a retired position.
                errors.append(f"{where}: position {key!r} is retired on this vehicle")
                continue
            if row is not None and row.effective_at > now:
                # A revision is scheduled. Matching the label in effect now writes nothing;
                # anything else would overtake the scheduled revision, so it is refused.
                current = current_rows[kind].get(slot)
                if current is not None and label in (None, current.label):
                    plan.positions.append(
                        {
                            "kind": kind,
                            "key": key,
                            "label": label,
                            "slot": slot,
                            "row": current,
                            "action": "UNCHANGED",
                        }
                    )
                    continue
                errors.append(
                    f"{where}: a later revision of this position is already scheduled for "
                    f"{row.effective_at:%Y-%m-%d %H:%M} UTC; import after it takes effect"
                )
                continue
            if slot is None:
                action = "NEW_POSITION"
            elif row is None:
                action = "ADD_ROW"
            elif label is not None and row.label != label:
                action = "RELABEL"
            else:
                action = "UNCHANGED"
            plan.positions.append(
                {
                    "kind": kind,
                    "key": key,
                    "label": label,
                    "slot": slot,
                    "row": row,
                    "action": action,
                }
            )
    listed = {(p["kind"], p["key"]) for p in plan.positions}
    retired = {
        kind: {slot for slot, row in rows[kind].items() if row.retired} for kind in KIND_OF.values()
    }
    has_row = {
        (kind, key)
        for kind in KIND_OF.values()
        for key, slot in slots[kind].items()
        if slot in current_rows[kind]  # a row in effect now: _apply places items and kits in it
    }

    def position(kind, key, where):
        if key is None:
            return None
        if not isinstance(key, str):
            errors.append(f"{where}: must be a position key")
            return None
        if (kind, key) not in listed and (kind, key) not in has_row:
            errors.append(
                f"{where}: no {kind.lower()} position {key!r} on the vehicle or in the file"
            )
        elif slots[kind].get(key) in retired[kind]:
            errors.append(f"{where}: position {key!r} is retired on this vehicle")
        return key

    # Items outside kits (IMP-7), compared with the vehicle's checklist in force.
    effective, template_items, override = {}, set(), None
    if plan.version_id:
        template_items = {
            str(i)
            for i in ChecklistItemDef.objects.filter(
                checklist_version_id=plan.version_id
            ).values_list("item_id", flat=True)
        }
        if found is not None:
            try:
                version, override = governing_versions(org, str(found.pk), template.pk, now)
                if str(version.pk) != plan.version_id:
                    override = None
                resolved = resolve_checklist(
                    organization_id=org,
                    vehicle_id=str(found.pk),
                    template_id=template.pk,
                    as_of=now,
                )
                effective = {
                    str(r.requirement.item_id): r.requirement
                    for r in resolved.items
                    if r.requirement.kit_id is None
                }
            except ResolutionError as error:
                errors.append(f"template: the vehicle's checklist cannot be resolved ({error})")
        else:
            effective = {
                str(d.item_id): d
                for d in ChecklistItemDef.objects.filter(
                    checklist_version_id=plan.version_id, kit__isnull=True
                )
            }
    slot_of = {}
    for model in (Compartment, VehicleSystem):
        slot_of.update(
            {
                str(pk): str(s)
                for pk, s in model.objects.filter(organization_id=org).values_list(
                    "pk", "stable_slot_id"
                )
            }
        )
    labels = [e.get("item") for e in vehicle.get("items") or [] if isinstance(e.get("item"), str)]
    by_label = _active_items_by_label(org, labels)
    seen_items = set()
    for index, entry in enumerate(vehicle.get("items") or []):
        where = f"items[{index}]"
        label = _text(entry.get("item"), f"{where}.item", MAX_LABEL, errors)
        fields = _check_item(entry, where, errors)
        compartment, system = entry.get("compartment"), entry.get("system")
        if (compartment is None) == (system is None):
            errors.append(f"{where}: name exactly one of compartment or system")
            continue
        kind = "COMPARTMENT" if compartment else "SYSTEM"
        key = position(kind, compartment or system, f"{where}.{kind.lower()}")
        if label is None or fields is None:
            continue
        if label in seen_items:
            errors.append(f"{where}.item: {label!r} is listed twice")
            continue
        seen_items.add(label)
        matches = by_label.get(label, [])
        active = [pk for pk, is_active in matches if is_active]
        if len(active) > 1:
            errors.append(f"{where}.item: several active items are labelled {label!r}")
            continue
        if matches and not active:
            errors.append(f"{where}.item: {label!r} names only inactive items")
            continue
        item_id = active[0] if active else None
        slot = slots[kind].get(key)
        current = effective.get(item_id) if item_id else None
        if (
            current is not None
            and slot is not None
            and _requirement_signature(current, slot_of) == _signature(fields, kind, slot)
        ):
            continue  # the vehicle's checklist already has it as written
        if item_id is None and label not in plan.new_items:
            plan.new_items.append(label)
        plan.item_changes.append(
            {
                "label": label,
                "item_id": item_id,
                "action": "REPLACE" if item_id in template_items else "ADD",
                "kind": kind,
                "key": key,
                "fields": fields,
            }
        )
    if plan.item_changes and override is not None:
        changed = {c["item_id"] for c in plan.item_changes if c["item_id"]}
        plan.carried_entries = [
            e
            for e in VehicleOverrideItem.objects.filter(override_version=override).select_related(
                "definition"
            )
            if str(e.item_id) not in changed
        ]

    # Kit list and kit instances (KDEF-9, IMP-3).
    definitions = {
        d.key: d
        for d in KitDefinition.objects.filter(
            organization_id=org,
            key__in=[
                k.get("kit") for k in vehicle.get("kits") or [] if isinstance(k.get("kit"), str)
            ],
        )
    }
    in_force = kit_list_effective_at(org, str(found.pk), now) if found else None
    current_lines = {}
    if in_force is not None:
        for line in VehicleKitRequirement.objects.filter(version=in_force).select_related(
            "compartment"
        ):
            current_lines[(str(line.definition_id), str(line.compartment.stable_slot_id))] = line
    wanted = {key: (line.required_count, line.display_order) for key, line in current_lines.items()}
    file_keys = {}
    for index, entry in enumerate(vehicle.get("kits") or []):
        where = f"kits[{index}]"
        kit_key = entry.get("kit")
        definition = definitions.get(kit_key) if isinstance(kit_key, str) else None
        if definition is None or not definition.active:
            errors.append(f"{where}.kit: no active kit definition has that key")
            continue
        if entry.get("compartment") is None:
            errors.append(f"{where}.compartment: name the compartment the kits are in")
            continue
        key = position("COMPARTMENT", entry.get("compartment"), f"{where}.compartment")
        if key is None:
            continue
        count = entry.get("count")
        if type(count) is not int or not 1 <= count <= STORED_COUNT_MAX:
            errors.append(f"{where}.count: must be a whole number from 1 to {STORED_COUNT_MAX}")
            continue
        if (definition.key, key) in file_keys:
            errors.append(f"{where}: kit {definition.key!r} is listed twice in that compartment")
            continue
        file_keys[(definition.key, key)] = count
        slot = slots["COMPARTMENT"].get(key)
        if slot is not None and (str(definition.pk), slot) in current_lines:
            line = current_lines[(str(definition.pk), slot)]
            if line.required_count != count:
                plan.kit_list_changed = True
            wanted[(str(definition.pk), slot)] = (count, line.display_order)
        else:
            plan.kit_list_changed = True
        plan.kit_lines.append(
            {"definition": definition, "key": key, "count": count, "index": index}
        )
        present = (
            KitInstance.objects.filter(
                organization_id=org,
                vehicle=found,
                active=True,
                compartment__stable_slot_id=slot,
                pk__in=KitInstanceDefinition.objects.filter(definition=definition).values("kit_id"),
            ).count()
            if found is not None and slot is not None
            else 0
        )
        if present < count:
            plan.new_kits.append((definition, key, count - present))
    adding = sum(missing for _d, _k, missing in plan.new_kits)
    if adding > MAX_NEW_KITS:
        errors.append(
            f"kits: the file would add {adding} kits; one import adds at most {MAX_NEW_KITS}"
        )
    plan.kit_lines_in_force = current_lines

    # Schedule binding (IMP-5, IMP-7).
    if template is not None:
        bound = set()
        if found is not None:
            latest = {}
            for binding in RequirementBinding.objects.filter(
                organization_id=org, vehicle=found, template=template
            ).order_by("slot", "-revision"):
                latest.setdefault(binding.slot, binding)
            bound = {slot for slot, b in latest.items() if b.action == "BIND"}
        schedule = vehicle.get("schedule")
        if schedule is not None and not isinstance(schedule, str):
            errors.append("schedule: must be a schedule name")
            schedule = None
        if bound:
            if schedule is not None and schedule not in bound:
                errors.append(
                    f"schedule: the vehicle is bound to {sorted(bound)} for this template; "
                    "an import never changes a binding"
                )
        else:
            slots_known = sorted(
                set(ScheduleRule.objects.filter(organization_id=org).values_list("slot", flat=True))
            )
            if schedule is None:
                if len(slots_known) == 1:
                    plan.binding_slot = slots_known[0]
                else:
                    errors.append(
                        "schedule: name the schedule to use (the organization has several)"
                    )
            elif schedule not in slots_known:
                errors.append("schedule: no schedule has that name")
            else:
                plan.binding_slot = schedule
        if plan.binding_slot is not None and found is not None:
            # SPEC 4.8 CFG-2: a binding cannot make a retired position required again.
            from cya.kits.positions import template_slot_rows

            for kind, row_ids in template_slot_rows(org, template.pk, now).items():
                placed = set(
                    SLOT_MODEL[kind]
                    .objects.filter(pk__in=row_ids)
                    .values_list("stable_slot_id", flat=True)
                )
                if {str(s) for s in placed} & retired[kind]:
                    errors.append(
                        "schedule: the template places items in a position retired on this vehicle"
                    )
                    break

    plan.base_token = digest(
        {
            "source": source,
            "vehicle": [found.pk, found.display_name, found.number] if found else None,
            "rows": sorted(
                (
                    k,
                    s,
                    r.pk,
                    r.revision,
                    r.label,
                    r.effective_at.isoformat(),
                    getattr(r, "sealable", None),
                )
                for k in rows
                for s, r in rows[k].items()
            ),
            "keys": sorted((k, key, s) for k in slots for key, s in slots[k].items()),
            "version": plan.version_id,
            "override": str(override.pk) if override else None,
            "items": sorted((label, [p for p, _a in v]) for label, v in by_label.items()),
            "kit_list": str(in_force.pk) if in_force else None,
            "kits": sorted(
                KitInstance.objects.filter(organization_id=org, vehicle=found).values_list(
                    "pk", "active", "compartment_id"
                )
            )
            if found
            else [],
            "definitions": sorted((d.key, d.pk, d.active) for d in definitions.values()),
            "bindings": sorted(
                RequirementBinding.objects.filter(organization_id=org, vehicle=found).values_list(
                    "pk", flat=True
                )
            )
            if found
            else [],
        }
    )
    return plan


def preview_vehicle_file(organization_id, data, fmt):
    """Parse and plan a vehicle file with no writes (IMP-2); FileErrors become plan errors."""
    try:
        vehicle = vehicle_files.parse(data, fmt)
    except vehicle_files.FileErrors as error:
        return VehiclePlan(errors=list(error.errors))
    return build_plan(organization_id, vehicle, source=digest_bytes(data))


def digest_bytes(data):
    return hashlib.sha256(data if isinstance(data, bytes) else b"").hexdigest()


# --- apply -----------------------------------------------------------------------------------


def _apply(org, plan, receipt, current, vehicle_id, kit_ids):
    """Write the plan under one receipt (IMP-3), with the ids its response recorded."""
    at = receipt.recorded_at
    evidence = {
        "command": receipt,
        "recorded_at": at,
        "actor_user": current.user,
        "actor_roles_at_time": list(current.roles),
    }
    fields = plan.vehicle_fields
    vehicle = plan.vehicle
    if vehicle is None:
        vehicle = Vehicle.objects.create(
            id=vehicle_id,
            organization_id=org,
            display_name=fields["display_name"],
            number=fields["number"],
            active=True,
            key=fields["key"],
        )
    elif (vehicle.display_name, vehicle.number) != (fields["display_name"], fields["number"]):
        Vehicle.objects.filter(pk=vehicle.pk).update(
            display_name=fields["display_name"], number=fields["number"]
        )

    rows = {}  # (kind, key) -> the vehicle's row in that position after this import
    for p in plan.positions:
        model = SLOT_MODEL[p["kind"]]
        if p["action"] == "UNCHANGED":
            rows[(p["kind"], p["key"])] = p["row"]
            continue
        slot = p["slot"] or new_id()
        revision = 1 if p["row"] is None else p["row"].revision + 1
        row = model.objects.create(
            id=new_id(),
            organization_id=org,
            vehicle=vehicle,
            stable_slot_id=slot,
            revision=revision,
            label=p["label"],
            effective_at=at,
            # A relabel keeps the position's sealable designation (SPEC 10.2 CAB-1).
            **({"sealable": p["row"].sealable} if model is Compartment and p["row"] else {}),
            # and its display order (CFG-2); a retired position never reaches here.
            display_order=p["row"].display_order if p["row"] else None,
        )
        if p["action"] == "NEW_POSITION":
            PositionKey.objects.create(
                id=new_id(),
                organization_id=org,
                kind=p["kind"],
                stable_slot_id=slot,
                key=p["key"],
                **evidence,
            )
        rows[(p["kind"], p["key"])] = row
    for kind in KIND_OF.values():
        keyed = dict(
            PositionKey.objects.filter(organization_id=org, kind=kind).values_list(
                "key", "stable_slot_id"
            )
        )
        for key, slot in keyed.items():
            if (kind, key) not in rows:
                row = (
                    SLOT_MODEL[kind]
                    .objects.filter(vehicle=vehicle, stable_slot_id=slot, effective_at__lte=at)
                    .order_by("-revision")
                    .first()
                )
                if row is not None:
                    rows[(kind, key)] = row

    created = {}
    for label in plan.new_items:
        created[label] = Item.objects.create(
            id=new_id(), organization_id=org, label=label, active=True
        )
    if plan.item_changes:
        top = VehicleOverrideVersion.objects.filter(vehicle=vehicle).aggregate(m=Max("revision"))
        override = VehicleOverrideVersion.objects.create(
            id=new_id(),
            organization_id=org,
            vehicle=vehicle,
            template_version_id=plan.version_id,
            revision=(top["m"] or 0) + 1,
            created_at=at,
        )
        for entry in plan.carried_entries:
            definition = None
            if entry.definition_id:
                old = entry.definition
                definition = ChecklistItemDef.objects.create(
                    id=new_id(),
                    organization_id=org,
                    override_version=override,
                    item_id=old.item_id,
                    display_order=old.display_order,
                    kind=old.kind,
                    instructions=old.instructions,
                    expected_quantity=old.expected_quantity,
                    unit=old.unit,
                    allowed_outcomes=old.allowed_outcomes,
                    expiration_tracked=old.expiration_tracked,
                    compartment_id=old.compartment_id,
                    system_id=old.system_id,
                    kit_id=old.kit_id,
                    bulk_eligible=old.bulk_eligible,
                )
            VehicleOverrideItem.objects.create(
                id=new_id(),
                organization_id=org,
                override_version=override,
                item_id=entry.item_id,
                action=entry.action,
                definition=definition,
            )
        orders = dict(
            ChecklistItemDef.objects.filter(checklist_version_id=plan.version_id).values_list(
                "item_id", "display_order"
            )
        )
        for index, change in enumerate(plan.item_changes):
            item_id = change["item_id"] or str(created[change["label"]].pk)
            row = rows[(change["kind"], change["key"])]
            definition = ChecklistItemDef.objects.create(
                id=new_id(),
                organization_id=org,
                override_version=override,
                item_id=item_id,
                display_order=orders.get(item_id, 1000 + index * 10),
                compartment_id=row.pk if change["kind"] == "COMPARTMENT" else None,
                system_id=row.pk if change["kind"] == "SYSTEM" else None,
                kit_id=None,
                bulk_eligible=False,
                **change["fields"],
            )
            VehicleOverrideItem.objects.create(
                id=new_id(),
                organization_id=org,
                override_version=override,
                item_id=item_id,
                action=change["action"],
                definition=definition,
            )

    if plan.kit_list_changed:
        top = VehicleKitListVersion.objects.filter(vehicle=vehicle).aggregate(m=Max("revision"))
        version = VehicleKitListVersion.objects.create(
            id=new_id(),
            organization_id=org,
            vehicle=vehicle,
            revision=(top["m"] or 0) + 1,
            created_at=at,
        )
        lines = {}
        for (definition_id, slot), line in plan.kit_lines_in_force.items():
            lines[(definition_id, slot)] = (
                line.compartment_id,
                line.required_count,
                line.display_order,
            )
        for entry in plan.kit_lines:
            row = rows[("COMPARTMENT", entry["key"])]
            key = (str(entry["definition"].pk), str(row.stable_slot_id))
            order = lines[key][2] if key in lines else 1000 + entry["index"] * 10
            lines[key] = (row.pk, entry["count"], order)
        for (definition_id, _slot), (compartment_id, count, order) in lines.items():
            VehicleKitRequirement.objects.create(
                id=new_id(),
                organization_id=org,
                version=version,
                definition_id=definition_id,
                compartment_id=compartment_id,
                required_count=count,
                display_order=order,
            )

    planned = iter(kit_ids)
    for definition, key, missing in plan.new_kits:
        row = rows[("COMPARTMENT", key)]
        for number in range(missing):
            kit = KitInstance.objects.create(
                id=next(planned),
                organization_id=org,
                label=f"{definition.label} {number + 1}",
                active=True,
                vehicle=vehicle,
                compartment=row,
            )
            KitAssignmentEvent.objects.create(
                id=new_id(),
                organization_id=org,
                kit=kit,
                sequence=1,
                prior_event=None,
                vehicle=vehicle,
                compartment=row,
                active=True,
                effective_at=at,
                **evidence,
            )
            KitInstanceDefinition.objects.create(
                id=new_id(), organization_id=org, kit=kit, definition=definition, **evidence
            )

    if plan.binding_slot is not None:
        prior = (
            RequirementBinding.objects.filter(
                organization_id=org, vehicle=vehicle, template=plan.template, slot=plan.binding_slot
            )
            .order_by("-revision")
            .first()
        )
        RequirementBinding.objects.create(
            organization_id=org,
            vehicle=vehicle,
            template=plan.template,
            slot=plan.binding_slot,
            revision=prior.revision + 1 if prior else 1,
            action="BIND",
            effective_at=at,
            **evidence,
        )
        from cya.checks.locks import ensure_item_locks

        ensure_item_locks(
            org,
            [str(vehicle.pk)],
            ChecklistItemDef.objects.filter(checklist_version__template=plan.template)
            .values_list("item_id", flat=True)
            .distinct(),
        )


def import_vehicle_file(*, data, fmt, base_token, **context):
    """Apply a previewed vehicle file, all or nothing, as drafts (IMP-3, IMP-7)."""
    org = context["organization_id"]
    canonical_id(context["actor_id"])
    if not isinstance(base_token, str) or len(base_token) != 64:
        raise ImportRefused(["base_token: preview the file first and confirm that preview"])
    try:
        vehicle = vehicle_files.parse(data, fmt)
    except vehicle_files.FileErrors as error:
        raise ImportRefused(list(error.errors)) from None
    applied = {}

    def build(actor):
        # Every writer of a vehicle's configuration (kits, kit lists, overrides, bindings,
        # compartment designations) locks the vehicle row first. Holding it while the plan is
        # recomputed (READ COMMITTED reads the latest commits) means none of them can change
        # what the plan read before this command commits. Organization, then vehicle.
        try:
            vin = key_rules.check_vin(vehicle.get("vin"))
        except key_rules.KeyRuleError:
            vin = None  # build_plan reports it
        if vin is not None:
            list(Vehicle.objects.select_for_update().filter(organization_id=org, key=vin))
        plan = build_plan(org, vehicle, source=digest_bytes(data))
        if plan.errors:
            raise ImportRefused(plan.errors)
        if plan.base_token != base_token:
            raise ImportRefused(
                ["the configuration changed since this file was previewed; preview it again"]
            )
        vehicle_id = str(plan.vehicle.pk) if plan.vehicle else new_id()
        # The receipt stores the response before the effects run, and the database gives a new
        # kit its definition only as that response lists it (KDEF-3), so the ids are fixed here.
        planned_kits = [
            {"kit_id": new_id(), "definition_id": str(definition.pk)}
            for definition, _key, missing in plan.new_kits
            for _ in range(missing)
        ]
        body = {
            "code": "VEHICLE_IMPORTED",
            "vehicle_id": vehicle_id,
            **plan.summary(),
            "kits": planned_kits,
        }

        def apply(receipt, current):
            _apply(org, plan, receipt, current, vehicle_id, [k["kit_id"] for k in planned_kits])
            applied["vehicle_id"] = vehicle_id

        return Plan(
            response_body=body,
            apply=apply,
            target_type="ORGANIZATION",
            target_id=org,
            new_snapshot={
                "schema_version": 1,
                "values": {"file_type": vehicle_files.FILE_TYPE, "format": fmt, **plan.summary()},
            },
        )

    result = run_command(
        organization_id=org,
        operation_id=context["operation_id"],
        command_type=IMPORT_COMMAND,
        request_digest=digest({"fmt": fmt, "data": digest_bytes(data), "base_token": base_token}),
        correlation_id=context["correlation_id"],
        request_attempt_id=context["request_attempt_id"],
        authorize=_authorize(org, context["actor_id"], IMPORT_ACTION, lock_organization=True),
        prepare=build,
    )
    if not result.replayed and applied.get("vehicle_id"):
        _reconcile_after(org, [applied["vehicle_id"]])  # logs each failed unit, like kit commands
    return result


# --- export ----------------------------------------------------------------------------------


def vehicle_file(organization_id, vehicle_id, template_id=None):
    """A vehicle in normal form from the current configuration (IMP-4); re-imports unchanged."""
    org, now = str(organization_id), timezone.now()
    vehicle = Vehicle.objects.filter(organization_id=org, pk=vehicle_id).first()
    if vehicle is None:
        raise ImportRefused(["vehicle: not found"])
    if vehicle.key is None:
        raise ImportRefused(["vehicle: set its VIN before exporting it"])
    latest = {}
    for binding in RequirementBinding.objects.filter(organization_id=org, vehicle=vehicle).order_by(
        "template_id", "slot", "-revision"
    ):
        latest.setdefault((str(binding.template_id), binding.slot), binding)
    bound = {}
    for (template, slot), binding in sorted(latest.items()):
        if binding.action == "BIND":
            bound.setdefault(template, slot)
    if template_id is None:
        if len(bound) != 1:
            raise ImportRefused(
                ["template: name the template to export (the vehicle has several or none)"]
            )
        template_id = next(iter(bound))
    template = ChecklistTemplate.objects.get(organization_id=org, pk=template_id)
    if template.key is None:
        raise ImportRefused(["template: set the template's key before exporting"])
    keys = {
        kind: dict(
            PositionKey.objects.filter(organization_id=org, kind=kind).values_list(
                "stable_slot_id", "key"
            )
        )
        for kind in KIND_OF.values()
    }
    keys = {kind: {str(s): k for s, k in value.items()} for kind, value in keys.items()}
    positions, key_of_row = {}, {}
    for name, kind in KIND_OF.items():
        positions[name] = []
        for slot, row in sorted(
            # SPEC 4.8 CFG-2: display order (empty last), then label; retired positions left out.
            (
                (s, r)
                for s, r in _latest_rows(vehicle, SLOT_MODEL[kind], now).items()
                if not r.retired
            ),
            key=lambda x: (x[1].display_order is None, x[1].display_order or 0, x[1].label),
        ):
            if slot not in keys[kind]:
                raise ImportRefused(
                    [f"{name}: set the key of position {row.label!r} before exporting"]
                )
            positions[name].append({"key": keys[kind][slot], "label": row.label})
    for kind in KIND_OF.values():
        for pk, slot in (
            SLOT_MODEL[kind].objects.filter(organization_id=org).values_list("pk", "stable_slot_id")
        ):
            if str(slot) in keys[kind]:
                key_of_row[str(pk)] = keys[kind][str(slot)]
    resolved = resolve_checklist(
        organization_id=org, vehicle_id=str(vehicle.pk), template_id=template.pk, as_of=now
    )
    labels = dict(Item.objects.filter(organization_id=org).values_list("pk", "label"))
    items = []
    for r in resolved.items:
        requirement = r.requirement
        if requirement.kit_id is not None:
            continue
        row = requirement.compartment_id or requirement.system_id
        place = "compartment" if requirement.compartment_id else "system"
        items.append(
            {
                "item": labels.get(str(requirement.item_id), ""),
                "compartment": key_of_row.get(str(row)) if place == "compartment" else None,
                "system": key_of_row.get(str(row)) if place == "system" else None,
                "kind": requirement.kind,
                "expected_quantity": requirement.expected_quantity,
                "unit": requirement.unit,
                "allowed_outcomes": list(requirement.allowed_outcomes),
                "expiration_tracked": requirement.expiration_tracked,
                "instructions": requirement.instructions,
            }
        )
    kits = []
    in_force = kit_list_effective_at(org, str(vehicle.pk), now)
    if in_force is not None:
        for line in (
            VehicleKitRequirement.objects.filter(version=in_force)
            .select_related("definition", "compartment")
            .order_by("display_order", "id")
        ):
            kits.append(
                {
                    "kit": line.definition.key,
                    "compartment": keys["COMPARTMENT"].get(str(line.compartment.stable_slot_id)),
                    "count": line.required_count,
                }
            )
    return {
        "vin": vehicle.key,
        "display_name": vehicle.display_name,
        "number": vehicle.number,
        "template": template.key,
        "schedule": bound.get(str(template.pk)),
        "compartments": positions["compartments"],
        "systems": positions["systems"],
        "items": items,
        "kits": kits,
    }


def audited_vehicle_export(*, vehicle_id, fmt, template_id=None, **context):
    """Export one vehicle as an audited, write-free command (IMP-4, RPT-2).

    Returns (result, file bytes). Supervisors and Administrators may export (SPEC 3.3); the
    audit records the actor, vehicle, format and counts, never the contents.
    """
    org = context["organization_id"]
    canonical_id(context["actor_id"])
    canonical_id(vehicle_id)
    if fmt not in vehicle_files.FORMATS:
        raise ImportRefused([f"format must be one of {list(vehicle_files.FORMATS)}"])
    produced = {}

    def build(actor):
        vehicle = vehicle_file(org, vehicle_id, template_id)
        produced["data"] = vehicle_files.render(vehicle, fmt)
        values = {
            "file_type": vehicle_files.FILE_TYPE,
            "format": fmt,
            "vehicle_id": vehicle_id,
            "items": len(vehicle["items"]),
            "kits": len(vehicle["kits"]),
        }

        def apply(receipt, _current):
            return None

        return Plan(
            response_body={"code": "VEHICLE_EXPORTED", **values},
            apply=apply,
            target_type="ORGANIZATION",
            target_id=org,
            new_snapshot={"schema_version": 1, "values": values},
        )

    result = run_command(
        organization_id=org,
        operation_id=context["operation_id"],
        command_type=EXPORT_COMMAND,
        request_digest=digest({"vehicle_id": vehicle_id, "fmt": fmt, "template_id": template_id}),
        correlation_id=context["correlation_id"],
        request_attempt_id=context["request_attempt_id"],
        authorize=_authorize(org, context["actor_id"], EXPORT_ACTION),
        prepare=build,
    )
    return result, produced.get("data")
