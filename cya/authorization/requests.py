"""Request guards over current, locked identity/role facts; no client role inputs."""

from contextlib import ExitStack, contextmanager
from functools import wraps
from inspect import iscoroutinefunction

from django.db import DatabaseError, transaction
from django.http import HttpResponse, JsonResponse

from cya.accounts.models import Organization, User
from cya.authorization.backend import (
    AuthorizationUnavailable,
    canonical_id,
    get_backend,
    load_state,
)
from cya.authorization.guard import (
    AuditUnavailable,
    PermissionDenied,
    Principal,
    record_denial,
    require_permission,
)
from cya.authorization.locking import locked_identities
from cya.authorization.policy import POLICY, Role


@contextmanager
def authorized_request(request, organization_id, action, *, context_loader=None):
    """Hold authorization locks through a brief synchronous command/view body.

    organization_id and context_loader come from the route's server-side
    handler. The loader receives the freshly loaded user and must read target
    ownership, current assignment and category revision from server records.
    This must be the outermost application transaction, so denial auditing
    happens after rollback. It is not a receipt/business-audit executor.
    Only the actor is locked; the organization is read without an exclusive
    lock so different users can enter concurrently. Multi-user commands acquire
    their complete identity set in sorted order before checking permission.
    Role/active-state commands instead use their organization-first boundary;
    do not acquire that organization lock from inside this user-only guard.
    """
    organization_id = canonical_id(organization_id)
    backend = get_backend()
    try:
        with transaction.atomic(durable=True):
            cached_user = getattr(request, "cya_user", None)
            if (
                not isinstance(cached_user, User)
                or str(cached_user.organization_id) != organization_id
            ):
                require_permission(None, organization_id, action)
            with ExitStack() as stack:
                try:
                    org, users = stack.enter_context(
                        locked_identities(
                            organization_id, [str(cached_user.pk)], lock_organization=False
                        )
                    )
                except (Organization.DoesNotExist, User.DoesNotExist):
                    require_permission(None, organization_id, action)
                user = users[str(cached_user.pk)]
                state = load_state(backend, user)
                principal = Principal(
                    organization_id,
                    str(user.pk),
                    frozenset(Role.CREW if r == Role.EMT else r for r in state.grants),
                    user.active is True and org.active is True,
                )
                if not isinstance(action, str) or action not in POLICY:
                    require_permission(principal, organization_id, action)
                context = context_loader(request, user) if context_loader else None
                require_permission(principal, organization_id, action, context=context)
                yield principal
    except PermissionDenied as error:
        record_denial(error, backend.record_denial)
        raise


def permission_required(action, *, organization_loader, context_loader=None):
    """Decorate a synchronous, non-streaming view; never infer permissions from UI.

    Loaders receive (request, *view_args, **view_kwargs); the context loader
    additionally receives the current locked user as its second argument.
    Future domain handlers still own business validation, receipt replay and
    success/failure audit. No arbitrary route is exposed by this helper.
    """

    def decorate(view):
        if iscoroutinefunction(view):
            raise TypeError("Authorization decorator requires a synchronous view")

        @wraps(view)
        def guarded(request, *args, **kwargs):
            try:
                organization_id = organization_loader(request, *args, **kwargs)
                loader = (
                    (lambda req, user: context_loader(req, user, *args, **kwargs))
                    if context_loader
                    else None
                )
                with authorized_request(request, organization_id, action, context_loader=loader):
                    response = view(request, *args, **kwargs)
                    if not isinstance(response, HttpResponse):
                        raise AuthorizationUnavailable("A non-streaming HTTP response is required")
                    if hasattr(response, "render"):
                        response = response.render()
                        if not isinstance(response, HttpResponse):
                            raise AuthorizationUnavailable(
                                "A non-streaming HTTP response is required"
                            )
                    return response
            except PermissionDenied:
                return JsonResponse({"code": "FORBIDDEN"}, status=403)
            except (AuthorizationUnavailable, AuditUnavailable, DatabaseError):
                return JsonResponse({"code": "AUTHORIZATION_UNAVAILABLE"}, status=503)

        return guarded

    return decorate
