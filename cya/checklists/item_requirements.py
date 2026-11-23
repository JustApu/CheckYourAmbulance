"""Vehicle item requirements at checklist publication (SPEC 6.0.0 CHK-5, CHK-11; issue #256 C1).

After an organization's item-requirement cutover, publishing a vehicle checklist version links
each of its item rows outside a kit to the version of the vehicle item requirement it inspects.
In C1 requirement versions are published only this way, by these rules, at the checklist
version's effective instant E, for an item I kept at place P on vehicle V:

(a) A requirement of (V, I) in force at P at E with equal requirements: the row names that
    version.
(b) One in force at P with different requirements: if no other checklist uses that requirement
    at or after E, a new version of it takes effect at E; otherwise the publication is refused
    (AC-42 (1)), naming the other checklists.
(c) None at P, but this checklist's previous version kept I at P' with requirement R, and no
    other checklist uses R at or after E: R moves, so a new version of R at P takes effect at E
    (a move keeps the stock's identity, CHK-11).
(d) Otherwise a new requirement for (V, I) at P, with its first version at E.

A requirement's version is in force from its effective instant until the requirement's next
version, so at most one requirement is in force per (V, I, P) at any instant (18.1 item 8); the
database refuses a version that would break that (migration 0014). Kit content keeps its kit
definition's requirements and is never linked.
"""

from cya.checklists.models import (
    ChecklistItemDef,
    ChecklistPublication,
    Compartment,
    ItemRequirementCutover,
    LegacyItemRequirementMap,
    VehicleItemRequirement,
    VehicleItemRequirementVersion,
    VehicleSystem,
)

FIELDS = (
    "kind",
    "expected_quantity",
    "unit",
    "allowed_outcomes",
    "expiration_tracked",
    "instructions",
)


class ItemRequirementError(ValueError):
    """A publication would give an item and place requirements other than its vehicle's."""


def has_cutover(organization_id):
    return ItemRequirementCutover.objects.filter(organization_id=organization_id).exists()


def place_of(row):
    """(place_kind, stable slot id or None) of an item row or requirement version."""
    if row.compartment_id:
        slot = Compartment.objects.values_list("stable_slot_id", flat=True).get(
            pk=row.compartment_id
        )
        return ("COMPARTMENT", str(slot))
    if row.system_id:
        slot = VehicleSystem.objects.values_list("stable_slot_id", flat=True).get(pk=row.system_id)
        return ("SYSTEM", str(slot))
    return ("NOT_PLACED", None)


def fields_of(row):
    return {name: getattr(row, name) for name in FIELDS}


def _version_place(version):
    return (version.place_kind, str(version.place_slot_id) if version.place_slot_id else None)


def _in_force(versions, at):
    """{requirement id: its version in force at `at`} among versions of one vehicle and item."""
    current = {}
    for version in sorted(versions, key=lambda v: v.effective_at):
        if version.effective_at <= at:
            current[version.requirement_id] = version
    return current


def requirement_of_row(row):
    """The requirement id a published item row inspects: its own link, else the source-row map."""
    if row.item_requirement_version_id:
        return VehicleItemRequirementVersion.objects.values_list("requirement_id", flat=True).get(
            pk=row.item_requirement_version_id
        )
    return (
        LegacyItemRequirementMap.objects.filter(item_def_id=row.pk)
        .values_list("requirement_id", flat=True)
        .first()
    )


def _in_force_from(version_id, template_id, at):
    """Whether a published checklist version is still in force at or after `at`."""
    publication = ChecklistPublication.objects.filter(checklist_version_id=version_id).first()
    if publication is None:
        return False
    later = (
        ChecklistPublication.objects.filter(
            checklist_version__template_id=template_id, effective_at__gt=publication.effective_at
        )
        .order_by("effective_at")
        .values_list("effective_at", flat=True)
        .first()
    )
    return later is None or later > at


