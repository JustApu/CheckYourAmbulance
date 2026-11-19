"""Checklist bindings of one vehicle (SPEC 4.8 CFG-7, CFG-8, AC-37; issue #177).

Supervisors and Administrators (bind_unbind_template, PERM-49) see which checklist templates and
schedule slots a vehicle follows, bind another one from now or from a station-local date and
time, and unbind one. Anyone else gets 403 and an audited denial, and the command refuses a
direct post the same way.

A binding may be recorded while the vehicle is inactive; it owes nothing until the vehicle is
active (CFG-7). Unbinding is two steps (CFG-8): a preview of the inspections that will be
superseded and of those that stay owed, then a confirmation carrying the preview's digest. The
command recomputes the preview when it records the unbinding and refuses on any difference; the
page then shows the fresh preview.

Every change is a form post carrying an operation id rendered into the form, so a resubmitted
form replays rather than repeats.
"""

import logging
from urllib.parse import urlencode

from django.db.models import Max
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cya.checklists.models import ChecklistTemplate
from cya.checklists.ownership import cutover_at
from cya.core.audit_commands import CommandDenied, OperationConflict
from cya.kits import admin_pages
from cya.kits.views import UNAVAILABLE, _context_ids
from cya.obligations.commands import BIND_ACTION, BindingError, PreviewChanged, bind_requirement
from cya.obligations.models import RequirementBinding
from cya.obligations.unbinding import PreviewTooLarge, unbind_preview
from cya.schedules.models import ScheduleRule

logger = logging.getLogger(__name__)

DENIAL = "BIND_UNBIND_TEMPLATE"
SAVED = {
    "bound": "Template bound.",
    "unbound": "Template unbound. The inspections listed as superseded are no longer owed.",
    "assigned": "Checklist assigned.",
    "ended": "Assignment ended. The inspections listed as superseded are no longer owed.",
}
# Continuing obligations by state (cya.obligations.state.State), as the preview counts them.
STATE_LABELS = {
    "UPCOMING": "not yet open",
    "CURRENT": "open",
    "DUE": "due",
    "OVERDUE": "overdue",
    "SATISFIED": "done",
    "EXEMPT": "exempt",
    "WAIVED": "waived",
    "SUSPENDED": "suspended",
    "OWED": "owed",
}
CHANGED = "Inspections or the binding changed since the preview. Review the updated preview."


def _chains(organization_id, vehicle_id, per_vehicle=False):
    """The latest revision of each (template, slot) binding of the vehicle, by template label.

    per_vehicle (after the cutover): only the vehicle's own checklists' assignments; the legacy
    bindings are read-only history (SPEC 5.0.0 18.2).
    """
    chains = RequirementBinding.objects.filter(
        organization_id=organization_id, vehicle_id=vehicle_id
    )
    if per_vehicle:
        chains = chains.filter(template__vehicle_id=vehicle_id)
    tops = list(
        chains.values("template_id", "slot")
        .annotate(top=Max("revision"))
        .order_by("template_id", "slot")[: admin_pages.LIST_LIMIT]
    )
    if not tops:
        return []
    revisions = []
    for top in tops:
        revision = (
            RequirementBinding.objects.filter(
                organization_id=organization_id,
                vehicle_id=vehicle_id,
                template_id=top["template_id"],
                slot=top["slot"],
                revision=top["top"],
            )
            .select_related("template")
            .first()
        )
        if revision is not None:
            revisions.append(revision)
    return sorted(revisions, key=lambda r: (r.template.label.casefold(), r.slot))


def _rows(organization_id, vehicle_id, now, per_vehicle=False):
    rows = []
    for revision in _chains(organization_id, vehicle_id, per_vehicle):
        bound = revision.action == "BIND"
        upcoming = revision.effective_at > now
        if bound:
            state = "Starts" if upcoming else "Bound since"
        else:
            state = "Ends" if upcoming else "Unbound since"
        rows.append(
            {
                "template": revision.template,
                "slot": revision.slot,
                "state": state,
                "effective_at": revision.effective_at,
                # The page unbinds now, and a revision must take effect after the previous one,
                # so a binding that has not started yet cannot be unbound here (PR #183 review).
                "can_unbind": bound and not upcoming,
            }
        )
    return rows


def _templates(organization_id, vehicle_id=None):
    """Active checklists the form offers: the vehicle's own after the cutover (CFG-7)."""
    found = ChecklistTemplate.objects.filter(organization_id=organization_id, active=True)
    if vehicle_id is not None:
        found = found.filter(vehicle_id=vehicle_id)
    return list(
        found.order_by("precedence", "label", "id").values("id", "label")[: admin_pages.LIST_LIMIT]
    )


def _slots(organization_id):
    """Each published schedule slot with its latest rule, for the bind form."""
    tops = list(
        ScheduleRule.objects.filter(organization_id=organization_id)
        .values("slot")
        .annotate(top=Max("revision"))
        .order_by("slot")[: admin_pages.LIST_LIMIT]
    )
    rules = {}
    for top in tops:
        rule = (
            ScheduleRule.objects.filter(
                organization_id=organization_id, slot=top["slot"], revision=top["top"]
            )
            .values("slot", "wall_time", "recurrence", "interval_days")
            .first()
        )
        if rule is not None:
            rules[rule["slot"]] = rule
    return [rules[slot] for slot in sorted(rules)]


