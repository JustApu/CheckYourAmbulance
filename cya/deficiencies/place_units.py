"""Which place a deficiency row concerns, for the attention readers (SPEC 6.0.0 7.5, issue #256 C3).

After an organization's item-requirement cutover, a deficiency on an item outside a kit concerns
one item requirement (one stock of the item in one place, CHK-11): the header and each affected
row name its version. A deficiency reported before the cutover names none; one whose place a
Supervisor recorded is mapped by the cutover (deficiency_requirement_map). Any other row keeps the
per-item rule: it applies to every place of the item.

Both helpers add nothing to a read's statement count: with_place annotates the header query the
reader already runs, and affected_unit is pure.
"""

from django.db.models import CharField, F, OuterRef, Subquery
from django.db.models.functions import Coalesce

from cya.deficiencies.models import DeficiencyRequirementMap


def with_place(deficiencies):
    """Annotate place_requirement_id: the item requirement an item target outside a kit concerns.

    The header's version first (reported after the cutover), else the cutover's map for a
    deficiency whose place was recorded before it (SPEC 6.0.0 18.3 item 2); otherwise None.
    """
    mapped = DeficiencyRequirementMap.objects.filter(deficiency_id=OuterRef("pk")).values(
        "requirement_id"
    )[:1]
    return deficiencies.annotate(
        place_requirement_id=Coalesce(
            F("item_requirement_version__requirement_id"),
            Subquery(mapped),
            output_field=CharField(),
        )
    )


def affected_unit(place, item_id, line, requirement_id):
    """The item requirement an affected row is evaluated for, or None (per item, or a kit).

    A row that names its version is that requirement's; a row of the item that a placed item
    target names (place: (item_id, requirement id)) is the target's; any other row keeps the
    per-item rule.
    """
    if requirement_id:
        return str(requirement_id)
    if place and not line and place[0] == item_id:
        return place[1]
    return None
