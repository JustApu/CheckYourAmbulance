"""Duplicating a checklist template or a vehicle (SPEC 4.8 CFG-3, CFG-4; issue #130 part 4).

Supervisors and Administrators (policy action duplicate_configuration, SPEC 3.3). The creator
enters the new record's key (SPEC 4.0.0 IMP-6), so both commands take the organization lock
first: keys are unique across the organization. See docs/configuration-management.md.
"""

from dataclasses import dataclass, field

from django.db.models import Max, Q
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
    "checks, obligations and problems",
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
        # SPEC 5.0.0 CFG-3 is withdrawn: shared templates are read-only after the cutover.
        from cya.checklists.ownership import require_legacy_writable

        require_legacy_writable(org, DuplicationError)
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
    kit_list_in_force: bool = False  # an empty list in force ("carries no kits") still copies
    kit_lines: list = field(default_factory=list)  # VehicleKitRequirement rows in force
    overrides: list = field(default_factory=list)  # (template_id, override, [(entry, def)])
    bindings: list = field(default_factory=list)  # (template_id, slot)
    # SPEC 5.0.0 CFG-4, after the cutover: the source's own checklists, each as
    # (checklist, version copied or None, [item rows], [kit-definition rows]), and their
    # assignments in force (checklist id, slot) and the position keys of copied positions.
    checklists: list = field(default_factory=list)
    assignments: list = field(default_factory=list)
    keys: list = field(default_factory=list)  # (kind, stable slot, key)
    excluded: list = field(default_factory=list)  # what the copy leaves out, and why
    slot_of: dict = field(default_factory=dict)  # named source row id -> (kind, stable slot)
    # SPEC 6.0.0 CFG-4, after the item-requirement cutover (None before it): each source
    # requirement's version to copy and the copy's place, (version, (place kind, slot or None)).
    requirements: list | None = None
    merged: dict = field(default_factory=dict)  # left-out requirement id -> the one kept there
    mapped: dict = field(default_factory=dict)  # copied item row id -> its requirement id

    def requirement_of(self, definition):
        """(version, place) a copied row outside a kit takes its requirements from, or None.

        A row takes its own requirement (through its map row), or the one kept in its place when
        its own was left out; a row with no map row takes the copied requirement at its item
        and place, if there is one.
        """
        if self.requirements is None or definition.kit_id is not None:
            return None
        found = self.mapped.get(str(definition.pk))
        found = self.merged.get(found, found)
        for version, place in self.requirements:
            if found is not None and str(version.requirement_id) == found:
                return version, place
            if (
                found is None
                and str(version.item_id) == str(definition.item_id)
                and place == self.place_of(definition)
            ):
                return version, place
        return None

    def place_of(self, definition):
        """The copy's place for a source row: its slot, or Not placed when that is retired."""
        row = definition.compartment_id or definition.system_id
        if row is None:
            return ("NOT_PLACED", None)
        kind_slot = self.slot_of.get(str(row))
        copied = {(kind, str(r.stable_slot_id)) for kind, r in self.positions}
        return kind_slot if kind_slot in copied else ("NOT_PLACED", None)

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
            "kit_list_in_force": self.kit_list_in_force,
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
            "checklists": [
                {
                    "checklist_id": str(checklist.pk),
                    "precedence": checklist.precedence,
                    "version_id": str(version.pk) if version else None,
                    "items": [str(d.pk) for d in definitions],
                    "kits": [str(p.pk) for p in placements],
                }
                for checklist, version, definitions, placements in self.checklists
            ],
            "assignments": [{"checklist_id": c, "slot": s} for c, s in self.assignments],
            "keys": [list(k) for k in self.keys],
            # Only after the item-requirement cutover, so earlier previews keep their token.
            **(
                {
                    "item_requirements": [
                        {
                            "requirement_id": str(version.requirement_id),
                            "version_id": str(version.pk),
                            "place": list(place),
                        }
                        for version, place in self.requirements
                    ]
                }
                if self.requirements is not None
                else {}
            ),
            "excluded": list(self.excluded),
            "never_copied": list(NEVER_COPIED),
        }

    def token(self):
        return digest(self.shown())


def _latest_rows(model, group, **filters):
    """The highest-revision row of each `group` among the rows matching `filters`.

    One grouped query finds each group's latest revision, then only those rows are fetched,
    so the cost follows the number of positions or bindings, not the length of their
    append-only histories (PR #166 review).
    """
    tops = list(
        model.objects.filter(**filters).values(*group).annotate(top=Max("revision")).order_by()
    )
    if not tops:
        return []
    wanted = Q()
    for top in tops:
        wanted |= Q(**{name: top[name] for name in group}, revision=top["top"])
    return list(model.objects.filter(wanted, **filters))


