"""Administration page for the inspection resume limit (SPEC 6.3 SES-8, §3.3; issue #256 Part E).

An Administrator sets, in whole hours from 1 to 24, how long after it opens an inspection
session may be resumed. It applies to sessions opened from then on. The command in
cya.inspections.resume_limit decides who may act; hiding the page is a convenience only.
"""

import logging

from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cya.core.audit_commands import CommandDenied, OperationConflict
from cya.inspections import resume_limit
from cya.kits import admin_pages
from cya.kits.views import UNAVAILABLE, _context_ids

logger = logging.getLogger(__name__)

DENIAL = "CONFIGURE_INSPECTION_RESUME_LIMIT"
INVALID = "Enter a whole number of hours from 1 to 24."


@never_cache
@require_http_methods(["GET", "POST"])
def resume_limit_settings(request):
    allowed, refused = admin_pages.allowed(request, resume_limit.LIMIT_ACTION, DENIAL)
    if refused:
        return refused
    user, _roles = allowed
    org = str(user.organization_id)

    def page(status=200, errors=None, saved=None):
        context = {
            "title": "Inspection sessions",
            "hours": resume_limit.resume_limit_hours(org),
            "operation_id": admin_pages.new_operation_id(),
            "errors": errors or [],
            "saved": saved,
        }
        return render(request, "inspections/session_settings.html", context, status=status)

    if request.method == "GET":
        return page(saved="Saved." if request.GET.get("saved") else None)
    operation_id = admin_pages.operation_id_from(request)
    raw = request.POST.get("hours", "").strip()
    if operation_id is None or not raw.isdigit():
        return page(400, [INVALID])
    try:
        resume_limit.set_resume_limit(
            hours=int(raw), **{**_context_ids(user), "operation_id": operation_id}
        )
    except resume_limit.ResumeLimitError as error:
        return page(400, ["That is already the limit." if error.code == "NO_CHANGE" else INVALID])
    except CommandDenied:
        return render(request, "shell/forbidden.html", {"title": "Not permitted"}, status=403)
    except OperationConflict:
        return page(409, ["This form was already used; try again."])
    except UNAVAILABLE as error:
        logger.error("resume limit unavailable (%s)", type(error).__name__)
        return page(503, ["The change could not be completed; nothing was changed. Try again."])
    return HttpResponseRedirect(reverse("inspection-session-settings") + "?saved=1")
