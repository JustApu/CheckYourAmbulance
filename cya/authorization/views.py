"""Role administration over HTTP: the walking skeleton's end-to-end action (issue 25 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/25)).

GET shows the organization's append-only role history to an Administrator. POST
changes one role through commands.change_role, which owns authorization,
receipt, effect and audit in a single transaction. The handler only parses and
validates input and maps outcomes to responses; the role, target and operation
id come from the form, never the actor, organization or roles (SPEC 3.2).
"""

import logging
import uuid
from urllib.parse import urlencode

from django.db import DatabaseError
from django.db.models import Q
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cya.accounts.models import User
from cya.accounts.returns import sign_in_redirect
from cya.authorization.backend import AuthorizationUnavailable, canonical_id
from cya.authorization.commands import change_role
from cya.authorization.lifecycle import InvalidRoleHistory, LastAdministrator
from cya.authorization.models import RoleGrant
from cya.authorization.policy import Role
from cya.core.audit_commands import CommandDenied, OperationConflict, OutcomeUnknown, RetryLater
from cya.core.audit_transactions import AuditPersistenceFailure
from cya.shell import navigation
from cya.shell.views import deny_section

logger = logging.getLogger(__name__)

HISTORY_LIMIT = 50
TARGET_LIMIT = 50
SEARCH_LIMIT = 100
ACTIONS = ("GRANT", "REVOKE")
SELECTABLE_ROLES = tuple(role.value for role in Role if role != Role.EMT)  # EMT is a legacy alias
ROLE_LABELS = {
    "CREW": "Crew",
    "SUPERVISOR": "Supervisor",
    "SUPPLY_OFFICER": "Supply Officer",
    "ADMINISTRATOR": "Administrator",
}

# Sanitized messages keyed by outcome; no exception text reaches the page.
MESSAGES = {
    "INVALID": "The request was not valid. Check the user, role and action and try again.",
    "NOT_FOUND": "That user is not available.",
    "LAST_ADMINISTRATOR": "The last active administrator must keep the Administrator role.",
    "OPERATION_CONFLICT": (
        "This request id was already used for a different request. "
        "The earlier change is unchanged and this one was not applied."
    ),
    "UNAVAILABLE": "The change could not be confirmed. Do not assume it was saved; reload to check.",
}


def _parse(post):
    """Return (target, role, action, operation_id) or raise ValueError."""
    target = canonical_id(post.get("target", ""))
    operation_id = canonical_id(post.get("operation_id", ""))
    if uuid.UUID(operation_id).version != 4:
        raise ValueError("operation id must be a UUID4")
    role, action = post.get("role", ""), post.get("action", "")
    if role not in SELECTABLE_ROLES or action not in ACTIONS:
        raise ValueError("unknown role or action")
    return target, role, action, operation_id


def _is_administrator(user):
    return navigation.ADMINISTRATOR in navigation.roles_for(user)


def _render_history(
    request,
    user,
    *,
    status=200,
    error=None,
    saved=False,
    search_query="",
    search_error=None,
    form_values=None,
    preview=None,
):
    # Only an Administrator is shown history, users or the form; any other
    # caller who reaches an error page sees the message alone.
    show = _is_administrator(user)
    history = (
        RoleGrant.objects.filter(organization_id=user.organization_id)
        .select_related("user", "actor_user")
        .order_by("-recorded_at", "-sequence", "-pk")[:HISTORY_LIMIT]
        if show
        else []
    )
    target_query = User.objects.filter(organization_id=user.organization_id, active=True)
    if search_query:
        target_query = target_query.filter(
            Q(email__icontains=search_query) | Q(display_name__icontains=search_query)
        )
    target_query = target_query.order_by("email", "id") if show and not search_error else []
    target_total = target_query.count() if show and not search_error else 0
    targets = target_query[:TARGET_LIMIT] if show and not search_error else []
    context = {
        "title": "Role administration",
        "show": show,
        "history": history,
        "targets": targets,
        "target_total": target_total,
        "target_limit": TARGET_LIMIT,
        "target_search_query": search_query,
        "target_search_error": search_error,
        "roles": SELECTABLE_ROLES,
        "role_labels": ROLE_LABELS,
        "role_options": tuple((role, ROLE_LABELS.get(role, role)) for role in SELECTABLE_ROLES),
        "actions": ACTIONS,
        # A fresh id per rendered form: a double submit replays instead of repeating.
        "operation_id": str(uuid.uuid4()),
        "error": error,
        "saved": saved,
        "form_values": form_values or {},
        "preview": preview,
    }
    return render(request, "authorization/roles.html", context, status=status)


def _failure(request, user, outcome, status, *, search_query="", form_values=None):
    return _render_history(
        request,
        user,
        status=status,
        error=MESSAGES[outcome],
        search_query=search_query,
        form_values=form_values,
    )


def _search_query(values):
    supplied = values.getlist("q")
    if len(supplied) > 1:
        return "", "Search was supplied more than once. Remove the duplicate and retry."
    query = supplied[0].strip() if supplied else ""
    if len(query) > SEARCH_LIMIT:
        return "", f"Search is limited to {SEARCH_LIMIT} characters."
    return query, None