def _positions_now(vehicle, now):
    """(kind, row) for each position of the vehicle in effect now, retired ones left out."""
    found = []
    for kind, model in SLOT_MODELS:
        rows = _latest_rows(model, ("stable_slot_id",), vehicle=vehicle, effective_at__lte=now)
        for row in sorted(
            rows, key=lambda r: (r.display_order is None, r.display_order or 0, r.label)
        ):
            if not row.retired:
                found.append((kind, row))
    return found


def _bindings_now(org, vehicle_id, now):
    """(template_id, slot) of each binding in force now (its revision in force is a BIND)."""
    rows = _latest_rows(
        RequirementBinding,
        ("template_id", "slot"),
        organization_id=org,
        vehicle_id=vehicle_id,
        effective_at__lte=now,
    )
    return sorted((str(row.template_id), row.slot) for row in rows if row.action == "BIND")


def _slots_of(vehicle, row_ids):
    """{row id: (kind, stable slot)} for just the source rows the copied configuration names."""
    wanted = {str(row_id) for row_id in row_ids if row_id is not None}
    found = {}
    for kind, model in SLOT_MODELS:
        for pk, slot in model.objects.filter(vehicle=vehicle, pk__in=wanted).values_list(
            "pk", "stable_slot_id"
        ):
            found[str(pk)] = (kind, str(slot))
    return found


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

    in_force = kit_list_effective_at(org, str(source.pk), now)
    plan.kit_list_in_force = in_force is not None
    lines = (
        list(
            VehicleKitRequirement.objects.filter(version=in_force)
            .select_related("compartment", "definition")
            .order_by("display_order", "id")
        )
        if in_force is not None
        else []
    )
    from cya.checklists.ownership import cutover_at

    if cutover_at(org) is not None:
        return _plan_own_checklists(plan, org, source, now, lines, copied_slots)
    plan.bindings = _bindings_now(org, str(source.pk), now)
    candidates = []  # (template_id, override, [(entry, definition)])
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
        entries = [
            (entry, entry.definition)
            for entry in VehicleOverrideItem.objects.filter(override_version=override)
            .select_related("definition", "item")
            .order_by("id")
        ]
        candidates.append((template_id, override, entries))

    named = [line.compartment_id for line in lines]
    for _template_id, _override, entries in candidates:
        for _entry, definition in entries:
            if definition is not None:
                named += [definition.compartment_id, definition.system_id]
    plan.slot_of = _slots_of(source, named)

    for line in lines:
        if plan.slot_of.get(str(line.compartment_id)) in copied_slots:
            plan.kit_lines.append(line)
        else:
            plan.excluded.append(f"kit list line {line.definition.key}: its compartment is retired")
    for template_id, override, entries in candidates:
        kept = []
        for entry, definition in entries:
            if definition is not None and definition.kit_id is not None:
                plan.excluded.append(
                    f"override entry for {entry.item.label}: it names one of the source's kits"
                )
                continue
            row = definition and (definition.compartment_id or definition.system_id)
            if row is not None and plan.slot_of.get(str(row)) not in copied_slots:
                plan.excluded.append(
                    f"override entry for {entry.item.label}: its position is retired"
                )
                continue
            kept.append((entry, definition))
        plan.overrides.append((template_id, override, kept))
    return plan


def _version_to_copy(checklist, now):
    """The checklist's version in force now, else its first scheduled one, else None."""
    from cya.checklists.models import ChecklistPublication

    publications = ChecklistPublication.objects.filter(checklist_version__template=checklist)
    found = (
        publications.filter(effective_at__lte=now).order_by("-effective_at").first()
        or publications.filter(effective_at__gt=now).order_by("effective_at").first()
    )
    return found.checklist_version if found else None


