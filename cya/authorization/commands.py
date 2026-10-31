"""Trusted role/active-state commands; all effects and evidence commit together.

These functions are the server boundary for future UI handlers. They accept
IDs and intent, never client role snapshots. Do not wrap in authorized_request:
run_command owns the outermost transaction and independent failure audit.
"""

from cya.accounts.models import User
from cya.authorization.backend import canonical_id, load_state
from cya.authorization.guard import PermissionDenied, Principal, require_permission
from cya.authorization.lifecycle import plan_role_change, validate_deactivation
from cya.authorization.locking import locked_identities
from cya.authorization.persistence import DatabaseRoleBackend, append_change, digest
from cya.authorization.policy import Role
from cya.core.audit_commands import Actor, CommandDenied, Plan, run_command


def _administrators(organization_id, backend):
    # Organization lock serializes every role/active mutation, so these current
    # rows cannot change during the decision; no cached session roles are used.
    return frozenset(
        user.pk
        for user in User.objects.filter(organization_id=organization_id, active=True)
        if Role.ADMINISTRATOR in load_state(backend, user).grants
    )


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


def set_user_active(*, active, **context):
    if type(active) is not bool:
        raise ValueError("Active must be a boolean")
    return _execute(intent={"active": active}, **context)
