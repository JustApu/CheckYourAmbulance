"""Audited notification commands: mandatory policy, own preferences, mark read (SPEC 13).

Like cya.operations.commands, each function is the server boundary for a future handler: it takes
identifiers and intent, never client role snapshots, and runs through run_command (do not wrap it
in a transaction). Actor, organization and roles come from the signed-in user.

  set_policy      administrator only (configure_schedules_thresholds_notifications): which
                  event/channel pairs are mandatory. Data, not code; there are no default rows.
  set_recipient_rule  administrator only (same permission): turn one recipient kind on or off
                  for one event type, overriding the documented default (recipients.py).
  set_preference  any role holder, for themselves only. An optional preference cannot switch off
                  a mandatory event/channel (409 MANDATORY_EVENT), and enqueue also ignores a
                  stale off-switch once policy makes the pair mandatory.
  mark_read       the recipient only, for an in-app item. Another user's item looks absent.
"""

from cya.authorization.backend import canonical_id, load_state
from cya.authorization.guard import PermissionDenied, Principal, require_permission
from cya.authorization.locking import locked_identities
from cya.authorization.persistence import DatabaseRoleBackend, digest
from cya.authorization.policy import Role
from cya.core.audit_commands import Actor, CommandDenied, Plan, run_command
from cya.notifications import recipients, rules
from cya.notifications.models import (
    NotificationOutbox,
    NotificationPolicy,
    NotificationPref,
    NotificationRead,
    NotificationRecipientRule,
)
from cya.notifications.rules import NotificationError

POLICY_ACTION = "configure_schedules_thresholds_notifications"


def _snapshot(**values):
    return {"schema_version": 1, "values": values}


def _execute(*, command_type, request, build, permission=None, **context):
    organization_id, actor_id = context["organization_id"], context["actor_id"]
    for value in (organization_id, actor_id):
        canonical_id(value)
    backend = DatabaseRoleBackend()

    def authorize(org):
        with locked_identities(organization_id, [actor_id], lock_organization=False) as (
            current_org,
            users,
        ):
            user = users[actor_id]
            roles = frozenset(
                Role.CREW if r == Role.EMT else r for r in load_state(backend, user).grants
            )
            actor = Actor(user=user, roles=tuple(sorted(r.value for r in roles)))
            active = user.active and current_org.active
            if not roles or not active:
                raise CommandDenied(actor)
            if permission is not None:
                try:
                    require_permission(
                        Principal(organization_id, actor_id, roles, active),
                        organization_id,
                        permission,
                    )
                except PermissionDenied:
                    raise CommandDenied(actor) from None
            return actor

    return run_command(
        organization_id=organization_id,
        operation_id=context["operation_id"],
        command_type=command_type,
        request_digest=digest(request),
        correlation_id=context["correlation_id"],
        request_attempt_id=context["request_attempt_id"],
        authorize=authorize,
        prepare=build,
    )


def set_policy(*, event_type, channel, mandatory, **context):
    """Administrator marks an event/channel pair mandatory or optional (NOT-1)."""
    if not isinstance(mandatory, bool):
        raise NotificationError(
            "mandatory must be true or false", code="MALFORMED_REQUEST", status=400
        )
    event, chan = rules.event_type(event_type), rules.channel(channel)
    org = context["organization_id"]

    def build(actor):
        row = (
            NotificationPolicy.objects.select_for_update()
            .filter(organization_id=org, event_type=event, channel=chan)
            .first()
        )
        prior = (
            None
            if row is None
            else _snapshot(event_type=event, channel=chan, mandatory=row.mandatory)
        )

        def apply(receipt, current):
            NotificationPolicy.objects.update_or_create(
                organization_id=org,
                event_type=event,
                channel=chan,
                defaults={"mandatory": mandatory},
            )

        return Plan(
            response_body={
                "code": "SAVED",
                "event_type": event,
                "channel": chan,
                "mandatory": mandatory,
            },
            apply=apply,
            target_type="ORGANIZATION",
            target_id=org,
            prior_snapshot=prior,
            new_snapshot=_snapshot(event_type=event, channel=chan, mandatory=mandatory),
        )

    return _execute(
        command_type="NOTIFICATION_POLICY_SET",
        request={"event": event, "channel": chan, "mandatory": mandatory},
        build=build,
        permission=POLICY_ACTION,
        **context,
    )


