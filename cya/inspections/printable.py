"""Printable outage checklists and kit reference (issue #9 Part A, SPEC §17.8 DEP-11).

The app is online-only, so crews record checks on paper during an outage. The outage sheet lists
the vehicle's current checklist rows, grouped the way the checklist screen groups them, with
blank space to record the result by hand. A kit gets one row, not its contents: the crew either
confirms the seal or checks the contents against the separate kit reference, which is printed
once per checklist revision and laminated (owner decisions on issue #9). Both documents carry only
checklist definitions: no check results, crew names or other personal data.
"""

from dataclasses import dataclass, field

from django.utils.safestring import mark_safe

from cya.checklists.models import (
    ChecklistItemDef,
    ChecklistTemplate,
    ChecklistVersion,
    Compartment,
)
from cya.checklists.overlay import effective_template_version
from cya.kits.sealable import kit_sealable

OUTCOME_LABELS = {
    "PASSED": "Pass",
    "FAILED": "Fail",
    "UNABLE_TO_INSPECT": "Unable",
    "NOT_APPLICABLE": "N/A",
}
# Kinds whose result is written as a number rather than only ticked (SPEC 4.4).
VALUE_KINDS = frozenset({"QUANTITY", "MEASUREMENT", "VEHICLE_READING"})
REFERENCE_ITEM_LIMIT = 5000  # kit item definitions read for one reference document
REFERENCE_TEMPLATE_LIMIT = 200  # checklist templates read for one reference document


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
    # A sealable compartment (SPEC 10.2 CAB-9) prints one seal-or-contents row, not its items.
    sealed: bool = False


@dataclass
class KitRow:
    label: str
    place: str
    # Designated sealed (SEAL-1): the row offers the seal; otherwise contents are checked.
    sealed: bool = False
    # Inside a sealable compartment, whose intact seal covers the kit (CAB-2).
    in_sealed_compartment: bool = False


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


def outage_sheet(rows, sealable_compartments=frozenset(), sealed_kits=frozenset()):
    """(sections, kits): the vehicle's own rows by place in checklist order, and one row per kit
    instance in order of first appearance. A kit's contents are on the kit reference instead.

    sealable_compartments holds the compartment ids (as rows name them) designated sealable at
    the sheet's instant; sealed_kits the kit ids designated sealed then. A sealable compartment
    becomes one sealed section with no item rows, including one that holds only kits.
    """
    found, kits = {}, {}
    for row in rows:
        sealed_place = bool(row.compartment_id) and str(row.compartment_id) in sealable_compartments
        if sealed_place and ("compartment", row.compartment_id) not in found:
            found[("compartment", row.compartment_id)] = PrintSection(
                heading=row.compartment_label, sealed=True
            )
        if row.kit_id:
            if row.kit_id not in kits:
                kits[row.kit_id] = KitRow(
                    label=row.kit_label,
                    place=row.compartment_label or "",
                    sealed=str(row.kit_id) in sealed_kits,
                    in_sealed_compartment=sealed_place,
                )
            continue
        key, heading = _section_key(row)
        if key not in found:
            found[key] = PrintSection(heading=heading)
        if not found[key].sealed:
            found[key].rows.append(_print_row(row))
    return list(found.values()), list(kits.values())


def sealable_designations(organization_id, checklist):
    """(sealable compartment ids, sealed kit ids) for a vehicle checklist, at its instant.

    A row's compartment id is the slot revision in force at that instant (vehicle_slots), so
    its sealable flag is the designation then (CAB-1). Kit designations come from the
    append-only kit_seal_designation history (SEAL-1).
    """
    compartment_ids = {str(r.compartment_id) for r in checklist.rows if r.compartment_id}
    sealable = {
        str(pk)
        for pk in Compartment.objects.filter(
            organization_id=organization_id, pk__in=compartment_ids, sealable=True
        ).values_list("pk", flat=True)
    }
    kit_ids = {str(r.kit_id) for r in checklist.rows if r.kit_id}
    sealed = set(kit_sealable(organization_id, kit_ids, checklist.as_of))
    return frozenset(sealable), frozenset(sealed)


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
    templates = list(
        ChecklistTemplate.objects.filter(organization_id=organization_id, active=True).order_by(
            "label", "pk"
        )[: REFERENCE_TEMPLATE_LIMIT + 1]
    )
    if len(templates) > REFERENCE_TEMPLATE_LIMIT:
        raise ValueError("too many active checklist templates to print one kit reference")
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


# Characters printed as themselves in a CSS string; anything else is a CSS hex escape, so names
# cannot end the string, the declaration or the <style> element.
CSS_PLAIN = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ,.-:()")


def css_string(text):
    """text as a quoted CSS string literal, safe to place inside a <style> element."""
    escaped = "".join(c if c in CSS_PLAIN else f"\\{ord(c):06x}" for c in text)
    return mark_safe(f'"{escaped}"')


def reference_page_header(templates):
    """The kit reference's running page header: every revision and version ID it prints.

    A printed page of the reference carries this in its top margin (CSS @page), so a page that
    comes loose still names the checklist revisions it was printed from.
    """
    parts = [f"{t['label']} ({t['id']})" for t in templates]
    return css_string("Kit reference: " + "; ".join(parts) if parts else "Kit reference")
