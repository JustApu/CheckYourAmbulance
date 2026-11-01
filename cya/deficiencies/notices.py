"""Notifications caused by deficiency events (SPEC 13 NOT-3, 8.7, AC-17; issue 14).

Every writer of a deficiency event that SPEC names as a notification calls notify_event(receipt,
event) from its Plan.apply, after the event and the projection are written. The notification event
and its recipients come from cya.notifications.recipients (documented defaults plus administrator
overrides); this module only says who "assignee", "reporter" and "requester" are for the event:

- assignee: the deficiency's assignee after the event (for ASSIGNED, the new assignee);
- reporter: the user who reported the deficiency;
- requester: for EXTENSION_DECIDED, the author of the request it decides.

The intent commits or rolls back with the event. A deficiency event type not listed here notifies
nobody. Overdue deadlines have no event; cya.deficiencies.overdue evaluates them on a schedule.
"""

from cya.deficiencies.models import Deficiency, DeficiencyEvent, DeficiencyProjection
from cya.notifications.recipients import notify

# deficiency_event.event_type -> notification event type (default_recipients.json)
EVENT_NOTICES = {
    "ASSIGNED": "deficiency.assigned",
    "VERIFICATION_ADDED": "deficiency.reopened_verification_added",
    "VERIFICATION_REOPENED": "deficiency.reopened_verification_invalidated",
    "ADMINISTRATOR_REOPENED": "deficiency.reopened_by_administrator",
    "REOPENED": "deficiency.reopened_reported_in_error",
    "CORRECTION_NOTICE": "deficiency.correction_notice",
    "EXTENSION_REQUESTED": "deficiency.extension_requested",
    "EXTENSION_DECIDED": "deficiency.extension_decided",
    "ADMINISTRATOR_CLOSED": "deficiency.administrator_closed",
}
SOURCE = "deficiency_event"


def deficiency_link(deficiency_id):
    return f"/deficiencies/{deficiency_id}/"


def people(organization_id, deficiency_id):
    """(assignee id or None, reporter id, vehicle id) as stored now, under the deficiency lock."""
    deficiency = Deficiency.objects.only("reporter_id", "vehicle_id").get(
        organization_id=organization_id, pk=deficiency_id
    )
    assignee = (
        DeficiencyProjection.objects.filter(
            organization_id=organization_id, deficiency_id=deficiency_id
        )
        .values_list("assignee_id", flat=True)
        .first()
    )
    return assignee, deficiency.reporter_id, deficiency.vehicle_id


def notify_event(receipt, event):
    """Enqueue the notification an event of this type causes, if any; returns the rows."""
    notification = EVENT_NOTICES.get(event.event_type)
    if notification is None:
        return []
    organization_id = str(event.organization_id)
    deficiency_id = str(event.deficiency_id)
    assignee, reporter, vehicle = people(organization_id, deficiency_id)
    requester = None
    if event.event_type == "EXTENSION_DECIDED":
        requester = (
            DeficiencyEvent.objects.filter(
                organization_id=organization_id, pk=event.request_event_id
            )
            .values_list("actor_user_id", flat=True)
            .first()
        )
    payload = {
        "deficiency_id": deficiency_id,
        "vehicle_id": str(vehicle),
        "event": event.event_type,
        "status": event.to_status,
        "decision": event.decision,
    }
    payload = {key: value for key, value in payload.items() if value}
    return notify(
        receipt,
        event_type=notification,
        source_event_type=SOURCE,
        source_event_id=event.pk,
        deep_link=deficiency_link(deficiency_id),
        payload=payload,
        actor_user_id=receipt.actor_user_id,
        assignee_id=assignee,
        reporter_id=reporter,
        requester_id=requester,
    )
