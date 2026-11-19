"""Reading legacy requirements after the cutover (SPEC 5.0.0 18.2 step 3).

An obligation created before the cutover pins a legacy requirement: a legacy template's
publication and binding, the vehicle's override and an item row of the template or override.
After the cutover the same due instant resolves to the migrated vehicle checklist, whose rows
are new. carried_over says whether a requirement resolved from migrated rows is the legacy one
carried across the migration maps unchanged. The planner then keeps the existing obligation
instead of replacing it ("preserve existing obligations and their due instants without
recomputing"); a real change made after the cutover still replaces it under SPEC 5.7.

verify_carry_over is the migration's own check (step 4): inside the migration's transaction it
resolves every future due instant both ways and reports any difference, which refuses the
whole migration.
"""

from datetime import timedelta

from cya.checklists.models import (
    ChecklistPublication,
    LegacyChecklistMap,
    LegacyItemDefMap,
    LegacyVersionMap,
)
from cya.kits.models import ChecklistKitPlacement
from cya.obligations.models import Obligation, RequirementBinding
from cya.obligations.requirements import desired_at, occurrences

# Pins compared as they are: none of them changes in the migration.
DIRECT = (
    "schedule_rule_id",
    "conditional_rule_id",
    "kit_id",
    "kit_event_id",
    "kit_definition_item_id",
    "kit_requirement_id",
)


def pins_of_version(version):
    """The pins of a requirement_version row, by the names a DesiredItem uses."""
    return {
        "binding_id": version.binding_id,
        "publication_id": version.publication_id,
        "override_version_id": version.override_version_id,
        "item_def_id": version.item_def_id,
        "placement_id": version.placement_id,
        "schedule_rule_id": version.schedule_rule_id,
        "conditional_rule_id": version.conditional_rule_id,
        "kit_id": version.kit_instance_id,
        "kit_event_id": version.kit_event_id,
        "kit_definition_item_id": version.kit_definition_item_id,
        "kit_requirement_id": version.kit_requirement_id,
    }


def pins_of_desired(desired):
    return {
        "binding_id": desired.binding_id,
        "publication_id": desired.publication_id,
        "override_version_id": desired.override_version_id,
        "item_def_id": desired.item_def_id,
        "placement_id": desired.placement_id,
        **{name: getattr(desired, name) for name in DIRECT},
    }


def _text(value):
    return str(value) if value is not None else None


def _same_chain(old_binding, new_binding):
    """The new assignment chain is the migrated copy of the old binding chain."""
    if old_binding is None or new_binding is None:
        return False
    if (str(old_binding.vehicle_id), old_binding.slot) != (
        str(new_binding.vehicle_id),
        new_binding.slot,
    ):
        return False
    return LegacyChecklistMap.objects.filter(
        vehicle_id=new_binding.vehicle_id,
        checklist_id=new_binding.template_id,
        legacy_template_id=old_binding.template_id,
    ).exists()


def _version_of(publication_id):
    return (
        ChecklistPublication.objects.filter(pk=publication_id)
        .values_list("checklist_version_id", flat=True)
        .first()
    )


def carried_over(old, new):
    """Whether pins `new` (migrated rows) are legacy pins `old` carried across the maps."""
    old = {k: _text(v) for k, v in old.items()}
    new = {k: _text(v) for k, v in new.items()}
    if old == new:
        return True
    if any(old[name] != new[name] for name in DIRECT) or new["override_version_id"]:
        return False
    bindings = {
        str(b.pk): b
        for b in RequirementBinding.objects.filter(pk__in=[old["binding_id"], new["binding_id"]])
    }
    if not _same_chain(bindings.get(old["binding_id"]), bindings.get(new["binding_id"])):
        return False
    old_version, new_version = (
        _version_of(old["publication_id"]),
        _version_of(new["publication_id"]),
    )
    mapped = LegacyVersionMap.objects.filter(checklist_version_id=new_version).first()
    if mapped is None or (
        _text(mapped.legacy_template_version_id),
        _text(mapped.legacy_override_version_id),
    ) != (_text(old_version), old["override_version_id"]):
        return False
    if (old["item_def_id"] is None) != (new["item_def_id"] is None):
        return False
    if (
        new["item_def_id"] is not None
        and not LegacyItemDefMap.objects.filter(
            item_def_id=new["item_def_id"], legacy_item_def_id=old["item_def_id"]
        ).exists()
    ):
        return False
    return _same_placement(old, new, old_version, new_version)


