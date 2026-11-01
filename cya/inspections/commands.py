"""Audited session commands (SPEC §6.1–§6.2; issue 78 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/78) Part A).

These are server boundaries for the future UI. They accept IDs and intent, never a role or
client timestamp. The receipt and audit commit with the event and disposable projection.
Vehicle row locking serializes competing session writers. Part B uses the same lock for the
persisted-activity clock: a check recorded in a session and the auto-close of that session take
the vehicle lock and then read the session's activity, so whichever runs second sees the other
(SPEC 6.3 SES-1, SES-2, SES-6).
"""

import json
import logging

from django.db.models import OuterRef, Subquery
from django.utils import timezone

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id, load_state
from cya.authorization.guard import PermissionDenied, Principal, require_permission
from cya.authorization.locking import locked_identities
from cya.authorization.persistence import DatabaseRoleBackend, digest
from cya.authorization.policy import Role
from cya.checklists.models import ChecklistTemplate, Vehicle
from cya.checklists.overlay import ResolutionError, resolve_checklist
from cya.core.audit_commands import Actor, CommandDenied, Plan, run_command
from cya.inspections.models import (
    InspectionSession,
    SessionEvent,
    SessionParticipant,
    SessionProjection,
)
from cya.inspections.projections import (
    INACTIVITY,
    current_state,
    is_expired,
    rebuild_session_projection,
)
from cya.notifications.recipients import notify
from tools.command_identity import worker_operation_id

logger = logging.getLogger(__name__)

SERVICE_IDENTITY = "worker.session_auto_close"
AUTO_CLOSE_COMMAND = "SESSION_AUTO_CLOSE"


class SessionError(ValueError):
    """Invalid session intent or state; run_command audits a rolled-back failure."""


def _execute(
    *,
    organization_id,
    actor_id,
    operation_id,
    correlation_id,
    request_attempt_id,
    command_type,
    authorization_action,
    request,
    build,
):
    canonical_id(organization_id)
    canonical_id(actor_id)
    backend = DatabaseRoleBackend()

    def authorize(org):
        with locked_identities(organization_id, [actor_id], lock_organization=False) as (
            current_org,
            users,
        ):
            user = users[actor_id]
            roles = frozenset(
                Role.CREW if role == Role.EMT else role for role in load_state(backend, user).grants
            )
            actor = Actor(user=user, roles=tuple(sorted(role.value for role in roles)))
            principal = Principal(
                organization_id, actor_id, roles, user.active and current_org.active
            )
            try:
                require_permission(principal, organization_id, authorization_action)
            except PermissionDenied:
                raise CommandDenied(actor) from None
            return actor

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type=command_type,
        request_digest=digest(request),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=authorize,
        prepare=build,
    )


def _vehicle(org, vehicle_id):
    vehicle = (
        Vehicle.objects.select_for_update()
        .filter(organization_id=org, pk=vehicle_id, active=True)
        .first()
    )
    if vehicle is None:
        raise SessionError("active vehicle is unavailable")
    return vehicle


def _session(org, session_id, actor):
    session = InspectionSession.objects.filter(organization_id=org, pk=session_id).first()
    if session is None:
        # Match the response for a session the caller cannot access; do not reveal existence.
        raise CommandDenied(actor)
    return session


def _evidence(receipt, actor):
    return {
        "command": receipt,
        "recorded_at": receipt.recorded_at,
        "actor_user": actor.user,
        "actor_roles_at_time": list(actor.roles),
    }


def _participant(session, receipt, actor):
    if not SessionParticipant.objects.filter(session=session, user=actor.user).exists():
        SessionParticipant.objects.create(
            organization_id=session.organization_id,
            session=session,
            user=actor.user,
            **_evidence(receipt, actor),
        )


def _event(session, receipt, actor, kind, prior, *, effective_at=None):
    event = SessionEvent.objects.create(
        organization_id=session.organization_id,
        session=session,
        sequence=1 if prior is None else prior.sequence + 1,
        prior_event=prior,
        event_type=kind,
        effective_at=receipt.recorded_at if effective_at is None else effective_at,
        persisted_activity_command=receipt if kind in ("OPEN", "REOPEN") else None,
        **_evidence(receipt, actor),
    )
    rebuild_session_projection(session)
    return event


