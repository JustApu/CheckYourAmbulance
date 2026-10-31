"""Command handlers for ingesting roster snapshots, recording session corrections, and publishing rules."""

import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from cya.accounts.fields import new_id
from cya.authorization.backend import load_state
from cya.authorization.locking import locked_identities
from cya.authorization.persistence import DatabaseRoleBackend
from cya.authorization.policy import Role
from cya.conditions.evaluator import (
    RosterStatus,
    _find_qualifying_current_obligation_check,
    derive_effective_roster,
    evaluate_carry_over_rule,
    latest_obligation_determination,
)
from cya.conditions.models import (
    ConditionalRule,
    ObligationDetermination,
    RosterMember,
    RosterSnapshot,
    SessionRosterCorrection,
)
from cya.core.audit_commands import Actor, CommandDenied, Plan, run_command
from cya.core.models import AuditEvent
from cya.integrations import RosterShift
from tools.command_identity import worker_operation_id

logger = logging.getLogger(__name__)

SUPERVISOR_NOTIFICATION_SINKS: list = []


def register_conflict_notification_sink(sink_callable):
    """Register a callback for supervisor conflict notifications (Finding 6)."""
    SUPERVISOR_NOTIFICATION_SINKS.append(sink_callable)


def clear_conflict_notification_sinks():
    """Clear registered notification sinks (for test isolation)."""
    SUPERVISOR_NOTIFICATION_SINKS.clear()


def make_audit_snapshot(values: dict) -> dict:
    return {"schema_version": 1, "values": values}


def _deliver_conflict_notification_to_sinks(
    *,
    organization,
    intent: dict,
    receipt=None,
    actor_user=None,
    actor_roles: list[str] | None = None,
    correlation_id: str | None = None,
) -> dict:
    """Deliver a durable conflict notification intent to registered sinks and record outcome."""
    now = timezone.now()
    corr_id = correlation_id or intent.get("correlation_id") or new_id()
    target_id = intent.get("receipt_id") or (str(receipt.id) if receipt else new_id())

    if not SUPERVISOR_NOTIFICATION_SINKS:
        intent["delivery_status"] = "PENDING"
        return intent

    errors: list[str] = []
    for sink in list(SUPERVISOR_NOTIFICATION_SINKS):
        try:
            sink(intent)
        except Exception as exc:
            logger.exception(
                "Supervisor conflict notification sink failed for notification %s",
                intent.get("notification_id"),
            )
            errors.append(f"{type(exc).__name__}: {exc}")

    if errors:
        intent["delivery_status"] = "FAILED"
        intent["delivery_errors"] = errors
        AuditEvent.objects.create(
            organization=organization,
            command=receipt,
            request_attempt_id=new_id(),
            actor_user=actor_user,
            roles_at_time=list(actor_roles or []),
            action="ROSTER_CONFLICT_NOTIFICATION_FAILED",
            target_type="COMMAND_RECEIPT",
            target_id=target_id,
            recorded_at=now,
            correlation_id=corr_id,
            outcome="FAILED",
            reason_code="NOTIFICATION_SINK_ERROR",
            new_snapshot=make_audit_snapshot(dict(intent)),
        )
    else:
        intent["delivery_status"] = "DELIVERED"
        AuditEvent.objects.create(
            organization=organization,
            command=receipt,
            request_attempt_id=new_id(),
            actor_user=actor_user,
            roles_at_time=list(actor_roles or []),
            action="ROSTER_CONFLICT_NOTIFICATION_DELIVERED",
            target_type="COMMAND_RECEIPT",
            target_id=target_id,
            recorded_at=now,
            correlation_id=corr_id,
            outcome="SUCCEEDED",
            reason_code="CONCURRENT_SESSION_DISAGREEMENT",
            new_snapshot=make_audit_snapshot(dict(intent)),
        )
    return intent


def notify_supervisor_roster_conflict(
    *,
    organization,
    vehicle_id: str,
    conflicting_correction_ids: list[str],
    conflicting_sessions: list[str],
    detected_at: datetime,
    receipt=None,
    actor_user=None,
    actor_roles: list[str] | None = None,
    correlation_id: str | None = None,
    covers_from: datetime | None = None,
    covers_until: datetime | None = None,
    session_id: str | None = None,
) -> dict:
    """Persist durable supervisor conflict notification intent and schedule delivery (SPEC 15.3, Finding 4)."""
    notification_id = new_id()
    corr_id = correlation_id or new_id()
    target_id = str(receipt.id) if receipt else notification_id

    intent = {
        "notification_id": notification_id,
        "organization_id": str(organization.pk),
        "target_roles": ["SUPERVISOR", "ADMINISTRATOR"],
        "vehicle_id": str(vehicle_id),
        "session_id": str(session_id) if session_id else None,
        "covers_from": covers_from.isoformat() if covers_from else None,
        "covers_until": covers_until.isoformat() if covers_until else None,
        "conflicting_correction_ids": list(conflicting_correction_ids),
        "conflicting_sessions": list(conflicting_sessions),
        "detected_at": detected_at.isoformat(),
        "receipt_id": str(receipt.id) if receipt else None,
        "correlation_id": corr_id,
        "status": "CONFLICT",
        "delivery_status": "PENDING",
    }

    AuditEvent.objects.create(
        organization=organization,
        command=receipt,
        request_attempt_id=new_id(),
        actor_user=actor_user,
        roles_at_time=list(actor_roles or []),
        action="ROSTER_CONFLICT_DETECTED",
        target_type="COMMAND_RECEIPT",
        target_id=target_id,
        recorded_at=detected_at,
        correlation_id=corr_id,
        outcome="SUCCEEDED",
        reason_code="CONCURRENT_SESSION_DISAGREEMENT",
        new_snapshot=make_audit_snapshot(dict(intent)),
    )

    def _on_commit():
        _deliver_conflict_notification_to_sinks(
            organization=organization,
            intent=intent,
            receipt=receipt,
            actor_user=actor_user,
            actor_roles=actor_roles,
            correlation_id=corr_id,
        )

    transaction.on_commit(_on_commit)
    return intent


