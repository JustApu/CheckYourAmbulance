"""Rebuild session state from immutable events; never use a session as compliance evidence."""

from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from cya.checks.models import CheckRecord
from cya.inspections.models import (
    INACTIVITY_HOURS,
    InspectionSession,
    SessionEvent,
    SessionProjection,
)
from cya.obligations.projections import get_vehicle_obligations_as_of

INACTIVITY = timedelta(hours=INACTIVITY_HOURS)


@dataclass(frozen=True)
class SessionState:
    """A session's state folded from its history (SPEC 6.3 SES-1, SES-2, SES-8, SES-9).

    last_activity_at is A, the last persisted activity: the latest OPEN or REOPEN (the events
    that carry an activity receipt; JOIN and END never do) or the session's latest committed
    check, whichever is later. resume_deadline_at is the effective deadline R* = max(R, A), where
    R is opened_at plus the captured resume limit. ends_at is the end instant
    E = min(A + 6 h, R*): an Open session is in progress only before it.
    """

    state: str
    last_activity_at: object
    last_activity_command_id: str
    last_event_id: str
    sequence: int
    resume_deadline_at: object = None
    ends_at: object = None

    @property
    def deadline_reached_first(self):
        """Whether the end instant is the resume deadline rather than the inactivity limit."""
        return self.last_activity_at + INACTIVITY > self.resume_deadline_at


def resume_deadline(session):
    """R: the session's open instant plus the resume limit captured when it opened (SES-8)."""
    return session.opened_at + timedelta(hours=session.resume_limit_hours)


def fold_session(events, latest_check=None, *, deadline):
    """Reject a broken or out-of-order stream rather than trusting the mutable cache.

    latest_check is the session's most recent committed check as (recorded_at, command_id), or
    None. A check is persisted activity (SPEC 6.3 SES-2), so it can only move the clock later.
    deadline is the session's resume deadline R (resume_deadline).
    """
    state = None
    activity_at = None
    activity_command_id = None
    prior = None
    for index, event in enumerate(events, start=1):
        if event.sequence != index or event.prior_event_id != prior:
            raise ValueError("session event chain is invalid")
        if index == 1:
            if (
                event.event_type != "OPEN"
                or event.persisted_activity_command_id != event.command_id
            ):
                raise ValueError("session must start with an attributed OPEN")
            state = "OPEN"
        elif event.event_type == "JOIN":
            if state != "OPEN" or event.persisted_activity_command_id is not None:
                raise ValueError("JOIN requires OPEN and is not activity")
        elif event.event_type == "REOPEN":
            if state == "OPEN" or event.persisted_activity_command_id != event.command_id:
                raise ValueError("REOPEN requires an ended session and activity receipt")
            state = "OPEN"
        elif event.event_type in (
            "END_INCOMPLETE",
            "END_CLOSED",
            "AUTO_INCOMPLETE",
            "DEADLINE_INCOMPLETE",
        ):
            if state != "OPEN" or event.persisted_activity_command_id is not None:
                raise ValueError("END requires OPEN and is not activity")
            state = "CLOSED" if event.event_type == "END_CLOSED" else "INCOMPLETE"
        else:
            raise ValueError("unknown session event")
        if event.persisted_activity_command_id is not None:
            activity_at = event.effective_at
            activity_command_id = event.persisted_activity_command_id
        prior = event.pk
    if prior is None:
        raise ValueError("session has no OPEN event")
    if latest_check is not None and latest_check[0] > activity_at:
        activity_at, activity_command_id = latest_check
    # SES-9: activity recorded after R under the rules before 6.0.0 stays valid history, so the
    # effective deadline is never before it. A 6.0.0 session accepts nothing at or after R*.
    effective_deadline = max(deadline, activity_at)
    ends_at = min(activity_at + INACTIVITY, effective_deadline)
    return SessionState(
        state, activity_at, activity_command_id, prior, index, effective_deadline, ends_at
    )


def is_expired(state, now):
    """An Open session at or past its end instant (SES-9) has ended; only the job's event is
    pending. Reads, refusals and the job all use this one instant."""
    return state.state == "OPEN" and now >= state.ends_at


def deadline_passed(state, now):
    """At or past R* nothing more is accepted into the session: no reopen, join or check."""
    return now >= state.resume_deadline_at


def current_state(session):
    """Read and validate one consistent event-history result set."""
    events = SessionEvent.objects.filter(session=session).order_by("sequence")
    latest = (
        CheckRecord.objects.filter(session=session)
        .order_by("-recorded_at", "-id")
        .values_list("recorded_at", "command_id")
        .first()
    )
    return fold_session(events, latest, deadline=resume_deadline(session))


def rebuild_session_projection(session):
    """Caller owns a transaction and holds the vehicle row lock."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("session projection rebuild requires a transaction")
    state = current_state(session)
    SessionProjection.objects.update_or_create(
        session=session,
        defaults={
            "organization_id": session.organization_id,
            "state": state.state,
            "last_activity_at": state.last_activity_at,
            "last_activity_command_id": state.last_activity_command_id,
            "last_applied_event_id": state.last_event_id,
            "ends_at": state.ends_at,
        },
    )
    return state


def read_session(
    organization_id, session_id, *, as_of=None, check_lookup=None, suspension_lookup=None
):
    """Return a fresh session view and current item obligations for its vehicle.

    The caller must authorize this read. Reopen never substitutes a newer checklist into the
    original session; current obligations are recomputed separately at the requested instant.
    """
    session = InspectionSession.objects.get(organization_id=organization_id, pk=session_id)
    state = current_state(session)
    instant = timezone.now() if as_of is None else as_of
    obligations = get_vehicle_obligations_as_of(
        str(session.vehicle_id),
        instant,
        organization_id=str(organization_id),
        check_lookup=check_lookup,
        suspension_lookup=suspension_lookup,
    )
    return session, state, obligations