def _plan(org, body, apply, *, previous=None, current=None):
    return Plan(
        response_body=body,
        apply=apply,
        target_type="ORGANIZATION",
        target_id=org,
        prior_snapshot={"schema_version": 1, "values": previous} if previous else None,
        new_snapshot={"schema_version": 1, "values": current},
    )


def open_or_join_session(*, vehicle_id, template_id, separate=False, **context):
    """Join the latest open session on a vehicle, or open a separately pinned one.

    A repeated join by an existing participant is an audited no-op. JOIN never advances the
    activity clock. An explicit separate=True always starts another session on this vehicle.
    """
    canonical_id(vehicle_id)
    canonical_id(template_id)
    if type(separate) is not bool:
        raise SessionError("separate must be a boolean")
    org = context["organization_id"]

    def build(actor):
        _vehicle(org, vehicle_id)
        template = (
            ChecklistTemplate.objects.select_for_update()
            .filter(organization_id=org, pk=template_id, active=True)
            .first()
        )
        if template is None:
            raise SessionError("active checklist template is unavailable")
        if not separate:
            # History is authoritative even when a disposable projection is missing or stale.
            latest_event = (
                SessionEvent.objects.filter(session_id=OuterRef("pk"))
                .order_by("-sequence")
                .values("event_type")[:1]
            )
            candidates = (
                InspectionSession.objects.filter(organization_id=org, vehicle_id=vehicle_id)
                .annotate(latest_event_type=Subquery(latest_event))
                .filter(latest_event_type__in=("OPEN", "JOIN", "REOPEN"))
                .order_by("-opened_at", "-id")
            )
            moment = timezone.now()
            for candidate in candidates:
                state = current_state(candidate)
                # An idle session has already closed (SES-1); only the job's event is pending.
                if state.state == "OPEN" and not is_expired(state, moment):
                    already = SessionParticipant.objects.filter(
                        session=candidate, user=actor.user
                    ).exists()
                    body = {
                        "code": "ALREADY_PARTICIPANT" if already else "SESSION_JOINED",
                        "session_id": str(candidate.pk),
                        "checklist_version_id": str(candidate.checklist_version_id),
                        "state": "OPEN",
                    }

                    def apply(
                        receipt, current, *, candidate=candidate, already=already, state=state
                    ):
                        if not already:
                            _participant(candidate, receipt, current)
                            prior = SessionEvent.objects.get(pk=state.last_event_id)
                            _event(candidate, receipt, current, "JOIN", prior)

                    return _plan(
                        org,
                        body,
                        apply,
                        current={"session_id": str(candidate.pk), "state": "OPEN"},
                    )
        opened_at = timezone.now()
        try:
            resolved = resolve_checklist(
                organization_id=org,
                vehicle_id=vehicle_id,
                template_id=template_id,
                as_of=opened_at,
            )
        except ResolutionError as error:
            raise SessionError("published checklist is unavailable for this vehicle") from error
        session_id = new_id()

        def apply(receipt, current):
            session = InspectionSession.objects.create(
                id=session_id,
                organization_id=org,
                vehicle_id=vehicle_id,
                checklist_version_id=resolved.template_version_id,
                opened_at=opened_at,
                **_evidence(receipt, current),
            )
            _participant(session, receipt, current)
            _event(session, receipt, current, "OPEN", None, effective_at=opened_at)

        return _plan(
            org,
            {
                "code": "SESSION_OPENED",
                "session_id": session_id,
                "checklist_version_id": str(resolved.template_version_id),
                "state": "OPEN",
            },
            apply,
            current={
                "session_id": session_id,
                "state": "OPEN",
                "checklist_version_id": str(resolved.template_version_id),
            },
        )

    return _execute(
        command_type="SESSION_OPEN_OR_JOIN",
        authorization_action="open_join_inspection_session",
        request={"vehicle_id": vehicle_id, "template_id": template_id, "separate": separate},
        build=build,
        **context,
    )


