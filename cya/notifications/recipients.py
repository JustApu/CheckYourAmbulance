"""Who receives each notification event: documented defaults plus administrator overrides.

SPEC 13 NOT-1 and NOT-3, issue 14 (https://github.com/JustApu/CheckYourAmbulance/issues/14). The
default recipients are data, not code: default_recipients.json lists every event type, the
recipient kinds it can name ("available") and those on by default ("default"), as approved by the
owner on 2026-10-27 and 2026-11-01. An administrator turns any available kind on or off for one
event with a notification_recipient_rule row (commands.set_recipient_rule); without a row the
default applies. No event is mandatory here: mandatory delivery is notification_policy.

notify() is what a producing command calls from its Plan.apply. It resolves the effective kinds to
users and roles and hands them to enqueue, inside the command's transaction, so a failure to
resolve or write a notification rolls the command back (NOT-5: the outbox row is never the only
record, and a notification never changes operational data). Delivery happens after commit and its
failure never undoes the command. Named people who are no longer active users of the organization
are skipped rather than failing the command, and the person who caused the event is never told
about their own action.
"""

import json
from functools import cache
from pathlib import Path

from cya.accounts.models import User
from cya.authorization.policy import Role
from cya.notifications import rules
from cya.notifications.enqueue import enqueue
from cya.notifications.models import RECIPIENT_KINDS, NotificationRecipientRule

DEFAULTS_FILE = Path(__file__).with_name("default_recipients.json")
ROLE_KINDS = {"SUPERVISORS": Role.SUPERVISOR.value, "ADMINISTRATORS": Role.ADMINISTRATOR.value}
PERSON_KINDS = ("ASSIGNEE", "REPORTER", "REQUESTER", "PARTICIPANTS")


@cache
def defaults():
    """{event_type: (available kinds, default kinds)} from the documented file, validated."""
    data = json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("events"), dict):
        raise ValueError("default_recipients.json is malformed")
    found = {}
    for event, entry in data["events"].items():
        rules.event_type(event)
        available, default = tuple(entry["available"]), tuple(entry["default"])
        if (
            not set(available) <= set(RECIPIENT_KINDS)
            or not set(default) <= set(available)
            or len(set(available)) != len(available)
        ):
            raise ValueError(f"default_recipients.json: bad recipients for {event}")
        found[event] = (available, default)
    return found


def known_event(event_type):
    event = rules.event_type(event_type)
    if event not in defaults():
        raise rules.NotificationError("unknown notification event", code="NOT_FOUND", status=404)
    return event


def available_kind(event_type, recipient):
    if recipient not in defaults()[event_type][0]:
        raise rules.NotificationError(
            "recipient is not available for this event", code="MALFORMED_REQUEST", status=400
        )
    return recipient


def effective_kinds(organization_id, event_type):
    """The recipient kinds on for this event: the default, then this organization's overrides."""
    available, default = defaults()[event_type]
    kinds = set(default)
    rows = NotificationRecipientRule.objects.filter(
        organization_id=organization_id, event_type=event_type
    ).values_list("recipient", "enabled")
    for recipient, enabled in rows:
        if recipient in available:
            (kinds.add if enabled else kinds.discard)(recipient)
    return kinds


def configuration(organization_id):
    """Every event with its available kinds, the default and the effective recipients."""
    overrides = {}
    for event, recipient, enabled in NotificationRecipientRule.objects.filter(
        organization_id=organization_id
    ).values_list("event_type", "recipient", "enabled"):
        overrides.setdefault(event, {})[recipient] = enabled
    result = []
    for event, (available, default) in sorted(defaults().items()):
        own = {k: v for k, v in overrides.get(event, {}).items() if k in available}
        on = (set(default) | {k for k, v in own.items() if v}) - {
            k for k, v in own.items() if not v
        }
        result.append(
            {
                "event_type": event,
                "available": list(available),
                "default": list(default),
                "overrides": [{"recipient": k, "enabled": v} for k, v in sorted(own.items())],
                "recipients": [k for k in available if k in on],
            }
        )
    return result


def notify(
    receipt,
    *,
    event_type,
    source_event_type,
    source_event_id,
    deep_link,
    payload,
    actor_user_id=None,
    assignee_id=None,
    reporter_id=None,
    requester_id=None,
    participant_ids=(),
):
    """Enqueue event_type for its effective recipients inside the causing command's transaction.

    The person arguments say who those kinds mean for this event; a kind left unset names nobody.
    Returns the outbox rows written (none when every recipient is off, inactive or the actor).
    """
    event = known_event(event_type)
    kinds = effective_kinds(receipt.organization_id, event)
    people = {
        "ASSIGNEE": [assignee_id],
        "REPORTER": [reporter_id],
        "REQUESTER": [requester_id],
        "PARTICIPANTS": list(participant_ids),
    }
    named = {str(p) for kind in PERSON_KINDS if kind in kinds for p in people[kind] if p}
    active = {
        str(pk)
        for pk in User.objects.filter(
            organization_id=receipt.organization_id, pk__in=named, active=True
        ).values_list("pk", flat=True)
    }
    roles = sorted(ROLE_KINDS[k] for k in kinds if k in ROLE_KINDS)
    if not active and not roles:
        return []
    return enqueue(
        receipt,
        source_event_type=source_event_type,
        source_event_id=str(source_event_id),
        event_type=event,
        template_key=event,
        payload=payload,
        deep_link=deep_link,
        roles=roles,
        user_ids=sorted(active),
        exclude_user_ids={str(actor_user_id)} if actor_user_id else (),
    )
