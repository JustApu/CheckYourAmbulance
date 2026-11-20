"""Organization-scoped role management and audited role-history detail.

Administrators see active users and current roles; changes still pass through
an explicit review and confirmation. The separate history page reads immutable
role events. POST changes one role through commands.change_role, which owns
authorization, receipt, effect and audit in a single transaction. The handler
only parses and validates input; the actor and organization always come from
the authenticated request, never the form (SPEC 3.2).
"""

import logging
import uuid
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db import DatabaseError
from django.db.models import F, Q, Window
from django.db.models.functions import RowNumber
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cya.accounts.models import User
from cya.accounts.returns import sign_in_redirect
from cya.authorization.backend import AuthorizationUnavailable, canonical_id
from cya.authorization.commands import change_role
from cya.authorization.lifecycle import (
    InvalidRoleHistory,
    LastAdministrator,
    RoleEvent,
    current_roles,
)
from cya.authorization.models import RoleGrant
from cya.authorization.policy import Role
from cya.core.audit_commands import CommandDenied, OperationConflict, OutcomeUnknown, RetryLater
from cya.core.audit_transactions import AuditPersistenceFailure
from cya.shell import navigation
from cya.shell.views import deny_section

logger = logging.getLogger(__name__)

HISTORY_LIMIT = 50
TARGET_LIMIT = 50
ROLE_EVENT_LIMIT = 500
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
    "ONE_ROLE": "Review one role change at a time. Select one role to add or remove.",
    "HISTORY_UNAVAILABLE": "Current roles could not be verified. Reload the page before trying again.",
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


def _current_roles_by_user(organization_id, targets):
    target_ids = [str(target.pk) for target in targets]
    events = {target_id: [] for target_id in target_ids}
    unavailable = set()
    rows = (
        RoleGrant.objects.filter(organization_id=organization_id, user_id__in=target_ids)
        .annotate(
            event_number=Window(
                expression=RowNumber(),
                partition_by=[F("user_id")],
                order_by=F("sequence").asc(),
            )
        )
        .filter(event_number__lte=ROLE_EVENT_LIMIT + 1)
        .order_by("user_id", "sequence")
        .values(
            "id",
            "organization_id",
            "user_id",
            "sequence",
            "role",
            "action",
            "prior_grant_id",
            "event_number",
        )
    )
    for row in rows:
        target_id = str(row["user_id"])
        if row["event_number"] > ROLE_EVENT_LIMIT:
            unavailable.add(target_id)
            continue
        events[target_id].append(
            RoleEvent(
                id=str(row["id"]),
                organization_id=str(row["organization_id"]),
                user_id=target_id,
                sequence=row["sequence"],
                role=row["role"],
                action=row["action"],
                prior_grant_id=str(row["prior_grant_id"]) if row["prior_grant_id"] else None,
            )
        )
    result = {}
    for target_id, target_events in events.items():
        if target_id in unavailable:
            result[target_id] = None
            continue
        try:
            state = current_roles(
                target_events,
                organization_id=str(organization_id),
                user_id=target_id,
            )
        except InvalidRoleHistory:
            result[target_id] = None
            continue
        codes = {"CREW" if role.value in ("CREW", "EMT") else role.value for role in state.grants}
        result[target_id] = tuple(sorted(codes))
    return result


