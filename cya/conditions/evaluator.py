"""Evaluation engine for conditional inspection obligations and fail-closed rosters (SPEC 5.6, 15.2, 15.3)."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.module_loading import import_string

from cya.accounts.fields import new_id
from cya.authorization.backend import load_state
from cya.authorization.locking import locked_identities
from cya.authorization.persistence import DatabaseRoleBackend
from cya.authorization.policy import Role
from cya.conditions.models import (
    ConditionalRule,
    ObligationDetermination,
    RosterSnapshot,
    SessionRosterCorrection,
)
from cya.core.audit_commands import Actor, CommandDenied, Plan, run_command
from cya.integrations import IntegrationNotConfigured
from tools.command_identity import worker_operation_id

DEFAULT_STALENESS_LIMIT_SECONDS = 3 * 3600  # 3 hours (SPEC 15.2, HUM-3)

_UNSET = object()


class PriorCheckLookupUnavailable(IntegrationNotConfigured):
    """The prior-check lookup collaborator is not configured or could not be reached."""


def resolve_prior_check_lookup(
    prior_check_lookup: Any = _UNSET,
) -> Callable:
    """Resolve an explicit or settings-configured prior-check lookup collaborator.

    Raises PriorCheckLookupUnavailable when no valid callable is configured.
    """
    candidate = (
        getattr(settings, "CYA_PRIOR_CHECK_LOOKUP", None)
        if prior_check_lookup is _UNSET or prior_check_lookup is None
        else prior_check_lookup
    )
    if candidate is None:
        raise PriorCheckLookupUnavailable("settings.CYA_PRIOR_CHECK_LOOKUP is not set.")
    if callable(candidate):
        return candidate
    if isinstance(candidate, str) and candidate:
        try:
            resolved = import_string(candidate)
        except Exception as exc:
            raise PriorCheckLookupUnavailable(
                "settings.CYA_PRIOR_CHECK_LOOKUP could not be imported."
            ) from exc
        if callable(resolved):
            return resolved
    raise PriorCheckLookupUnavailable(
        "prior_check_lookup must be a callable or import path to a callable."
    )


def _candidate_outcome_satisfies_obligation(candidate: dict[str, Any]) -> bool:
    """Return True if a check record satisfies an obligation under SPEC §5.4."""
    if (
        candidate.get("invalidated")
        or candidate.get("suspended")
        or candidate.get("unable_to_inspect")
    ):
        return False
    raw_outcome = candidate.get("outcome")
    if raw_outcome is not None:
        norm = str(raw_outcome).upper()
        if norm in ("PASS", "PASSED", "FAIL", "FAILED"):
            return True
        if norm in ("NA", "NOT_APPLICABLE"):
            return bool(candidate.get("na_rule_id"))
        return False
    if candidate.get("na_rule_id"):
        return True
    return candidate.get("passed") in (True, False)


def _find_qualifying_current_obligation_check(
    organization,
    *,
    obligation_id: str,
    vehicle_id: str,
    item_id: str,
    due_at: datetime,
    grace_at: datetime | None,
    evaluation_time: datetime,
    prior_check_id: str | None,
    early_at: datetime | None = None,
    prior_check_lookup: Any = _UNSET,
) -> dict[str, Any] | None:
    """Check whether a restored obligation at due_at is already satisfied by a linked qualifying check or supervisor completion.

    Requires an explicit check-to-obligation link (`obligation_id` or `shared_rule_id` + `shared_obligation_ids` per SCH-5/SCH-6),
    honors early on-time checks in `[E, D)` as well as `[D, evaluation_time]` (`SCH-1`/`SCH-2`), accepts valid Passed, Failed,
    and rule-backed NA outcomes (`SPEC §5.4`), and raises `PriorCheckLookupUnavailable` if the collaborator is unavailable or fails.
    """
    collab = resolve_prior_check_lookup(prior_check_lookup)
    if early_at is not None:
        early_bound = early_at if early_at.tzinfo else early_at.replace(tzinfo=UTC)
    else:
        early_bound = datetime(1970, 1, 1, tzinfo=UTC)
    window_end = max(
        evaluation_time + timedelta(microseconds=1),
        (grace_at if grace_at is not None else due_at) + timedelta(hours=12),
    )
    try:
        candidate = collab(
            organization,
            str(vehicle_id),
            str(item_id),
            early_bound,
            window_end,
            obligation_id=str(obligation_id),
        )
    except PriorCheckLookupUnavailable:
        raise
    except Exception as exc:
        raise PriorCheckLookupUnavailable(
            "prior check lookup collaborator failed during obligation satisfaction check."
        ) from exc

    if isinstance(candidate, dict) and candidate.get("id"):
        cand_id = str(candidate["id"])
        shared_ids = {
            str(x)
            for x in (
                candidate.get("shared_obligation_ids")
                or candidate.get("linked_obligation_ids")
                or ()
            )
        }
        explicitly_shared = (
            bool(candidate.get("shared_rule_id")) and str(obligation_id) in shared_ids
        )
        if not (prior_check_id and cand_id == str(prior_check_id) and not explicitly_shared):
            cand_ob_id = candidate.get("obligation_id")
            has_explicit_link = (
                cand_ob_id is not None and str(cand_ob_id) == str(obligation_id)
            ) or explicitly_shared
            if has_explicit_link and _candidate_outcome_satisfies_obligation(candidate):
                raw_ts = (
                    candidate.get("checked_at")
                    or candidate.get("recorded_at")
                    or candidate.get("completed_at")
                    or candidate.get("performed_at")
                )
                check_ts = None
                ts_valid = True
                if isinstance(raw_ts, datetime):
                    check_ts = raw_ts if raw_ts.tzinfo else raw_ts.replace(tzinfo=UTC)
                elif isinstance(raw_ts, str):
                    try:
                        parsed_ts = datetime.fromisoformat(raw_ts)
                        check_ts = parsed_ts if parsed_ts.tzinfo else parsed_ts.replace(tzinfo=UTC)
                    except (ValueError, TypeError):
                        ts_valid = False
                if ts_valid and (check_ts is None or (early_bound <= check_ts <= evaluation_time)):
                    return candidate

    # Check if an active SUPERVISOR_COMPLETED determination exists for this obligation (SPEC §5.4.1, UTI-2/UTI-3)
    superseded_ids = set(
        ObligationDetermination.objects.filter(
            organization=organization,
            obligation_id=obligation_id,
            prior_determination__isnull=False,
            recorded_at__lte=evaluation_time,
        ).values_list("prior_determination_id", flat=True)
    )
    sup_qs = ObligationDetermination.objects.filter(
        organization=organization,
        obligation_id=obligation_id,
        determination_type="SUPERVISOR_COMPLETED",
        recorded_at__lte=evaluation_time,
    )
    if superseded_ids:
        sup_qs = sup_qs.exclude(id__in=superseded_ids)
    sup_det = sup_qs.order_by("-recorded_at").first()
    if sup_det is not None:
        return {
            "id": str(sup_det.id),
            "outcome": "SUPERVISOR_COMPLETED",
            "passed": False,
            "obligation_id": str(obligation_id),
        }
    return None


def latest_obligation_determination(
    organization, obligation_id: str, *, for_update: bool = False
) -> ObligationDetermination | None:
    """Return the current leaf determination for an obligation by excluding superseded prior determinations."""
    superseded_ids = set(
        ObligationDetermination.objects.filter(
            organization=organization,
            obligation_id=obligation_id,
            prior_determination__isnull=False,
        ).values_list("prior_determination_id", flat=True)
    )
    qs = ObligationDetermination.objects.filter(
        organization=organization,
        obligation_id=obligation_id,
    )
    if for_update:
        qs = qs.select_for_update()
    if superseded_ids:
        qs = qs.exclude(id__in=superseded_ids)
    return qs.order_by("-recorded_at").first()


class RosterStatus:
    SCHEDULED = "SCHEDULED"
    CORRECTED = "CORRECTED"
    UNAVAILABLE = "UNAVAILABLE"
    STALE = "STALE"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class EffectiveRosterResult:
    status: str
    members: frozenset[str]
    scheduled_snapshot: RosterSnapshot | None = None
    active_correction: SessionRosterCorrection | None = None
    conflicting_corrections: tuple[SessionRosterCorrection, ...] = ()

    @property
    def is_available(self) -> bool:
        return self.status in (RosterStatus.SCHEDULED, RosterStatus.CORRECTED)

    @property
    def conflicting_sessions(self) -> frozenset[str]:
        return frozenset(c.session_id for c in self.conflicting_corrections)


@dataclass(frozen=True)
class CarryOverEvaluation:
    waived: bool
    reason: str
    current_roster: EffectiveRosterResult
    prior_roster: EffectiveRosterResult | None = None
    prior_check_id: str | None = None
    evaluated_inputs: dict[str, Any] = None


def derive_effective_roster(
    organization,
    vehicle_id: str | None,
    target_time: datetime,
    *,
    evaluation_time: datetime | None = None,
    staleness_limit_seconds: int = DEFAULT_STALENESS_LIMIT_SECONDS,
    enforce_staleness: bool = True,
) -> EffectiveRosterResult:
    """Derive the effective duty roster for a vehicle at target_time (SPEC 15.2, 15.3, CND-2, CND-3, CND-7).

    Rules:
    1. The candidate scheduled roster is the most recently recorded RosterSnapshot covering target_time.
    2. Fleet-wide snapshots (vehicle_id IS NULL or '') cover all vehicles unless overridden by a vehicle-specific snapshot.
    3. If provider's latest snapshot covering target_time is UNAVAILABLE, or if an UNAVAILABLE snapshot was ingested
       subsequently, status fails closed to UNAVAILABLE (Finding 1).
    4. If enforce_staleness is True and evaluation_time - fetched_at > staleness_limit, status is STALE (HUM-3, CND-3).
       When re-evaluating an existing waiver (enforce_staleness=False, CND-7), aged but unchanged data stays SCHEDULED.
    5. Session roster corrections for the vehicle covering target_time overlay scheduled roster.
    6. Corrections superseded via resolves_correction are excluded from conflicting sets (Finding 3).
    7. If multiple active session corrections disagree on membership, status is CONFLICT (fails closed, AC-23).
    8. If corrections agree, the most recent correction by server recorded_at governs (OQ-44).
    """
    if target_time.tzinfo is None:
        target_time = target_time.replace(tzinfo=UTC)

    eval_clock = evaluation_time or timezone.now()
    if eval_clock.tzinfo is None:
        eval_clock = eval_clock.replace(tzinfo=UTC)

    # 1. Query candidate scheduled snapshot across applicable scopes (vehicle-specific and fleet-wide),
    # ordered by latest fetched_at and recorded_at so newer fleet-wide or vehicle-specific refreshes
    # supersede older snapshots (Finding 2). Exclude SESSION_ACTUAL snapshots from scheduled lookup.
    base_scheduled_qs = RosterSnapshot.objects.filter(
        organization=organization,
        shift_start__lte=target_time,
        shift_end__gt=target_time,
    ).exclude(provider="SESSION_ACTUAL")

    if vehicle_id:
        base_scheduled_qs = base_scheduled_qs.filter(
            models.Q(vehicle_id=vehicle_id)
            | models.Q(vehicle_id__isnull=True)
            | models.Q(vehicle_id="")
        )
    else:
        base_scheduled_qs = base_scheduled_qs.filter(
            models.Q(vehicle_id__isnull=True) | models.Q(vehicle_id="")
        )

    candidate_snapshot = (
        base_scheduled_qs.select_related("command")
        .prefetch_related("members")
        .order_by("-fetched_at", "-recorded_at")
        .first()
    )

    scheduled_status = RosterStatus.SCHEDULED
    scheduled_members = frozenset()

    if candidate_snapshot is None:
        scheduled_status = RosterStatus.UNAVAILABLE
    elif candidate_snapshot.availability == "UNAVAILABLE":
        scheduled_status = RosterStatus.UNAVAILABLE
    else:
        # Check if a subsequent scheduled refresh batch or UNAVAILABLE snapshot superseded this candidate
        # (including nonempty refreshes that removed the shift at target_time; Codex Finding 1).
        cand_rb = (
            (candidate_snapshot.command.response_body or {})
            if candidate_snapshot.command_id
            else {}
        )
        cand_batch_id = cand_rb.get("batch_id")

        newer_qs = (
            RosterSnapshot.objects.filter(organization=organization)
            .exclude(provider="SESSION_ACTUAL")
            .filter(
                models.Q(fetched_at__gt=candidate_snapshot.fetched_at)
                | models.Q(
                    fetched_at=candidate_snapshot.fetched_at,
                    recorded_at__gt=candidate_snapshot.recorded_at,
                )
            )
            .select_related("command")
            .order_by("-fetched_at", "-recorded_at")
        )
        if vehicle_id:
            newer_qs = newer_qs.filter(
                models.Q(vehicle_id=vehicle_id)
                | models.Q(vehicle_id__isnull=True)
                | models.Q(vehicle_id="")
                | models.Q(command__response_body__scope_vehicle_id="")
                | models.Q(command__response_body__scope_vehicle_id=str(vehicle_id))
            )
        else:
            newer_qs = newer_qs.filter(
                models.Q(vehicle_id__isnull=True)
                | models.Q(vehicle_id="")
                | models.Q(command__response_body__scope_vehicle_id="")
            )

        superseded_by_newer_batch = None
        superseded_unavailable = False
        for ns in newer_qs:
            if ns.availability == "UNAVAILABLE":
                ns_veh = ns.vehicle_id or ""
                applies_to_vehicle = ns_veh in ("", str(vehicle_id)) if vehicle_id else ns_veh == ""
                if applies_to_vehicle and ns.shift_start <= target_time < ns.shift_end:
                    superseded_unavailable = True
                    break
                continue
            ns_rb = (ns.command.response_body or {}) if ns.command_id else {}
            scope_veh = (
                str(ns_rb.get("scope_vehicle_id") or "")
                if "scope_vehicle_id" in ns_rb
                else str(ns.vehicle_id or "")
            )
            scope_covers_vehicle = (
                scope_veh in ("", str(vehicle_id)) if vehicle_id else scope_veh == ""
            )
            if not scope_covers_vehicle:
                continue
            ns_batch_id = ns_rb.get("batch_id")
            if ns_batch_id and ns_batch_id == cand_batch_id:
                continue
            iv_start_str = ns_rb.get("interval_start")
            iv_end_str = ns_rb.get("interval_end")
            if iv_start_str and iv_end_str:
                try:
                    iv_start = datetime.fromisoformat(iv_start_str)
                    iv_end = datetime.fromisoformat(iv_end_str)
                    if iv_start.tzinfo is None:
                        iv_start = iv_start.replace(tzinfo=UTC)
                    if iv_end.tzinfo is None:
                        iv_end = iv_end.replace(tzinfo=UTC)
                except (ValueError, TypeError):
                    superseded_unavailable = True
                    break
                if iv_start <= target_time < iv_end:
                    superseded_by_newer_batch = ns
                    break

        if superseded_unavailable:
            scheduled_status = RosterStatus.UNAVAILABLE
        elif superseded_by_newer_batch is not None:
            # A newer authoritative refresh covered target_time and removed this shift.
            fetched_at = superseded_by_newer_batch.fetched_at
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=UTC)
            age_seconds = (eval_clock - fetched_at).total_seconds()
            if enforce_staleness and age_seconds > staleness_limit_seconds:
                scheduled_status = RosterStatus.STALE
            else:
                scheduled_status = RosterStatus.SCHEDULED
                scheduled_members = frozenset()
            candidate_snapshot = None
        else:
            # Staleness check against eval_clock (now minus fetched_at per HUM-3, CND-7)
            fetched_at = candidate_snapshot.fetched_at
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=UTC)
            age_seconds = (eval_clock - fetched_at).total_seconds()
            if enforce_staleness and age_seconds > staleness_limit_seconds:
                scheduled_status = RosterStatus.STALE
            else:
                scheduled_members = frozenset(
                    candidate_snapshot.members.values_list("person_identity", flat=True)
                )

    # 2. Query session roster corrections covering target_time using strict half-open [covers_from, covers_until)
    corrections_qs = (
        SessionRosterCorrection.objects.filter(
            organization=organization,
            covers_from__lte=target_time,
            covers_until__gt=target_time,
        )
        .select_related("snapshot")
        .prefetch_related("snapshot__members")
        .order_by("-recorded_at")
    )
    if vehicle_id:
        corrections_qs = corrections_qs.filter(vehicle_id=vehicle_id)

    corrections = list(corrections_qs)

    if not corrections:
        if scheduled_status == RosterStatus.SCHEDULED:
            return EffectiveRosterResult(
                status=RosterStatus.SCHEDULED,
                members=scheduled_members,
                scheduled_snapshot=candidate_snapshot,
            )
        return EffectiveRosterResult(
            status=scheduled_status,
            members=frozenset(),
            scheduled_snapshot=candidate_snapshot,
        )

    # Filter out superseded corrections pointed to by resolves_correction (Finding 3, Claude Finding 4).
    # First pick the latest correction per session so an earlier correction in the same session
    # cannot be revived when the session's latest correction is resolved.
    resolved_qs = SessionRosterCorrection.objects.filter(
        organization=organization,
        resolves_correction__isnull=False,
    )
    if vehicle_id:
        resolved_qs = resolved_qs.filter(vehicle_id=vehicle_id)
    resolved_ids = {
        str(rid) for rid in resolved_qs.values_list("resolves_correction_id", flat=True)
    }

    latest_by_session: dict[str, SessionRosterCorrection] = {}
    for corr in corrections:
        if corr.session_id not in latest_by_session:
            latest_by_session[corr.session_id] = corr

    active_corrections = [
        corr for corr in latest_by_session.values() if str(corr.id) not in resolved_ids
    ]
    if not active_corrections:
        # If all corrections were resolved, fall back to scheduled
        if scheduled_status == RosterStatus.SCHEDULED:
            return EffectiveRosterResult(
                status=RosterStatus.SCHEDULED,
                members=scheduled_members,
                scheduled_snapshot=candidate_snapshot,
            )
        return EffectiveRosterResult(
            status=scheduled_status,
            members=frozenset(),
            scheduled_snapshot=candidate_snapshot,
        )

    # Check for disagreement between active sessions
    members_by_correction = {
        corr: (
            frozenset(corr.snapshot.members.values_list("person_identity", flat=True))
            if corr.snapshot
            else frozenset()
        )
        for corr in active_corrections
    }

    distinct_member_sets = {frozenset(s) for s in members_by_correction.values()}
    if len(distinct_member_sets) > 1:
        # Concurrent sessions conflict on roster membership!
        return EffectiveRosterResult(
            status=RosterStatus.CONFLICT,
            members=frozenset(),
            scheduled_snapshot=candidate_snapshot,
            conflicting_corrections=tuple(active_corrections),
        )

    # All active session corrections agree; pick the latest by recorded_at
    winning_correction = max(active_corrections, key=lambda c: c.recorded_at)
    return EffectiveRosterResult(
        status=RosterStatus.CORRECTED,
        members=members_by_correction[winning_correction],
        scheduled_snapshot=candidate_snapshot,
        active_correction=winning_correction,
    )


def evaluate_carry_over_rule(
    organization,
    vehicle_id: str,
    item_id: str,
    due_at: datetime,
    rule: ConditionalRule,
    prior_check_lookup: Any = _UNSET,
    *,
    early_at: datetime | None = None,
    grace_at: datetime | None = None,
    evaluation_time: datetime | None = None,
    staleness_limit_seconds: int = DEFAULT_STALENESS_LIMIT_SECONDS,
    re_evaluating_existing_waiver: bool = False,
    prior_shift_bounds: tuple[datetime, datetime] | None = None,
    stored_prior_check_at: datetime | None = None,
) -> CarryOverEvaluation:
    """Evaluate CARRY_OVER_FROM_PRIOR_SHIFT rule for an obligation at due_at (SPEC 5.6, CND-1..CND-7).

    Conditions:
    (a) Every person on the effective roster at D was also on the effective roster for the
        immediately preceding shift that ends at D. If ANY person begins duty at D, condition fails.
    (b) A qualifying, valid, non-invalidated check for the same item was completed during that preceding shift.
    Fail-closed: For new determinations (re_evaluating_existing_waiver=False), if roster is UNAVAILABLE,
    STALE, or in CONFLICT, the obligation is required (HUM-3, CND-3). When re-evaluating an existing
    waiver (re_evaluating_existing_waiver=True, CND-7), aged but unchanged roster data does not reinstate
    the waiver; only a roster change, unavailability, conflict, or invalidated/non-qualifying check does.
    """
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=UTC)

    effective_early = None
    if early_at is not None:
        effective_early = early_at if early_at.tzinfo else early_at.replace(tzinfo=UTC)

    effective_grace = grace_at or (due_at + timedelta(hours=2))
    if effective_grace.tzinfo is None:
        effective_grace = effective_grace.replace(tzinfo=UTC)

    eval_clock = evaluation_time or timezone.now()
    if eval_clock.tzinfo is None:
        eval_clock = eval_clock.replace(tzinfo=UTC)

    enforce_staleness = not re_evaluating_existing_waiver

    # 1. Effective roster at due_at (evaluating with eval_clock per Finding 2 and CND-7)
    current_roster = derive_effective_roster(
        organization,
        vehicle_id,
        due_at,
        evaluation_time=eval_clock,
        staleness_limit_seconds=staleness_limit_seconds,
        enforce_staleness=enforce_staleness,
    )

    # Validate that rule is a governing CARRY_OVER_FROM_PRIOR_SHIFT rule effective at due_at (Claude Finding 3)
    rule_effective_at = rule.effective_at if rule is not None else None
    if rule_effective_at is not None and rule_effective_at.tzinfo is None:
        rule_effective_at = rule_effective_at.replace(tzinfo=UTC)

    if (
        rule is None
        or rule.organization_id != organization.pk
        or rule.rule_type != "CARRY_OVER_FROM_PRIOR_SHIFT"
        or rule_effective_at is None
        or rule_effective_at > due_at
    ):
        return CarryOverEvaluation(
            waived=False,
            reason="Fail closed: conditional rule is not a governing CARRY_OVER_FROM_PRIOR_SHIFT rule effective at due instant.",
            current_roster=current_roster,
            evaluated_inputs={
                "rule_id": str(rule.id) if rule else None,
                "rule_revision": rule.revision if rule else None,
                "fail_closed": True,
            },
        )

    rule_item_id = (rule.typed_inputs or {}).get("item_id")
    if rule_item_id is not None and str(rule_item_id) != str(item_id):
        return CarryOverEvaluation(
            waived=False,
            reason="Fail closed: conditional rule item_id does not match evaluated item.",
            current_roster=current_roster,
            evaluated_inputs={
                "rule_id": str(rule.id),
                "rule_revision": rule.revision,
                "fail_closed": True,
            },
        )

    if not current_roster.is_available:
        reason = f"Fail closed: effective roster at due instant is {current_roster.status.lower()}."
        return CarryOverEvaluation(
            waived=False,
            reason=reason,
            current_roster=current_roster,
            evaluated_inputs={
                "rule_id": str(rule.id),
                "rule_revision": rule.revision,
                "current_status": current_roster.status,
                "fail_closed": True,
            },
        )

    if not current_roster.members:
        return CarryOverEvaluation(
            waived=False,
            reason="Fail closed: effective roster has zero members on duty.",
            current_roster=current_roster,
            evaluated_inputs={
                "rule_id": str(rule.id),
                "rule_revision": rule.revision,
                "current_members": [],
                "fail_closed": True,
            },
        )

    # 2. Immediately preceding shift ending at due_at
    prior_instant = due_at - timedelta(microseconds=1)
    prior_roster = derive_effective_roster(
        organization,
        vehicle_id,
        prior_instant,
        evaluation_time=eval_clock,
        staleness_limit_seconds=staleness_limit_seconds,
        enforce_staleness=enforce_staleness,
    )

    if not prior_roster.is_available:
        reason = f"Fail closed: prior shift roster is {prior_roster.status.lower()}."
        return CarryOverEvaluation(
            waived=False,
            reason=reason,
            current_roster=current_roster,
            prior_roster=prior_roster,
            evaluated_inputs={
                "rule_id": str(rule.id),
                "rule_revision": rule.revision,
                "prior_status": prior_roster.status,
                "fail_closed": True,
            },
        )

    # Verify that the preceding shift actually ends at due_at (Finding 3, Codex Finding 4)
    shift_start = None
    shift_end = None
    if (
        prior_roster.active_correction is not None
        and prior_roster.active_correction.covers_until == due_at
    ):
        shift_start = prior_roster.active_correction.covers_from
        shift_end = due_at
    elif (
        prior_roster.scheduled_snapshot is not None
        and prior_roster.scheduled_snapshot.shift_end == due_at
    ):
        shift_start = prior_roster.scheduled_snapshot.shift_start
        shift_end = due_at

    if shift_start is None or shift_end != due_at or shift_start >= due_at:
        return CarryOverEvaluation(
            waived=False,
            reason="Fail closed: no preceding shift ending at obligation boundary.",
            current_roster=current_roster,
            prior_roster=prior_roster,
            evaluated_inputs={
                "rule_id": str(rule.id),
                "rule_revision": rule.revision,
                "prior_status": prior_roster.status,
                "fail_closed": True,
            },
        )

    # Verify no newer UNAVAILABLE refresh overlaps any part of the dependency window [dep_start, due_at] (CND-7, Codex 4356132089)
    dep_start = (
        min(shift_start, prior_shift_bounds[0])
        if prior_shift_bounds is not None and prior_shift_bounds[0] is not None
        else shift_start
    )
    unavail_qs = RosterSnapshot.objects.filter(
        organization=organization,
        availability="UNAVAILABLE",
        shift_start__lte=due_at,
        shift_end__gt=dep_start,
    )
    if vehicle_id:
        unavail_qs = unavail_qs.filter(
            models.Q(vehicle_id=vehicle_id)
            | models.Q(vehicle_id__isnull=True)
            | models.Q(vehicle_id="")
        )
    else:
        unavail_qs = unavail_qs.filter(models.Q(vehicle_id__isnull=True) | models.Q(vehicle_id=""))
    for u_snap in unavail_qs.order_by("-fetched_at", "-recorded_at"):
        newer_avail_qs = (
            RosterSnapshot.objects.filter(
                organization=organization,
                availability="AVAILABLE",
                shift_start__lt=u_snap.shift_end,
                shift_end__gt=u_snap.shift_start,
            )
            .exclude(provider="SESSION_ACTUAL")
            .filter(
                models.Q(fetched_at__gt=u_snap.fetched_at)
                | models.Q(
                    fetched_at=u_snap.fetched_at,
                    recorded_at__gt=u_snap.recorded_at,
                )
            )
        )
        if vehicle_id:
            newer_avail_qs = newer_avail_qs.filter(
                models.Q(vehicle_id=vehicle_id)
                | models.Q(vehicle_id__isnull=True)
                | models.Q(vehicle_id="")
            )
        else:
            newer_avail_qs = newer_avail_qs.filter(
                models.Q(vehicle_id__isnull=True) | models.Q(vehicle_id="")
            )
        avail_snaps = list(newer_avail_qs.order_by("shift_start", "shift_end"))
        fully_recovered = bool(avail_snaps)
        seg_start = max(dep_start, u_snap.shift_start)
        seg_end = min(due_at, u_snap.shift_end)
        if fully_recovered and seg_start < seg_end:
            covered_until = seg_start
            for avail_snap in avail_snaps:
                if avail_snap.shift_end <= covered_until:
                    continue
                if avail_snap.shift_start > covered_until:
                    break
                covered_until = avail_snap.shift_end
                if covered_until >= seg_end:
                    break
            if covered_until < seg_end:
                fully_recovered = False
        if fully_recovered and u_snap.shift_start <= due_at < u_snap.shift_end:
            if not any(
                avail_snap.shift_start <= due_at < avail_snap.shift_end
                for avail_snap in avail_snaps
            ):
                fully_recovered = False
        if not fully_recovered:
            unavail_prior = EffectiveRosterResult(
                status=RosterStatus.UNAVAILABLE,
                members=frozenset(),
                scheduled_snapshot=u_snap,
            )
            return CarryOverEvaluation(
                waived=False,
                reason="Fail closed: prior shift roster is unavailable.",
                current_roster=current_roster,
                prior_roster=unavail_prior,
                evaluated_inputs={
                    "rule_id": str(rule.id),
                    "rule_revision": rule.revision,
                    "prior_status": RosterStatus.UNAVAILABLE,
                    "fail_closed": True,
                },
            )

    # 3. Check condition (a): current crew subset of prior crew
    new_crew_members = current_roster.members - prior_roster.members
    if new_crew_members:
        reason = f"Condition unmet: crew member(s) {sorted(new_crew_members)} began duty at shift boundary."
        return CarryOverEvaluation(
            waived=False,
            reason=reason,
            current_roster=current_roster,
            prior_roster=prior_roster,
            evaluated_inputs={
                "rule_id": str(rule.id),
                "rule_revision": rule.revision,
                "current_members": sorted(current_roster.members),
                "prior_members": sorted(prior_roster.members),
                "new_crew_members": sorted(new_crew_members),
                "condition_a_satisfied": False,
            },
        )

    # 4. Check condition (b): qualifying valid passed check during preceding shift
    if prior_check_lookup is None and not re_evaluating_existing_waiver:
        if getattr(settings, "CYA_PRIOR_CHECK_LOOKUP", None) is None:
            return CarryOverEvaluation(
                waived=False,
                reason="Fail closed: prior check lookup collaborator is unavailable.",
                current_roster=current_roster,
                prior_roster=prior_roster,
                evaluated_inputs={
                    "rule_id": str(rule.id),
                    "rule_revision": rule.revision,
                    "current_members": sorted(current_roster.members),
                    "prior_members": sorted(prior_roster.members),
                    "condition_a_satisfied": True,
                    "condition_b_satisfied": False,
                    "fail_closed": True,
                },
            )

    try:
        collab_lookup = resolve_prior_check_lookup(prior_check_lookup)
        qualifying_check = collab_lookup(organization, vehicle_id, item_id, shift_start, due_at)
    except PriorCheckLookupUnavailable:
        if re_evaluating_existing_waiver:
            raise
        return CarryOverEvaluation(
            waived=False,
            reason="Fail closed: prior check lookup collaborator is unavailable.",
            current_roster=current_roster,
            prior_roster=prior_roster,
            evaluated_inputs={
                "rule_id": str(rule.id),
                "rule_revision": rule.revision,
                "current_members": sorted(current_roster.members),
                "prior_members": sorted(prior_roster.members),
                "condition_a_satisfied": True,
                "condition_b_satisfied": False,
                "fail_closed": True,
            },
        )
    except Exception as exc:
        if re_evaluating_existing_waiver:
            raise PriorCheckLookupUnavailable(
                "prior check lookup collaborator failed during waiver re-evaluation."
            ) from exc
        return CarryOverEvaluation(
            waived=False,
            reason="Fail closed: prior check lookup collaborator is unavailable.",
            current_roster=current_roster,
            prior_roster=prior_roster,
            evaluated_inputs={
                "rule_id": str(rule.id),
                "rule_revision": rule.revision,
                "current_members": sorted(current_roster.members),
                "prior_members": sorted(prior_roster.members),
                "condition_a_satisfied": True,
                "condition_b_satisfied": False,
                "fail_closed": True,
            },
        )

    check_id = qualifying_check.get("id") if isinstance(qualifying_check, dict) else None
    check_passed = (
        isinstance(qualifying_check, dict)
        and qualifying_check.get("passed") is True
        and not qualifying_check.get("invalidated")
        and bool(check_id)
    )
    check_ts = None
    if isinstance(qualifying_check, dict):
        raw_ts = (
            qualifying_check.get("checked_at")
            or qualifying_check.get("recorded_at")
            or qualifying_check.get("completed_at")
            or qualifying_check.get("performed_at")
        )
        if isinstance(raw_ts, datetime):
            check_ts = raw_ts if raw_ts.tzinfo else raw_ts.replace(tzinfo=UTC)
        elif isinstance(raw_ts, str):
            try:
                parsed_ts = datetime.fromisoformat(raw_ts)
                check_ts = parsed_ts if parsed_ts.tzinfo else parsed_ts.replace(tzinfo=UTC)
            except (ValueError, TypeError):
                check_passed = False
    if check_ts is None and stored_prior_check_at is not None:
        check_ts = stored_prior_check_at
    if check_ts is not None and not (shift_start <= check_ts < due_at):
        check_passed = False
    if check_ts is None and prior_shift_bounds is not None:
        old_start, old_end = prior_shift_bounds
        if shift_start != old_start or due_at != old_end:
            check_passed = False

    if not check_passed:
        reason = "Condition unmet: no qualifying valid passed check during preceding shift."
        return CarryOverEvaluation(
            waived=False,
            reason=reason,
            current_roster=current_roster,
            prior_roster=prior_roster,
            prior_check_id=str(check_id) if check_id else None,
            evaluated_inputs={
                "rule_id": str(rule.id),
                "rule_revision": rule.revision,
                "current_members": sorted(current_roster.members),
                "prior_members": sorted(prior_roster.members),
                "condition_a_satisfied": True,
                "condition_b_satisfied": False,
                "prior_check_found": bool(qualifying_check),
            },
        )

    # 5. Both conditions met -> WAIVED!
    reason = (
        f"Waived by rule CARRY_OVER_FROM_PRIOR_SHIFT: all current crew {sorted(current_roster.members)} "
        f"carried over from prior shift and check {check_id} passed."
    )
    return CarryOverEvaluation(
        waived=True,
        reason=reason,
        current_roster=current_roster,
        prior_roster=prior_roster,
        prior_check_id=str(check_id),
        evaluated_inputs={
            "rule_id": str(rule.id),
            "rule_revision": rule.revision,
            "vehicle_id": str(vehicle_id),
            "item_id": str(item_id),
            **({"early_at": effective_early.isoformat()} if effective_early is not None else {}),
            "due_at": due_at.isoformat(),
            "grace_at": effective_grace.isoformat(),
            "target_time": due_at.isoformat(),
            "prior_shift_start": shift_start.isoformat(),
            "prior_shift_end": due_at.isoformat(),
            "current_members": sorted(current_roster.members),
            "prior_members": sorted(prior_roster.members),
            "condition_a_satisfied": True,
            "condition_b_satisfied": True,
            "prior_check_id": str(check_id),
            "prior_check_at": check_ts.isoformat() if check_ts else None,
            "snapshot_id": str(current_roster.scheduled_snapshot.id)
            if current_roster.scheduled_snapshot
            else None,
            "snapshot_fetched_at": current_roster.scheduled_snapshot.fetched_at.isoformat()
            if current_roster.scheduled_snapshot
            else None,
            "snapshot_provider": current_roster.scheduled_snapshot.provider
            if current_roster.scheduled_snapshot
            else None,
            "correction_id": str(current_roster.active_correction.id)
            if current_roster.active_correction
            else None,
            "prior_snapshot_id": str(prior_roster.scheduled_snapshot.id)
            if prior_roster.scheduled_snapshot
            else None,
            "prior_correction_id": str(prior_roster.active_correction.id)
            if prior_roster.active_correction
            else None,
        },
    )


def record_obligation_determination(
    *,
    organization,
    obligation_id: str,
    determination_type: str,
    vehicle_id: str | None = None,
    due_at: datetime | None = None,
    rule: ConditionalRule | None = None,
    prior_determination: ObligationDetermination | None = None,
    explanation: str | None = None,
    evaluated_inputs: dict[str, Any] | None = None,
    actor_user=None,
    actor_roles_at_time: list[str] | None = None,
    service_identity: str | None = None,
    job_key: str | list[str] | None = None,
    operation_id: str | None = None,
    correlation_id: str | None = None,
) -> ObligationDetermination:
    """Persist an immutable obligation determination event wrapped in CommandReceipt with replay support (Finding 6)."""
    org_id = str(organization.pk)
    determination_id = new_id()
    correlation_id = correlation_id or new_id()
    attempt_id = new_id()

    target_vehicle_id = vehicle_id
    if target_vehicle_id is None and evaluated_inputs:
        target_vehicle_id = evaluated_inputs.get("vehicle_id")

    target_due_at = due_at
    if target_due_at is None and evaluated_inputs:
        d_val = evaluated_inputs.get("due_at") or evaluated_inputs.get("target_time")
        if isinstance(d_val, str):
            try:
                target_due_at = datetime.fromisoformat(d_val)
                if target_due_at.tzinfo is None:
                    target_due_at = target_due_at.replace(tzinfo=UTC)
            except Exception:
                pass
        elif isinstance(d_val, datetime):
            target_due_at = d_val if d_val.tzinfo else d_val.replace(tzinfo=UTC)

    if actor_user is not None:
        op_id = operation_id or new_id()
        actor = Actor(user=actor_user, roles=tuple(actor_roles_at_time or []))
    else:
        svc = service_identity or "worker.obligation_evaluator"
        if job_key is None:
            raw_key = [
                "obligation-determination",
                str(obligation_id),
                determination_type,
                str(rule.id) if rule else "",
                str(rule.revision) if rule else "",
            ]
            jk = json.dumps(raw_key, ensure_ascii=True, separators=(",", ":"))
        elif isinstance(job_key, (list, tuple)):
            jk = json.dumps(list(job_key), ensure_ascii=True, separators=(",", ":"))
        else:
            jk = str(job_key)
        op_id = operation_id or str(
            worker_operation_id(org_id, svc, "RECORD_OBLIGATION_DETERMINATION", jk)
        )
        actor = Actor(service_identity=svc, job_key=jk)

    digest_payload = {
        "obligation_id": str(obligation_id),
        "type": determination_type,
        "rule_id": str(rule.id) if rule else None,
        "inputs": evaluated_inputs or {},
    }
    request_digest = hashlib.sha256(json.dumps(digest_payload, sort_keys=True).encode()).hexdigest()

    def authorize(org):
        if not org.active:
            raise CommandDenied()
        if actor_user is not None:
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
                # Evaluator decisions and UTI completion/invalidation have dedicated commands.
                if determination_type in (
                    "WAIVED",
                    "REINSTATED",
                    "SUPERVISOR_COMPLETED",
                    "INVALIDATED",
                ):
                    raise CommandDenied(act)
                if Role.SUPERVISOR not in roles and Role.ADMINISTRATOR not in roles:
                    raise CommandDenied(act)
                return act
        if determination_type in ("SUPERVISOR_COMPLETED", "INVALIDATED"):
            raise CommandDenied(actor)
        return actor

    def prepare(act):
        def apply(receipt, current):
            return ObligationDetermination.objects.create(
                id=determination_id,
                organization=organization,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=act.user,
                actor_roles_at_time=list(act.roles),
                service_identity=act.service_identity,
                job_key=act.job_key,
                obligation_id=obligation_id,
                vehicle_id=target_vehicle_id,
                due_at=target_due_at,
                determination_type=determination_type,
                rule=rule,
                prior_determination=prior_determination,
                explanation=explanation,
                evaluated_inputs=evaluated_inputs or {},
            )

        return Plan(
            response_body={
                "determination_id": determination_id,
                "determination_type": determination_type,
            },
            apply=apply,
            response_status=201,
            new_snapshot={"schema_version": 1, "values": evaluated_inputs or {}},
        )

    result = run_command(
        organization_id=org_id,
        operation_id=op_id,
        command_type="RECORD_OBLIGATION_DETERMINATION",
        request_digest=request_digest,
        correlation_id=correlation_id,
        request_attempt_id=attempt_id,
        authorize=authorize,
        prepare=prepare,
    )

    if result.replayed:
        return ObligationDetermination.objects.get(
            organization=organization, command__operation_id=op_id
        )

    return ObligationDetermination.objects.get(id=determination_id)


def check_and_apply_reinstatement(
    *,
    organization,
    obligation_id: str,
    vehicle_id: str,
    item_id: str,
    due_at: datetime,
    grace_at: datetime,
    rule: ConditionalRule,
    early_at: datetime | None = None,
    prior_check_lookup: Any = _UNSET,
    current_time: datetime | None = None,
    service_identity: str = "worker.reinstatement_evaluator",
    job_key: str | list[str] | None = None,
) -> ObligationDetermination | None:
    """Re-evaluate an obligation previously waived; if waiver is invalidated, append REINSTATED determination (CND-5, CND-7, OQ-37)."""
    now = current_time or timezone.now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=UTC)
    if grace_at.tzinfo is None:
        grace_at = grace_at.replace(tzinfo=UTC)

    # Find latest determination for this obligation
    latest_det = latest_obligation_determination(organization, obligation_id)

    if latest_det is None or latest_det.determination_type != "WAIVED":
        # Only WAIVED determinations can be reinstated
        return None

    stored_inputs = dict(latest_det.evaluated_inputs or {})
    parsed_early_at = None
    if early_at is not None:
        parsed_early_at = early_at if early_at.tzinfo else early_at.replace(tzinfo=UTC)
    elif stored_inputs.get("early_at"):
        try:
            ea = datetime.fromisoformat(stored_inputs["early_at"])
            parsed_early_at = ea if ea.tzinfo else ea.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            parsed_early_at = None
    prior_check_id = stored_inputs.get("prior_check_id")
    cond_b_ok = stored_inputs.get("condition_b_satisfied") is True
    check_invalidated = stored_inputs.get("prior_check_invalidated") is True
    parsed_stored_shift_start = None
    parsed_stored_shift_end = None
    parsed_stored_check_at = None
    if stored_inputs.get("prior_shift_start"):
        try:
            ps = datetime.fromisoformat(stored_inputs["prior_shift_start"])
            parsed_stored_shift_start = ps if ps.tzinfo else ps.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            parsed_stored_shift_start = None
    if stored_inputs.get("prior_shift_end"):
        try:
            pe = datetime.fromisoformat(stored_inputs["prior_shift_end"])
            parsed_stored_shift_end = pe if pe.tzinfo else pe.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            parsed_stored_shift_end = None
    stored_check_at_str = stored_inputs.get("prior_check_at") or stored_inputs.get(
        "prior_check_checked_at"
    )
    if stored_check_at_str:
        try:
            pc = datetime.fromisoformat(stored_check_at_str)
            parsed_stored_check_at = pc if pc.tzinfo else pc.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            parsed_stored_check_at = None

    if (
        not cond_b_ok
        or check_invalidated
        or not prior_check_id
        or parsed_stored_shift_start is None
        or parsed_stored_shift_end is None
        or parsed_stored_shift_end != due_at
    ):
        should_reinstate = True
        eval_reason = "Fail closed: existing waiver has incomplete initial evaluation evidence."
        evaluated_inputs = dict(stored_inputs)
        evaluated_inputs.update(
            {
                "condition_b_satisfied": False,
                "fail_closed": True,
            }
        )
    else:
        prior_bounds = (parsed_stored_shift_start, parsed_stored_shift_end)
        evaluation = evaluate_carry_over_rule(
            organization=organization,
            vehicle_id=vehicle_id,
            item_id=item_id,
            due_at=due_at,
            rule=rule,
            prior_check_lookup=prior_check_lookup,
            evaluation_time=now,
            re_evaluating_existing_waiver=True,
            prior_shift_bounds=prior_bounds,
            stored_prior_check_at=parsed_stored_check_at,
        )
        should_reinstate = not evaluation.waived
        eval_reason = evaluation.reason
        evaluated_inputs = dict(stored_inputs)
        evaluated_inputs.update(dict(evaluation.evaluated_inputs or {}))

    if should_reinstate:
        # Waiver is no longer valid! Reinstatement is required.
        explanation = f"Waiver invalidated by roster update: {eval_reason}"
        current_qualifying_check = _find_qualifying_current_obligation_check(
            organization,
            obligation_id=str(obligation_id),
            vehicle_id=str(vehicle_id),
            item_id=str(item_id),
            due_at=due_at,
            grace_at=grace_at,
            evaluation_time=now,
            prior_check_id=str(prior_check_id) if prior_check_id else None,
            early_at=parsed_early_at,
            prior_check_lookup=prior_check_lookup,
        )
        already_satisfied = current_qualifying_check is not None
        is_overdue = (now > grace_at) and not already_satisfied
        evaluated_inputs["reinstatement_at"] = now.isoformat()
        evaluated_inputs["obligation_satisfied"] = already_satisfied
        evaluated_inputs["satisfied_by_check_id"] = (
            str(current_qualifying_check["id"]) if current_qualifying_check else None
        )
        if current_qualifying_check is not None:
            evaluated_inputs["inspection_outcome"] = current_qualifying_check.get(
                "outcome",
                "PASSED" if current_qualifying_check.get("passed") is True else "FAILED",
            )
        evaluated_inputs["is_overdue"] = is_overdue
        evaluated_inputs["overdue_from_grace"] = is_overdue
        evaluated_inputs["grace_at"] = grace_at.isoformat()
        evaluated_inputs["vehicle_id"] = vehicle_id
        if is_overdue:
            evaluated_inputs["overdue_from"] = grace_at.isoformat()
            explanation += (
                f" Obligation is overdue from grace deadline {grace_at.isoformat()} (CND-5, OQ-37)."
            )
        else:
            evaluated_inputs.pop("overdue_from", None)

        if job_key is None:
            job_key = [
                "reinstatement",
                str(obligation_id),
                str(latest_det.id),
                now.isoformat(),
            ]

        return record_obligation_determination(
            organization=organization,
            obligation_id=obligation_id,
            determination_type="REINSTATED",
            rule=rule,
            prior_determination=latest_det,
            explanation=explanation,
            evaluated_inputs=evaluated_inputs,
            service_identity=service_identity,
            job_key=job_key,
        )

    return None
