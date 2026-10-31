"""Placeholder pages for the sections the feature issues will build.

Each section is enforced on the server: a signed-out visitor is sent to
sign-in, and a signed-in user without a permitted role gets 403 (SPEC 3.2),
whatever the navigation shows.
"""

from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from cya.authorization.backend import AuthorizationUnavailable, get_backend
from cya.authorization.guard import AuditUnavailable, Denial, PermissionDenied, record_denial
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
        return HttpResponseRedirect("/auth/login/")
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
administration = _make_view("administration", "shell/administration.html")


@never_cache
@require_GET
def home(request):
    if request.cya_user is None:
        return HttpResponseRedirect("/auth/login/")
    return render(request, "shell/home.html", {"title": "Check Your Ambulance"})