def set_recipient_rule(*, event_type, recipient, enabled, **context):
    """Administrator turns one recipient kind on or off for one event (NOT-1, NOT-3; issue 14)."""
    if not isinstance(enabled, bool):
        raise NotificationError(
            "enabled must be true or false", code="MALFORMED_REQUEST", status=400
        )
    event = recipients.known_event(event_type)
    kind = recipients.available_kind(event, recipient)
    org = context["organization_id"]

    def build(actor):
        before = sorted(recipients.effective_kinds(org, event))
        row = (
            NotificationRecipientRule.objects.select_for_update()
            .filter(organization_id=org, event_type=event, recipient=kind)
            .first()
        )
        prior = _snapshot(
            event_type=event,
            recipient=kind,
            override=None if row is None else row.enabled,
            recipients=before,
        )
        after = sorted((set(before) | {kind}) if enabled else (set(before) - {kind}))

        def apply(receipt, current):
            NotificationRecipientRule.objects.update_or_create(
                organization_id=org,
                event_type=event,
                recipient=kind,
                defaults={"enabled": enabled},
            )

        return Plan(
            response_body={
                "code": "SAVED",
                "event_type": event,
                "recipient": kind,
                "enabled": enabled,
                "recipients": after,
            },
            apply=apply,
            target_type="ORGANIZATION",
            target_id=org,
            prior_snapshot=prior,
            new_snapshot=_snapshot(
                event_type=event, recipient=kind, override=enabled, recipients=after
            ),
        )

    return _execute(
        command_type="NOTIFICATION_RECIPIENT_SET",
        request={"event": event, "recipient": kind, "enabled": enabled},
        build=build,
        permission=POLICY_ACTION,
        **context,
    )


def set_preference(*, event_type, channel, enabled, **context):
    """A user switches an optional event/channel on or off for themselves (NOT-1)."""
    if not isinstance(enabled, bool):
        raise NotificationError(
            "enabled must be true or false", code="MALFORMED_REQUEST", status=400
        )
    event, chan = rules.event_type(event_type), rules.channel(channel)
    org, actor_id = context["organization_id"], context["actor_id"]

    def build(actor):
        if (
            not enabled
            and NotificationPolicy.objects.filter(
                organization_id=org, event_type=event, channel=chan, mandatory=True
            ).exists()
        ):
            raise NotificationError(
                "this notification is mandatory", code="MANDATORY_EVENT", status=409
            )
        row = (
            NotificationPref.objects.select_for_update()
            .filter(user_id=actor_id, event_type=event, channel=chan)
            .first()
        )
        prior = (
            None if row is None else _snapshot(event_type=event, channel=chan, enabled=row.enabled)
        )

        def apply(receipt, current):
            NotificationPref.objects.update_or_create(
                user_id=actor_id,
                event_type=event,
                channel=chan,
                defaults={"organization_id": org, "enabled": enabled},
            )

        return Plan(
            response_body={
                "code": "SAVED",
                "event_type": event,
                "channel": chan,
                "enabled": enabled,
            },
            apply=apply,
            target_type="USER",
            target_id=actor_id,
            prior_snapshot=prior,
            new_snapshot=_snapshot(event_type=event, channel=chan, enabled=enabled),
        )

    return _execute(
        command_type="NOTIFICATION_PREF_SET",
        request={"event": event, "channel": chan, "enabled": enabled},
        build=build,
        **context,
    )


def mark_read(*, outbox_id, **context):
    """The recipient marks one in-app item read; repeating it is a harmless no-op."""
    canonical_id(outbox_id)
    org, actor_id = context["organization_id"], context["actor_id"]

    def build(actor):
        item = NotificationOutbox.objects.filter(
            organization_id=org, pk=outbox_id, channel="IN_APP", recipient_user_id=actor_id
        ).first()
        if item is None:
            raise NotificationError("notification does not exist", code="NOT_FOUND", status=404)
        already = NotificationRead.objects.filter(outbox=item).exists()

        def apply(receipt, current):
            if not already:
                NotificationRead.objects.create(
                    organization_id=org,
                    command=receipt,
                    outbox=item,
                    actor_user_id=actor_id,
                    recorded_at=receipt.recorded_at,
                )

        return Plan(
            response_body={"code": "ALREADY_READ" if already else "SAVED", "outbox_id": outbox_id},
            apply=apply,
            target_type="USER",
            target_id=actor_id,
            new_snapshot=_snapshot(outbox_id=outbox_id),
        )

    return _execute(
        command_type="NOTIFICATION_MARK_READ", request={"outbox": outbox_id}, build=build, **context
    )