def reopen_session(*, session_id, **context):
    """A current participant may resume a Closed or Incomplete session without approval."""
    canonical_id(session_id)
    org = context["organization_id"]
    _settle_idle(org, session_id)

    def build(actor):
        session = _session(org, session_id, actor)
        _vehicle(org, str(session.vehicle_id))
        if not SessionParticipant.objects.filter(session=session, user=actor.user).exists():
            raise CommandDenied(actor)
        state = current_state(session)
        if state.state == "OPEN":
            if is_expired(state, timezone.now()):
                raise SessionError("session has gone idle and is closing; try again")
            raise SessionError("session is already open")
        prior = SessionEvent.objects.get(pk=state.last_event_id)

        def apply(receipt, current):
            event = _event(session, receipt, current, "REOPEN", prior)
            # The other recorded participants learn that someone reopened it (NOT-3, issue 14).
            notify(
                receipt,
                event_type="session.reopened",
                source_event_type="session_event",
                source_event_id=event.pk,
                deep_link=f"/inspection/sessions/{session_id}/",
                payload={"session_id": session_id, "vehicle_id": str(session.vehicle_id)},
                actor_user_id=receipt.actor_user_id,
                participant_ids=set(
                    SessionParticipant.objects.filter(session=session).values_list(
                        "user_id", flat=True
                    )
                ),
            )

        return _plan(
            org,
            {
                "code": "SESSION_REOPENED",
                "session_id": session_id,
                "checklist_version_id": str(session.checklist_version_id),
                "state": "OPEN",
                "refresh_requirements": True,
            },
            apply,
            previous={"session_id": session_id, "state": state.state},
            current={"session_id": session_id, "state": "OPEN"},
        )

    return _execute(
        command_type="SESSION_REOPEN",
        authorization_action="rejoin_inspection_session",
        request={"session_id": session_id},
        build=build,
        **context,
    )


def end_session(*, session_id, state="CLOSED", **context):
    """A participant explicitly ends a session; its state remains informational only."""
    canonical_id(session_id)
    if state not in ("CLOSED", "INCOMPLETE"):
        raise SessionError("end state must be CLOSED or INCOMPLETE")
    org = context["organization_id"]
    _settle_idle(org, session_id)

    def build(actor):
        session = _session(org, session_id, actor)
        _vehicle(org, str(session.vehicle_id))
        if not SessionParticipant.objects.filter(session=session, user=actor.user).exists():
            raise CommandDenied(actor)
        previous = current_state(session)
        if previous.state != "OPEN" or is_expired(previous, timezone.now()):
            raise SessionError("only an open session may be ended")
        prior = SessionEvent.objects.get(pk=previous.last_event_id)

        def apply(receipt, current):
            _event(session, receipt, current, "END_" + state, prior)

        return _plan(
            org,
            {"code": "SESSION_ENDED", "session_id": session_id, "state": state},
            apply,
            previous={"session_id": session_id, "state": "OPEN"},
            current={"session_id": session_id, "state": state},
        )

    return _execute(
        command_type="SESSION_END",
        authorization_action="end_inspection_session",
        request={"session_id": session_id, "state": state},
        build=build,
        **context,
    )


def _lock_vehicle(org, vehicle_id):
    """Lock the vehicle row whatever its status: a deactivated vehicle's sessions still close."""
    vehicle = Vehicle.objects.select_for_update().filter(organization_id=org, pk=vehicle_id).first()
    if vehicle is None:
        raise SessionError("vehicle is unavailable")
    return vehicle


