"""Crew page for viewing and handling a vehicle's sealed subjects.

- /inspection/vehicles/{id}/seals/: the live seal page.
- /inspection/paper-entry/{batch_id}/seals/: the same page in Paper entry mode (SPEC 8.5.0
  LATE-7; issue #9 B6c). Each verification, break or reseal copied from the outage sheet names
  the batch and is judged at its performance time. Only the batch's entry actor adds records to
  it (Part B2), so another user's batch answers 404 exactly like one that does not exist.

Hiding a control is a convenience only: every command authorizes each record again (SPEC 3.2).
Names reach the page only through json_script and the script's textContent.
"""

from django.db import DatabaseError
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from cya.inspections.views import _authorized, _vehicle, reverse_socket
from cya.inspections.workflows import report_options
from cya.shell.navigation import roles_for


def _render(request, user, vehicle, paper=None):
    """The seal page; with paper (the batch summary and banner context), in Paper entry mode."""
    try:
        options = report_options(user.organization_id, timezone.now())
    except (DatabaseError, ValueError):
        options = {"categories": [], "severities": []}
    config = {
        "vehicle_id": str(vehicle.pk),
        "subjects_url": reverse("api-seal-subjects", args=[vehicle.pk]),
        "scope_url": reverse("api-seal-scope", args=[vehicle.pk, "KIND", "SUBJECT"]),
        "command_url": reverse("api-seal-command", args=["ACTION"]),
        "checks_url": reverse("api-checks"),
        "socket_url": reverse_socket(str(vehicle.pk)),
        "report_options": options,
        # An audit request is never entered from paper (LATE-7).
        "can_request_audit": paper is None
        and bool(roles_for(user) & {"SUPERVISOR", "ADMINISTRATOR"}),
    }
    context = {"vehicle": vehicle, "config": config}
    if paper is not None:
        from cya.inspections.paper_views import paper_config

        summary = paper["paper"]
        config["paper_entry"] = paper_config(summary) | {
            "checklist_version_id": summary["checklist_version_id"],
            "state": paper["state"],
        }
        context |= paper | {
            "title": f"Seal records from paper: {vehicle.display_name}",
            "paper_seals_page": True,
        }
    return render(request, "seals/vehicle.html", context)


@never_cache
@require_GET
def vehicle_seals(request, vehicle_id):
    user, refusal = _authorized(request)
    if refusal:
        return refusal
    return _render(request, user, _vehicle(user, vehicle_id))


@never_cache
@require_GET
def paper_seals(request, batch_id):
    """The seal records of the user's own paper-entry batch (SPEC 8.5.0 LATE-7)."""
    from cya.inspections.paper_views import banner_context
    from cya.paper import readers

    user, refusal = _authorized(request)
    if refusal:
        return refusal
    canonical = readers.parse_batch_id(batch_id)
    batch = None if canonical is None else readers.own_batch(user, canonical)
    if batch is None:
        raise Http404
    return _render(request, user, batch.vehicle, paper=banner_context(readers.batch_summary(batch)))
