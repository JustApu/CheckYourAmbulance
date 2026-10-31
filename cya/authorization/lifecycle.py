"""Role evidence validation and lifecycle decisions, without persistence.

Callers must load these facts after taking organization/user locks, authorize
the actor, then append a command-linked event and audit atomically. A plan is
not an executed command. This module never seeds, grants, revokes or disables
any database identity; commands.py and persistence.py execute these plans.
"""

from dataclasses import dataclass
from types import MappingProxyType
from uuid import UUID

from cya.authorization.policy import Role


class InvalidRoleHistory(ValueError):
    """Evidence is inconsistent; authorization must fail closed."""


class LastAdministrator(ValueError):
    """The proposed change would remove the final active administrator."""


class BootstrapRefused(ValueError):
    """Operator bootstrap is not valid for this organization's current history."""


@dataclass(frozen=True)
class RoleEvent:
    id: str
    organization_id: str
    user_id: str
    sequence: int
    role: str
    action: str
    prior_grant_id: str | None = None


@dataclass(frozen=True)
class RoleState:
    organization_id: str
    user_id: str
    sequence: int
    grants: MappingProxyType


@dataclass(frozen=True)
class RoleChange:
    role: Role
    action: str
    prior_grant_id: str | None
    sequence: int


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def current_roles(events, *, organization_id, user_id):
    """Fold server-ordered per-user events; reject gaps, forks and wrong scope.

    Input is a finite, ordered database stream. Sequence is authoritative;
    UUID or timestamp ordering must never replace it. Grants are identified
    individually so a revoke cannot accidentally consume a later re-grant.
    """
    if not _uuid(organization_id) or not _uuid(user_id):
        raise InvalidRoleHistory("Invalid identity scope")
    grants, seen = {}, set()
    sequence = 0
    for event in events:
        if (
            not isinstance(event, RoleEvent)
            or event.organization_id != organization_id
            or event.user_id != user_id
            or not _uuid(event.id)
            or event.id in seen
            or type(event.sequence) is not int
            or event.sequence != sequence + 1
        ):
            raise InvalidRoleHistory("Invalid event identity, scope or sequence")
        try:
            role = Role(event.role)
        except (ValueError, TypeError) as exc:
            raise InvalidRoleHistory("Unknown role") from exc
        if event.action == "GRANT":
            if (
                role in grants
                or (role in (Role.CREW, Role.EMT) and (Role.CREW in grants or Role.EMT in grants))
                or event.prior_grant_id is not None
            ):
                raise InvalidRoleHistory("Duplicate or malformed grant")
            grants[role] = event.id
        elif event.action == "REVOKE":
            if role not in grants or event.prior_grant_id != grants[role]:
                raise InvalidRoleHistory("Revocation must reference the active matching grant")
            del grants[role]
        else:
            raise InvalidRoleHistory("Unknown role event action")
        seen.add(event.id)
        sequence = event.sequence
    return RoleState(organization_id, user_id, sequence, MappingProxyType(grants))


def _admin_guard(*, target_id, target_active, has_admin, active_administrator_ids):
    if type(target_active) is not bool or not isinstance(active_administrator_ids, frozenset):
        raise InvalidRoleHistory("Invalid administrator snapshot")
    if not all(_uuid(user_id) for user_id in active_administrator_ids):
        raise InvalidRoleHistory("Invalid administrator identity")
    if (target_id in active_administrator_ids) != (target_active and has_admin):
        raise InvalidRoleHistory("Inconsistent administrator snapshot")
    if target_active and has_admin and len(active_administrator_ids) == 1:
        raise LastAdministrator("The last active administrator must remain active")


BASE_ROLES = (Role.CREW, Role.EMT)


def plan_role_change(state, role, action, *, target_active, active_administrator_ids):
    """Return one append plan or None for an already-satisfied requested state.

    Caller must first require manage_users_roles on its current actor. The
    administrator set must be derived in the same organization transaction,
    while its organization anchor is locked; it never comes from a request.
    """
    role = Role(role)
    if action not in ("GRANT", "REVOKE") or type(target_active) is not bool:
        raise ValueError("Invalid role-change request")
    if role in BASE_ROLES:
        active_base = next((r for r in BASE_ROLES if r in state.grants), None)
        if action == "GRANT":
            if active_base is not None:
                return None
            return RoleChange(Role.CREW, action, None, state.sequence + 1)
        # action == "REVOKE"
        if active_base is None:
            return None
        return RoleChange(active_base, action, state.grants[active_base], state.sequence + 1)
    if action == "GRANT":
        if role in state.grants:
            return None
        return RoleChange(role, action, None, state.sequence + 1)
    if role not in state.grants:
        return None
    if role == Role.ADMINISTRATOR:
        _admin_guard(
            target_id=state.user_id,
            target_active=target_active,
            has_admin=True,
            active_administrator_ids=active_administrator_ids,
        )
    return RoleChange(role, action, state.grants[role], state.sequence + 1)


def validate_deactivation(state, *, target_active, active_administrator_ids):
    """Reject disabling the final active admin, using the same locked snapshot."""
    _admin_guard(
        target_id=state.user_id,
        target_active=target_active,
        has_admin=Role.ADMINISTRATOR in state.grants,
        active_administrator_ids=active_administrator_ids,
    )


def plan_initial_crew(state):
    """Plan only Crew for a newly provisioned identity, inside its creation command."""
    if state.sequence != 0 or state.grants:
        raise InvalidRoleHistory("Initial provisioning requires an empty role history")
    return RoleChange(Role.CREW, "GRANT", None, 1)


# Deprecated backward-compatibility alias for plan_initial_crew
plan_initial_emt = plan_initial_crew


def plan_first_administrator(state, *, target_active, has_administrator_history):
    """Decision for the operator-only command; never an HTTP privilege switch.

    The operator command must lock the organization before reading whether
    any administrator grant has ever existed, including subsequently revoked
    grants and inactive users. An existing provisioned active crew member is required.
    No identity, grant or audit is created by this pure decision function.
    """
    if (
        target_active is not True
        or has_administrator_history is not False
        or set(state.grants) not in ({Role.CREW}, {Role.EMT})
    ):
        raise BootstrapRefused(
            "First administrator requires an active crew member and no admin history"
        )
    return RoleChange(Role.ADMINISTRATOR, "GRANT", None, state.sequence + 1)
