"""Migrate an organization to vehicle-owned checklists at one cutover instant (SPEC 5.0.0 18.2).

plan_cutover only reads. It works out, as of an instant T, what the migration would write:

1. **Positions.** A vehicle-local position is the pair (vehicle, stable slot); every position
   row is already keyed by its vehicle. Each existing pair is kept as it is and recorded in
   legacy_position_map, and each vehicle gets its own copy of every organization-wide position
   key it uses (vehicle_position_key). Labels, order, retirement and sealable revisions are
   untouched.
2. **Checklists.** Each (vehicle, legacy template) pair that has a binding or a published
   override becomes exactly one vehicle checklist. Its precedence ordinal follows the legacy
   template's identity order within the vehicle, which is the order the generator used
   (CHK-10). Its versions are the vehicle's effective checklist (template version with the
   vehicle's override applied, CHK-6 as approved before 5.0.0) at T and at every later
   published boundary, each published with that boundary as its effective instant. Placements
   are mapped to the vehicle's own rows of the same slots.
3. **Assignments.** Each binding chain (vehicle, legacy template, slot) becomes an assignment
   chain of the vehicle checklist on the same slot: bound at T if the legacy binding is in force
   at T, then every later revision, with the same effective instants. One template serving
   several slots stays one checklist with several assignments; several templates in one slot
   stay several checklists. Nothing is merged or copied per slot.

migrate_to_vehicle_checklists applies the plan as one audited CHECKLIST_CUTOVER command, all or
nothing, at the receipt's instant T. Before it commits, verify_carry_over checks, inside the same
transaction, that every future due instant resolves to the same requirements as before (the
legacy answer "as the records stood" before T, carried across the maps); any difference refuses
the whole command. Nothing old is changed: templates, overrides, bindings, position keys,
obligations, checks, sessions, kit lists, seals and audit events keep their rows and meaning.
A retry with the same operation id replays the first result; any other run is refused once the
cutover is recorded.
"""

from dataclasses import dataclass, field

from django.utils import timezone

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id
from cya.authorization.persistence import digest
from cya.checklists.models import (
    ChecklistCutover,
    ChecklistItemDef,
    ChecklistPublication,
    ChecklistTemplate,
    ChecklistVersion,
    Compartment,
    LegacyChecklistMap,
    LegacyItemDefMap,
    LegacyPositionMap,
    LegacyVersionMap,
    Vehicle,
    VehicleSystem,
)
from cya.checklists.overlay import (
    NoEffectiveVersionError,
    ResolutionError,
    governing_versions,
    resolved_requirements,
    vehicle_slots,
)
from cya.core.audit_commands import Plan, run_command

COMMAND = "CHECKLIST_CUTOVER"
ACTION = "import_configuration"  # Administrators only (SPEC 3.3)
VERIFY_DAYS = 8  # future due instants checked beyond those already generated
MAX_PROBLEMS = 50


class CutoverError(ValueError):
    """The migration cannot be applied as planned; nothing was written."""


@dataclass(frozen=True)
class VersionPlan:
    effective_at: object
    template_version_id: str
    override_version_id: str | None
    items: tuple  # (legacy definition id, ChecklistItemDef fields)
    placements: tuple  # (definition id, display order)


@dataclass(frozen=True)
class PairPlan:
    vehicle_id: str
    legacy_template_id: str
    label: str
    active: bool
    precedence: int
    versions: tuple
    chains: tuple  # (slot, ((action, effective_at), ...))


@dataclass
class CutoverPlan:
    organization_id: str
    at: object
    pairs: list = field(default_factory=list)
    positions: list = field(default_factory=list)  # (vehicle, kind, stable slot)
    keys: list = field(default_factory=list)  # (vehicle, kind, stable slot, key)
    problems: list = field(default_factory=list)
    open_sessions: int = 0

    def problem(self, text):
        if len(self.problems) < MAX_PROBLEMS:
            self.problems.append(text)

    def counts(self):
        return {
            "checklists": len(self.pairs),
            "versions": sum(len(p.versions) for p in self.pairs),
            "assignment_revisions": sum(len(r) for p in self.pairs for _s, r in p.chains),
            "positions": len(self.positions),
            "position_keys": len(self.keys),
            "open_sessions_kept": self.open_sessions,
        }


# --- planning (read only) ---------------------------------------------------------------------