def pending_conflict_notifications(organization) -> list[dict]:
    """Return durable conflict notification intents that have not yet been delivered."""
    delivered_ids = set()
    for ev in AuditEvent.objects.filter(
        organization=organization,
        action="ROSTER_CONFLICT_NOTIFICATION_DELIVERED",
    ):
        vals = (ev.new_snapshot or {}).get("values") or {}
        if vals.get("notification_id"):
            delivered_ids.add(vals["notification_id"])

    pending = []
    for ev in AuditEvent.objects.filter(
        organization=organization,
        action="ROSTER_CONFLICT_DETECTED",
    ).order_by("recorded_at"):
        vals = dict((ev.new_snapshot or {}).get("values") or {})
        nid = vals.get("notification_id")
        if nid and nid not in delivered_ids:
            pending.append(vals)
    return pending


def retry_pending_conflict_notifications(organization) -> list[dict]:
    """Retry sink delivery for all pending or previously failed conflict notifications."""
    results = []
    for intent in pending_conflict_notifications(organization):
        res = _deliver_conflict_notification_to_sinks(
            organization=organization,
            intent=dict(intent),
        )
        results.append(res)
    return results


def _reevaluate_affected_waivers(
    *,
    organization,
    vehicle_id: str | None,
    window_start: datetime,
    window_end: datetime,
    receipt,
    actor: Actor,
    evaluation_time: datetime,
    source_label: str,
    correction_id: str | None = None,
    prior_check_lookup: Callable | None = None,
) -> None:
    """Re-evaluate active WAIVED determinations overlapping [window_start, window_end) and append REINSTATED if invalidated."""
    candidate_waivers = ObligationDetermination.objects.filter(
        organization=organization,
        determination_type="WAIVED",
    ).filter(models.Q(due_at__isnull=True) | models.Q(due_at__gte=window_start))
    if vehicle_id:
        candidate_waivers = candidate_waivers.filter(
            models.Q(vehicle_id=vehicle_id) | models.Q(evaluated_inputs__vehicle_id=str(vehicle_id))
        )

    distinct_obligation_ids = set(candidate_waivers.values_list("obligation_id", flat=True))

    for ob_id in sorted(distinct_obligation_ids):
        latest_det = latest_obligation_determination(organization, ob_id, for_update=True)

        if latest_det is None or latest_det.determination_type != "WAIVED":
            continue

        stored_inputs = dict(latest_det.evaluated_inputs or {})
        ob_due = latest_det.due_at
        if ob_due is None:
            d_str = stored_inputs.get("due_at") or stored_inputs.get("target_time")
            if d_str:
                try:
                    ob_due = datetime.fromisoformat(d_str)
                    if ob_due.tzinfo is None:
                        ob_due = ob_due.replace(tzinfo=UTC)
                except (ValueError, TypeError):
                    ob_due = None

        if ob_due is None:
            continue

        # Determine the obligation's full dependency window [dep_start, ob_due] including its prior shift (Codex Finding 4)
        dep_start = ob_due - timedelta(hours=24)
        parsed_stored_shift_start = None
        parsed_stored_shift_end = None
        p_start_str = stored_inputs.get("prior_shift_start")
        p_end_str = stored_inputs.get("prior_shift_end")
        if p_start_str:
            try:
                parsed_p = datetime.fromisoformat(p_start_str)
                parsed_stored_shift_start = (
                    parsed_p if parsed_p.tzinfo else parsed_p.replace(tzinfo=UTC)
                )
                dep_start = parsed_stored_shift_start
            except (ValueError, TypeError):
                parsed_stored_shift_start = None
        if p_end_str:
            try:
                parsed_e = datetime.fromisoformat(p_end_str)
                parsed_stored_shift_end = (
                    parsed_e if parsed_e.tzinfo else parsed_e.replace(tzinfo=UTC)
                )
            except (ValueError, TypeError):
                parsed_stored_shift_end = None

        overlaps = (window_start < ob_due and window_end > dep_start) or (
            window_start <= ob_due < window_end
        )
        if not overlaps:
            continue

        ob_vehicle_id = latest_det.vehicle_id or stored_inputs.get("vehicle_id") or vehicle_id
        eval_clock = evaluation_time

        ob_rule = latest_det.rule
        if ob_rule is None:
            ob_rule = (
                ConditionalRule.objects.filter(
                    organization=organization,
                    rule_type="CARRY_OVER_FROM_PRIOR_SHIFT",
                    effective_at__lte=ob_due,
                )
                .order_by("-revision")
                .first()
            )

        ob_item_id = stored_inputs.get("item_id") or (
            (ob_rule.typed_inputs or {}).get("item_id") if ob_rule else None
        )
        prior_check_id = stored_inputs.get("prior_check_id")
        cond_b_ok = stored_inputs.get("condition_b_satisfied") is True
        check_invalidated = stored_inputs.get("prior_check_invalidated") is True
        stored_check_at_str = stored_inputs.get("prior_check_at") or stored_inputs.get(
            "prior_check_checked_at"
        )
        parsed_stored_check_at = None
        if stored_check_at_str:
            try:
                parsed_c = datetime.fromisoformat(stored_check_at_str)
                parsed_stored_check_at = (
                    parsed_c if parsed_c.tzinfo else parsed_c.replace(tzinfo=UTC)
                )
            except (ValueError, TypeError):
                parsed_stored_check_at = None

        if (
            not cond_b_ok
            or check_invalidated
            or not prior_check_id
            or parsed_stored_shift_start is None
            or parsed_stored_shift_end is None
            or parsed_stored_shift_end != ob_due
        ):
            ob_effective = derive_effective_roster(
                organization,
                str(ob_vehicle_id or ""),
                ob_due,
                evaluation_time=eval_clock,
                enforce_staleness=False,
            )
            should_reinstate = True
            reason = "Fail closed: existing waiver has incomplete initial evaluation evidence."
            eval_inputs_delta = {
                "condition_b_satisfied": False,
                "fail_closed": True,
            }
        else:
            evaluation = evaluate_carry_over_rule(
                organization=organization,
                vehicle_id=str(ob_vehicle_id or ""),
                item_id=str(ob_item_id or ""),
                due_at=ob_due,
                rule=ob_rule,
                prior_check_lookup=prior_check_lookup,
                evaluation_time=eval_clock,
                re_evaluating_existing_waiver=True,
                prior_shift_bounds=(parsed_stored_shift_start, parsed_stored_shift_end),
                stored_prior_check_at=parsed_stored_check_at,
            )
            ob_effective = evaluation.current_roster
            should_reinstate = not evaluation.waived
            reason = evaluation.reason
            eval_inputs_delta = dict(evaluation.evaluated_inputs or {})

        if should_reinstate:
            if not ob_effective.is_available and ob_effective.status not in reason:
                reason = f"Effective roster is {ob_effective.status}: {reason}"
            elif ob_effective.is_available and not ob_effective.members:
                reason = f"No scheduled or actual crew on duty: {reason}"

            reinstated_inputs = dict(stored_inputs)
            reinstated_inputs.update(eval_inputs_delta)
            if ob_vehicle_id:
                reinstated_inputs["vehicle_id"] = str(ob_vehicle_id)
            reinstated_inputs["due_at"] = ob_due.isoformat()
            reinstated_inputs["reinstatement_at"] = receipt.recorded_at.isoformat()
            if correction_id:
                reinstated_inputs["correction_id"] = str(correction_id)
            reinstated_inputs["current_members"] = sorted(ob_effective.members)
            reinstated_inputs["reason"] = reason

            if ob_effective.status == RosterStatus.CONFLICT:
                reinstated_inputs["conflicting_correction_ids"] = sorted(
                    str(c.id) for c in ob_effective.conflicting_corrections
                )
                reinstated_inputs["conflicting_sessions"] = sorted(
                    str(s) for s in ob_effective.conflicting_sessions
                )

            parsed_early_at = None
            early_at_str = stored_inputs.get("early_at")
            if early_at_str:
                try:
                    ea = datetime.fromisoformat(early_at_str)
                    if ea.tzinfo is None:
                        ea = ea.replace(tzinfo=UTC)
                    parsed_early_at = ea
                except (ValueError, TypeError):
                    parsed_early_at = None

            parsed_grace_at = None
            grace_at_str = stored_inputs.get("grace_at")
            if grace_at_str:
                try:
                    grace_at = datetime.fromisoformat(grace_at_str)
                    if grace_at.tzinfo is None:
                        grace_at = grace_at.replace(tzinfo=UTC)
                    parsed_grace_at = grace_at
                except (ValueError, TypeError):
                    parsed_grace_at = None

            current_qualifying_check = _find_qualifying_current_obligation_check(
                organization,
                obligation_id=str(latest_det.obligation_id),
                vehicle_id=str(ob_vehicle_id or ""),
                item_id=str(ob_item_id or ""),
                due_at=ob_due,
                grace_at=parsed_grace_at,
                evaluation_time=receipt.recorded_at,
                prior_check_id=str(prior_check_id) if prior_check_id else None,
                early_at=parsed_early_at,
                prior_check_lookup=prior_check_lookup,
            )
            already_satisfied = current_qualifying_check is not None
            reinstated_inputs["obligation_satisfied"] = already_satisfied
            reinstated_inputs["satisfied_by_check_id"] = (
                str(current_qualifying_check["id"]) if current_qualifying_check else None
            )
            if current_qualifying_check is not None:
                reinstated_inputs["inspection_outcome"] = current_qualifying_check.get(
                    "outcome",
                    "PASSED" if current_qualifying_check.get("passed") is True else "FAILED",
                )
            if (
                parsed_grace_at is not None
                and receipt.recorded_at > parsed_grace_at
                and not already_satisfied
            ):
                reinstated_inputs["is_overdue"] = True
                reinstated_inputs["overdue_from"] = parsed_grace_at.isoformat()
            else:
                reinstated_inputs["is_overdue"] = False
                reinstated_inputs.pop("overdue_from", None)

            ObligationDetermination.objects.create(
                id=new_id(),
                organization=organization,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=actor.user,
                actor_roles_at_time=list(actor.roles),
                service_identity=actor.service_identity,
                job_key=actor.job_key,
                obligation_id=latest_det.obligation_id,
                vehicle_id=str(ob_vehicle_id) if ob_vehicle_id else None,
                due_at=ob_due,
                determination_type="REINSTATED",
                rule=latest_det.rule,
                prior_determination=latest_det,
                explanation=f"Waiver invalidated by {source_label}: {reason}",
                evaluated_inputs=reinstated_inputs,
            )


