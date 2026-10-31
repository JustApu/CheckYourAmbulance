"""The in-app inbox and the administrator's failure view: bounded reads, scoped server-side.

Every read filters on the caller's own user and organization; a caller never names another user.
Read marks live in the append-only notification_read table, so "unread" is the absence of a mark.
"""

from django.db.models import Exists, OuterRef

from cya.notifications.models import (
    NotificationOutbox,
    NotificationPolicy,
    NotificationPref,
    NotificationRead,
)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _own(user):
    return NotificationOutbox.objects.filter(
        organization_id=user.organization_id, recipient_user_id=user.pk, channel="IN_APP"
    )


def inbox(user, *, limit=DEFAULT_LIMIT, unread_only=False):
    """The user's newest in-app items, newest first, with a read flag and a deep link."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer from 1 to {MAX_LIMIT}")
    rows = _own(user).annotate(
        is_read=Exists(NotificationRead.objects.filter(outbox=OuterRef("pk")))
    )
    if unread_only:
        rows = rows.filter(is_read=False)
    found = list(rows.order_by("-recorded_at", "-id")[: limit + 1])
    return {
        "items": [
            {
                "id": str(row.pk),
                "event_type": row.event_type,
                "template_key": row.template_key,
                "payload": row.payload,
                "deep_link": row.deep_link,
                "mandatory": row.mandatory,
                "recorded_at": row.recorded_at,
                "read": row.is_read,
            }
            for row in found[:limit]
        ],
        "truncated": len(found) > limit,
    }


def unread_count(user):
    read = NotificationRead.objects.filter(outbox=OuterRef("pk"))
    return _own(user).filter(~Exists(read)).count()


def preferences(user):
    """The caller's own optional switches; nothing about anyone else's."""
    rows = NotificationPref.objects.filter(
        organization_id=user.organization_id, user_id=user.pk
    ).order_by("event_type", "channel")[: MAX_LIMIT + 1]
    return [
        {"event_type": row.event_type, "channel": row.channel, "enabled": row.enabled}
        for row in rows[:MAX_LIMIT]
    ]


def policies(organization_id):
    """Mandatory event/channel policy for the organization (administrator view)."""
    rows = NotificationPolicy.objects.filter(organization_id=organization_id).order_by(
        "event_type", "channel"
    )[:MAX_LIMIT]
    return [
        {"event_type": row.event_type, "channel": row.channel, "mandatory": row.mandatory}
        for row in rows
    ]