def _bindings(organization_id):
    from cya.obligations.models import RequirementBinding

    chains = {}
    rows = RequirementBinding.objects.filter(
        organization_id=organization_id, template__vehicle__isnull=True
    ).order_by("vehicle_id", "template_id", "slot", "revision")
    for row in rows:
        key = (str(row.vehicle_id), str(row.template_id), row.slot)
        chains.setdefault(key, []).append(row)
    return chains


def _override_pairs(organization_id):
    return {
        (str(vehicle), str(template))
        for vehicle, template in ChecklistPublication.objects.filter(
            organization_id=organization_id, override_version__isnull=False
        ).values_list(
            "override_version__vehicle_id", "override_version__template_version__template_id"
        )
    }


def _chain_after(revisions, at):
    """The assignment revisions that reproduce a legacy chain from `at` on (exact instants)."""
    in_force = None
    for row in revisions:
        if row.effective_at <= at:
            in_force = row
    state = "BIND" if in_force is not None and in_force.action == "BIND" else "UNBIND"
    found = [("BIND", at)] if state == "BIND" else []
    for row in revisions:
        if row.effective_at > at and row.action != state:
            found.append((row.action, row.effective_at))
            state = row.action
    return tuple(found)


def _boundaries(organization_id, vehicle_id, template_id, at):
    later = ChecklistPublication.objects.filter(
        organization_id=organization_id, effective_at__gt=at
    )
    instants = set(
        later.filter(checklist_version__template_id=template_id).values_list(
            "effective_at", flat=True
        )
    )
    instants |= set(
        later.filter(
            override_version__vehicle_id=vehicle_id,
            override_version__template_version__template_id=template_id,
        ).values_list("effective_at", flat=True)
    )
    return [at, *sorted(instants)]


def _own_fields(plan, vehicle_id, requirement, rows, instant):
    """ChecklistItemDef fields for one requirement, placed on the vehicle's own rows."""
    compartment = rows["compartment"].get(requirement.compartment_id)
    system = rows["system"].get(requirement.system_id)
    if (requirement.compartment_id and compartment is None) or (
        requirement.system_id and system is None
    ):
        plan.problem(
            f"vehicle {vehicle_id} has no position for item {requirement.item_id} at {instant}"
        )
        return None
    return {
        "item_id": requirement.item_id,
        "display_order": requirement.display_order,
        "kind": requirement.kind,
        "instructions": requirement.instructions,
        "expected_quantity": requirement.expected_quantity,
        "unit": requirement.unit,
        "allowed_outcomes": list(requirement.allowed_outcomes),
        "expiration_tracked": requirement.expiration_tracked,
        "compartment_id": str(compartment.pk) if compartment else None,
        "system_id": str(system.pk) if system else None,
        "kit_id": requirement.kit_id,
        "bulk_eligible": False,
    }


def _version_at(plan, vehicle_id, template_id, instant):
    """The vehicle's effective checklist at `instant` as a VersionPlan, or None if none yet."""
    from cya.kits.models import ChecklistKitPlacement

    org = plan.organization_id
    try:
        version, override = governing_versions(org, vehicle_id, template_id, instant)
    except NoEffectiveVersionError:
        return None
    except ResolutionError as error:
        plan.problem(f"vehicle {vehicle_id}, template {template_id} at {instant}: {error}")
        return None
    requirements = resolved_requirements(version, override)
    rows = {
        kind: vehicle_slots(
            kind, vehicle_id, {getattr(r, f"{kind}_id") for r in requirements} - {None}, instant
        )
        for kind in ("compartment", "system")
    }
    items = []
    for requirement in requirements:
        fields = _own_fields(plan, vehicle_id, requirement, rows, instant)
        if fields is not None:
            items.append((str(requirement.definition_id), fields))
    if not items:
        plan.problem(f"vehicle {vehicle_id}, template {template_id} at {instant}: no items")
        return None
    placements = tuple(
        ChecklistKitPlacement.objects.filter(checklist_version=version)
        .order_by("display_order", "id")
        .values_list("definition_id", "display_order")
    )
    return VersionPlan(
        effective_at=instant,
        template_version_id=str(version.pk),
        override_version_id=str(override.pk) if override else None,
        items=tuple(items),
        placements=tuple((str(d), o) for d, o in placements),
    )