def _page(request, user, vehicle, *, status=200, errors=None, form=None, preview=None):
    now = timezone.now()
    org = str(user.organization_id)
    # SPEC 5.0.0 CFG-7, CFG-8: after the cutover a binding is the assignment of one of the
    # vehicle's own checklists to a shared schedule slot.
    per_vehicle = cutover_at(org) is not None
    context = {
        "title": (
            f"Checklist assignments: {vehicle.display_name}"
            if per_vehicle
            else f"Checklist bindings: {vehicle.display_name}"
        ),
        "vehicle": vehicle,
        "rows": _rows(org, str(vehicle.pk), now, per_vehicle),
        "templates": _templates(org, str(vehicle.pk) if per_vehicle else None),
        "slots": _slots(org),
        "list_limit": admin_pages.LIST_LIMIT,
        "bind_operation_id": admin_pages.new_operation_id(),
        "form": form or {},
        "preview": preview,
        "zone": timezone.get_current_timezone_name(),
        "errors": errors or [],
        "saved": SAVED.get(request.GET.get("saved", "")),
    }
    page = "admin_assignments.html" if per_vehicle else "admin_bindings.html"
    return render(request, f"operations/{page}", context, status=status)


def _ids(request, user):
    operation_id = admin_pages.operation_id_from(request)
    if operation_id is None:
        return None
    return {**_context_ids(user), "operation_id": operation_id}


def _template_of(user, value):
    """The organization's template named by the form, or None."""
    try:
        return ChecklistTemplate.objects.filter(
            organization_id=user.organization_id, pk=value
        ).first()
    except (ValueError, TypeError):
        return None


def _preview(user, vehicle, template, slot):
    """The preview for the page: what it lists, the digest it confirms and a fresh operation id."""
    result = unbind_preview(
        str(user.organization_id), str(vehicle.pk), str(template.pk), slot, timezone.now()
    )
    continuing = [
        {
            "due_at": o.due_at,
            "early_at": o.early_at,
            "states": ", ".join(
                f"{count} {STATE_LABELS.get(state, state.lower())}" for state, count in o.states
            ),
        }
        for o in result.continuing
    ]
    return {
        "template": template,
        "slot": slot,
        "result": result,
        "continuing": continuing,
        "operation_id": admin_pages.new_operation_id(),
    }


def _run(request, render_error, call):
    """(the command's result, None) on success, else (None, the error response)."""
    try:
        return call(), None
    except PreviewChanged:
        raise  # the caller shows the fresh preview
    except (BindingError, ValueError) as error:
        return None, render_error(400, [str(error)])
    except CommandDenied:
        return None, render(request, "shell/forbidden.html", {"title": "Not permitted"}, status=403)
    except OperationConflict:
        return None, render_error(
            409, ["this form was already used for a different change; try again"]
        )
    except UNAVAILABLE as error:
        logger.error("binding change unavailable (%s)", type(error).__name__)
        return None, render_error(
            503, ["the change could not be completed; nothing was changed. Try again."]
        )


@never_cache
@require_http_methods(["GET", "POST"])
def bindings(request, vehicle_id):
    allowed, refused = admin_pages.allowed(request, BIND_ACTION, DENIAL)
    if refused:
        return refused
    user, _roles = allowed
    vehicle = admin_pages.vehicle_of(user, vehicle_id)
    if request.method == "GET":
        return _page(request, user, vehicle)
    stage = request.POST.get("stage")
    form = {
        "template_id": request.POST.get("template_id", ""),
        "slot": request.POST.get("slot", ""),
        "when": request.POST.get("when", "now"),
        "at": request.POST.get("at", ""),
    }

    def render_error(status, errors, preview=None):
        return _page(
            request, user, vehicle, status=status, errors=errors, form=form, preview=preview
        )

    if stage not in ("bind", "unbind-preview", "unbind"):
        return render_error(400, ["the request was not valid"])
    template = _template_of(user, form["template_id"])
    if template is None:
        return render_error(400, ["choose a checklist template"])
    slot = form["slot"]

    if stage == "unbind-preview":
        try:
            preview = _preview(user, vehicle, template, slot)
        except PreviewTooLarge as error:
            return render_error(400, [str(error)])
        except UNAVAILABLE as error:
            logger.error("unbinding preview unavailable (%s)", type(error).__name__)
            return render_error(503, ["the preview could not be prepared. Try again."])
        if not preview["result"].bound:
            return render_error(400, ["that template is not bound to this vehicle"])
        if preview["result"].binding.effective_at > preview["result"].at:
            return render_error(
                400,
                [
                    "that binding has not started yet; it can be unbound once it takes effect, "
                    "or from the command line"
                ],
            )
        return _page(request, user, vehicle, preview=preview)

    ids = _ids(request, user)
    if ids is None:
        return render_error(400, ["the form was not valid; try again"])
    if stage == "bind":
        try:
            effective_at = admin_pages.local_effective_at(request.POST, BindingError)
        except BindingError as error:
            return render_error(400, [str(error)])
        saved = "assigned" if template.vehicle_id else "bound"

        def call():
            return bind_requirement(
                vehicle_id=str(vehicle.pk),
                template_id=str(template.pk),
                slot=slot,
                action="BIND",
                effective_at=effective_at,
                **ids,
            )

    else:
        saved = "ended" if template.vehicle_id else "unbound"

        def call():
            return bind_requirement(
                vehicle_id=str(vehicle.pk),
                template_id=str(template.pk),
                slot=slot,
                action="UNBIND",
                preview_token=request.POST.get("token", ""),
                **ids,
            )

    try:
        _result, failed = _run(request, render_error, call)
    except PreviewChanged:
        try:
            fresh = _preview(user, vehicle, template, slot)
        except (PreviewTooLarge, *UNAVAILABLE):
            fresh = None
        if fresh is not None and fresh["result"].bound:
            return render_error(409, [CHANGED], preview=fresh)
        return render_error(409, [CHANGED])
    if failed:
        return failed
    url = reverse("vehicle-bindings", args=[vehicle.pk])
    return HttpResponseRedirect(url + "?" + urlencode({"saved": saved}))
