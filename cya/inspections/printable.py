"""Printable outage checklists and kit reference (issue #9 Part A, SPEC §17.8 DEP-11).

The app is online-only, so crews record checks on paper during an outage. The outage sheet lists
the vehicle's current checklist rows, grouped the way the checklist screen groups them, with
blank space to record the result by hand. A kit gets one row, not its contents: the crew either
confirms the seal or checks the contents against the separate kit reference, which is printed
once per checklist revision and laminated (owner decisions on issue #9). Both documents carry only
checklist definitions: no check results, crew names or other personal data.
"""

from dataclasses import dataclass, field

from cya.checklists.models import ChecklistItemDef, ChecklistTemplate, ChecklistVersion
from cya.checklists.overlay import effective_template_version

OUTCOME_LABELS = {
    "PASSED": "Pass",
    "FAILED": "Fail",
    "UNABLE_TO_INSPECT": "Unable",
    "NOT_APPLICABLE": "N/A",
}
# Kinds whose result is written as a number rather than only ticked (SPEC 4.4).
VALUE_KINDS = frozenset({"QUANTITY", "MEASUREMENT", "VEHICLE_READING"})
REFERENCE_ITEM_LIMIT = 5000  # kit item definitions read for one reference document


@dataclass
class PrintRow:
    label: str
    instructions: str
    outcomes: tuple
    expected: str
    value_unit: str | None


@dataclass
class PrintSection:
    heading: str
    rows: list = field(default_factory=list)


@dataclass
class KitRow:
    label: str
    place: str


def _expected(kind, quantity, unit):
    if kind != "QUANTITY" or quantity is None:
        return ""
    return f"{quantity} {unit}" if unit else str(quantity)


def _value_unit(row):
    """The unit printed beside the blank value box, or None when no value is written."""
    if row.kind not in VALUE_KINDS:
        return None
    return row.unit or ""


def _print_row(row):
    return PrintRow(
        label=row.item_label,
        instructions=row.instructions,
        outcomes=tuple(OUTCOME_LABELS.get(code, code) for code in row.allowed_outcomes),
        expected=_expected(row.kind, row.expected_quantity, row.unit),
        value_unit=_value_unit(row),
    )


def _section_key(row):
    """Rows group by compartment, then vehicle system, else vehicle-wide."""
    if row.compartment_label:
        return ("compartment", row.compartment_id), row.compartment_label
    if row.system_label:
        return ("system", row.system_id), row.system_label
    return ("vehicle", None), "Vehicle-wide"


def outage_sheet(rows):
    """(sections, kits): the vehicle's own rows by place in checklist order, and one row per kit
    instance in order of first appearance. A kit's contents are on the kit reference instead."""
    found, kits = {}, {}
    for row in rows:
        if row.kit_id:
            if row.kit_id not in kits:
                kits[row.kit_id] = KitRow(label=row.kit_label, place=row.compartment_label or "")
            continue
        key, heading = _section_key(row)
        if key not in found:
            found[key] = PrintSection(heading=heading)
        found[key].rows.append(_print_row(row))
    return list(found.values()), list(kits.values())


def versions(organization_id, rows):
    """'Template label, revision N' for each checklist version the rows come from, in order."""
    ids = list(dict.fromkeys(row.checklist_version_id for row in rows if row.checklist_version_id))
    found = {
        str(version.pk): version
        for version in ChecklistVersion.objects.filter(
            organization_id=organization_id, pk__in=ids
        ).select_related("template")
    }
    result = []
    for version_id in ids:
        version = found.get(version_id)
        if version is None:
            raise ValueError(f"checklist version {version_id} is not in this organization")
        result.append(
            {
                "label": f"{version.template.label}, revision {version.revision}",
                "id": version_id,
            }
        )
    return result


def _kit_types(version):
    """[(kit type name, [PrintRow])] for one template version, in display order.

    A definition's kit names the kit type it describes (SPEC 4.2, OQ-53); every instance of that
    type should match this list. Vehicle overrides are not applied: this is the agency standard.
    """
    definitions = list(
        ChecklistItemDef.objects.filter(checklist_version=version, kit__isnull=False)
        .select_related("item", "kit")
        .order_by("kit__label", "kit_id", "display_order", "pk")[: REFERENCE_ITEM_LIMIT + 1]
    )
    if len(definitions) > REFERENCE_ITEM_LIMIT:
        raise ValueError(f"checklist version {version.pk} lists too many kit items to print")
    kits = {}
    for definition in definitions:
        name = definition.kit.label
        kits.setdefault((name, definition.kit_id), []).append(
            PrintRow(
                label=definition.item.label,
                instructions=definition.instructions,
                outcomes=(),
                expected=_expected(definition.kind, definition.expected_quantity, definition.unit),
                value_unit=None,
            )
        )
    return [(name, rows) for (name, _kit_id), rows in kits.items()]


def kit_reference(organization_id, as_of):
    """Kit types per active checklist template, from each template's version in force at as_of.

    Templates without a version in force, or whose version names no kit, are left out.
    """
    result = []
    templates = ChecklistTemplate.objects.filter(
        organization_id=organization_id, active=True
    ).order_by("label", "pk")
    for template in templates:
        version = effective_template_version(organization_id, template.pk, as_of)
        if version is None:
            continue
        kit_types = _kit_types(version)
        if kit_types:
            result.append(
                {
                    "label": f"{template.label}, revision {version.revision}",
                    "id": str(version.pk),
                    "kit_types": kit_types,
                }
            )
    return result