def other_users(requirement_id, template_id, at):
    """Labels of other checklists whose version in force at or after `at` uses the requirement."""
    rows = list(
        ChecklistItemDef.objects.filter(item_requirement_version__requirement_id=requirement_id)
        .exclude(checklist_version__template_id=template_id)
        .values_list(
            "checklist_version_id",
            "checklist_version__template_id",
            "checklist_version__template__label",
        )
    ) + list(
        LegacyItemRequirementMap.objects.filter(requirement_id=requirement_id)
        .exclude(checklist_version__template_id=template_id)
        .values_list(
            "checklist_version_id",
            "checklist_version__template_id",
            "checklist_version__template__label",
        )
    )
    return sorted(
        {label for version_id, other, label in rows if _in_force_from(version_id, other, at)}
    )


def _previous_rows(template_id, at):
    """{item id: row} of the template's version in force just before `at`, if any."""
    publication = (
        ChecklistPublication.objects.filter(
            checklist_version__template_id=template_id, effective_at__lt=at
        )
        .order_by("-effective_at")
        .first()
    )
    if publication is None:
        return {}
    return {
        str(row.item_id): row
        for row in ChecklistItemDef.objects.filter(
            checklist_version_id=publication.checklist_version_id, kit__isnull=True
        )
    }


class _Writer:
    def __init__(self, organization_id, vehicle_id, receipt, actor):
        self.evidence = {
            "organization_id": organization_id,
            "command": receipt,
            "recorded_at": receipt.recorded_at,
            "actor_user": actor.user,
            "actor_roles_at_time": list(actor.roles),
        }
        self.vehicle_id = vehicle_id

    def requirement(self, item_id):
        return VehicleItemRequirement.objects.create(
            vehicle_id=self.vehicle_id, item_id=item_id, **self.evidence
        )

    def version(self, requirement_id, item_id, at, row):
        kind, slot = place_of(row)
        revision = (
            VehicleItemRequirementVersion.objects.filter(requirement_id=requirement_id).count() + 1
        )
        return VehicleItemRequirementVersion.objects.create(
            requirement_id=requirement_id,
            vehicle_id=self.vehicle_id,
            item_id=item_id,
            revision=revision,
            effective_at=at,
            place_kind=kind,
            place_slot_id=slot,
            compartment_id=row.compartment_id,
            system_id=row.system_id,
            **fields_of(row),
            **self.evidence,
        )


def link_rows(*, organization_id, vehicle_id, template_id, rows, effective_at, receipt, actor):
    """Link each row outside a kit to its item requirement version, publishing versions as needed.

    Runs inside publish_template_version's transaction, before the publication row is written
    (the freeze triggers then keep the links). Raises ItemRequirementError on a refusal.
    """
    writer = _Writer(organization_id, vehicle_id, receipt, actor)
    previous = _previous_rows(template_id, effective_at)
    for row in rows:
        if row.kit_id is not None:
            continue
        version = _resolve(writer, template_id, row, effective_at, previous)
        ChecklistItemDef.objects.filter(pk=row.pk).update(item_requirement_version_id=version.pk)


def _resolve(writer, template_id, row, at, previous):
    item_id = str(row.item_id)
    place = place_of(row)
    versions = list(
        VehicleItemRequirementVersion.objects.filter(vehicle_id=writer.vehicle_id, item_id=item_id)
    )
    later = [v for v in versions if v.effective_at > at]
    if later:
        raise ItemRequirementError(
            f"item {item_id} has a requirement version published for a later instant; "
            "publish this checklist at or after it"
        )
    current = _in_force(versions, at)
    here = [v for v in current.values() if _version_place(v) == place]
    if here:
        (version,) = here  # at most one in force per item and place (18.1 item 8)
        if fields_of(version) == fields_of(row):
            return version  # (a)
        users = other_users(version.requirement_id, template_id, at)
        if users:  # AC-42 (1)
            raise ItemRequirementError(
                f"item {item_id} at this place has other requirements in "
                f"{', '.join(users)}; publish matching requirements"
            )
        return writer.version(version.requirement_id, item_id, at, row)  # (b)
    before = previous.get(item_id)
    moved = requirement_of_row(before) if before is not None else None
    if (
        moved
        and str(moved) in {str(k) for k in current}
        and not other_users(moved, template_id, at)
    ):
        return writer.version(moved, item_id, at, row)  # (c)
    return writer.version(writer.requirement(item_id).pk, item_id, at, row)  # (d)