def _validate_conditional_rule_inputs(rule_type: str, typed_inputs: dict) -> None:
    """Validate conditional rule_type and typed_inputs structure (Claude Finding 3)."""
    if rule_type not in ("CARRY_OVER_FROM_PRIOR_SHIFT", "SEAL_SUBSTITUTION"):
        raise ValidationError(f"Unsupported conditional rule_type: {rule_type}")
    if not isinstance(typed_inputs, dict) or not typed_inputs:
        raise ValidationError("typed_inputs must be a non-empty dictionary of typed rule inputs")
    if rule_type == "CARRY_OVER_FROM_PRIOR_SHIFT":
        allowed_keys = {
            "item_id",
            "item_ids",
            "checklist_id",
            "compartment_id",
            "require_passed_check",
            "max_prior_shift_hours",
        }
        required_any = {"item_id", "item_ids", "checklist_id"}
    else:
        allowed_keys = {
            "seal",
            "seal_type",
            "compartment_id",
            "item_id",
            "item_ids",
            "checklist_id",
        }
        required_any = allowed_keys

    keys = set(typed_inputs.keys())
    if not keys.issubset(allowed_keys) or not (keys & required_any):
        raise ValidationError(f"Invalid typed_inputs keys for {rule_type}")
    for key, val in typed_inputs.items():
        if val is None or val == "" or val == []:
            raise ValidationError(f"typed_inputs[{key!r}] must be non-empty")


