"""Migration to vehicle item requirements (SPEC 6.0.0 18.3, CHK-11; issue #256 C1).

plan_item_requirements reads only. It replays every published vehicle checklist version of
the organization in effective order (then checklist precedence) and applies the publication
rules of cya.checklists.item_requirements to each item row outside a kit. Where publication
would refuse or add a place, the migration cannot decide, so it refuses and lists the case:

- conflict: another checklist in force keeps the same item at the same place with other
  requirements (a Supervisor first publishes matching versions);
- possible move or split: a later version moves the item while another checklist still keeps
  it at the old place.

Kit content maps to no requirement. Deficiencies that name an item without a place, on a
vehicle where the item has more than one requirement, are listed; C3 makes them block once
deficiencies record a place. Any problem refuses the whole migration with nothing written.

migrate_item_requirements applies the plan as one audited ITEM_REQUIREMENT_CUTOVER command:
the cutover record, every requirement and version (inserted in effective order, so the
database's interval guard sees the same history), and one source-row map row per item row of
every published version. Checklist versions, obligations, checks, links and deficiencies are
not changed; earlier records resolve through the map. A retry with the same operation_id
replays the first result; a second migration is refused.
"""

from dataclasses import dataclass, field

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id
from cya.authorization.persistence import digest
from cya.checklists.item_requirements import FIELDS, fields_of
from cya.checklists.models import (
    ChecklistCutover,
    ChecklistItemDef,
    ChecklistPublication,
    ItemRequirementCutover,
    LegacyItemRequirementMap,
    Vehicle,
    VehicleItemRequirement,
    VehicleItemRequirementVersion,
)
from cya.core.audit_commands import Plan, run_command

COMMAND = "ITEM_REQUIREMENT_CUTOVER"
ACTION = "import_configuration"
MAX_PROBLEMS = 50


class ItemRequirementMigrationError(ValueError):
    pass


@dataclass
class VersionPlan:
    id: str
    requirement_id: str
    revision: int
    effective_at: object
    place: tuple
    compartment_id: object
    system_id: object
    fields: dict


@dataclass
class RequirementPlan:
    id: str
    vehicle_id: str
    item_id: str
    versions: list = field(default_factory=list)

    def at(self, instant):
        current = None
        for version in self.versions:
            if version.effective_at <= instant:
                current = version
        return current


@dataclass
class MapPlan:
    vehicle_id: str
    checklist_version_id: str
    item_def_id: str
    requirement_id: str | None
    version_id: str | None


@dataclass
class ItemRequirementPlan:
    organization_id: str
    requirements: list = field(default_factory=list)
    maps: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    ambiguous_deficiencies: list = field(default_factory=list)

    def problem(self, text):
        if len(self.problems) < MAX_PROBLEMS:
            self.problems.append(text)

    def counts(self):
        return {
            "requirements": len(self.requirements),
            "requirement_versions": sum(len(r.versions) for r in self.requirements),
            "mapped_item_rows": sum(1 for m in self.maps if m.requirement_id),
            "kit_content_rows": sum(1 for m in self.maps if not m.requirement_id),
            "ambiguous_deficiencies": len(self.ambiguous_deficiencies),
        }


def _place(row):
    if row.compartment_id:
        return ("COMPARTMENT", str(row.compartment.stable_slot_id))
    if row.system_id:
        return ("SYSTEM", str(row.system.stable_slot_id))
    return ("NOT_PLACED", None)


def _place_label(row):
    if row.compartment_id:
        return row.compartment.label
    if row.system_id:
        return row.system.label
    return "Not placed"