def _render_roles(
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
    # Only an Administrator is shown users and the form; any other caller
    # who reaches an error page sees the message alone.
    show = _is_administrator(user)
    target_query = User.objects.filter(organization_id=user.organization_id, active=True)
    if search_query:
        target_query = target_query.filter(
            Q(email__icontains=search_query) | Q(display_name__icontains=search_query)
        )
    target_query = target_query.order_by("email", "id") if show and not search_error else []
    target_total = target_query.count() if show and not search_error else 0
    targets = list(target_query[:TARGET_LIMIT]) if show and not search_error and not preview else []
    role_list_error = None
    if targets:
        try:
            roles_by_user = _current_roles_by_user(user.organization_id, targets)
        except DatabaseError:
            logger.error("role list unavailable")
            targets = []
            role_list_error = "The user list could not be loaded. Reload the page to try again."
            status = 503
        else:
            for target in targets:
                target.current_role_codes = roles_by_user[str(target.pk)]
                target.role_history_unavailable = target.current_role_codes is None
                target.current_role_labels = (
                    tuple(ROLE_LABELS[code] for code in target.current_role_codes)
                    if target.current_role_codes is not None
                    else ()
                )
                target.role_operation_id = (
                    None if target.role_history_unavailable else str(uuid.uuid4())
                )
    # Issue #235 A4/D5: a search reload says what it found, in the title and in focus.
    searched = request.method == "GET" and "q" in request.GET and show
    title = "Role administration"
    if searched and search_query and not search_error:
        title = f"Role administration ({target_total} matching)"
    context = {
        "title": title,
        "searched": searched,
        "show": show,
        "targets": targets,
        "target_total": target_total,
        "target_limit": TARGET_LIMIT,
        "target_search_query": search_query,
        "target_search_error": search_error,
        "role_list_error": role_list_error,
        "role_options": tuple((role, ROLE_LABELS.get(role, role)) for role in SELECTABLE_ROLES),
        "error": error,
        "saved": saved,
        "form_values": form_values or {},
        "preview": preview,
    }
    return render(request, "authorization/roles.html", context, status=status)


def _failure(request, user, outcome, status, *, search_query="", form_values=None):
    return _render_roles(
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
    matrix_selection = request.POST.getlist("role_selection_present") == ["1"]
    selected_roles = None
    try:
        if matrix_selection:
            target_id = canonical_id(request.POST.get("target", ""))
            operation_id = canonical_id(request.POST.get("operation_id", ""))
            if uuid.UUID(operation_id).version != 4:
                raise ValueError("operation id must be a UUID4")
            selected_values = request.POST.getlist("roles")
            selected_roles = set(selected_values)
            if len(selected_values) != len(selected_roles) or not selected_roles.issubset(
                SELECTABLE_ROLES
            ):
                raise ValueError("role selection is invalid")
        else:
            # Keep the existing single-role form contract for trusted internal clients while
            # the administration page uses the staged role matrix.
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
        return _render_roles(
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
    if matrix_selection:
        try:
            current_by_user = _current_roles_by_user(user.organization_id, [target])
        except DatabaseError:
            logger.error("role list unavailable during preview")
            return _failure(
                request,
                user,
                "HISTORY_UNAVAILABLE",
                503,
                search_query=query,
                form_values=values,
            )
        current_roles_for_user = current_by_user[str(target.pk)]
        if current_roles_for_user is None:
            return _failure(
                request,
                user,
                "HISTORY_UNAVAILABLE",
                503,
                search_query=query,
                form_values=values,
            )
        changed_roles = set(current_roles_for_user).symmetric_difference(selected_roles)
        if len(changed_roles) != 1:
            return _failure(
                request,
                user,
                "ONE_ROLE",
                400,
                search_query=query,
                form_values=values,
            )
        role = changed_roles.pop()
        action = "GRANT" if role in selected_roles else "REVOKE"
    preview = {
        "target": target,
        "role": role,
        "role_label": ROLE_LABELS[role],
        "action": action,
        "action_label": action.title(),
        "operation_id": operation_id,
    }
    return _render_roles(
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
                return _render_roles(
                    request,
                    user,
                    status=400,
                    search_query=query,
                    search_error=search_error,
                    form_values=values,
                )
            return _render_roles(request, user, search_query=query, form_values=values)
        if not _is_administrator(user):
            return deny_section(request, user, navigation.roles_for(user), "VIEW_ADMINISTRATION")
        return _failure(request, user, "INVALID", 400)
    current = navigation.roles_for(user)
    if navigation.ADMINISTRATOR not in current:
        return deny_section(request, user, current, "VIEW_ADMINISTRATION")
    query, search_error = _search_query(request.GET)
    return _render_roles(
        request,
        user,
        saved=request.GET.get("saved") == "1",
        search_query=query,
        search_error=search_error,
    )


@never_cache
@require_http_methods(["GET"])
def role_history(request, target_id):
    user = request.cya_user
    if user is None:
        return sign_in_redirect(request)
    current = navigation.roles_for(user)
    if navigation.ADMINISTRATOR not in current:
        return deny_section(request, user, current, "VIEW_ADMINISTRATION")

    target = User.objects.filter(organization_id=user.organization_id, pk=target_id).first()
    if target is None:
        return render(
            request,
            "authorization/role_history.html",
            {"title": "Role history", "target": None, "history": []},
            status=404,
        )
    try:
        history_query = (
            RoleGrant.objects.filter(organization_id=user.organization_id, user_id=target.pk)
            .select_related("actor_user")
            .order_by("-recorded_at", "-sequence", "-pk")
        )
        history_page = Paginator(history_query, HISTORY_LIMIT).get_page(request.GET.get("page"))
        history_total = history_page.paginator.count
        history = list(history_page.object_list)
    except DatabaseError:
        logger.error("role history unavailable")
        return render(
            request,
            "authorization/role_history.html",
            {
                "title": f"Role history for {target.display_name or target.email}",
                "target": target,
                "history": [],
                "error": "Role history could not be loaded. Reload the page to try again.",
            },
            status=503,
        )
    for event in history:
        event.action_label = "Granted" if event.action == "GRANT" else "Removed"
        role_code = "CREW" if event.role == "EMT" else event.role
        event.role_label = ROLE_LABELS.get(role_code, role_code)
    return render(
        request,
        "authorization/role_history.html",
        {
            "title": f"Role history for {target.display_name or target.email}",
            "target": target,
            "history": history,
            "history_total": history_total,
            "history_limit": HISTORY_LIMIT,
            "history_page": history_page,
            "station_zone": user.organization.station_zone,
        },
    )
