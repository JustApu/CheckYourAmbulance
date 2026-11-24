"""Vehicle files page: templates, exports and import (SPEC 2.1.0 4.7 IMP-1 to IMP-7, issue #129).

GET lists the organization's vehicles with their VINs and export links, and the blank
templates, to Supervisors and Administrators (export_configuration); Administrators also get the
import form (import_configuration). Anyone else gets 403 and an audited denial.

Import works like the kit library page: stage=preview shows the plan with no writes (IMP-2) and
carries the file back base64-encoded with the plan's base token and a fresh operation id;
stage=confirm runs the audited, all-or-nothing import of exactly what was previewed (IMP-3). The
server keeps no copy of the file between the two steps (IMP-4).
"""

import base64
import binascii
import logging
import uuid
from urllib.parse import urlencode

from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from cya.accounts.returns import sign_in_redirect
from cya.checklists.models import Vehicle
from cya.core.audit_commands import CommandDenied, OperationConflict
from cya.kits import vehicle_files
from cya.kits.library_files import MAX_BYTES
from cya.kits.library_import import EXPORT_ACTION, IMPORT_ACTION, ImportRefused
from cya.kits.vehicle_import import (
    audited_vehicle_export,
    import_vehicle_file,
    preview_vehicle_file,
)
from cya.kits.views import UNAVAILABLE, _can, _context_ids, _download
from cya.shell import navigation
from cya.shell.views import deny_section

logger = logging.getLogger(__name__)

LIST_LIMIT = 500
STALE = "the configuration changed since this file was previewed; preview it again"
REUSED = "this confirmation was already used for a different file; preview it again"


def _page(request, user, roles, *, status=200, plan=None, carried=None, errors=None):
    vehicles = list(
        Vehicle.objects.filter(organization_id=user.organization_id, active=True)
        .order_by("display_name", "id")
        .values("id", "display_name", "number", "key")[:LIST_LIMIT]
    )
    context = {
        "title": "Vehicle files",
        "vehicles": vehicles,
        "list_limit": LIST_LIMIT,
        "can_import": _can(user, roles, IMPORT_ACTION),
        "formats": vehicle_files.FORMATS,
        "plan": plan,
        "summary": plan.summary() if plan and not plan.errors else None,
        "carried": carried,
        "errors": errors or [],
        "imported": request.GET.get("imported") == "1",
    }
    return render(request, "kits/vehicle_files.html", context, status=status)


def _upload(request):
    fmt = request.POST.get("format")
    if fmt not in vehicle_files.FORMATS:
        raise ImportRefused([f"format: choose one of {list(vehicle_files.FORMATS)}"])
    uploaded = request.FILES.get("vehicle")
    if uploaded is None:
        raise ImportRefused(["file: choose a vehicle file to preview"])
    if uploaded.size > MAX_BYTES:
        raise ImportRefused([f"file: larger than {MAX_BYTES} bytes"])
    return uploaded.read(MAX_BYTES + 1), fmt


def _preview(request, user, roles):
    try:
        data, fmt = _upload(request)
    except ImportRefused as refused:
        return _page(request, user, roles, status=400, errors=refused.errors)
    plan = preview_vehicle_file(str(user.organization_id), data, fmt)
    carried = None
    if not plan.errors:
        carried = {
            "format": fmt,
            "data": base64.b64encode(data).decode("ascii"),
            "base_token": plan.base_token,
            "operation_id": str(uuid.uuid4()),
        }
    return _page(
        request,
        user,
        roles,
        status=400 if plan.errors else 200,
        plan=plan,
        carried=carried,
        errors=plan.errors,
    )