def create_conditional_rule(
    *,
    organization,
    rule_type: str,
    typed_inputs: dict,
    effective_at: datetime,
    actor_user=None,
    actor_roles_at_time: list[str] | None = None,
    operation_id: str | None = None,
    correlation_id: str | None = None,
    revision: int | None = None,
) -> ConditionalRule:
    """Create an immutable published revision of a conditional rule (Kind V, contracts §2)."""
    if effective_at.tzinfo is None:
        effective_at = effective_at.replace(tzinfo=UTC)

    _validate_conditional_rule_inputs(rule_type, typed_inputs)

    org_id = str(organization.pk)
    op_id = operation_id or new_id()
    corr_id = correlation_id or new_id()
    attempt_id = new_id()

    request_digest = hashlib.sha256(
        json.dumps(
            {
                "rule_type": rule_type,
                "typed_inputs": typed_inputs or {},
                "effective_at": effective_at.isoformat(),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()

    def authorize(org):
        if not org.active or actor_user is None:
            raise CommandDenied()
        with locked_identities(org_id, [actor_user.id], lock_organization=False) as (
            current_org,
            users,
        ):
            locked_user = users[actor_user.id]
            if not locked_user.active or not current_org.active:
                raise CommandDenied(Actor(user=locked_user))
            backend = DatabaseRoleBackend()
            state = load_state(backend, locked_user)
            roles = frozenset(Role.CREW if r == Role.EMT else r for r in state.grants)
            act = Actor(user=locked_user, roles=tuple(sorted(r.value for r in roles)))
            if Role.ADMINISTRATOR not in roles:
                raise CommandDenied(act)
            return act

    def prepare(actor):
        prior = (
            ConditionalRule.objects.filter(organization=organization, rule_type=rule_type)
            .order_by("-revision")
            .first()
        )
        if prior and effective_at <= prior.effective_at:
            raise ValueError("A revision must take effect after the previous revision")
        rev = revision if revision is not None else ((prior.revision + 1) if prior else 1)
        rule_id = new_id()

        def apply(receipt, current):
            return ConditionalRule.objects.create(
                id=rule_id,
                organization=organization,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=actor.user,
                actor_roles_at_time=list(actor.roles),
                revision=rev,
                rule_type=rule_type,
                typed_inputs=typed_inputs or {},
                effective_at=effective_at,
            )

        return Plan(
            response_body={"rule_id": rule_id, "rule_type": rule_type, "revision": rev},
            apply=apply,
            response_status=201,
            new_snapshot=make_audit_snapshot(typed_inputs or {}),
        )

    result = run_command(
        organization_id=org_id,
        operation_id=op_id,
        command_type="CONDITIONAL_RULE_PUBLISH",
        request_digest=request_digest,
        correlation_id=corr_id,
        request_attempt_id=attempt_id,
        authorize=authorize,
        prepare=prepare,
    )

    if result.replayed:
        return ConditionalRule.objects.get(organization=organization, command__operation_id=op_id)

    return ConditionalRule.objects.get(organization=organization, command__operation_id=op_id)


def _ingest_single_shift(
    *,
    organization,
    provider_name: str,
    shift: RosterShift,
    start: datetime,
    end: datetime,
    shift_vehicle_id: str | None,
    fetched_at: datetime,
    service_identity: str,
    jk: str,
    org_id: str,
    batch_id: str,
    batch_start: datetime,
    batch_end: datetime,
    batch_vehicle_id: str | None,
    reevaluate_window: tuple[datetime, datetime] | None = None,
    reevaluate_vehicle_id: str | None = None,
    prior_check_lookup: Callable | None = None,
) -> RosterSnapshot:
    op_id = str(worker_operation_id(org_id, service_identity, "INGEST_ROSTER_SNAPSHOT", jk))
    digest_payload = {
        "provider": provider_name,
        "shift_id": str(shift.shift_id),
        "vehicle_id": str(shift_vehicle_id or ""),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "users": sorted(str(u) for u in shift.user_ids),
    }
    req_digest = hashlib.sha256(json.dumps(digest_payload, sort_keys=True).encode()).hexdigest()
    attempt_id = new_id()
    corr_id = new_id()

    def auth_shift(org):
        return Actor(service_identity=service_identity, job_key=jk)

    def prep_shift(actor):
        snap_id = new_id()

        def apply_shift(receipt, current):
            snap = RosterSnapshot.objects.create(
                id=snap_id,
                organization=organization,
                command=receipt,
                recorded_at=receipt.recorded_at,
                service_identity=actor.service_identity,
                job_key=actor.job_key,
                provider=provider_name,
                fetched_at=fetched_at,
                availability="AVAILABLE",
                shift_start=start,
                shift_end=end,
                vehicle_id=shift_vehicle_id,
            )
            for user_id in shift.user_ids:
                RosterMember.objects.create(
                    id=new_id(),
                    organization=organization,
                    snapshot=snap,
                    person_identity=str(user_id),
                    assignment="CREW",
                )
            if reevaluate_window is not None:
                _reevaluate_affected_waivers(
                    organization=organization,
                    vehicle_id=reevaluate_vehicle_id,
                    window_start=reevaluate_window[0],
                    window_end=reevaluate_window[1],
                    receipt=receipt,
                    actor=actor,
                    evaluation_time=receipt.recorded_at,
                    source_label="scheduled roster update",
                    prior_check_lookup=prior_check_lookup,
                )
            return snap

        return Plan(
            response_body={
                "snapshot_id": snap_id,
                "shift_id": shift.shift_id,
                "batch_id": batch_id,
                "interval_start": batch_start.isoformat(),
                "interval_end": batch_end.isoformat(),
                "scope_vehicle_id": str(batch_vehicle_id or ""),
            },
            apply=apply_shift,
            response_status=201,
            new_snapshot=make_audit_snapshot(
                {"shift_id": shift.shift_id, "users": list(shift.user_ids)}
            ),
        )

    run_command(
        organization_id=org_id,
        operation_id=op_id,
        command_type="INGEST_ROSTER_SNAPSHOT",
        request_digest=req_digest,
        correlation_id=corr_id,
        request_attempt_id=attempt_id,
        authorize=auth_shift,
        prepare=prep_shift,
    )

    return RosterSnapshot.objects.get(organization=organization, command__operation_id=op_id)


def ingest_roster_snapshot(
    *,
    organization,
    provider_name: str,
    shifts: Sequence[RosterShift] | None,
    fetched_at: datetime,
    vehicle_id: str | None = None,
    service_identity: str = "worker.roster_sync",
    job_key: str | list[str] | None = None,
    is_available: bool = True,
    interval_start: datetime | None = None,
    interval_end: datetime | None = None,
    prior_check_lookup: Callable | None = None,
) -> list[RosterSnapshot]:
    """Ingest scheduled shifts from RosterProvider into immutable RosterSnapshot rows with replay support (Finding 1, 6)."""
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)

    org_id = str(organization.pk)
    created_snapshots = []

    if not is_available or shifts is None:
        # Record UNAVAILABLE snapshot
        start_time = interval_start or (fetched_at - timedelta(hours=24))
        end_time = interval_end or (fetched_at + timedelta(hours=24))
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=UTC)
        if end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=UTC)

        if job_key is None:
            raw_key = [
                "roster-unavailable",
                provider_name,
                start_time.isoformat(),
                end_time.isoformat(),
                str(vehicle_id or ""),
                fetched_at.isoformat(),
            ]
            jk = json.dumps(raw_key, ensure_ascii=True, separators=(",", ":"))
        elif isinstance(job_key, (list, tuple)):
            jk = json.dumps(list(job_key), ensure_ascii=True, separators=(",", ":"))
        else:
            jk = str(job_key)

        op_id = str(worker_operation_id(org_id, service_identity, "INGEST_ROSTER_UNAVAILABLE", jk))
        req_digest = hashlib.sha256(
            f"unavailable:{provider_name}:{start_time.isoformat()}:{end_time.isoformat()}:{vehicle_id}:{fetched_at.isoformat()}".encode()
        ).hexdigest()
        attempt_id = new_id()
        corr_id = new_id()

        def auth_unavail(org):
            return Actor(service_identity=service_identity, job_key=jk)

        def prep_unavail(actor):
            snap_id = new_id()

            def apply_unavail(receipt, current):
                snap = RosterSnapshot.objects.create(
                    id=snap_id,
                    organization=organization,
                    command=receipt,
                    recorded_at=receipt.recorded_at,
                    service_identity=actor.service_identity,
                    job_key=actor.job_key,
                    provider=provider_name,
                    fetched_at=fetched_at,
                    availability="UNAVAILABLE",
                    shift_start=start_time,
                    shift_end=end_time,
                    vehicle_id=vehicle_id,
                )
                _reevaluate_affected_waivers(
                    organization=organization,
                    vehicle_id=vehicle_id,
                    window_start=start_time,
                    window_end=end_time,
                    receipt=receipt,
                    actor=actor,
                    evaluation_time=receipt.recorded_at,
                    source_label="scheduled roster update",
                    prior_check_lookup=prior_check_lookup,
                )
                return snap

            return Plan(
                response_body={
                    "snapshot_id": snap_id,
                    "status": "UNAVAILABLE",
                    "interval_start": start_time.isoformat(),
                    "interval_end": end_time.isoformat(),
                    "scope_vehicle_id": str(vehicle_id or ""),
                },
                apply=apply_unavail,
                response_status=200,
                new_snapshot=make_audit_snapshot(
                    {"provider": provider_name, "status": "UNAVAILABLE"}
                ),
            )

        run_command(
            organization_id=org_id,
            operation_id=op_id,
            command_type="INGEST_ROSTER_UNAVAILABLE",
            request_digest=req_digest,
            correlation_id=corr_id,
            request_attempt_id=attempt_id,
            authorize=auth_unavail,
            prepare=prep_unavail,
        )

        snap = RosterSnapshot.objects.get(organization=organization, command__operation_id=op_id)
        created_snapshots.append(snap)
        return created_snapshots

    if is_available and (shifts is not None and len(shifts) == 0):
        # Authoritative empty feed (Finding 2, 3): record empty AVAILABLE snapshot
        start_time = interval_start or (fetched_at - timedelta(hours=24))
        end_time = interval_end or (fetched_at + timedelta(hours=24))
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=UTC)
        if end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=UTC)

        if job_key is None:
            raw_key = [
                "roster-empty",
                provider_name,
                str(vehicle_id or ""),
                start_time.isoformat(),
                end_time.isoformat(),
                fetched_at.isoformat(),
            ]
            jk = json.dumps(raw_key, ensure_ascii=True, separators=(",", ":"))
        elif isinstance(job_key, (list, tuple)):
            jk = json.dumps(list(job_key), ensure_ascii=True, separators=(",", ":"))
        else:
            jk = str(job_key)

        op_id = str(worker_operation_id(org_id, service_identity, "INGEST_ROSTER_SNAPSHOT", jk))
        req_digest = hashlib.sha256(
            f"empty:{provider_name}:{start_time.isoformat()}:{end_time.isoformat()}:{vehicle_id}:{fetched_at.isoformat()}".encode()
        ).hexdigest()
        attempt_id = new_id()
        corr_id = new_id()
        empty_batch_id = hashlib.sha256(
            f"batch-empty:{provider_name}:{start_time.isoformat()}:{end_time.isoformat()}:{vehicle_id}:{fetched_at.isoformat()}".encode()
        ).hexdigest()[:36]

        def auth_empty(org):
            return Actor(service_identity=service_identity, job_key=jk)

        def prep_empty(actor):
            snap_id = new_id()

            def apply_empty(receipt, current):
                snap = RosterSnapshot.objects.create(
                    id=snap_id,
                    organization=organization,
                    command=receipt,
                    recorded_at=receipt.recorded_at,
                    service_identity=actor.service_identity,
                    job_key=actor.job_key,
                    provider=provider_name,
                    fetched_at=fetched_at,
                    availability="AVAILABLE",
                    shift_start=start_time,
                    shift_end=end_time,
                    vehicle_id=vehicle_id,
                )
                _reevaluate_affected_waivers(
                    organization=organization,
                    vehicle_id=vehicle_id,
                    window_start=start_time,
                    window_end=end_time,
                    receipt=receipt,
                    actor=actor,
                    evaluation_time=receipt.recorded_at,
                    source_label="scheduled roster update",
                    prior_check_lookup=prior_check_lookup,
                )
                return snap

            return Plan(
                response_body={
                    "snapshot_id": snap_id,
                    "provider": provider_name,
                    "status": "AVAILABLE",
                    "members": 0,
                    "batch_id": empty_batch_id,
                    "interval_start": start_time.isoformat(),
                    "interval_end": end_time.isoformat(),
                    "scope_vehicle_id": str(vehicle_id or ""),
                },
                apply=apply_empty,
                response_status=201,
                new_snapshot=make_audit_snapshot(
                    {"provider": provider_name, "status": "AVAILABLE", "members": 0}
                ),
            )

        run_command(
            organization_id=org_id,
            operation_id=op_id,
            command_type="INGEST_ROSTER_SNAPSHOT",
            request_digest=req_digest,
            correlation_id=corr_id,
            request_attempt_id=attempt_id,
            authorize=auth_empty,
            prepare=prep_empty,
        )
        snap = RosterSnapshot.objects.get(organization=organization, command__operation_id=op_id)
        created_snapshots.append(snap)
        return created_snapshots

    # Ingest available shifts; preserve the caller's authoritative scope (vehicle_id=None for fleet-wide)
    normalized_starts = [s.start if s.start.tzinfo else s.start.replace(tzinfo=UTC) for s in shifts]
    normalized_ends = [s.end if s.end.tzinfo else s.end.replace(tzinfo=UTC) for s in shifts]
    batch_start = interval_start or min(normalized_starts)
    batch_end = interval_end or max(normalized_ends)
    if batch_start.tzinfo is None:
        batch_start = batch_start.replace(tzinfo=UTC)
    if batch_end.tzinfo is None:
        batch_end = batch_end.replace(tzinfo=UTC)

    batch_vehicle_id = vehicle_id
    batch_id = hashlib.sha256(
        json.dumps(
            {
                "provider": provider_name,
                "start": batch_start.isoformat(),
                "end": batch_end.isoformat(),
                "fetched_at": fetched_at.isoformat(),
                "vehicle_id": str(batch_vehicle_id or ""),
                "shifts": [str(s.shift_id) for s in shifts],
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()[:36]

    for idx, shift in enumerate(shifts):
        start = shift.start if shift.start.tzinfo else shift.start.replace(tzinfo=UTC)
        end = shift.end if shift.end.tzinfo else shift.end.replace(tzinfo=UTC)
        shift_vehicle_id = getattr(shift, "vehicle_id", None) or vehicle_id
        is_last = idx == len(shifts) - 1

        if job_key is None:
            raw_key = [
                "roster-shift",
                provider_name,
                str(shift_vehicle_id or ""),
                str(shift.shift_id),
                start.isoformat(),
                end.isoformat(),
                fetched_at.isoformat(),
            ]
            jk = json.dumps(raw_key, ensure_ascii=True, separators=(",", ":"))
        elif isinstance(job_key, (list, tuple)):
            jk = json.dumps(list(job_key) + [str(idx)], ensure_ascii=True, separators=(",", ":"))
        else:
            jk = json.dumps(
                [str(job_key), str(shift.shift_id)], ensure_ascii=True, separators=(",", ":")
            )

        snap = _ingest_single_shift(
            organization=organization,
            provider_name=provider_name,
            shift=shift,
            start=start,
            end=end,
            shift_vehicle_id=shift_vehicle_id,
            fetched_at=fetched_at,
            service_identity=service_identity,
            jk=jk,
            org_id=org_id,
            batch_id=batch_id,
            batch_start=batch_start,
            batch_end=batch_end,
            batch_vehicle_id=batch_vehicle_id,
            reevaluate_window=(batch_start, batch_end) if is_last else None,
            reevaluate_vehicle_id=batch_vehicle_id,
            prior_check_lookup=prior_check_lookup,
        )
        created_snapshots.append(snap)

    return created_snapshots


def record_session_roster_correction(
    *,
    organization,
    session_id: str,
    vehicle_id: str,
    covers_from: datetime,
    covers_until: datetime,
    members: Sequence[str],
    actor_user,
    actor_roles_at_time: list[str] | None = None,
    resolves_correction: SessionRosterCorrection | None = None,
    operation_id: str | None = None,
    correlation_id: str | None = None,
    prior_check_lookup: Callable | None = None,
) -> SessionRosterCorrection:
    """Record an attributed actual-roster correction with replay support and automatic re-evaluation (Finding 3, 4, 5, 6)."""
    if covers_from.tzinfo is None:
        covers_from = covers_from.replace(tzinfo=UTC)
    if covers_until.tzinfo is None:
        covers_until = covers_until.replace(tzinfo=UTC)

    org_id = str(organization.pk)
    op_id = operation_id or new_id()
    corr_id = correlation_id or new_id()
    attempt_id = new_id()

    resolves_id = str(resolves_correction.id) if resolves_correction else None
    request_digest = hashlib.sha256(
        json.dumps(
            {
                "session_id": str(session_id),
                "vehicle_id": str(vehicle_id),
                "covers_from": covers_from.isoformat(),
                "covers_until": covers_until.isoformat(),
                "members": sorted(str(m) for m in members),
                "resolves_correction_id": resolves_id,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()

    def authorize(org):
        if not org.active:
            raise CommandDenied()
        with locked_identities(org_id, [actor_user.id], lock_organization=False) as (
            current_org,
            users,
        ):
            locked_user = users[actor_user.id]
            if not locked_user.active or not current_org.active:
                raise CommandDenied(Actor(user=locked_user))
            backend = DatabaseRoleBackend()
            state = load_state(backend, locked_user)
            roles = frozenset(Role.CREW if r == Role.EMT else r for r in state.grants)
            act = Actor(user=locked_user, roles=tuple(sorted(r.value for r in roles)))
            if resolves_correction is not None:
                # SPEC 15.3: Conflict resolution requires supervisor or administrator
                if Role.SUPERVISOR not in roles and Role.ADMINISTRATOR not in roles:
                    raise CommandDenied(act)
            else:
                if not (roles & {Role.CREW, Role.SUPERVISOR, Role.ADMINISTRATOR}):
                    raise CommandDenied(act)
            return act

    def prepare(actor):
        corr_id_new = new_id()
        snap_id_new = new_id()

        def apply(receipt, current):
            # Create actual roster snapshot
            snap = RosterSnapshot.objects.create(
                id=snap_id_new,
                organization=organization,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=actor.user,
                actor_roles_at_time=list(actor.roles),
                provider="SESSION_ACTUAL",
                fetched_at=receipt.recorded_at,
                availability="AVAILABLE",
                shift_start=covers_from,
                shift_end=covers_until,
                vehicle_id=vehicle_id,
            )
            for person_id in members:
                RosterMember.objects.create(
                    id=new_id(),
                    organization=organization,
                    snapshot=snap,
                    person_identity=str(person_id),
                    assignment="CREW",
                )

            corr = SessionRosterCorrection.objects.create(
                id=corr_id_new,
                organization=organization,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=actor.user,
                actor_roles_at_time=list(actor.roles),
                session_id=session_id,
                vehicle_id=vehicle_id,
                snapshot=snap,
                covers_from=covers_from,
                covers_until=covers_until,
                resolves_correction=resolves_correction,
            )

            # Finding 5 & Claude Finding 1: Detect conflicts across the half-open [covers_from, covers_until)
            # window without false-positive overlap at adjacent shift boundaries.
            overlapping_corrections = list(
                SessionRosterCorrection.objects.filter(
                    organization=organization,
                    vehicle_id=vehicle_id,
                    covers_from__lt=covers_until,
                    covers_until__gt=covers_from,
                )
            )
            candidate_points = {covers_from}
            for ov in overlapping_corrections:
                if covers_from <= ov.covers_from < covers_until:
                    candidate_points.add(ov.covers_from)
                if covers_from < ov.covers_until < covers_until:
                    candidate_points.add(ov.covers_until)

            all_conflicting_ids: set[str] = set()
            all_conflicting_sessions: set[str] = set()
            for pt in sorted(candidate_points):
                eff = derive_effective_roster(
                    organization,
                    vehicle_id,
                    pt,
                    evaluation_time=receipt.recorded_at,
                )
                if eff.status == RosterStatus.CONFLICT:
                    for c_obj in eff.conflicting_corrections:
                        all_conflicting_ids.add(str(c_obj.id))
                    for s_id in eff.conflicting_sessions:
                        all_conflicting_sessions.add(str(s_id))

            if all_conflicting_ids:
                notify_supervisor_roster_conflict(
                    organization=organization,
                    vehicle_id=str(vehicle_id),
                    conflicting_correction_ids=sorted(all_conflicting_ids),
                    conflicting_sessions=sorted(all_conflicting_sessions),
                    detected_at=receipt.recorded_at,
                    receipt=receipt,
                    actor_user=actor.user,
                    actor_roles=list(actor.roles),
                    correlation_id=corr_id,
                    covers_from=covers_from,
                    covers_until=covers_until,
                    session_id=str(session_id),
                )

            _reevaluate_affected_waivers(
                organization=organization,
                vehicle_id=vehicle_id,
                window_start=covers_from,
                window_end=covers_until,
                receipt=receipt,
                actor=actor,
                evaluation_time=receipt.recorded_at,
                source_label="session roster correction",
                correction_id=str(corr.id),
                prior_check_lookup=prior_check_lookup,
            )

            return corr

        return Plan(
            response_body={"correction_id": corr_id_new, "session_id": session_id},
            apply=apply,
            response_status=201,
            new_snapshot=make_audit_snapshot({"members": list(members), "session_id": session_id}),
        )

    run_command(
        organization_id=org_id,
        operation_id=op_id,
        command_type="RECORD_SESSION_ROSTER_CORRECTION",
        request_digest=request_digest,
        correlation_id=corr_id,
        request_attempt_id=attempt_id,
        authorize=authorize,
        prepare=prepare,
    )

    return SessionRosterCorrection.objects.get(
        organization=organization, command__operation_id=op_id
    )
