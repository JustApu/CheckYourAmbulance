"""Trusted role/active-state commands; all effects and evidence commit together.

These functions are the server boundary for future UI handlers. They accept
IDs and intent, never client role snapshots. Do not wrap in authorized_request:
run_command owns the outermost transaction and independent failure audit.
"""

from cya.accounts.models import User
from cya.authorization.backend import canonical_id, load_state
from cya.authorization.guard import PermissionDenied, Principal, require_permission
from cya.authorization.lifecycle import RoleState, plan_role_change, validate_deactivation
from cya.authorization.locking import locked_identities
from cya.authorization.persistence import DatabaseRoleBackend, append_change, digest
from cya.authorization.policy import Role
from cya.core.audit_commands import Actor, CommandDenied, Plan, run_command
from cya.core.audit_repository import append_audit

# The roles an Administrator can choose. EMT is a legacy alias of Crew.
SELECTABLE_ROLES = tuple(role.value for role in Role if role != Role.EMT)


class RolesChanged(ValueError):
    """The target's current roles differ from the roles the page was showing."""


class TargetInactive(ValueError):
    """Inactive users cannot receive role changes."""


class NoRoleChange(ValueError):
    """The selection equals the current roles, so there is nothing to save."""


def _role_codes(state):
    codes = {"CREW" if role == Role.EMT else role.value for role in state.grants}
    return sorted(codes, key=SELECTABLE_ROLES.index)


def _administrators(organization_id, backend):
    # Organization lock serializes every role/active mutation, so these current
    # rows cannot change during the decision; no cached session roles are used.
    return frozenset(
        user.pk
        for user in User.objects.filter(organization_id=organization_id, active=True)
        if Role.ADMINISTRATOR in load_state(backend, user).grants
    )


def _authorizer(organization_id, actor_id, target_id, backend, locked):
    def authorize(org):
        with locked_identities(organization_id, [actor_id, target_id]) as (current_org, users):
            actor_user = users[actor_id]
            state = load_state(backend, actor_user)
            actor = Actor(
                user=actor_user,
                roles=tuple(
                    sorted((Role.CREW if r == Role.EMT else r).value for r in state.grants)
                ),
            )
            principal = Principal(
                organization_id,
                actor_id,
                frozenset(Role.CREW if r == Role.EMT else r for r in state.grants),
                actor_user.active and current_org.active,
            )
            try:
                require_permission(principal, organization_id, "manage_users_roles")
            except PermissionDenied:
                raise CommandDenied(actor) from None
            locked.update(users)
            # Nested atomic exit retains locks until run_command commits.
            return actor

    return authorize


def _execute(
    *,
    organization_id,
    actor_id,
    target_id,
    operation_id,
    correlation_id,
    request_attempt_id,
    intent,
):
    for value in (organization_id, actor_id, target_id):
        canonical_id(value)
    backend, locked = DatabaseRoleBackend(), {}
    authorize = _authorizer(organization_id, actor_id, target_id, backend, locked)

    def prepare(actor):
        target = locked[target_id]
        state = load_state(backend, target)
        before = {"roles": sorted(r.value for r in state.grants), "active": target.active}
        after = {"roles": list(before["roles"]), "active": target.active}
        admins = _administrators(organization_id, backend)
        change = None
        if "role" in intent:
            change = plan_role_change(
                state,
                intent["role"],
                intent["action"],
                target_active=target.active,
                active_administrator_ids=admins,
            )
            if change:
                if change.action == "GRANT":
                    after["roles"] = sorted([*after["roles"], change.role.value])
                else:
                    after["roles"].remove(change.role.value)
        else:
            if intent["active"] is False:
                validate_deactivation(
                    state, target_active=target.active, active_administrator_ids=admins
                )
            after["active"] = intent["active"]

        def apply(receipt, current_actor):
            if change is not None:
                append_change(
                    user=target, change=change, receipt=receipt, actor_roles=current_actor.roles
                )
            elif "active" in intent and target.active != intent["active"]:
                User.objects.filter(pk=target.pk).update(active=intent["active"])

        return Plan(
            response_body={"code": "SAVED", "user_id": target_id, **after},
            apply=apply,
            target_type="USER",
            target_id=target_id,
            prior_snapshot={"schema_version": 1, "values": before},
            new_snapshot={"schema_version": 1, "values": after},
        )

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type="ROLE_CHANGE" if "role" in intent else "USER_ACTIVE_CHANGE",
        request_digest=digest({"target_id": target_id, **intent}),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=authorize,
        prepare=prepare,
    )