def _plan_own_checklists(plan, org, source, now, lines, copied_slots):
    """CFG-4 after the cutover: copy the source's own checklists, assignments and keys."""
    from cya.kits.models import VehiclePositionKey

    named = [line.compartment_id for line in lines]
    for checklist in ChecklistTemplate.objects.filter(organization_id=org, vehicle=source).order_by(
        "precedence"
    ):
        version = _version_to_copy(checklist, now)
        definitions = (
            list(ChecklistItemDef.objects.filter(checklist_version=version).order_by("id"))
            if version
            else []
        )
        placements = list(placements_of(version.pk)) if version else []
        named += [d.compartment_id for d in definitions] + [d.system_id for d in definitions]
        plan.checklists.append((checklist, version, definitions, placements))
    own = {str(c.pk) for c, _v, _d, _p in plan.checklists}
    plan.assignments = [
        (t, slot) for t, slot in _bindings_now(org, str(source.pk), now) if t in own
    ]
    plan.slot_of = _slots_of(source, named)
    for line in lines:
        if plan.slot_of.get(str(line.compartment_id)) in copied_slots:
            plan.kit_lines.append(line)
        else:
            plan.excluded.append(f"kit list line {line.definition.key}: its compartment is retired")
    for checklist, _version, definitions, _placements in plan.checklists:
        for definition in definitions:
            row = definition.compartment_id or definition.system_id
            if row is not None and plan.slot_of.get(str(row)) not in copied_slots:
                plan.excluded.append(
                    f"the place of an item of {checklist.label}: its position is retired, so "
                    "the copy lists the item as Not placed"
                )
    plan.keys = sorted(
        (kind, str(slot), key)
        for kind, slot, key in VehiclePositionKey.objects.filter(vehicle=source).values_list(
            "kind", "stable_slot_id", "key"
        )
        if (kind, str(slot)) in copied_slots
    )
    from cya.checklists.item_requirements import has_cutover

    if has_cutover(org):
        _plan_requirements(plan, source, now, copied_slots)
    return plan


def _plan_requirements(plan, source, now, copied_slots):
    """SPEC 6.0.0 CFG-4: the source's item requirements, each at its version in force now (else
    its first scheduled one), placed on the copy by stable slot, or Not placed where the position
    is retired. Two that would then share an item and place (18.1 item 8) keep one: the one
    whose own place that is, else the first by id; the other is left out and shown.
    """
    from cya.checklists.item_requirements import current_or_next
    from cya.checklists.models import Item, ItemRequirementMap

    chosen = current_or_next(str(source.pk), now)

    def target(version):
        place = (version.place_kind, str(version.place_slot_id) if version.place_slot_id else None)
        if place[0] == "NOT_PLACED" or place in copied_slots:
            return place
        return ("NOT_PLACED", None)

    labels = {
        str(pk): label
        for pk, label in Item.objects.filter(
            pk__in={v.item_id for v in chosen.values()}
        ).values_list("pk", "label")
    }
    plan.requirements, kept = [], {}
    ordered = sorted(
        chosen.values(),
        key=lambda v: (target(v)[0] != v.place_kind, str(v.item_id), str(v.requirement_id)),
    )
    for version in ordered:
        place, key = target(version), str(version.requirement_id)
        if (str(version.item_id), place) in kept:
            plan.merged[key] = kept[(str(version.item_id), place)]
            plan.excluded.append(
                f"an item requirement of {labels.get(str(version.item_id))}: its position is "
                "retired and the copy already has the item Not placed"
            )
            continue
        kept[(str(version.item_id), place)] = key
        plan.requirements.append((version, place))
    plan.requirements.sort(key=lambda entry: (str(entry[0].item_id), str(entry[0].requirement_id)))
    rows = [d.pk for _c, _v, definitions, _p in plan.checklists for d in definitions]
    plan.mapped = {
        str(row): str(requirement)
        for row, requirement in ItemRequirementMap.objects.filter(
            item_def_id__in=rows, requirement__isnull=False
        ).values_list("item_def_id", "requirement_id")
    }


def _copy_requirements(org, plan, vehicle_id, by_slot, evidence):
    """The copy's item requirements, one version each effective at the copy's creation."""
    from cya.checklists.item_requirements import FIELDS
    from cya.checklists.models import (
        VehicleItemRequirement,
        VehicleItemRequirementVersion,
        new_requirement_id,
    )

    for version, (kind, slot) in plan.requirements:
        requirement = VehicleItemRequirement.objects.create(
            id=new_requirement_id(),
            organization_id=org,
            vehicle_id=vehicle_id,
            item_id=version.item_id,
            **evidence,
        )
        row = by_slot.get((kind, slot)) if slot else None
        VehicleItemRequirementVersion.objects.create(
            organization_id=org,
            requirement=requirement,
            vehicle_id=vehicle_id,
            item_id=version.item_id,
            revision=1,
            effective_at=evidence["recorded_at"],
            place_kind=kind,
            place_slot_id=slot,
            compartment_id=row if kind == "COMPARTMENT" else None,
            system_id=row if kind == "SYSTEM" else None,
            bulk_eligible=version.bulk_eligible,
            **{name: getattr(version, name) for name in FIELDS},
            **evidence,
        )