class _Vehicle:
    """Replay state of one vehicle: requirements by item, and each checklist's current links."""

    def __init__(self, plan, vehicle):
        self.plan, self.vehicle = plan, vehicle
        self.by_item = {}
        self.current = {}  # template id -> {item id: requirement plan}

    def users(self, requirement, template_id):
        return sorted(
            label
            for other, (label, refs) in self.current.items()
            if other != template_id and any(r is requirement for r in refs.values())
        )

    def version(self, requirement, at, row, place):
        version = VersionPlan(
            id=new_id(),
            requirement_id=requirement.id,
            revision=len(requirement.versions) + 1,
            effective_at=at,
            place=place,
            compartment_id=row.compartment_id,
            system_id=row.system_id,
            fields=fields_of(row),
        )
        requirement.versions.append(version)
        return version

    def publish(self, template, version_id, at, rows):
        refs = {}
        previous = self.current.get(str(template.pk), (template.label, {}))[1]
        for row in rows:
            if row.override_version_id is not None:
                self.plan.problem(f"item row {row.pk} belongs to a legacy override version")
                continue
            if row.kit_id is not None:
                self.plan.maps.append(
                    MapPlan(str(self.vehicle.pk), version_id, str(row.pk), None, None)
                )
                continue
            item_id = str(row.item_id)
            place = _place(row)
            requirements = self.by_item.setdefault(item_id, [])
            here = [r for r in requirements if (v := r.at(at)) is not None and v.place == place]
            where = f"{self.vehicle.display_name}: {row.item.label} at {_place_label(row)}"
            if here:
                requirement = here[0]
                current = requirement.at(at)
                if current.fields == fields_of(row):
                    version = current
                elif users := self.users(requirement, str(template.pk)):
                    self.plan.problem(
                        f"conflict, {where}: {template.label} requires other than "
                        f"{', '.join(users)}"
                    )
                    continue
                else:
                    version = self.version(requirement, at, row, place)
            else:
                moved = previous.get(item_id)
                if moved is not None and (users := self.users(moved, str(template.pk))):
                    self.plan.problem(
                        f"possible move or split, {where}: {template.label} moves it while "
                        f"{', '.join(users)} keeps it at the earlier place"
                    )
                    continue
                if moved is not None:
                    requirement = moved
                else:
                    requirement = RequirementPlan(new_id(), str(self.vehicle.pk), item_id)
                    requirements.append(requirement)
                    self.plan.requirements.append(requirement)
                version = self.version(requirement, at, row, place)
            refs[item_id] = requirement
            self.plan.maps.append(
                MapPlan(str(self.vehicle.pk), version_id, str(row.pk), requirement.id, version.id)
            )
        self.current[str(template.pk)] = (template.label, refs)


def _ambiguous_deficiencies(plan, vehicles):
    from cya.deficiencies.models import Deficiency

    places = {
        (state.vehicle.pk, item_id): len(requirements)
        for state in vehicles.values()
        for item_id, requirements in state.by_item.items()
    }
    for deficiency in Deficiency.objects.filter(
        organization_id=plan.organization_id,
        item__isnull=False,
        kit__isnull=True,
        compartment__isnull=True,
    ).select_related("vehicle", "item"):
        if places.get((deficiency.vehicle_id, str(deficiency.item_id)), 0) > 1:
            plan.ambiguous_deficiencies.append(
                f"deficiency {deficiency.pk} names {deficiency.item.label} on "
                f"{deficiency.vehicle.display_name}, which is kept in more than one place"
            )


def plan_item_requirements(organization_id):
    """Plan SPEC 18.3 items 1 and 2 for one organization; reads only."""
    org = str(canonical_id(organization_id))
    plan = ItemRequirementPlan(org)
    if ItemRequirementCutover.objects.filter(organization_id=org).exists():
        plan.problem("the organization has already moved to item requirements")
        return plan
    if not ChecklistCutover.objects.filter(organization_id=org).exists():
        plan.problem("move the organization to vehicle checklists first (SPEC 18.2)")
        return plan
    vehicles = {}
    publications = (
        ChecklistPublication.objects.filter(
            organization_id=org, checklist_version__template__vehicle__isnull=False
        )
        .select_related("checklist_version__template__vehicle")
        .order_by(
            "effective_at",
            "checklist_version__template__precedence",
            "checklist_version__template_id",
        )
    )
    for publication in publications:
        version = publication.checklist_version
        template = version.template
        vehicle = template.vehicle
        state = vehicles.setdefault(vehicle.pk, _Vehicle(plan, vehicle))
        rows = list(
            ChecklistItemDef.objects.filter(checklist_version=version)
            .select_related("item", "compartment", "system")
            .order_by("display_order", "pk")
        )
        state.publish(template, str(version.pk), publication.effective_at, rows)
    _ambiguous_deficiencies(plan, vehicles)
    return plan