def auto_close_session(*, organization_id, session_id):
    """Close one session that has been idle for six hours (SPEC 6.3 SES-1, SES-6).

    The close is stamped at the last persisted activity plus six hours, not at the instant the
    job ran, so a late job leaves a correct record. Under the vehicle lock the activity is read
    again from the event history and from the session's committed checks, so work that arrived
    after the job chose this session either keeps it open (the job refuses) or, if the close
    committed first, is rejected by the check command. Refusals roll back and leave no receipt,
    so the same job may be offered again. An applied close is idempotent on the session's latest
    event: replaying it returns the original result; after a reopen the key is new.
    """
    canonical_id(organization_id)
    canonical_id(session_id)
    session = InspectionSession.objects.filter(
        organization_id=organization_id, pk=session_id
    ).first()
    if session is None:
        raise SessionError("session is unavailable")
    latest = (
        SessionEvent.objects.filter(session=session)
        .order_by("-sequence")
        .values_list("id", flat=True)
        .first()
    )
    job_key = json.dumps([str(session.pk), str(latest)], separators=(",", ":"))

    def authorize(org):
        if not org.active:
            raise CommandDenied()
        return Actor(service_identity=SERVICE_IDENTITY, job_key=job_key)

    def build(actor):
        _lock_vehicle(organization_id, str(session.vehicle_id))
        state = current_state(session)
        if str(state.last_event_id) != str(latest):
            raise SessionError("session changed since the job chose it")
        if state.state != "OPEN":
            raise SessionError("session is not open")
        closed_at = state.last_activity_at + INACTIVITY
        if timezone.now() < closed_at:
            raise SessionError("session has not been idle for the inactivity limit")
        prior = SessionEvent.objects.get(pk=state.last_event_id)

        def apply(receipt, current):
            _event(session, receipt, current, "AUTO_INCOMPLETE", prior, effective_at=closed_at)

        return _plan(
            organization_id,
            {
                "code": "SESSION_AUTO_CLOSED",
                "session_id": str(session.pk),
                "state": "INCOMPLETE",
                "closed_at": closed_at.isoformat(),
            },
            apply,
            previous={"session_id": str(session.pk), "state": "OPEN"},
            current={"session_id": str(session.pk), "state": "INCOMPLETE"},
        )

    return run_command(
        organization_id=organization_id,
        operation_id=str(
            worker_operation_id(organization_id, SERVICE_IDENTITY, AUTO_CLOSE_COMMAND, job_key)
        ),
        command_type=AUTO_CLOSE_COMMAND,
        request_digest=digest({"session_id": str(session.pk), "after_event": str(latest)}),
        correlation_id=new_id(),
        request_attempt_id=new_id(),
        authorize=authorize,
        prepare=build,
    )


def _settle_idle(org, session_id):
    """Run the close the job would, for a session a caller names, so it never waits for the job.

    A refusal means another closer or new activity got there first, which is fine: the caller's
    own command re-reads the state under the lock and decides.
    """
    session = InspectionSession.objects.filter(organization_id=org, pk=session_id).first()
    if session is None:
        return
    if is_expired(current_state(session), timezone.now()):
        try:
            auto_close_session(organization_id=org, session_id=session_id)
        except SessionError:
            pass


def auto_close_inactive_sessions(*, limit=200):
    """Close every session idle for six hours, oldest first, at most `limit` per run.

    Candidates come from the disposable projection; every close re-checks the authoritative
    history, so a stale projection can only delay a close, never cause a wrong one. Returns
    counts of closed, skipped (no longer due) and failed sessions. Safe to run repeatedly.
    """
    if type(limit) is not int or not 1 <= limit <= 10_000:
        raise ValueError("limit must be an integer from 1 to 10000")
    cutoff = timezone.now() - INACTIVITY
    due = list(
        SessionProjection.objects.filter(state="OPEN", last_activity_at__lte=cutoff)
        .order_by("last_activity_at", "id")
        .values_list("organization_id", "session_id")[:limit]
    )
    counts = {"closed": 0, "skipped": 0, "failed": 0}
    for org, session_id in due:
        try:
            auto_close_session(organization_id=str(org), session_id=str(session_id))
            counts["closed"] += 1
        except SessionError:
            counts["skipped"] += 1
        except Exception as error:  # keep closing the rest; a later run retries this one
            logger.error("session auto-close failed (%s)", type(error).__name__)
            counts["failed"] += 1
    return counts
