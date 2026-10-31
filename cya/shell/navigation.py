"""Navigation for the application shell, filtered by the signed-in user's roles.

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
# records are open to every built-in role (SPEC 3.3); user and role
# administration is Administrator-only.
SECTIONS = (
    Section("fleet", "Fleet", "fleet", ALL_ROLES),
    Section("inspection", "Inspection", "inspection", ALL_ROLES),
    Section("status", "Vehicle status", "status", ALL_ROLES),
    Section("deficiencies", "Deficiencies", "deficiencies", ALL_ROLES),
    Section("administration", "Administration", "administration", frozenset({ADMINISTRATOR})),
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