def _form_values(values):
    return {
        "target": values.get("target", ""),
        "role": values.get("role", ""),
        "action": values.get("action", ""),
    }


def _preview(request, user):
    if not _is_administrator(user):
        return deny_section(request, user, navigation.roles_for(user), "VIEW_ADMINISTRATION")
    query, search_error = _search_query(request.POST)
    values = _form_values(request.POST)
    try:
        target_id, role, action, operation_id = _parse(request.POST)
    except ValueError:
        return _failure(
            request,
            user,
            "INVALID",
            400,
            search_query=query,
            form_values=values,
        )
    if search_error:
        return _render_history(
            request,
            user,
            status=400,
            search_query=query,
            search_error=search_error,
            form_values=values,
        )
    target = User.objects.filter(
        organization_id=user.organization_id, active=True, pk=target_id
    ).first()
    if target is None:
        return _failure(
            request,
            user,
            "NOT_FOUND",
            404,
            search_query=query,
            form_values=values,
        )
    preview = {
        "target": target,
        "role": role,
        "role_label": ROLE_LABELS[role],
        "action": action,
        "action_label": action.title(),
        "operation_id": operation_id,
    }
    return _render_history(
        request,
        user,
        search_query=query,
        preview=preview,
        form_values=values,
    )


def _change(request, user):
    query, _search_error = _search_query(request.POST)
    values = _form_values(request.POST)
    try:
        target, role, action, operation_id = _parse(request.POST)
    except ValueError:
        return _failure(request, user, "INVALID", 400, search_query=query, form_values=values)
    try:
        result = change_role(
            organization_id=str(user.organization_id),
            actor_id=str(user.pk),
            target_id=target,
            operation_id=operation_id,
            correlation_id=str(uuid.uuid4()),
            request_attempt_id=str(uuid.uuid4()),
            role=role,
            action=action,
        )
    except CommandDenied:
        # run_command already committed the denial audit after the rollback.
        return render(request, "shell/forbidden.html", {"title": "Not permitted"}, status=403)
    except OperationConflict:
        return _failure(
            request, user, "OPERATION_CONFLICT", 409, search_query=query, form_values=values
        )
    except LastAdministrator:
        return _failure(
            request, user, "LAST_ADMINISTRATOR", 409, search_query=query, form_values=values
        )
    except InvalidRoleHistory:
        return _failure(request, user, "INVALID", 400, search_query=query, form_values=values)
    except User.DoesNotExist:
        # The target is resolved before permission is checked, so a non-administrator
        # must not learn which user ids exist: they get the same 403 as a denial.
        if not _is_administrator(user):
            return render(request, "shell/forbidden.html", {"title": "Not permitted"}, status=403)
        return _failure(request, user, "NOT_FOUND", 404, search_query=query, form_values=values)
    except (
        RetryLater,
        OutcomeUnknown,
        AuditPersistenceFailure,
        AuthorizationUnavailable,
        DatabaseError,
    ):
        logger.error("role change unavailable")
        return _failure(request, user, "UNAVAILABLE", 503, search_query=query, form_values=values)
    if result.operation_reused:
        # Same actor and id but a different request: run_command replays the
        # original acknowledgement and applies nothing, so never report it saved.
        return _failure(
            request, user, "OPERATION_CONFLICT", 409, search_query=query, form_values=values
        )
    # Post/redirect/get: reloading the result page never resubmits the change.
    query_string = "?" + urlencode({"saved": "1", **({"q": query} if query else {})})
    return HttpResponseRedirect(reverse("administration-roles") + query_string)


@never_cache
@require_http_methods(["GET", "POST"])
def roles(request):
    user = request.cya_user
    if user is None:
        return sign_in_redirect(request)
    if request.method == "POST":
        stage = request.POST.get("stage")
        if stage == "confirm":
            return _change(request, user)
        if stage == "preview":
            return _preview(request, user)
        if stage == "edit":
            if not _is_administrator(user):
                return deny_section(
                    request, user, navigation.roles_for(user), "VIEW_ADMINISTRATION"
                )
            query, search_error = _search_query(request.POST)
            values = _form_values(request.POST)
            if search_error:
                return _render_history(
                    request,
                    user,
                    status=400,
                    search_query=query,
                    search_error=search_error,
                    form_values=values,
                )
            return _render_history(request, user, search_query=query, form_values=values)
        if not _is_administrator(user):
            return deny_section(request, user, navigation.roles_for(user), "VIEW_ADMINISTRATION")
        return _failure(request, user, "INVALID", 400)
    current = navigation.roles_for(user)
    if navigation.ADMINISTRATOR not in current:
        return deny_section(request, user, current, "VIEW_ADMINISTRATION")
    query, search_error = _search_query(request.GET)
    return _render_history(
        request,
        user,
        saved=request.GET.get("saved") == "1",
        search_query=query,
        search_error=search_error,
    )
