"""Printable outage checklists (issue #9 Part A, SPEC §17.8 DEP-11).

The app is online-only, so crews record checks on paper during an outage. A sheet lists the
vehicle's current checklist rows, grouped the way the checklist screen groups them, with blank
space to record the result by hand. It carries only checklist definitions: no check results,
crew names or other personal data, so a printed copy can sit in the rig.
"""

from dataclasses import dataclass, field

from cya.checklists.models import ChecklistVersion

OUTCOME_LABELS = {
    "PASSED": "Pass",
    "FAILED": "Fail",
    "UNABLE_TO_INSPECT": "Unable",
    "NOT_APPLICABLE": "N/A",
}
# Kinds whose result is written as a number rather than only ticked (SPEC 4.4).
VALUE_KINDS = frozenset({"QUANTITY", "MEASUREMENT", "VEHICLE_READING"})


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
    kit: bool
    rows: list = field(default_factory=list)


def _expected(row):
    if row.kind != "QUANTITY" or row.expected_quantity is None:
        return ""
    unit = f" {row.unit}" if row.unit else ""
    return f"{row.expected_quantity}{unit}"


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
        expected=_expected(row),
        value_unit=_value_unit(row),
    )


def _section_key(row):
    """Kit rows group by kit instance; other rows by compartment, then system."""
    if row.kit_id:
        place = f" ({row.compartment_label})" if row.compartment_label else ""
        return ("kit", row.kit_id), f"Kit: {row.kit_label}{place}", True
    if row.compartment_label:
        return ("compartment", row.compartment_id), row.compartment_label, False
    if row.system_label:
        return ("system", row.system_id), row.system_label, False
    return ("vehicle", None), "Vehicle-wide", False


def sections(rows):
    """Rows grouped into sections in checklist order; kit sections follow the others.

    Each kit section is printed on its own page, so placing kits last keeps the vehicle's own
    items together and lets a kit's pages travel with the kit.
    """
    found = {}
    for row in rows:
        key, heading, kit = _section_key(row)
        if key not in found:
            found[key] = PrintSection(heading=heading, kit=kit)
        found[key].rows.append(_print_row(row))
    ordered = list(found.values())
    return [s for s in ordered if not s.kit] + [s for s in ordered if s.kit]


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
