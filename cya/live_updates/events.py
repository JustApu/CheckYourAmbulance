"""Stable event kinds shared by business publishers and the WebSocket consumer."""

EVENT_KINDS = frozenset(
    {
        "check.recorded",
        "check.corrected",
        "check.invalidated",
        "deficiency.changed",
        "odometer.recorded",
        "odometer.corrected",
        "odometer.invalidated",
        "session.changed",
        "status.changed",
        "obligations.changed",
        "status.request",
    }
)
