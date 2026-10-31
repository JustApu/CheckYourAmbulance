"""Write notification intent inside the causing command's transaction (SPEC 13, NOT-1, NOT-5).

A command's Plan.apply calls enqueue(receipt, ...). The outbox rows commit or roll back with the
event, so a notification is never the only record of an event and never mutates operational data.
Recipients named by role and by user are merged and de-duplicated; the unique key (source event,
channel, recipient) is the backstop. A channel is skipped for a recipient who switched it off for
this event type unless the administrator made that event/channel mandatory. Nothing here is
hard-coded: event types, templates and mandatory policy are data (OQ-20 stays open).
"""

import logging

from django.db import connection, transaction

from cya.accounts.models import User
from cya.authorization.persistence import current_role_codes
from cya.authorization.policy import Role
from cya.core.audit_repository import _uuid
from cya.notifications import hooks, rules
from cya.notifications.models import (
    CHANNELS,
    NotificationOutbox,
    NotificationPolicy,
    NotificationPref,
)

logger = logging.getLogger(__name__)
MAX_RECIPIENTS = 500


def resolve_recipients(organization_id, *, roles=(), user_ids=()):
    """Active organization users holding any named role or named directly, once each."""
    roles = {Role.CREW.value if r == Role.EMT.value else r for r in roles}
    if not roles <= {r.value for r in Role}:
        raise rules.NotificationError(
            "unknown recipient role", code="MALFORMED_REQUEST", status=400
        )
    for user_id in user_ids:
        _uuid(user_id)
    candidates = User.objects.filter(organization_id=organization_id, active=True)
    named = set(user_ids)
    found = {user.pk: user for user in candidates.filter(pk__in=named)} if named else {}
    if named - set(found):
        raise rules.NotificationError("recipient does not exist", code="NOT_FOUND", status=404)
    if roles:
        # Every active user is considered; the cap is enforced on the result, never on the scan.
        for user in candidates.order_by("id").iterator():
            if user.pk not in found and roles & set(current_role_codes(user)):
                found[user.pk] = user
                if len(found) > MAX_RECIPIENTS:
                    break
    if len(found) > MAX_RECIPIENTS:
        raise rules.NotificationError("too many recipients", code="MALFORMED_REQUEST", status=400)
    return list(found.values())


def _policy(organization_id, event_type):
    rows = NotificationPolicy.objects.filter(organization_id=organization_id, event_type=event_type)
    return {row.channel: row.mandatory for row in rows}


def _switched_off(organization_id, event_type, user_ids):
    rows = NotificationPref.objects.filter(
        organization_id=organization_id, event_type=event_type, user_id__in=user_ids, enabled=False
    )
    return {(row.user_id, row.channel) for row in rows}


def enqueue(
    receipt,
    *,
    source_event_type,
    source_event_id,
    event_type,
    template_key,
    payload,
    deep_link,
    roles=(),
    user_ids=(),
    channels=CHANNELS,
    exclude_user_ids=(),
):
    """Insert one outbox row per recipient and channel; returns the rows created.

    Must run inside the command's transaction (Plan.apply). exclude_user_ids lets a command skip
    the actor who caused the event. Replays never reach apply, so a replay enqueues nothing.
    """
    if not connection.in_atomic_block:
        raise RuntimeError("enqueue must run inside the causing command's transaction")
    if not isinstance(source_event_type, str) or not source_event_type.strip():
        raise rules.NotificationError(
            "source_event_type is invalid", code="MALFORMED_REQUEST", status=400
        )
    _uuid(source_event_id)
    event_type, template_key = rules.event_type(event_type), rules.template_key(template_key)
    payload, deep_link = rules.payload(payload), rules.deep_link(deep_link)
    channels = tuple(dict.fromkeys(rules.channel(c) for c in channels))
    organization_id = receipt.organization_id
    excluded = set(exclude_user_ids)
    recipients = [
        u
        for u in resolve_recipients(organization_id, roles=roles, user_ids=user_ids)
        if u.pk not in excluded
    ]
    policy = _policy(organization_id, event_type)
    off = _switched_off(organization_id, event_type, [u.pk for u in recipients])
    rows = [
        NotificationOutbox(
            organization_id=organization_id,
            command=receipt,
            recorded_at=receipt.recorded_at,
            source_event_type=source_event_type,
            source_event_id=source_event_id,
            event_type=event_type,
            channel=channel,
            recipient_user=user,
            template_key=template_key,
            payload=payload,
            deep_link=deep_link,
            mandatory=policy.get(channel, False),
        )
        for user in recipients
        for channel in channels
        if policy.get(channel, False) or (user.pk, channel) not in off
    ]
    if rows:
        NotificationOutbox.objects.bulk_create(rows)
        transaction.on_commit(lambda: hooks.wake(str(organization_id)), robust=True)
    return rows