def change_role(*, role, action, **context):
    role = Role(role)
    if action not in ("GRANT", "REVOKE"):
        raise ValueError("Unknown role action")
    return _execute(intent={"role": role.value, "action": action}, **context)


def _batch_order(role):
    return SELECTABLE_ROLES.index(role)


def change_roles(
    *,
    organization_id,
    actor_id,
    target_id,
    operation_id,
    correlation_id,
    request_attempt_id,
    expected_roles,
    selected_roles,
):
    """Apply every difference between a user's current and selected roles, or none.

    Issue #236: one Save is one audited command with one receipt. Each role
    change is its own role event and its own audit row, linked to that receipt.
    expected_roles are the roles the page was rendered with: when the user's
    current roles differ, the Save is refused (RolesChanged) rather than
    applying a stale selection. Additions are planned before removals, each
    against the state left by the previous change, so sequences chain and the
    last-administrator guard sees the same locked snapshot. Any refusal rolls
    back the whole Save. Replaying operation_id returns the original result.
    """
    for value in (organization_id, actor_id, target_id):
        canonical_id(value)
    expected, selected = set(expected_roles), set(selected_roles)
    for roles, values in ((expected, expected_roles), (selected, selected_roles)):
        if len(roles) != len(values) or not roles <= set(SELECTABLE_ROLES):
            raise ValueError("Role selection is invalid")
    intent = {
        "expected_roles": sorted(expected, key=_batch_order),
        "selected_roles": sorted(selected, key=_batch_order),
    }
    backend, locked = DatabaseRoleBackend(), {}
    authorize = _authorizer(organization_id, actor_id, target_id, backend, locked)

    def prepare(actor):
        # Checked after authorization, so only an Administrator learns these outcomes.
        if expected == selected:
            raise NoRoleChange("Nothing to save")
        target = locked[target_id]
        if not target.active:
            raise TargetInactive("Inactive users cannot receive role changes")
        state = load_state(backend, target)
        before = _role_codes(state)
        if set(before) != expected:
            raise RolesChanged("Current roles differ from the page")
        admins = _administrators(organization_id, backend)
        requests = [(role, "GRANT") for role in intent["selected_roles"] if role not in expected]
        requests += [(role, "REVOKE") for role in intent["expected_roles"] if role not in selected]
        changes, grants, sequence = [], dict(state.grants), state.sequence
        for role, action in requests:
            running = RoleState(state.organization_id, state.user_id, sequence, grants)
            change = plan_role_change(
                running,
                role,
                action,
                target_active=target.active,
                active_administrator_ids=admins,
            )
            if change is None:  # Already satisfied: cannot happen after the stale check.
                raise RolesChanged("Current roles differ from the page")
            changes.append(change)
            sequence = change.sequence
            if action == "GRANT":
                # A placeholder: no change in one Save revokes a role it grants.
                grants[change.role] = None
            else:
                del grants[change.role]
        after = sorted(selected, key=_batch_order)
        added = [change.role.value for change in changes if change.action == "GRANT"]
        removed = [
            "CREW" if change.role == Role.EMT else change.role.value
            for change in changes
            if change.action == "REVOKE"
        ]

        def apply(receipt, current_actor):
            for change in changes:
                append_change(
                    user=target, change=change, receipt=receipt, actor_roles=current_actor.roles
                )
                # The audit guard ties a receipt's rows to its command type; the
                # snapshot names this one change ({"role", "action"}), while the
                # automatic summary row holds the before and after role sets.
                append_audit(
                    organization=receipt.organization,
                    command=receipt,
                    request_attempt_id=request_attempt_id,
                    correlation_id=correlation_id,
                    action="ROLE_CHANGES",
                    outcome="SUCCEEDED",
                    actor_user=current_actor.user,
                    roles_at_time=list(current_actor.roles),
                    target_type="USER",
                    target_id=target_id,
                    new_snapshot={
                        "schema_version": 1,
                        "values": {"role": change.role.value, "action": change.action},
                    },
                )

        return Plan(
            response_body={
                "code": "SAVED",
                "user_id": target_id,
                "roles": after,
                "added": added,
                "removed": removed,
            },
            apply=apply,
            target_type="USER",
            target_id=target_id,
            prior_snapshot={"schema_version": 1, "values": {"roles": before}},
            new_snapshot={"schema_version": 1, "values": {"roles": after}},
        )

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type="ROLE_CHANGES",
        request_digest=digest({"target_id": target_id, **intent}),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=authorize,
        prepare=prepare,
    )


def set_user_active(*, active, **context):
    if type(active) is not bool:
        raise ValueError("Active must be a boolean")
    return _execute(intent={"active": active}, **context)