def _versions(plan, vehicle_id, template_id):
    found = []
    for instant in _boundaries(plan.organization_id, vehicle_id, template_id, plan.at):
        version = _version_at(plan, vehicle_id, template_id, instant)
        if version is None:
            continue
        previous = found[-1] if found else None
        if previous and (previous.template_version_id, previous.override_version_id) == (
            version.template_version_id,
            version.override_version_id,
        ):
            continue
        found.append(version)
    return tuple(found)


def _positions(plan):
    org = plan.organization_id
    for kind, model in (("COMPARTMENT", Compartment), ("SYSTEM", VehicleSystem)):
        pairs = set(
            model.objects.filter(organization_id=org).values_list("vehicle_id", "stable_slot_id")
        )
        plan.positions += sorted((str(v), kind, str(s)) for v, s in pairs)
    from cya.kits.models import PositionKey

    keys = {
        (row.kind, str(row.stable_slot_id)): row.key
        for row in PositionKey.objects.filter(organization_id=org)
    }
    plan.keys = [
        (vehicle, kind, slot, keys[(kind, slot)])
        for vehicle, kind, slot in plan.positions
        if (kind, slot) in keys
    ]


def plan_cutover(organization_id, at=None):
    """What the migration would write at instant `at` (default now), and every problem."""
    org = str(organization_id)
    plan = CutoverPlan(org, at or timezone.now())
    if ChecklistCutover.objects.filter(organization_id=org).exists():
        plan.problem("the organization already moved to vehicle checklists")
        return plan
    chains = _bindings(org)
    pairs = {(v, t) for v, t, _s in chains} | _override_pairs(org)
    templates = {
        str(t.pk): t for t in ChecklistTemplate.objects.filter(pk__in={t for _v, t in pairs})
    }
    by_vehicle = {}
    for vehicle_id, template_id in pairs:
        by_vehicle.setdefault(vehicle_id, []).append(template_id)
    for vehicle_id in sorted(by_vehicle):
        # CHK-10: the generator ordered a vehicle's templates by identity; keep that order.
        for precedence, template_id in enumerate(sorted(by_vehicle[vehicle_id]), start=1):
            template = templates[template_id]
            plan.pairs.append(
                PairPlan(
                    vehicle_id=vehicle_id,
                    legacy_template_id=template_id,
                    label=template.label,
                    active=template.active,
                    precedence=precedence,
                    versions=_versions(plan, vehicle_id, template_id),
                    chains=tuple(
                        (slot, _chain_after(rows, plan.at))
                        for (v, t, slot), rows in sorted(chains.items())
                        if (v, t) == (vehicle_id, template_id)
                    ),
                )
            )
    _positions(plan)
    from cya.inspections.models import InspectionSession

    plan.open_sessions = InspectionSession.objects.filter(
        organization_id=org, checklist_version__template__vehicle__isnull=True
    ).count()
    return plan


# --- applying ---------------------------------------------------------------------------------


def _evidence(org, receipt, actor):
    return {
        "organization_id": org,
        "command": receipt,
        "recorded_at": receipt.recorded_at,
        "actor_user": actor.user,
        "actor_roles_at_time": list(actor.roles),
    }


def _write_versions(org, pair, checklist, evidence):
    from cya.kits.models import ChecklistKitPlacement

    for revision, version in enumerate(pair.versions, start=1):
        row = ChecklistVersion.objects.create(
            id=new_id(),
            organization_id=org,
            template=checklist,
            revision=revision,
            created_at=evidence["recorded_at"],
        )
        for legacy_id, fields in version.items:
            item_def = ChecklistItemDef.objects.create(
                id=new_id(), organization_id=org, checklist_version=row, **fields
            )
            LegacyItemDefMap.objects.create(
                item_def=item_def, legacy_item_def_id=legacy_id, **evidence
            )
        ChecklistKitPlacement.objects.bulk_create(
            ChecklistKitPlacement(
                id=new_id(),
                organization_id=org,
                checklist_version=row,
                definition_id=definition,
                display_order=order,
            )
            for definition, order in version.placements
        )
        ChecklistPublication.objects.create(
            id=new_id(),
            organization_id=org,
            command=evidence["command"],
            recorded_at=evidence["recorded_at"],
            actor_user=evidence["actor_user"],
            actor_roles_at_time=evidence["actor_roles_at_time"],
            checklist_version=row,
            effective_at=version.effective_at,
        )
        LegacyVersionMap.objects.create(
            checklist_version=row,
            legacy_template_version_id=version.template_version_id,
            legacy_override_version_id=version.override_version_id,
            **evidence,
        )


