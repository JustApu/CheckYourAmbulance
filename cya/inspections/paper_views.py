"""Crew pages for Paper entry (SPEC 8.0.0 REC-5, §7.9 LATE-1 to LATE-8, DEP-11; issue #9 B6).

- /inspection/vehicles/{id}/paper-entry/?template={id}: the setup page. It asks once for what
  the outage sheet says (the printed Version ID, the kit reference versions used, the date and
  time written on it and who did the work) and saves the batch header through
  POST /api/v1/paper-entry-batches (`inspections/paper_entry.js`).
- /inspection/paper-entry/{batch_id}/: the paper checklist screen, the session screen's own
  script and template in paper mode (config.paper_entry): each tap saves one check naming the
  batch; nothing is saved per batch (LATE-6).

Hiding a control is a convenience only: every endpoint authorizes each record again (SPEC 3.2).
Only the batch's entry actor may add records to it (Part B2), so another user's batch answers 404
exactly like one that does not exist. Names reach the page only through json_script and the
script's textContent, never as markup or in a URL.
"""

from django.db import DatabaseError
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from cya.authorization.policy import permitted
from cya.inspections.views import (
    _authorized,
    _odometer_unit,
    _status,
    _vehicle,
    reverse_socket,
)
from cya.inspections.workflows import report_options
from cya.paper import readers
from cya.paper.commands import OTHERS_ACTION, OWN_ACTION
from cya.shell import navigation
from cya.shell.templatetags.station_time import station_time
from cya.shell.views import deny_section

RESOLUTION_NOTES = {
    "EXACT": "",
    "GAP_FORWARD": (
        "That time did not exist on that date because the clocks moved forward, so it was taken "
        "as the first valid time after the change."
    ),
    "AMBIGUOUS_FIRST": (
        "That time happened twice that night because the clocks moved back, so the first of the "
        "two was used."
    ),
}


def paper_permissions(user):
    """(may enter own paper records, may attribute them to others) for the signed-in user."""
    roles = navigation.roles_for(user)
    user_id = str(user.pk)
    return permitted(OWN_ACTION, user_id, roles), permitted(OTHERS_ACTION, user_id, roles)


def _version_label(template_label, version):
    return (
        f"{template_label}, revision {version['revision']} — Version ID {version['version_id']}"
        f" (in force from {station_time(version['effective_at'])})"
    )


def _kit_label(kit, version):
    return (
        f"{kit['label']}, revision {version['revision']} — Kit version {version['version_id']}"
        f" (in force from {station_time(version['effective_at'])})"
    )


def _choices_config(choices):
    """version_choices as the setup script reads it: ids, labels and defaults only."""
    return [
        {
            "version_id": version["version_id"],
            "label": _version_label(choices["template_label"], version),
            "current": version["current"],
            "kits": [
                {
                    "definition_id": kit["definition_id"],
                    "label": kit["label"],
                    "versions": [
                        {
                            "version_id": option["version_id"],
                            "label": _kit_label(kit, option),
                            "current": option["current"],
                        }
                        for option in kit["versions"]
                    ],
                }
                for kit in version["kits"]
            ],
        }
        for version in choices["versions"]
    ]


@never_cache
@require_GET
def paper_entry_new(request, vehicle_id):
    """The Paper entry setup page for one of the vehicle's own checklists (REC-5, LATE-2..4)."""
    user, refusal = _authorized(request)
    if refusal:
        return refusal
    found = _vehicle(user, vehicle_id)
    own, others = paper_permissions(user)
    if not (own or others):
        return deny_section(request, user, navigation.roles_for(user), "ENTER_PAPER_RECORD")
    template_id = readers.parse_batch_id(request.GET.get("template"))
    if template_id is None:
        raise Http404
    now = timezone.now()
    choices = readers.version_choices(str(user.organization_id), str(found.pk), template_id, now)
    if choices is None:
        raise Http404
    config = {
        "vehicle_id": str(found.pk),
        "template_id": choices["template_id"],
        "user_id": str(user.pk),
        "own_name": user.display_name,
        # A Supervisor or Administrator chooses performers by search (LATE-3); a Crew member
        # can only name themself.
        "can_attribute": others,
        "versions": _choices_config(choices),
        "urls": {
            "batches": reverse("api-paper-entry-batches"),
            "performers": reverse("api-paper-entry-performers"),
        },
    }
    context = {
        "title": f"Paper entry: {found.display_name}",
        "vehicle": found,
        "template_label": choices["template_label"],
        "has_versions": bool(choices["versions"]),
        "station_zone": user.organization.station_zone,
        "can_attribute": others,
        "config": config,
    }
    return render(request, "inspections/paper_entry_new.html", context)


@never_cache
@require_GET
def paper_entry(request, batch_id):
    """The paper checklist screen for the user's own batch (REC-5, LATE-4 to LATE-8)."""
    user, refusal = _authorized(request)
    if refusal:
        return refusal
    canonical = readers.parse_batch_id(batch_id)
    batch = None if canonical is None else readers.own_batch(user, canonical)
    if batch is None:
        raise Http404
    summary = readers.batch_summary(batch)
    now = timezone.now()
    state = "INVALIDATED" if summary["invalidated"] else "OPEN"
    written = (
        f"{summary['performed_local_date'].isoformat()} "
        f"{summary['performed_local_time'].strftime('%H:%M')}"
    )
    odometer_unit = _odometer_unit(user, batch.vehicle)
    config = {
        "user_id": str(user.pk),
        "vehicle_id": str(batch.vehicle_id),
        "time_zone": user.organization.station_zone,
        "checklist_version_id": summary["checklist_version_id"],
        "state": state,
        "past_deadline": False,
        # Paper mode lists the sheet in its own order; the saved view is neither used nor saved.
        "view": "NORMAL",
        "needs_attention_only": False,
        "odometer_unit": odometer_unit,
        "paper_entry": {
            "batch_id": summary["batch_id"],
            "performed_at": summary["performed_at"].isoformat(),
            "performed_label": written,
            "performers": [
                p["display_name"] + ("" if p["active"] else " (inactive)")
                for p in summary["performers"]
            ],
            "has_records": summary["has_records"],
        },
        "urls": {
            "checklist": reverse("api-vehicle-checklist", args=[batch.vehicle_id]),
            "checks": reverse("api-checks"),
            "odometer": reverse("api-odometer-entries"),
            "vehicle": reverse("inspection-vehicle", args=[batch.vehicle_id]),
            "socket": reverse_socket(batch.vehicle_id),
            "suggestions": reverse("api-deficiency-suggestions"),
            "deficiency_detail": reverse("deficiencies-detail", args=["DEFICIENCY"]),
            "print_checklist": reverse("inspection-vehicle-print", args=[batch.vehicle_id]),
            "kit_reference": reverse("inspection-kit-reference"),
        },
    }
    try:
        options = report_options(user.organization_id, now)
    except (DatabaseError, ValueError):
        options = {"categories": [], "severities": [], "unavailable": True}
    context = {
        "title": f"Paper entry: {batch.vehicle.display_name}",
        "vehicle": batch.vehicle,
        "paper": summary,
        "paper_written": written,
        "paper_note": RESOLUTION_NOTES.get(summary["performed_resolution"], ""),
        "odometer_unit_label": {"mi": "miles", "km": "kilometres"}.get(odometer_unit),
        "template_label": summary["template_label"],
        "state": state,
        "state_label": "Invalidated" if state == "INVALIDATED" else "Open",
        "past_deadline": False,
        "status": _status(user, batch.vehicle, now),
        "config": config,
        "report_options": options,
    }
    return render(request, "inspections/session.html", context)