def _evidence(org, receipt, actor):
    return {
        "organization_id": org,
        "command": receipt,
        "recorded_at": receipt.recorded_at,
        "actor_user": actor.user,
        "actor_roles_at_time": list(actor.roles),
    }


def _write(plan, evidence):
    ItemRequirementCutover.objects.create(id=new_id(), **evidence)
    for requirement in plan.requirements:
        VehicleItemRequirement.objects.create(
            id=requirement.id,
            vehicle_id=requirement.vehicle_id,
            item_id=requirement.item_id,
            **evidence,
        )
    owner = {r.id: r for r in plan.requirements}
    versions = sorted(
        (v for r in plan.requirements for v in r.versions),
        key=lambda v: (v.effective_at, v.revision),
    )
    for version in versions:
        requirement = owner[version.requirement_id]
        VehicleItemRequirementVersion.objects.create(
            id=version.id,
            requirement_id=requirement.id,
            vehicle_id=requirement.vehicle_id,
            item_id=requirement.item_id,
            revision=version.revision,
            effective_at=version.effective_at,
            place_kind=version.place[0],
            place_slot_id=version.place[1],
            compartment_id=version.compartment_id,
            system_id=version.system_id,
            **{name: version.fields[name] for name in FIELDS},
            **evidence,
        )
    LegacyItemRequirementMap.objects.bulk_create(
        LegacyItemRequirementMap(
            id=new_id(),
            vehicle_id=row.vehicle_id,
            checklist_version_id=row.checklist_version_id,
            item_def_id=row.item_def_id,
            kit_content=row.requirement_id is None,
            requirement_id=row.requirement_id,
            requirement_version_id=row.version_id,
            **evidence,
        )
        for row in plan.maps
    )


def migrate_item_requirements(*, organization_id, actor_id, operation_id):
    """Apply SPEC 18.3 items 1 and 2 as one audited, all-or-nothing command."""
    from cya.kits.definitions import authorizer

    org = str(canonical_id(organization_id))
    canonical_id(actor_id)

    def prepare(_actor):
        # The configuration lock: no checklist can be published while the plan is applied.
        list(Vehicle.objects.select_for_update().filter(organization_id=org).order_by("pk"))
        probe = plan_item_requirements(org)
        if probe.problems:
            raise ItemRequirementMigrationError("; ".join(probe.problems))

        def apply(receipt, actor):
            plan = plan_item_requirements(org)
            if plan.problems:
                raise ItemRequirementMigrationError("; ".join(plan.problems))
            _write(plan, _evidence(org, receipt, actor))

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


def resolve_item_requirement(item_def_id):
    """(requirement id, version id) an item row inspects, through its link or the map.

    Both are None for kit content and for rows of versions that were never published.
    """
    row = (
        ChecklistItemDef.objects.filter(pk=item_def_id)
        .values("item_requirement_version_id", "item_requirement_version__requirement_id")
        .first()
    )
    if row and row["item_requirement_version_id"]:
        return (
            str(row["item_requirement_version__requirement_id"]),
            str(row["item_requirement_version_id"]),
        )
    mapped = (
        LegacyItemRequirementMap.objects.filter(item_def_id=item_def_id)
        .values_list("requirement_id", "requirement_version_id")
        .first()
    )
    if mapped and mapped[0]:
        return (str(mapped[0]), str(mapped[1]))
    return (None, None)