def _confirm(request, user, roles):
    fmt, token = request.POST.get("format"), request.POST.get("base_token", "")
    try:
        data = base64.b64decode(request.POST.get("data", ""), validate=True)
        operation_id = str(uuid.UUID(request.POST.get("operation_id", ""), version=4))
    except (binascii.Error, ValueError):
        return _page(
            request,
            user,
            roles,
            status=400,
            errors=["the confirmation was not valid; preview the file again"],
        )
    ids = {**_context_ids(user), "operation_id": operation_id}
    try:
        result = import_vehicle_file(data=data, fmt=fmt, base_token=token, **ids)
    except CommandDenied:
        return render(request, "shell/forbidden.html", {"title": "Not permitted"}, status=403)
    except ImportRefused as refused:
        status = 409 if STALE in refused.errors else 400
        return _page(request, user, roles, status=status, errors=refused.errors)
    except OperationConflict:
        return _page(request, user, roles, status=409, errors=[REUSED])
    except UNAVAILABLE as error:
        logger.error("vehicle file import unavailable (%s)", type(error).__name__)
        return _page(
            request,
            user,
            roles,
            status=503,
            errors=["the import could not be completed; nothing was changed. Try again."],
        )
    if result.operation_reused:
        return _page(request, user, roles, status=409, errors=[REUSED])
    return HttpResponseRedirect(reverse("vehicle-files") + "?" + urlencode({"imported": "1"}))


@never_cache
@require_http_methods(["GET", "POST"])
def vehicle_files_page(request):
    user = request.cya_user
    if user is None:
        return sign_in_redirect(request)
    roles = navigation.roles_for(user)
    if not _can(user, roles, EXPORT_ACTION):
        return deny_section(request, user, roles, "VIEW_VEHICLE_FILES")
    if request.method == "GET":
        return _page(request, user, roles)
    if not _can(user, roles, IMPORT_ACTION):
        return deny_section(request, user, roles, "IMPORT_CONFIGURATION")
    stage = request.POST.get("stage")
    if stage == "preview":
        return _preview(request, user, roles)
    if stage == "confirm":
        return _confirm(request, user, roles)
    return _page(request, user, roles, status=400, errors=["the request was not valid"])


@never_cache
@require_GET
def template(request, fmt):
    user = request.cya_user
    if user is None:
        return sign_in_redirect(request)
    roles = navigation.roles_for(user)
    if not _can(user, roles, EXPORT_ACTION):
        return deny_section(request, user, roles, "VIEW_VEHICLE_FILES")
    if fmt not in vehicle_files.FORMATS:
        return HttpResponse("Unknown format.", status=404, content_type="text/plain")
    from cya.checklists.ownership import cutover_at

    if cutover_at(str(user.organization_id)) is not None:
        # SPEC 5.0.0 IMP-7: after the cutover a vehicle file carries its own checklists.
        from cya.kits import vehicle_files_v3

        # SPEC 6.0.0 IMP-7: the example carries item requirements (schema 3).
        data = vehicle_files_v3.render(vehicle_files_v3.TEMPLATE_VEHICLE, fmt)
        return _download(data, fmt, "vehicle-template")
    return _download(vehicle_files.template(fmt), fmt, "vehicle-template")


@never_cache
@require_GET
def export(request, vehicle_id, fmt):
    user = request.cya_user
    if user is None:
        return sign_in_redirect(request)
    roles = navigation.roles_for(user)
    if not _can(user, roles, EXPORT_ACTION):
        return deny_section(request, user, roles, "VIEW_VEHICLE_FILES")
    if fmt not in vehicle_files.FORMATS:
        return HttpResponse("Unknown format.", status=404, content_type="text/plain")
    try:
        _result, data = audited_vehicle_export(
            vehicle_id=str(vehicle_id), fmt=fmt, **_context_ids(user)
        )
    except ImportRefused as refused:
        if refused.errors == ["vehicle: not found"]:
            return HttpResponse("Unknown vehicle.", status=404, content_type="text/plain")
        return HttpResponse("; ".join(refused.errors), status=409, content_type="text/plain")
    except CommandDenied:
        return render(request, "shell/forbidden.html", {"title": "Not permitted"}, status=403)
    except UNAVAILABLE as error:
        logger.error("vehicle file export unavailable (%s)", type(error).__name__)
        return HttpResponse(
            "The export is unavailable; try again.", status=503, content_type="text/plain"
        )
    vin = Vehicle.objects.values_list("key", flat=True).get(pk=vehicle_id)
    return _download(data, fmt, f"vehicle-{vin}")
