"""Shell pages: the home page, the Administration landing page and the 403 page.

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
        "mail-relay",
        "Outgoing mail",
        "set the relay for operations alerts and email, and send a test message.",
        "configure_schedules_thresholds_notifications",
    ),
    (
        "crew-assignment-settings",
        "Crew assignments",
        "set the longest a vehicle crew assignment may run.",
        "configure_schedules_thresholds_notifications",
    ),
    (
        "vehicle-files",
        "Vehicle files",
        "export and import one vehicle's VIN, compartments, items and kit list.",
        "export_configuration",
    ),
    (
        "kit-definitions",
        "Kit definitions",
        "create, edit, duplicate, publish and deactivate kit definitions.",
        "manage_kit_definitions",
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
        "checklist-templates",
        "Checklist templates",
        "create, edit, duplicate and publish checklist templates.",
        "draft_edit_template",
    ),
    (
        "position-vehicles",
        "Compartments and systems",
        "add, rename, reorder and retire a vehicle's positions.",
        "manage_vehicle_positions",
    ),
)


# The landing page's groups, in order, and each group's pages in task order (issue #194):
# people, then a vehicle's setup, then kits, then settings for the whole system.
ADMINISTRATION_GROUPS = (
    ("people", "People", "Who can do what.", ("administration-roles",)),
    (
        "vehicles",
        "Vehicles",
        "Vehicles, their compartments and systems, and their checklists.",
        ("vehicle-admin-list", "position-vehicles", "checklist-templates", "vehicle-files"),
    ),
    (
        "kits",
        "Kits",
        "What each kit holds, which kits each vehicle carries, and the shared library.",
        ("kit-definitions", "kit-list-vehicles", "kit-instance-vehicles", "kit-library"),
    ),
    (
        "system",
        "System",
        "Settings for the whole system.",
        ("mail-relay", "crew-assignment-settings"),
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
    links = {
        name: {"url_name": name, "label": label, "description": description}
        for name, label, description, action in ADMINISTRATION_LINKS
        if (
            navigation.ADMINISTRATOR in roles
            if action is None
            else permitted(action, str(user.pk), roles)
        )
    }
    groups = [
        {
            "key": key,
            "label": label,
            "summary": summary,
            "links": [links[name] for name in names if name in links],
        }
        for key, label, summary, names in ADMINISTRATION_GROUPS
    ]
    groups = [group for group in groups if group["links"]]
    return render(request, "shell/administration.html", {"title": section.label, "groups": groups})


@never_cache
@require_GET
def home(request):
    if request.cya_user is None:
        return sign_in_redirect(request)
    from cya.operations.crew_views import message_for
    from cya.shell import home as home_screen

    crew_saved, crew_error = message_for(request, request.cya_user)
    return render(
        request,
        "shell/home.html",
        {
            "title": "Check Your Ambulance",
            "home": home_screen.build(request.cya_user),
            "crew_saved": crew_saved,
            "crew_error": crew_error,
        },
    )
