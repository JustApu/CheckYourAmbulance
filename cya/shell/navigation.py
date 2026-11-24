"""Navigation for the application shell, filtered by the signed-in user's roles.

SECTIONS is the access table: each section page checks it on the server. TABS and MORE are
what the navigation shows (issue #194): the same five places for every role, Home, Inspect,
Problems, Vehicles and More, with Administration under More for roles that may open it. "Problems" is the
on-screen word for what the SPEC, code and records call deficiencies. Vehicles brings the
vehicle status list and fleet readiness together.

Hiding a link is a convenience only: every page still enforces access on the
server (SPEC 3.2). Roles come from an injected lookup, settings.CYA_ROLE_LOOKUP
(a dotted path to a callable taking the user and returning role names), which
issue 22 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/22) provides. While it is unset, or if it fails, the user has no roles
and sees no sections (fail closed).
"""

import logging
from dataclasses import dataclass

from django.conf import settings
from django.urls import reverse
from django.utils.module_loading import import_string

logger = logging.getLogger(__name__)

CREW = "CREW"
SUPERVISOR = "SUPERVISOR"
SUPPLY_OFFICER = "SUPPLY_OFFICER"
ADMINISTRATOR = "ADMINISTRATOR"
ALL_ROLES = frozenset({CREW, SUPERVISOR, SUPPLY_OFFICER, ADMINISTRATOR})

# Deprecated backward-compatibility alias for CREW
EMT = CREW


@dataclass(frozen=True)
class Section:
    key: str
    label: str
    url_name: str
    roles: frozenset  # a user needs at least one of these (union of roles, SPEC 3.2)


# Placeholders for the feature issues. Inspection, fleet and deficiency
# records are open to every built-in role (SPEC 3.3). Administration is open to
# Supervisors (issue #130 part 5b) and Supply Officers (part 5c, for kit instances): its
# landing page lists only the pages a user may open, and every page checks its own action,
# so role administration and import stay Administrator-only.
SECTIONS = (
    Section("fleet", "Fleet", "fleet-readiness", ALL_ROLES),
    Section("inspection", "Inspection", "inspection", ALL_ROLES),
    Section("status", "Vehicle status", "status", ALL_ROLES),
    Section("deficiencies", "Problems", "deficiencies", ALL_ROLES),
    Section(
        "administration",
        "Administration",
        "administration",
        frozenset({SUPERVISOR, SUPPLY_OFFICER, ADMINISTRATOR}),
    ),
)


@dataclass(frozen=True)
class Tab:
    key: str
    label: str
    icon: str
    url_name: str
    paths: tuple  # URL path prefixes this tab owns, for marking the current page
    roles: frozenset


TABS = (
    Tab("home", "Home", "home", "home", ("/",), ALL_ROLES),
    Tab("inspect", "Inspect", "inspect", "inspection", ("/inspection/",), ALL_ROLES),
    Tab("problems", "Problems", "problems", "deficiencies", ("/problems/",), ALL_ROLES),
    Tab("vehicles", "Vehicles", "vehicles", "status", ("/status/", "/fleet/"), ALL_ROLES),
)


def roles_for(user):
    """The user's role names, or an empty set if unknown (fail closed)."""
    lookup = getattr(settings, "CYA_ROLE_LOOKUP", None)
    if user is None or not lookup:
        return frozenset()
    try:
        roles = frozenset(import_string(lookup)(user))
        return roles if roles <= ALL_ROLES else frozenset()
    except Exception as exc:
        # Collaborator text could hold private data; log the class only.
        logger.error("role lookup failed (%s)", type(exc).__name__)
        return frozenset()


def sections_for(user):
    roles = roles_for(user)
    return [section for section in SECTIONS if section.roles & roles]


def section_by_key(key):
    return next(section for section in SECTIONS if section.key == key)


def _owns(paths, path):
    return any(path == prefix if prefix == "/" else path.startswith(prefix) for prefix in paths)


# Keyboard shortcuts (issue #194): "G" then this letter goes to the place. Letters are unique.
SHORTCUTS = {
    "home": "h",
    "inspect": "i",
    "problems": "p",
    "vehicles": "v",
    "administration": "a",
}


def app_navigation(user, path):
    """Tabs and More entries for the user, with the current one marked; None if no roles."""
    roles = roles_for(user)
    tabs = [tab for tab in TABS if tab.roles & roles]
    if not tabs:
        return None
    more = []
    if section_by_key("administration").roles & roles:
        more.append(
            {
                "key": "administration",
                "label": "Administration",
                "shortcut": SHORTCUTS["administration"],
                "icon": "administration",
                "url": reverse("administration"),
                "current": _owns(("/administration/",), path),
            }
        )
    return {
        "tabs": [
            {
                "key": tab.key,
                "label": tab.label,
                "shortcut": SHORTCUTS.get(tab.key),
                "icon": tab.icon,
                "url": reverse(tab.url_name),
                "current": _owns(tab.paths, path),
            }
            for tab in tabs
        ],
        "more": more,
        "more_current": any(item["current"] for item in more),
    }


def menu(user, current_key=None):
    """Template-ready navigation entries for the user."""
    return [
        {
            "key": section.key,
            "label": section.label,
            "url": reverse(section.url_name),
            "current": section.key == current_key,
        }
        for section in sections_for(user)
    ]
