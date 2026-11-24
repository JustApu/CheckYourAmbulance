"""Vehicle item requirements at checklist publication (SPEC 6.0.0 CHK-5, CHK-11; issue #256 C1).

After an organization's item-requirement cutover, publishing a vehicle checklist version maps
each of its item rows outside a kit to the version of the vehicle item requirement it inspects
(an append-only item_requirement_map row; kit content is mapped to no requirement).
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

from dataclasses import dataclass

from cya.checklists.models import (  # noqa: F401 - new_requirement_id is re-exported
    ChecklistItemDef,
    ChecklistPublication,
    Compartment,
    ItemRequirementCutover,
    ItemRequirementMap,
    VehicleItemRequirement,
    VehicleItemRequirementVersion,
    VehicleSystem,
    new_requirement_id,
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
    """The requirement id a published item row inspects, from its map row (None if none)."""
    return (
        ItemRequirementMap.objects.filter(item_def_id=row.pk)
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
        ItemRequirementMap.objects.filter(requirement_id=requirement_id)
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

    def map(self, row, version):
        """The append-only link of a published row to its requirement version (or kit content)."""
        ItemRequirementMap.objects.create(
            vehicle_id=self.vehicle_id,
            checklist_version_id=row.checklist_version_id,
            item_def_id=row.pk,
            kit_content=version is None,
            requirement_id=version.requirement_id if version else None,
            requirement_version_id=version.pk if version else None,
            **self.evidence,
        )

    def requirement(self, item_id):
        return VehicleItemRequirement.objects.create(
            id=new_requirement_id(), vehicle_id=self.vehicle_id, item_id=item_id, **self.evidence
        )

    def version(self, requirement_id, item_id, at, row, version_id=None):
        kind, slot = place_of(row)
        revision = (
            VehicleItemRequirementVersion.objects.filter(requirement_id=requirement_id).count() + 1
        )
        return VehicleItemRequirementVersion.objects.create(
            **({"id": version_id} if version_id else {}),
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

    Runs inside publish_template_version's transaction, before the publication row is written.
    Each row gets an append-only map row: item rows are versioned and the runtime account cannot
    update them (SPEC 17.7 item 1). Raises ItemRequirementError on a refusal.
    """
    writer = _Writer(organization_id, vehicle_id, receipt, actor)
    previous = _previous_rows(template_id, effective_at)
    for row in rows:
        if row.kit_id is not None:
            writer.map(row, None)  # kit content keeps its kit definition's requirements
            continue
        version = _resolve(writer, template_id, row, effective_at, previous)
        writer.map(row, version)


def version_at(requirement_id, at):
    """The requirement's version in force at `at` (the latest effective by then), or None."""
    return (
        VehicleItemRequirementVersion.objects.filter(
            requirement_id=requirement_id, effective_at__lte=at
        )
        .order_by("-effective_at")
        .first()
    )


def latest_version(requirement_id):
    """The requirement's latest version, in force or scheduled, or None."""
    return (
        VehicleItemRequirementVersion.objects.filter(requirement_id=requirement_id)
        .order_by("-effective_at", "-revision")
        .first()
    )


def latest_here(vehicle_id, item_id, place):
    """The latest version, in force or scheduled, of the vehicle's requirement for the item
    whose latest version is at `place`, or None. Only the given place is looked at."""
    latest = {}
    for version in VehicleItemRequirementVersion.objects.filter(
        vehicle_id=vehicle_id, item_id=item_id
    ).order_by("effective_at", "revision"):
        latest[str(version.requirement_id)] = version
    found = [v for v in latest.values() if _version_place(v) == tuple(place)]
    return found[0] if found else None


def current_or_next(vehicle_id, at):
    """{requirement id: its version in force at `at`, else its first one scheduled after} for
    every requirement of the vehicle: what a vehicle file or a copy of the vehicle carries."""
    chosen = {}
    for version in VehicleItemRequirementVersion.objects.filter(vehicle_id=vehicle_id).order_by(
        "effective_at", "revision"
    ):
        key = str(version.requirement_id)
        if version.effective_at <= at or key not in chosen:
            chosen[key] = version
    return chosen


def in_force_here(vehicle_id, item_id, place, at):
    """The vehicle's requirement version for the item in force at `place` at `at`, or None.

    At most one exists (18.1 item 8). Only the given place is looked at: an item kept in several
    places has one requirement for each, and none of the others stands in for this one.
    """
    versions = VehicleItemRequirementVersion.objects.filter(vehicle_id=vehicle_id, item_id=item_id)
    found = [v for v in _in_force(versions, at).values() if _version_place(v) == tuple(place)]
    return found[0] if found else None