def _write_pair(org, pair, evidence):
    from cya.obligations.models import RequirementBinding

    checklist = ChecklistTemplate.objects.create(
        id=new_id(),
        organization_id=org,
        label=pair.label,
        active=pair.active,
        vehicle_id=pair.vehicle_id,
        precedence=pair.precedence,
    )
    LegacyChecklistMap.objects.create(
        vehicle_id=pair.vehicle_id,
        legacy_template_id=pair.legacy_template_id,
        checklist=checklist,
        **evidence,
    )
    _write_versions(org, pair, checklist, evidence)
    for slot, revisions in pair.chains:
        for number, (action, effective_at) in enumerate(revisions, start=1):
            RequirementBinding.objects.create(
                id=new_id(),
                vehicle_id=pair.vehicle_id,
                template=checklist,
                slot=slot,
                revision=number,
                action=action,
                effective_at=effective_at,
                **evidence,
            )
    return checklist


def _write_positions(plan, evidence):
    from cya.kits.models import VehiclePositionKey

    LegacyPositionMap.objects.bulk_create(
        LegacyPositionMap(
            id=new_id(),
            vehicle_id=vehicle,
            kind=kind,
            legacy_stable_slot_id=slot,
            stable_slot_id=slot,
            **evidence,
        )
        for vehicle, kind, slot in plan.positions
    )
    VehiclePositionKey.objects.bulk_create(
        VehiclePositionKey(
            id=new_id(), vehicle_id=vehicle, kind=kind, stable_slot_id=slot, key=key, **evidence
        )
        for vehicle, kind, slot, key in plan.keys
    )


def _item_locks(org, plan):
    from cya.checks.locks import ensure_item_locks

    for pair in plan.pairs:
        items = {fields["item_id"] for version in pair.versions for _l, fields in version.items}
        ensure_item_locks(org, [pair.vehicle_id], items)


def migrate_to_vehicle_checklists(*, organization_id, actor_id, operation_id):
    """Apply SPEC 18.2 for one organization as one audited, all-or-nothing command.

    operation_id makes a retry of the same run replay its first result (REC-4); a second
    migration under another id is refused because the cutover is already recorded.
    """
    from cya.kits.definitions import authorizer

    org = str(canonical_id(organization_id))
    canonical_id(actor_id)

    def prepare(_actor):
        # Lock every vehicle (the configuration lock, taken before templates), so no binding,
        # publication or position change can interleave with the plan.
        list(Vehicle.objects.select_for_update().filter(organization_id=org).order_by("pk"))
        probe = plan_cutover(org)
        if probe.problems:
            raise CutoverError("; ".join(probe.problems))

        def apply(receipt, actor):
            plan = plan_cutover(org, receipt.recorded_at)
            if plan.problems:
                raise CutoverError("; ".join(plan.problems))
            evidence = _evidence(org, receipt, actor)
            ChecklistCutover.objects.create(id=new_id(), **evidence)
            for pair in plan.pairs:
                _write_pair(org, pair, evidence)
            _write_positions(plan, evidence)
            _item_locks(org, plan)
            from cya.obligations.legacy import verify_carry_over

            mismatches = verify_carry_over(org, receipt.recorded_at, VERIFY_DAYS)
            if mismatches:
                raise CutoverError(
                    "migration would change future requirements: " + "; ".join(mismatches[:10])
                )

        body = {"code": COMMAND, **probe.counts()}
        return Plan(
            response_body=body,
            apply=apply,
            target_type="ORGANIZATION",
            target_id=org,
            new_snapshot={"schema_version": 1, "values": body},
        )

    return run_command(
        organization_id=org,
        operation_id=operation_id,
        command_type=COMMAND,
        request_digest=digest({"organization_id": org}),
        correlation_id=new_id(),
        request_attempt_id=new_id(),
        authorize=authorizer(org, str(actor_id), ACTION, lock_organization=True),
        prepare=prepare,
    )


__all__ = [
    "COMMAND",
    "CutoverError",
    "CutoverPlan",
    "migrate_to_vehicle_checklists",
    "plan_cutover",
]
