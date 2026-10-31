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