def plan_rows(*, vehicle_id, template_id, rows, effective_at):
    """{row id: RowPlan} for the rows outside a kit, read only: what link_rows would do.

    Rows of one checklist version name distinct items, and each rule reads only its own item's
    versions, so planning every row before writing any equals link_rows's row-by-row writes.
    """
    previous = _previous_rows(template_id, effective_at)
    return {
        str(row.pk): plan_row(str(vehicle_id), template_id, row, effective_at, previous)
        for row in rows
        if row.kit_id is None
    }


USES, NEW_VERSION, MOVES, NEW_REQUIREMENT, REFUSED = (
    "USES",
    "NEW_VERSION",
    "MOVES",
    "NEW_REQUIREMENT",
    "REFUSED",
)


@dataclass(frozen=True)
class RowPlan:
    """What publishing one row does to its item requirement: rules (a) to (d), or a refusal.

    rule is USES (a), NEW_VERSION (b), MOVES (c), NEW_REQUIREMENT (d) or REFUSED. version is the
    requirement version in force that the row uses (a), changes (b) or moves from (c).
    """

    rule: str
    item_id: str
    place: tuple
    requirement_id: str | None = None
    version: object = None
    users: tuple = ()  # REFUSED under AC-42 (1): the other checklists' labels
    reason: str = ""


def plan_row(vehicle_id, template_id, row, at, previous):
    """The rule (a) to (d) that publishing `row` at `at` applies, or its refusal. Reads only."""
    item_id = str(row.item_id)
    place = place_of(row)
    versions = list(
        VehicleItemRequirementVersion.objects.filter(vehicle_id=vehicle_id, item_id=item_id)
    )
    later = [v for v in versions if v.effective_at > at]

    def blocked_later(requirement_id=None, new_place=None):
        """Versions are appended in effective order (migration 0014): a version at `at` can
        follow neither its own requirement's later version nor, at a new place, another
        requirement's later version there (18.1 item 8). Unrelated places are not blocked."""
        return any(
            (requirement_id is not None and str(v.requirement_id) == str(requirement_id))
            or (new_place is not None and _version_place(v) == new_place)
            for v in later
        )

    def refused_later():
        return RowPlan(
            REFUSED,
            item_id,
            place,
            reason=f"item {item_id} has a requirement version published for a later instant "
            "here; publish this checklist at or after it",
        )

    current = _in_force(versions, at)
    here = [v for v in current.values() if _version_place(v) == place]
    if here:
        (version,) = here  # at most one in force per item and place (18.1 item 8)
        requirement_id = str(version.requirement_id)
        if fields_of(version) == fields_of(row):
            return RowPlan(USES, item_id, place, requirement_id, version)  # (a)
        users = other_users(version.requirement_id, template_id, at)
        if users:  # AC-42 (1)
            return RowPlan(
                REFUSED,
                item_id,
                place,
                requirement_id,
                version,
                tuple(users),
                f"item {item_id} at this place has other requirements in "
                f"{', '.join(users)}; publish matching requirements",
            )
        if blocked_later(requirement_id=requirement_id):
            return refused_later()
        return RowPlan(NEW_VERSION, item_id, place, requirement_id, version)  # (b)
    before = previous.get(item_id)
    moved = requirement_of_row(before) if before is not None else None
    if (
        moved
        and str(moved) in {str(k) for k in current}
        and not other_users(moved, template_id, at)
    ):
        if blocked_later(requirement_id=moved, new_place=place):
            return refused_later()
        source = next(v for k, v in current.items() if str(k) == str(moved))
        return RowPlan(MOVES, item_id, place, str(moved), source)  # (c)
    if blocked_later(new_place=place):
        return refused_later()
    return RowPlan(NEW_REQUIREMENT, item_id, place)  # (d)


def _resolve(writer, template_id, row, at, previous):
    plan = plan_row(writer.vehicle_id, template_id, row, at, previous)
    if plan.rule == REFUSED:
        raise ItemRequirementError(plan.reason)
    if plan.rule == USES:
        return plan.version
    if plan.rule == NEW_REQUIREMENT:
        return writer.version(writer.requirement(plan.item_id).pk, plan.item_id, at, row)
    return writer.version(plan.requirement_id, plan.item_id, at, row)
