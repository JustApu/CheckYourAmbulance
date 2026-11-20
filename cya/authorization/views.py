"""Organization-scoped role management and audited role-history detail.

Administrators see active users and current roles. A user's row stages any
number of role changes; its Save button is the explicit confirmation and posts
them to commands.change_roles, which applies all of them or none in one audited
command (issue #236). The separate history page reads immutable role events.
The single-change form contract (commands.change_role) remains for trusted
internal clients. Handlers only parse and validate input; the actor and
organization always come from the authenticated request, never the form
(SPEC 3.2).
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
from cya.authorization.commands import (
    SELECTABLE_ROLES,
    NoRoleChange,
    RolesChanged,
    TargetInactive,
    change_role,
    change_roles,
)
from cya.authorization.lifecycle import (
    InvalidRoleHistory,
    LastAdministrator,
    RoleEvent,
    current_roles,
)
from cya.authorization.models import RoleGrant
from cya.core.audit_commands import CommandDenied, OperationConflict, OutcomeUnknown, RetryLater
from cya.core.audit_transactions import AuditPersistenceFailure
from cya.core.models import CommandReceipt
from cya.shell import navigation
from cya.shell.views import deny_section

logger = logging.getLogger(__name__)

HISTORY_LIMIT = 50
TARGET_LIMIT = 50
ROLE_EVENT_LIMIT = 500
SEARCH_LIMIT = 100
ACTIONS = ("GRANT", "REVOKE")
ROLE_LABELS = {
    "CREW": "Crew",
    "SUPERVISOR": "Supervisor",
    "SUPPLY_OFFICER": "Supply Officer",
    "ADMINISTRATOR": "Administrator",
}


# Sanitized messages keyed by outcome; no exception text reaches the page.
MESSAGES = {
    "INVALID": "The request was not valid. Check the user, role and action and try again.",
    "HISTORY_UNAVAILABLE": "Current roles could not be verified. Reload the page before trying again.",
    "NOT_FOUND": "That user is not available.",
    "LAST_ADMINISTRATOR": "The last active administrator must keep the Administrator role.",
    "OPERATION_CONFLICT": (
        "This request id was already used for a different request. "
        "The earlier change is unchanged and this one was not applied."
    ),
    "UNAVAILABLE": "The change could not be confirmed. Do not assume it was saved; reload to check.",
}

# Issue #236: refusals of a Save, shown in that user's row. Nothing was applied.
ROW_MESSAGES = {
    "NO_CHANGE": "Nothing to save: the selected roles match the current roles.",
    "ROLES_CHANGED": (
        "Not saved: this user's roles changed since the page loaded. The current roles "
        "are shown now; make the change again if it is still needed."
    ),
    "LAST_ADMINISTRATOR": (
        "Not saved: removing the Administrator role was refused, because the last "
        "active administrator must keep it. No other change was applied."
    ),
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
    search_query="",
    search_error=None,
    form_values=None,
    row_message=None,
):
    """row_message is {"target": id, "kind": "saved" | "error", "text": ...}.

    It is shown in that user's row, or as a page banner when the row is not listed.
    """
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
    targets = list(target_query[:TARGET_LIMIT]) if show and not search_error else []
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
                target.is_self = target.pk == user.pk
                target.row_message = (
                    row_message if row_message and row_message["target"] == str(target.pk) else None
                )
    page_message = None
    if row_message and not any(target.row_message for target in targets):
        page_message = row_message
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
        "page_message": page_message,
        "row_message_shown": bool(row_message),
        "form_values": form_values or {},
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


def _role_list(codes):
    return ", ".join(ROLE_LABELS[code] for code in codes)


def _saved_message(user, value):
    """The D2 summary for a Save this Administrator made, read from its receipt."""
    try:
        operation_id = canonical_id(value)
    except ValueError:
        return None
    receipt = CommandReceipt.objects.filter(
        organization_id=user.organization_id,
        operation_id=operation_id,
        actor_user_id=user.pk,
        command_type="ROLE_CHANGES",
    ).first()
    if receipt is None or receipt.response_body.get("code") != "SAVED":
        return None
    body = receipt.response_body
    target = User.objects.filter(organization_id=user.organization_id, pk=body["user_id"]).first()
    if target is None:
        return None
    parts = []
    if body.get("added"):
        parts.append("added " + _role_list(body["added"]))
    if body.get("removed"):
        parts.append("removed " + _role_list(body["removed"]))
    return {
        "target": str(target.pk),
        "kind": "saved",
        "text": f"Saved for {target.display_name or target.email}: {'; '.join(parts)}.",
    }


def _parse_save(post):
    """Return (target, operation_id, current roles, selected roles) or raise ValueError."""
    target = canonical_id(post.get("target", ""))
    operation_id = canonical_id(post.get("operation_id", ""))
    if uuid.UUID(operation_id).version != 4:
        raise ValueError("operation id must be a UUID4")
    if post.getlist("role_selection_present") != ["1"]:
        raise ValueError("role selection is missing")
    return target, operation_id, post.getlist("current_roles"), post.getlist("roles")


def _save(request, user):
    """Issue #236: apply every staged change for one user, or none of them."""
    query, _search_error = _search_query(request.POST)
    values = _form_values(request.POST)

    def refused(kind, status, target_id):
        return _render_roles(
            request,
            user,
            status=status,
            search_query=query,
            form_values=values,
            row_message={"target": target_id, "kind": "error", "text": ROW_MESSAGES[kind]},
        )

    try:
        target, operation_id, current, selected = _parse_save(request.POST)
    except ValueError:
        return _failure(request, user, "INVALID", 400, search_query=query, form_values=values)
    try:
        result = change_roles(
            organization_id=str(user.organization_id),
            actor_id=str(user.pk),
            target_id=target,
            operation_id=operation_id,
            correlation_id=str(uuid.uuid4()),
            request_attempt_id=str(uuid.uuid4()),
            expected_roles=current,
            selected_roles=selected,
        )
    except CommandDenied:
        # run_command already committed the denial audit after the rollback.
        return render(request, "shell/forbidden.html", {"title": "Not permitted"}, status=403)
    except OperationConflict:
        return _failure(
            request, user, "OPERATION_CONFLICT", 409, search_query=query, form_values=values
        )
    except NoRoleChange:
        return refused("NO_CHANGE", 400, target)
    except RolesChanged:
        return refused("ROLES_CHANGED", 409, target)
    except LastAdministrator:
        return refused("LAST_ADMINISTRATOR", 409, target)
    except (TargetInactive, User.DoesNotExist):
        # As for single changes, a non-administrator learns nothing about user ids.
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
    except ValueError:
        # Invalid role lists and invalid history evidence; nothing was applied.
        return _failure(request, user, "INVALID", 400, search_query=query, form_values=values)
    if result.operation_reused:
        return _failure(
            request, user, "OPERATION_CONFLICT", 409, search_query=query, form_values=values
        )
    if target == str(user.pk) and "ADMINISTRATOR" in result.body.get("removed", ()):
        # D1: the warning said access ends on save; the role page would now be refused.
        return HttpResponseRedirect(reverse("home"))
    # Post/redirect/get: the saved page names the changes and never resubmits them.
    query_string = "?" + urlencode({"saved": operation_id, **({"q": query} if query else {})})
    return HttpResponseRedirect(reverse("administration-roles") + query_string)


@never_cache
@require_http_methods(["GET", "POST"])
def roles(request):
    user = request.cya_user
    if user is None:
        return sign_in_redirect(request)
    if request.method == "POST":
        stage = request.POST.get("stage")
        if stage == "save":
            return _save(request, user)
        if stage == "confirm":
            return _change(request, user)
        if not _is_administrator(user):
            return deny_section(request, user, navigation.roles_for(user), "VIEW_ADMINISTRATION")
        return _failure(request, user, "INVALID", 400)
    current = navigation.roles_for(user)
    if navigation.ADMINISTRATOR not in current:
        return deny_section(request, user, current, "VIEW_ADMINISTRATION")
    query, search_error = _search_query(request.GET)
    saved = request.GET.get("saved")
    row_message = _saved_message(user, saved) if saved and saved != "1" else None
    if saved == "1":
        # The single-change contract still redirects with a plain saved flag.
        row_message = {"target": None, "kind": "saved", "text": "Saved"}
    return _render_roles(
        request,
        user,
        search_query=query,
        search_error=search_error,
        row_message=row_message,
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
