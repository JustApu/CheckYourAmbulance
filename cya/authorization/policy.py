"""SPEC 3.3 permission matrix. Context must come from server-side records.

This module does not authenticate a user, load roles, or trust request JSON.
Callers obtain current roles and target facts inside their command transaction.
An allow decision never bypasses the action's business validation.
"""

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class Role(StrEnum):
    CREW = "CREW"
    SUPERVISOR = "SUPERVISOR"
    SUPPLY_OFFICER = "SUPPLY_OFFICER"
    ADMINISTRATOR = "ADMINISTRATOR"
    # Legacy compatibility alias; retained for historical evidence and reading existing grants.
    EMT = "EMT"


ROLE_ORDER = (Role.CREW, Role.SUPERVISOR, Role.SUPPLY_OFFICER, Role.ADMINISTRATOR)

# Column order is ROLE_ORDER; these are production policy, not test fixtures.
# The fixture comparison test detects drift from every approved matrix cell.
POLICY = MappingProxyType(
    {
        "record_check": ("allow", "allow", "allow", "allow"),
        "open_join_inspection_session": ("allow", "allow", "allow", "allow"),
        "rejoin_inspection_session": ("allow", "allow", "allow", "allow"),
        "end_inspection_session": ("allow", "allow", "allow", "allow"),
        "correct_check": ("own_only", "own_only", "own_only", "own_only"),
        "record_independent_check": ("allow", "allow", "allow", "allow"),
        "invalidate_others_check": ("deny", "deny", "deny", "other_only"),
        "create_deficiency": ("allow", "allow", "allow", "allow"),
        "link_unlink_deficiencies": ("allow", "allow", "allow", "allow"),
        "assign_deficiency_deadline": ("deny", "allow", "supply_only", "allow"),
        "update_external_dependency": ("allow", "allow", "supply_only", "allow"),
        "add_progress_note": ("allow", "allow", "supply_only", "allow"),
        "request_extension": ("allow", "allow", "assigned_only", "allow"),
        "decide_extension": ("deny", "allow", "deny", "allow"),
        "expand_verification": ("deny", "allow", "deny", "allow"),
        "acknowledge_escalation": ("deny", "allow", "deny", "allow"),
        "complete_unable_inspect": ("deny", "allow", "deny", "allow"),
        "invalidate_unable_completion": ("deny", "deny", "deny", "allow"),
        "suspend_routine_checks": ("deny", "allow", "deny", "allow"),
        "close_deficiency_by_verification": ("automatic",) * 4,
        "explicit_deficiency_close": ("deny", "deny", "deny", "allow"),
        "close_empty_scope_deficiency": ("deny", "allow", "deny", "allow"),
        "reported_in_error_by_correction": ("author_only",) * 4,
        "request_ad_hoc_inspection": ("deny", "allow", "deny", "allow"),
        "set_vehicle_status": ("request_only", "allow", "deny", "allow"),
        "draft_edit_template": ("deny", "allow", "deny", "allow"),
        "publish_template": ("deny", "allow", "deny", "allow"),
        "approve_stock_deviation": ("deny", "allow", "deny", "allow"),
        "approve_early_deviation_termination": ("deny", "allow", "deny", "allow"),
        "manage_users_roles": ("deny", "deny", "deny", "allow"),
        "manage_inventory_catalog_orders": ("deny", "view_only", "allow", "allow"),
        "configure_schedules_thresholds_notifications": ("deny", "deny", "deny", "allow"),
        "export_saved_filters": ("role_controlled",) * 4,
        "verify_break_reseal_kit_seal": ("allow",) * 4,
        "request_kit_audit": ("deny", "allow", "deny", "allow"),
        "manage_kit_assignment": ("deny", "allow", "allow", "allow"),
        "configure_deficiency_categories_severities": ("deny", "deny", "deny", "allow"),
        "regrade_deficiency_severity": ("deny", "allow", "deny", "allow"),
        "record_deficiency_correction": ("allow", "allow", "allow", "allow"),
    }
)


@dataclass(frozen=True)
class ResourceContext:
    """Facts loaded by the handler, never constructed from untrusted JSON."""

    author_id: str | None = None
    assignee_id: str | None = None
    supply_related: bool = False
    intent: str = "mutate"


def _allows(mode, actor_id, context):
    if mode == "allow":
        return True
    if mode in ("own_only", "author_only"):
        # COR-4 is checked in the correction command as a business precondition.
        return context.author_id is not None and context.author_id == actor_id
    if mode == "other_only":
        return context.author_id is not None and context.author_id != actor_id
    if mode == "assigned_only":
        return context.assignee_id is not None and context.assignee_id == actor_id
    if mode == "supply_only":
        return context.supply_related is True
    if mode == "request_only":
        return context.intent == "request"
    if mode == "view_only":
        return context.intent == "view"
    # automatic is system-owned, and export scope is not yet defined (OQ-16).
    return False


def permitted(action, actor_id, roles, *, active=True, context=None):
    """Union of current roles, deny by default including unknown/malformed input."""
    if active is not True or not isinstance(actor_id, str) or not actor_id:
        return False
    if not isinstance(action, str) or action not in POLICY:
        return False
    if context is None:
        context = ResourceContext()
    if not isinstance(context, ResourceContext) or context.intent not in (
        "mutate",
        "request",
        "view",
    ):
        return False
    if not isinstance(roles, (tuple, list, set, frozenset)):
        return False
    try:
        current = {Role.CREW if Role(role) == Role.EMT else Role(role) for role in roles}
    except (TypeError, ValueError):
        return False
    return any(
        _allows(POLICY[action][ROLE_ORDER.index(role)], actor_id, context) for role in current
    )


def unable_self_completion_allowed(actor_id, check_author_id):
    """OQ-41 provisional rule: supervisors may complete their own Unable check."""
    return bool(actor_id and check_author_id and str(actor_id) == str(check_author_id))
