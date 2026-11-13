"""Kit library page: definitions, templates, export, and import (SPEC 4.7, issue #129).

GET shows the organization's kit definitions and the download links to Supervisors and
Administrators (export_configuration); Administrators also get the import form
(import_configuration). Anyone else gets 403 and an audited denial.

Import is two POSTs. stage=preview reads the uploaded file and shows the plan without writing
anything (IMP-2). The preview page carries the file back, base64-encoded in a hidden field, with
the plan's base token and a fresh operation id, so stage=confirm runs the audited, all-or-nothing
import of exactly what was previewed, refusing it if the library changed meanwhile (IMP-3). The
server keeps no copy of the file between the two steps (IMP-4). A confirmed import redirects
(post/redirect/get) to the page with the counts.
"""

import base64
import binascii
import logging
import uuid
from urllib.parse import urlencode

from django.db import DatabaseError
from django.db.models import Max
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from cya.accounts.returns import sign_in_redirect
from cya.authorization.backend import AuthorizationUnavailable
from cya.authorization.policy import permitted
from cya.core.audit_commands import CommandDenied, OperationConflict, OutcomeUnknown, RetryLater
from cya.core.audit_transactions import AuditPersistenceFailure
from cya.kits import library_files
from cya.kits.library_import import (
    EXPORT_ACTION,
    IMPORT_ACTION,
    ImportRefused,
    audited_export,
    import_kit_library,
    preview_kit_library,
)
from cya.kits.models import KitDefinition, KitDefinitionPublication, KitDefinitionVersion
from cya.shell import navigation
from cya.shell.views import deny_section

logger = logging.getLogger(__name__)

LIST_LIMIT = 500
CONTENT_TYPES = {"json": "application/json; charset=utf-8", "csv": "text/csv; charset=utf-8"}
UNAVAILABLE = (
    RetryLater,
    OutcomeUnknown,
    AuditPersistenceFailure,
    AuthorizationUnavailable,
    DatabaseError,
)
STALE = "the kit library changed since this file was previewed; preview it again"


def _can(user, roles, action):
    return permitted(action, str(user.pk), roles)


def _context_ids(user):
    return {
        "organization_id": str(user.organization_id),
        "actor_id": str(user.pk),
        "operation_id": str(uuid.uuid4()),
        "correlation_id": str(uuid.uuid4()),
        "request_attempt_id": str(uuid.uuid4()),
    }


def _definitions(organization_id):
    rows = KitDefinition.objects.filter(organization_id=organization_id).order_by("key")[
        :LIST_LIMIT
    ]
    listed = []
    for definition in rows:
        versions = KitDefinitionVersion.objects.filter(definition=definition)
        latest = versions.aggregate(m=Max("revision"))["m"]
        published = (
            KitDefinitionPublication.objects.filter(version__definition=definition)
            .order_by("-effective_at")
            .select_related("version")
            .first()
        )
        listed.append(
            {
                "key": definition.key,
                "label": definition.label,
                "active": definition.active,
                "latest_revision": latest,
                "published_revision": published.version.revision if published else None,
                "published_effective_at": published.effective_at if published else None,
            }
        )
    return listed


def _page(request, user, roles, *, status=200, plan=None, carried=None, errors=None):
    context = {
        "title": "Kit library",
        "definitions": _definitions(user.organization_id),
        "list_limit": LIST_LIMIT,
        "can_import": _can(user, roles, IMPORT_ACTION),
        "formats": library_files.FORMATS,
        "plan": plan,
        "carried": carried,
        "errors": errors or [],
        "imported": request.GET.get("imported") == "1",
        "counts": {
            k: request.GET.get(k, "0") for k in ("created", "changed", "unchanged", "new_items")
        },
    }
    return render(request, "kits/library.html", context, status=status)


def _upload(request):
    """(file bytes, format) from a preview POST, or raise ImportRefused naming the problem."""
    fmt = request.POST.get("format")
    if fmt not in library_files.FORMATS:
        raise ImportRefused([f"format: choose one of {list(library_files.FORMATS)}"])
    uploaded = request.FILES.get("library")
    if uploaded is None:
        raise ImportRefused(["file: choose a kit-library file to preview"])
    if uploaded.size > library_files.MAX_BYTES:
        raise ImportRefused([f"file: larger than {library_files.MAX_BYTES} bytes"])
    return uploaded.read(library_files.MAX_BYTES + 1), fmt


def _preview(request, user, roles):
    try:
        data, fmt = _upload(request)
    except ImportRefused as refused:
        return _page(request, user, roles, status=400, errors=refused.errors)
    plan = preview_kit_library(str(user.organization_id), data, fmt)
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
        result = import_kit_library(data=data, fmt=fmt, base_token=token, **ids)
    except CommandDenied:
        return render(request, "shell/forbidden.html", {"title": "Not permitted"}, status=403)
    except ImportRefused as refused:
        status = 409 if STALE in refused.errors else 400
        return _page(request, user, roles, status=status, errors=refused.errors)
    except OperationConflict:
        return _page(
            request,
            user,
            roles,
            status=409,
            errors=["this confirmation was already used for a different file; preview it again"],
        )
    except UNAVAILABLE as error:
        logger.error("kit library import unavailable (%s)", type(error).__name__)
        return _page(
            request,
            user,
            roles,
            status=503,
            errors=["the import could not be completed; nothing was changed. Try again."],
        )
    if result.operation_reused:
        return _page(
            request,
            user,
            roles,
            status=409,
            errors=["this confirmation was already used for a different file; preview it again"],
        )
    counts = {k: result.body.get(k, 0) for k in ("created", "changed", "unchanged", "new_items")}
    return HttpResponseRedirect(
        reverse("kit-library") + "?" + urlencode({"imported": "1", **counts})
    )


@never_cache
@require_http_methods(["GET", "POST"])
def library(request):
    user = request.cya_user
    if user is None:
        return sign_in_redirect(request)
    roles = navigation.roles_for(user)
    if not _can(user, roles, EXPORT_ACTION):
        return deny_section(request, user, roles, "VIEW_KIT_LIBRARY")
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


def _download(data, fmt, filename):
    response = HttpResponse(data, content_type=CONTENT_TYPES[fmt])
    response["Content-Disposition"] = f'attachment; filename="{filename}.{fmt}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


@never_cache
@require_GET
def template(request, fmt):
    user = request.cya_user
    if user is None:
        return sign_in_redirect(request)
    roles = navigation.roles_for(user)
    if not _can(user, roles, EXPORT_ACTION):
        return deny_section(request, user, roles, "VIEW_KIT_LIBRARY")
    if fmt not in library_files.FORMATS:
        return HttpResponse("Unknown format.", status=404, content_type="text/plain")
    return _download(library_files.template(fmt), fmt, "kit-library-template")


@never_cache
@require_GET
def export(request, fmt):
    user = request.cya_user
    if user is None:
        return sign_in_redirect(request)
    roles = navigation.roles_for(user)
    if not _can(user, roles, EXPORT_ACTION):
        return deny_section(request, user, roles, "VIEW_KIT_LIBRARY")
    if fmt not in library_files.FORMATS:
        return HttpResponse("Unknown format.", status=404, content_type="text/plain")
    try:
        _result, data = audited_export(fmt=fmt, **_context_ids(user))
    except CommandDenied:
        return render(request, "shell/forbidden.html", {"title": "Not permitted"}, status=403)
    except UNAVAILABLE as error:
        logger.error("kit library export unavailable (%s)", type(error).__name__)
        return HttpResponse(
            "The export is unavailable; try again.", status=503, content_type="text/plain"
        )
    return _download(data, fmt, "kit-library")
