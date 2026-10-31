"""Role administration over HTTP: the walking skeleton's end-to-end action (issue 25 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/25)).

GET shows the organization's append-only role history to an Administrator. POST
changes one role through commands.change_role, which owns authorization,
receipt, effect and audit in a single transaction. The handler only parses and
validates input and maps outcomes to responses; the role, target and operation
id come from the form, never the actor, organization or roles (SPEC 3.2).
"""

import logging
import uuid

from django.db import DatabaseError
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cya.accounts.models import User
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
TARGET_LIMIT = 200
ACTIONS = ("GRANT", "REVOKE")
SELECTABLE_ROLES = tuple(role.value for role in Role if role != Role.EMT)  # EMT is a legacy alias

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


def _render_history(request, user, *, status=200, error=None, saved=False):
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
    targets = (
        User.objects.filter(organization_id=user.organization_id, active=True).order_by("email")[
            :TARGET_LIMIT
        ]
        if show
        else []
    )
    context = {
        "title": "Role administration",
        "show": show,
        "history": history,
        "targets": targets,
        "roles": SELECTABLE_ROLES,
        "actions": ACTIONS,
        # A fresh id per rendered form: a double submit replays instead of repeating.
        "operation_id": str(uuid.uuid4()),
        "error": error,
        "saved": saved,
    }
    return render(request, "authorization/roles.html", context, status=status)


def _failure(request, user, outcome, status):
    return _render_history(request, user, status=status, error=MESSAGES[outcome])


def _change(request, user):
    try:
        target, role, action, operation_id = _parse(request.POST)
    except ValueError:
        return _failure(request, user, "INVALID", 400)
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
        return _failure(request, user, "OPERATION_CONFLICT", 409)
    except LastAdministrator:
        return _failure(request, user, "LAST_ADMINISTRATOR", 409)
    except InvalidRoleHistory:
        return _failure(request, user, "INVALID", 400)
    except User.DoesNotExist:
        # The target is resolved before permission is checked, so a non-administrator
        # must not learn which user ids exist: they get the same 403 as a denial.
        if not _is_administrator(user):
            return render(request, "shell/forbidden.html", {"title": "Not permitted"}, status=403)
        return _failure(request, user, "NOT_FOUND", 404)
    except (
        RetryLater,
        OutcomeUnknown,
        AuditPersistenceFailure,
        AuthorizationUnavailable,
        DatabaseError,
    ):
        logger.error("role change unavailable")
        return _failure(request, user, "UNAVAILABLE", 503)
    if result.operation_reused:
        # Same actor and id but a different request: run_command replays the
        # original acknowledgement and applies nothing, so never report it saved.
        return _failure(request, user, "OPERATION_CONFLICT", 409)
    # Post/redirect/get: reloading the result page never resubmits the change.
    return HttpResponseRedirect(reverse("administration-roles") + "?saved=1")


@never_cache
@require_http_methods(["GET", "POST"])
def roles(request):
    user = request.cya_user
    if user is None:
        return HttpResponseRedirect("/auth/login/")
    if request.method == "POST":
        return _change(request, user)
    current = navigation.roles_for(user)
    if navigation.ADMINISTRATOR not in current:
        return deny_section(request, user, current, "VIEW_ADMINISTRATION")
    return _render_history(request, user, saved=request.GET.get("saved") == "1")
