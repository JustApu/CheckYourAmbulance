"""Shared pieces of the per-vehicle configuration pages (issue #130 part 5).

Each page names the policy action it needs (SPEC 3.3); anyone without it gets 403 and an
audited denial. Every change is a form post carrying an operation id rendered into the form,
so a resubmitted form replays rather than repeats.
"""

import re
import uuid
from datetime import datetime

from django.http import Http404
from django.shortcuts import render
from django.utils import timezone

from cya.accounts.returns import sign_in_redirect
from cya.checklists.models import Vehicle
from cya.kits.views import _can
from cya.shell import navigation
from cya.shell.views import deny_section

LIST_LIMIT = 500
LOCAL_DATETIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(:[0-9]{2})?")
# A form's operation id already used for a different change: run_command either refuses it
# (OperationConflict) or, for the same user, replays the earlier receipt with
# operation_reused=True and applies nothing (issue #297). Neither is a save. The pages' error
# banner already says "Nothing was changed."
REUSED = "this form was already used for a different change; reload the page and try again"


def allowed(request, action, denial):
    """(user, roles) when the user may take `action`, else (None, the response to return)."""
    user = request.cya_user
    if user is None:
        return None, sign_in_redirect(request)
    roles = navigation.roles_for(user)
    if not _can(user, roles, action):
        return None, deny_section(request, user, roles, denial)
    return (user, roles), None


def vehicle_of(user, vehicle_id):
    """The organization's vehicle, or 404."""
    vehicle = Vehicle.objects.filter(organization_id=user.organization_id, pk=vehicle_id).first()
    if vehicle is None:
        raise Http404("no such vehicle")
    return vehicle


def vehicle_choice(request, user, *, title, intro, url_name):
    """The vehicle list a configuration page starts from, each linking to `url_name`."""
    found = list(
        Vehicle.objects.filter(organization_id=user.organization_id)
        .order_by("-active", "display_name", "id")
        .values("id", "display_name", "number", "key", "active")[:LIST_LIMIT]
    )
    context = {
        "title": title,
        "intro": intro,
        "url_name": url_name,
        "vehicles": found,
        "list_limit": LIST_LIMIT,
    }
    return render(request, "kits/vehicle_choice.html", context)


def operation_id_from(request):
    """The form's operation id, only when it is exactly a canonical UUID v4 (PR #170 review).

    UUID(value, version=4) would rewrite another version's bits into a different v4 id, so
    several submitted strings could map to one idempotency key; anything else is refused.
    """
    raw = request.POST.get("operation_id", "")
    try:
        parsed = uuid.UUID(raw)
    except ValueError:
        return None
    return raw if parsed.version == 4 and str(parsed) == raw else None


def new_operation_id():
    return str(uuid.uuid4())


def local_effective_at(post, error):
    """None for "now", the aware instant of the station-local date and time for "at".

    A wall time skipped by a daylight-saving change is refused; a repeated one means its first
    occurrence (fold=0). Refusals raise `error` with a message for the page.
    """
    when = post.get("when")
    if when == "now":
        return None
    if when != "at":
        # Only an explicit "now" publishes now; anything else is refused (PR #172 review).
        raise error("choose when it takes effect: now, or at a date and time")
    value = post.get("at", "")
    # Exactly what a datetime-local control sends: a date and a time, with no zone. A date alone
    # would otherwise parse as midnight (PR #172 review).
    if not LOCAL_DATETIME.fullmatch(value):
        raise error("enter the date and time it takes effect")
    try:
        local = datetime.fromisoformat(value)
    except ValueError:
        raise error("enter the date and time it takes effect") from None
    zone = timezone.get_current_timezone()
    aware = local.replace(tzinfo=zone, fold=0)
    if datetime.fromtimestamp(aware.timestamp(), zone).replace(tzinfo=None) != local:
        raise error("that local time does not exist on that day (a clock change)")
    return aware