def _copy_checklists(org, plan, vehicle_id, row_for, by_slot, evidence):
    """The copy's own checklists (drafts), assignments and keys (CFG-4, after the cutover).

    After the item-requirement cutover a copied row outside a kit takes the place and
    requirements of its requirement's copied version, so publishing the draft links to that
    requirement (rule (a), cya.checklists.item_requirements) and never reverts a requirement
    edited since the source's version was published.
    """
    from cya.checklists.item_requirements import FIELDS
    from cya.kits.models import VehiclePositionKey

    at = evidence["recorded_at"]
    copied = {}
    for checklist, version, definitions, placements in plan.checklists:
        copy = ChecklistTemplate.objects.create(
            id=new_id(),
            organization_id=org,
            label=checklist.label,
            active=checklist.active,
            vehicle_id=vehicle_id,
            precedence=checklist.precedence,  # CHK-10: the order is preserved
        )
        copied[str(checklist.pk)] = copy
        if version is None:
            continue
        draft = ChecklistVersion.objects.create(
            id=new_id(), organization_id=org, template=copy, revision=1, created_at=at
        )
        for definition in definitions:
            values = {name: getattr(definition, name) for name in DEFINITION_FIELDS}
            for name in ("compartment_id", "system_id"):
                if values[name] is not None:
                    values[name] = row_for.get(str(values[name]))
            if values["compartment_id"] is None:
                values["kit_id"] = None  # a retired position: Not placed (CHK-9)
            taken = plan.requirement_of(definition)
            if taken is not None:
                requirement, (kind, slot) = taken
                values.update({name: getattr(requirement, name) for name in FIELDS})
                row = by_slot.get((kind, slot)) if slot else None
                values["compartment_id"] = row if kind == "COMPARTMENT" else None
                values["system_id"] = row if kind == "SYSTEM" else None
            ChecklistItemDef.objects.create(
                id=new_id(), organization_id=org, checklist_version=draft, **values
            )
        ChecklistKitPlacement.objects.bulk_create(
            ChecklistKitPlacement(
                id=new_id(),
                organization_id=org,
                checklist_version=draft,
                definition_id=p.definition_id,
                display_order=p.display_order,
            )
            for p in placements
        )
    for checklist_id, slot in plan.assignments:
        RequirementBinding.objects.create(
            organization_id=org,
            vehicle_id=vehicle_id,
            template=copied[checklist_id],
            slot=slot,
            revision=1,
            action="BIND",
            effective_at=at,
            **evidence,
        )
    VehiclePositionKey.objects.bulk_create(
        VehiclePositionKey(
            id=new_id(),
            organization_id=org,
            vehicle_id=vehicle_id,
            kind=kind,
            stable_slot_id=slot,
            key=key,
            **evidence,
        )
        for kind, slot, key in plan.keys
    )
    return [c.pk for c in copied.values()]


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
            for row_id, kind_slot in plan.slot_of.items():
                if kind_slot in by_slot:
                    row_for[row_id] = by_slot[kind_slot]
            if plan.kit_list_in_force:
                # CFG-4: one draft copying the lines in force, even when there are none: an
                # empty list in force means the vehicle carries no kits, not "no list".
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
            if plan.requirements is not None:
                _copy_requirements(org, plan, vehicle_id, by_slot, evidence)
            if plan.checklists:
                copies = _copy_checklists(org, plan, vehicle_id, row_for, by_slot, evidence)
                from cya.checks.locks import ensure_item_locks

                ensure_item_locks(
                    org,
                    [vehicle_id],
                    ChecklistItemDef.objects.filter(checklist_version__template_id__in=copies)
                    .values_list("item_id", flat=True)
                    .distinct(),
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
            "kit_list": len(shown["kit_lines"]) if plan.kit_list_in_force else None,
            "overrides": len(shown["overrides"]),
            "override_entries": sum(len(o["entries"]) for o in shown["overrides"]),
            "bindings": shown["bindings"],
            "excluded": shown["excluded"],
            # Only after the cutover, so a duplication before it keeps its result unchanged.
            **(
                {"checklists": len(shown["checklists"]), "assignments": shown["assignments"]}
                if plan.checklists
                else {}
            ),
            # Only after the item-requirement cutover (SPEC 6.0.0 CFG-4).
            **(
                {"item_requirements": len(plan.requirements)}
                if plan.requirements is not None
                else {}
            ),
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