def _same_placement(old, new, old_version, new_version):
    if (old["placement_id"] is None) != (new["placement_id"] is None):
        return False
    if new["placement_id"] is None:
        return True
    rows = {
        str(p.pk): p
        for p in ChecklistKitPlacement.objects.filter(
            pk__in=[old["placement_id"], new["placement_id"]]
        )
    }
    before, after = rows.get(old["placement_id"]), rows.get(new["placement_id"])
    return (
        before is not None
        and after is not None
        and str(before.checklist_version_id) == str(old_version)
        and str(after.checklist_version_id) == str(new_version)
        and before.definition_id == after.definition_id
    )


def legacy_templates_of(vehicle_id, checklist_id):
    """The legacy template a migrated vehicle checklist was made from, as a list (0 or 1)."""
    return [
        str(t)
        for t in LegacyChecklistMap.objects.filter(
            vehicle_id=vehicle_id, checklist_id=checklist_id
        ).values_list("legacy_template_id", flat=True)
    ]


# --- the migration's own check ----------------------------------------------------------------


def _due_instants(organization_id, vehicle_id, cutover, days):
    """Due instants to compare: every obligation already generated from the cutover on, plus
    every scheduled one in the next `days` days."""
    dues = set(
        Obligation.objects.filter(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            routine=True,
            due_at__gte=cutover,
        ).values_list("due_at", flat=True)
    )
    for occurrence in occurrences(
        organization_id, vehicle_id, cutover, cutover + timedelta(days=days)
    ):
        dues.add(occurrence.due_at)
    # The first due instant at or after each later published boundary, however far ahead.
    boundaries = ChecklistPublication.objects.filter(
        organization_id=organization_id,
        checklist_version__template__vehicle_id=vehicle_id,
        effective_at__gt=cutover + timedelta(days=days),
    ).values_list("effective_at", flat=True)
    for boundary in sorted(set(boundaries)):
        following = occurrences(
            organization_id, vehicle_id, boundary, boundary + timedelta(days=days)
        )
        if following:
            dues.add(following[0].due_at)
    return sorted(dues)


def _compare(organization_id, vehicle_id, due_at, cutover):
    before = desired_at(
        organization_id,
        vehicle_id,
        due_at,
        occurrences(organization_id, vehicle_id, due_at, due_at, cutover),
        cutover,
    )
    after = desired_at(
        organization_id,
        vehicle_id,
        due_at,
        occurrences(organization_id, vehicle_id, due_at, due_at),
    )
    where = f"vehicle {vehicle_id} at {due_at.isoformat()}"
    if set(before.items) != set(after.items):
        return f"{where}: items differ"
    for key, old in before.items.items():
        if not carried_over(pins_of_desired(old), pins_of_desired(after.items[key])):
            return f"{where}: item {key[0]} resolves differently"
    return None


def verify_carry_over(organization_id, cutover, days):
    """Differences between the legacy and migrated resolution of future due instants."""
    vehicles = sorted(
        {
            str(v)
            for v in RequirementBinding.objects.filter(organization_id=organization_id).values_list(
                "vehicle_id", flat=True
            )
        }
    )
    problems = []
    for vehicle_id in vehicles:
        for due_at in _due_instants(organization_id, vehicle_id, cutover, days):
            problem = _compare(organization_id, vehicle_id, due_at, cutover)
            if problem:
                problems.append(problem)
    return problems
