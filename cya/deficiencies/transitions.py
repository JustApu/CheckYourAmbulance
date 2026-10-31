"""The closed SPEC 8.2 deficiency status-transition table (issue 80 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/80)).

One validator for every lifecycle handler: a transition is allowed only when the table lists it,
whatever the caller's role. issue 80 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/80) exercises only creation (a deficiency begins Open); the
M4 lifecycle handlers reuse the same table and add their own event types, so an unlisted
transition is rejected here in one place rather than re-derived per handler.
"""

OPEN = "OPEN"
IN_PROGRESS = "IN_PROGRESS"
AWAITING_EXTERNAL_DEPENDENCY = "AWAITING_EXTERNAL_DEPENDENCY"
PENDING_VERIFICATION = "PENDING_VERIFICATION"
CLOSED_VERIFIED = "CLOSED_VERIFIED"
CLOSED_ADMINISTRATOR = "CLOSED_ADMINISTRATOR"
CLOSED_UNVERIFIABLE = "CLOSED_UNVERIFIABLE"
REPORTED_IN_ERROR = "REPORTED_IN_ERROR"

STATUSES = (
    OPEN,
    IN_PROGRESS,
    AWAITING_EXTERNAL_DEPENDENCY,
    PENDING_VERIFICATION,
    CLOSED_VERIFIED,
    CLOSED_ADMINISTRATOR,
    CLOSED_UNVERIFIABLE,
    REPORTED_IN_ERROR,
)
# Statuses that still concern the crew; a closed or cancelled deficiency takes no new evidence.
ACTIVE_STATUSES = (OPEN, IN_PROGRESS, AWAITING_EXTERNAL_DEPENDENCY, PENDING_VERIFICATION)
# The empty-scope closure exception applies before a correction has materialized its own scope.
EMPTY_SCOPE_CLOSURE_STATUSES = (OPEN, IN_PROGRESS, AWAITING_EXTERNAL_DEPENDENCY)

# (from_status, to_status): the arrows of SPEC 8.2. "Reopened" is an event whose status is Open.
ALLOWED_TRANSITIONS = frozenset(
    {
        (OPEN, IN_PROGRESS),
        (IN_PROGRESS, PENDING_VERIFICATION),
        (OPEN, AWAITING_EXTERNAL_DEPENDENCY),
        (AWAITING_EXTERNAL_DEPENDENCY, OPEN),
        (IN_PROGRESS, AWAITING_EXTERNAL_DEPENDENCY),
        (AWAITING_EXTERNAL_DEPENDENCY, IN_PROGRESS),
        (PENDING_VERIFICATION, CLOSED_VERIFIED),
        # A Failed verification attempt returns the deficiency to work (VER-5, SPEC 1.15.0).
        (PENDING_VERIFICATION, IN_PROGRESS),
        *((status, CLOSED_ADMINISTRATOR) for status in ACTIVE_STATUSES),
        *((status, CLOSED_UNVERIFIABLE) for status in EMPTY_SCOPE_CLOSURE_STATUSES),
        (OPEN, REPORTED_IN_ERROR),
        (IN_PROGRESS, REPORTED_IN_ERROR),
        (PENDING_VERIFICATION, REPORTED_IN_ERROR),
        (CLOSED_VERIFIED, OPEN),
        (CLOSED_ADMINISTRATOR, OPEN),
        (CLOSED_UNVERIFIABLE, OPEN),
        (REPORTED_IN_ERROR, OPEN),
    }
)


class TransitionRejected(ValueError):
    """The status change is not in the SPEC 8.2 table."""


def validate_transition(from_status, to_status):
    """Raise unless from_status -> to_status is listed; None as from_status means creation."""
    if to_status not in STATUSES:
        raise TransitionRejected(f"unknown status {to_status!r}")
    if from_status is None:
        if to_status != OPEN:
            raise TransitionRejected("a deficiency is created Open")
        return
    if (from_status, to_status) not in ALLOWED_TRANSITIONS:
        raise TransitionRejected(f"{from_status} to {to_status} is not an allowed transition")
