"""A crew member's view and filter preference (SPEC 6.5 VW-1, VW-2; OQ-38, issue 83 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/83)).

The preference belongs to the signed-in user and follows them across devices. save_preferences is
the one writer: an audited command (cya.core.audit_commands.run_command: one outermost
transaction, an operation-id receipt that replays the original result, a success or failure audit
with the prior and new values). Do not wrap it in a transaction.

Authorization: a user may read and change only their own preference. The identity is the
authenticated actor, never a field of the request, so no role permission is involved and no
SPEC 3.3 row applies; the actor must still be an active user of an active organization.
Defaults for a user who never saved one: Normal view, filter off.
"""

from cya.accounts.fields import new_id
from cya.accounts.models import User
from cya.attention.models import VIEWS, UserPreference
from cya.authorization.backend import load_state
from cya.authorization.locking import locked_identities
from cya.authorization.persistence import DatabaseRoleBackend, digest
from cya.authorization.policy import Role
from cya.core.audit_commands import Actor, Plan, run_command

COMMAND = "SAVE_VIEW_PREFERENCES"
DEFAULT_VIEW = "NORMAL"


class PreferenceError(ValueError):
    """The request is malformed; the command rolls back and is audited as failed."""


def get_preferences(organization_id, user_id):
    """{"preferred_view", "needs_attention_only"} for the user, defaults when none is saved."""
    row = UserPreference.objects.filter(organization_id=organization_id, user_id=user_id).first()
    if row is None:
        return {"preferred_view": DEFAULT_VIEW, "needs_attention_only": False}
    return {"preferred_view": row.preferred_view, "needs_attention_only": row.needs_attention_only}


def _authorize(organization_id, actor_id):
    backend = DatabaseRoleBackend()

    def authorize(_org):
        with locked_identities(organization_id, [actor_id], lock_organization=False) as (
            _current_org,
            users,
        ):
            user = users[actor_id]
            roles = frozenset(
                Role.CREW if r == Role.EMT else r for r in load_state(backend, user).grants
            )
            return Actor(user=user, roles=tuple(sorted(r.value for r in roles)))

    return authorize


def _snapshot(values):
    return {"schema_version": 1, "values": values}


def save_preferences(*, preferred_view, needs_attention_only, **context):
    """Replace the actor's preference with both values; replays on the same operation id."""
    org, actor_id = context["organization_id"], context["actor_id"]
    if preferred_view not in VIEWS:
        raise PreferenceError("preferred_view must be NORMAL or PRIORITY")
    if type(needs_attention_only) is not bool:
        raise PreferenceError("needs_attention_only must be true or false")
    wanted = {"preferred_view": preferred_view, "needs_attention_only": needs_attention_only}

    def build(actor):
        # The user row is the lock: it serializes the first save of two devices at once.
        User.objects.select_for_update().get(pk=actor.user.pk, organization_id=org)
        prior = UserPreference.objects.filter(organization_id=org, user_id=actor.user.pk).first()
        prior_values = (
            None
            if prior is None
            else {
                "preferred_view": prior.preferred_view,
                "needs_attention_only": prior.needs_attention_only,
            }
        )

        def apply(receipt, current):
            if prior is None:
                UserPreference.objects.create(
                    id=new_id(),
                    organization_id=org,
                    user_id=current.user.pk,
                    updated_at=receipt.recorded_at,
                    **wanted,
                )
            else:
                UserPreference.objects.filter(pk=prior.pk).update(
                    updated_at=receipt.recorded_at, **wanted
                )

        return Plan(
            response_body={"code": "VIEW_PREFERENCES_SAVED", **wanted},
            apply=apply,
            target_type="USER",
            target_id=str(actor.user.pk),
            prior_snapshot=None if prior_values is None else _snapshot(prior_values),
            new_snapshot=_snapshot(wanted),
        )

    return run_command(
        organization_id=org,
        operation_id=context["operation_id"],
        command_type=COMMAND,
        request_digest=digest(wanted),
        correlation_id=context["correlation_id"],
        request_attempt_id=context["request_attempt_id"],
        authorize=_authorize(org, actor_id),
        prepare=build,
    )
