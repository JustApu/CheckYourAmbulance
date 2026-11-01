"""Offline email sender for notification tests: records messages, scripts failures, no network."""

from cya.notifications.senders import SenderError


class StubSender:
    """Implements the sender protocol. `script` is consumed one entry per send call: None accepts
    the message, an exception instance is raised (after nothing is recorded)."""

    def __init__(self, script=()):
        self.sent = []
        self.script = list(script)
        self.calls = 0

    def send(self, message):
        self.calls += 1
        step = self.script.pop(0) if self.script else None
        if step is not None:
            raise step
        self.sent.append(message)


def transient():
    return SenderError("TRANSIENT")


def notified(event_type, source_event_id=None):
    """Sorted recipient ids of the in-app notifications of one event type (issue 14 tests)."""
    from cya.notifications.models import NotificationOutbox

    rows = NotificationOutbox.objects.filter(event_type=event_type, channel="IN_APP")
    if source_event_id is not None:
        rows = rows.filter(source_event_id=str(source_event_id))
    return sorted(rows.values_list("recipient_user_id", flat=True))
