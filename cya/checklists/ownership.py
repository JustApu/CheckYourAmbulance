"""Who owns a checklist, and which model governs an instant (SPEC 5.0.0 CHK-1, 18.2).

A checklist_template row with a vehicle is a vehicle checklist; one without is a legacy shared
template from before 5.0.0. An organization moves from the legacy model to vehicle checklists at
one recorded cutover instant (ChecklistCutover, written by the migration of SPEC 18.2):

- a legacy template's bindings govern due instants before the cutover only;
- a vehicle checklist's assignments govern due instants at or after it (they are recorded at or
  after the cutover and never take effect before they are recorded, so this holds by
  construction; the check here makes it explicit);
- an organization without a cutover has only legacy templates.

Every reader takes the same `recorded_before` instant as the requirement resolver: the cutover
counts only if it was recorded strictly before it, so a question answered "as the records stood"
before the cutover still gets the legacy answer (cya.obligations.planning's freeze instant).
"""

from cya.checklists.models import ChecklistCutover, ChecklistTemplate


class LegacyReadOnlyError(ValueError):
    """A legacy template, override or binding cannot change after the cutover (SPEC 18.2)."""


def cutover_at(organization_id, recorded_before=None):
    """The organization's cutover instant, or None (none, or not recorded before the bound)."""
    rows = ChecklistCutover.objects.filter(organization_id=organization_id)
    if recorded_before is not None:
        rows = rows.filter(recorded_at__lt=recorded_before)
    return rows.values_list("recorded_at", flat=True).first()


def governs(owner_vehicle_id, due_at, cutover):
    """Whether a binding of a checklist with this owner may govern due instant due_at.

    owner_vehicle_id is the checklist's vehicle (None for a legacy template); cutover is the
    result of cutover_at for the same records bound.
    """
    if owner_vehicle_id is None:
        return cutover is None or due_at < cutover
    return cutover is not None and due_at >= cutover


def owners(template_ids):
    """{template id: (owner vehicle id or None, precedence or None)} for the given templates."""
    return {
        str(pk): (str(vehicle) if vehicle else None, precedence)
        for pk, vehicle, precedence in ChecklistTemplate.objects.filter(
            pk__in=[str(t) for t in template_ids]
        ).values_list("pk", "vehicle_id", "precedence")
    }


def precedence_key(template_id, precedence):
    """CHK-10 order of checklists within one schedule slot: precedence, else legacy identity.

    Legacy templates and vehicle checklists never govern the same due instant (governs), so the
    leading flag only keeps the order total.
    """
    if precedence is None:
        return (0, 0, str(template_id))
    return (1, precedence, str(template_id))


def require_legacy_writable(organization_id, error=LegacyReadOnlyError):
    """Refuse a write to a legacy template, override or binding once the cutover is recorded."""
    if cutover_at(organization_id) is not None:
        raise error(
            "checklists belong to each vehicle now; legacy templates, overrides and their "
            "bindings are read-only (SPEC 5.0.0)"
        )


def require_template_writable(organization_id, template, vehicle_id=None, error=ValueError):
    """Refuse a write that the checklist's ownership does not allow.

    A legacy template is writable only before the cutover. A vehicle checklist is writable only
    after it, and, when vehicle_id is given (a schedule assignment), only for its own vehicle.
    """
    if template.vehicle_id is None:
        require_legacy_writable(organization_id, error)
        return
    if cutover_at(organization_id) is None:
        raise error("vehicle checklists start at the cutover")
    if vehicle_id is not None and str(template.vehicle_id) != str(vehicle_id):
        raise error("a vehicle checklist is assigned only to its own vehicle")
