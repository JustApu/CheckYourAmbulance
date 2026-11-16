"""Placeholder pages for the sections the feature issues will build.

Each section is enforced on the server: a signed-out visitor is sent to
sign-in, and a signed-in user without a permitted role gets 403 (SPEC 3.2),
whatever the navigation shows.
"""

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from cya.accounts.returns import sign_in_redirect
from cya.authorization.backend import AuthorizationUnavailable, get_backend
from cya.authorization.guard import AuditUnavailable, Denial, PermissionDenied, record_denial
from cya.authorization.policy import permitted
from cya.shell import navigation


def deny_section(request, user, roles, action):
    """Audit a refused section view, then answer 403 (503 if the audit fails)."""
    try:
        record_denial(
            PermissionDenied(
                Denial(
                    str(user.organization_id),
                    str(user.pk),
                    tuple(sorted(roles)),
                    action,
                    "FORBIDDEN",
                )
            ),
            get_backend().record_denial,
        )
    except (AuthorizationUnavailable, AuditUnavailable):
        return JsonResponse({"code": "AUTHORIZATION_UNAVAILABLE"}, status=503)
    return render(request, "shell/forbidden.html", {"title": "Not permitted"}, status=403)


def _section_page(request, key, template="shell/placeholder.html"):
    user = request.cya_user
    if user is None:
        return sign_in_redirect(request)
    section = navigation.section_by_key(key)
    roles = navigation.roles_for(user)
    if not section.roles & roles:
        return deny_section(request, user, roles, "VIEW_" + section.key.upper())
    return render(request, template, {"title": section.label})


def _make_view(key, template="shell/placeholder.html"):
    @never_cache
    @require_GET
    def view(request):
        return _section_page(request, key, template)

    view.__name__ = f"{key}_view"
    return view


fleet = _make_view("fleet")
inspection = _make_view("inspection")
deficiencies = _make_view("deficiencies")
# The administration pages and the action each needs (SPEC 3.3); the landing page lists only
# those the user may open, and every page still checks its own action on the server.
ADMINISTRATION_LINKS = (
    (
        "administration-roles",
        "Role administration",
        "grant or revoke a role and review the role history.",
        None,
    ),
    (
        "vehicle-admin-list",
        "Vehicles",
        "add, search, rename, deactivate, reactivate and duplicate vehicles.",
        "manage_vehicles",
    ),
    (
        "kit-library",
        "Kit library",
        "kit definitions, templates, export and import.",
        "export_configuration",
    ),
    (
        "vehicle-files",
        "Vehicle files",
        "export and import one vehicle's VIN, compartments, items and kit list.",
        "export_configuration",
    ),
    (
        "kit-list-vehicles",
        "Kit lists",
        "edit and publish the kits each vehicle must carry.",
        "manage_vehicle_kit_lists",
    ),
    (
        "kit-instance-vehicles",
        "Kit instances",
        "add a kit to a vehicle, or duplicate one.",
        "manage_kit_assignment",
    ),
    (
        "position-vehicles",
        "Compartments and systems",
        "add, rename, reorder and retire a vehicle's positions.",
        "manage_vehicle_positions",
    ),
)


@never_cache
@require_GET
def administration(request):
    user = request.cya_user
    if user is None:
        return sign_in_redirect(request)
    section = navigation.section_by_key("administration")
    roles = navigation.roles_for(user)
    if not section.roles & roles:
        return deny_section(request, user, roles, "VIEW_ADMINISTRATION")
    links = [
        {"url_name": name, "label": label, "description": description}
        for name, label, description, action in ADMINISTRATION_LINKS
        if (
            navigation.ADMINISTRATOR in roles
            if action is None
            else permitted(action, str(user.pk), roles)
        )
    ]
    return render(request, "shell/administration.html", {"title": section.label, "links": links})


@never_cache
@require_GET
def home(request):
    if request.cya_user is None:
        return sign_in_redirect(request)
    return render(request, "shell/home.html", {"title": "Check Your Ambulance"})
