"""Generate and reconcile obligations, idempotently and restart-safely (SPEC 5.7, 5.8; TIME-1).

The unit of work is one vehicle and one due instant D. reconcile_unit computes the writes the
unit needs (cya.obligations.planning), then commits them in one audited worker command:

1. Preview the plan without locks and derive the command's job key from the plan itself. The
   key names the business change (vehicle, D and the exact rows to write), not the wake time,
   so a retry after a crash reuses it and a rerun that has nothing to do writes nothing.
2. Inside the command, lock the vehicle row and then its templates (the lock that a template
   publication also takes), recompute the plan and compare. If a publication, a status change
   or another run got in first, nothing is written and the unit is planned again (bounded).
3. Append requirement versions, SUPERSEDED determinations, obligations and selection events.
   A failure anywhere rolls the whole unit back; the next run repeats it.

Generation (run_generation) and publication (reconcile_publication) call the same function,
so an old generator can never select a version that a publication has since replaced: it reads
publications only after taking the publication's own lock.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from cya.accounts.fields import new_id
from cya.checklists.models import (
    ChecklistPublication,
    ChecklistTemplate,
    Vehicle,
)
from cya.conditions.evaluator import latest_obligation_determination
from cya.conditions.models import ObligationDetermination
from cya.core.audit_commands import Actor, CommandDenied, run_command
from cya.core.audit_commands import Plan as CommandPlan
from cya.obligations.models import (
    Obligation,
    ObligationSelection,
    OdometerObligation,
    RequirementBinding,
    RequirementVersion,
)
from cya.obligations.planning import RequirementRevived, build_plan
from cya.obligations.requirements import occurrences
from cya.obligations.state import NO_KIT
from tools.command_identity import worker_operation_id

logger = logging.getLogger(__name__)

SERVICE_IDENTITY = "worker.obligation_generator"
COMMAND_TYPE = "OBLIGATIONS_RECONCILE"
MAX_ATTEMPTS = 3
MAX_HORIZON_HOURS = 168
MAX_LOOKBACK_HOURS = 744
TARGET_TYPE = "ORGANIZATION"


class PlanChanged(RuntimeError):
    """Another writer changed the unit between the preview and the locked recomputation."""


class ReconcileConflict(RuntimeError):
    """The unit kept changing under us; nothing was written and the next run will retry."""


class UnitUnavailable(ValueError):
    """The vehicle is missing, inactive or belongs to another organization."""


@dataclass(frozen=True)
class UnitResult:
    vehicle_id: str
    due_at: datetime
    status: str  # UNCHANGED, APPLIED, REPLAYED or WOULD_APPLY (dry run)
    counts: dict


@dataclass(frozen=True)
class UnitFailure:
    vehicle_id: str
    due_at: datetime
    error: str


@dataclass
class RunSummary:
    results: list = field(default_factory=list)
    failures: list = field(default_factory=list)

    def total(self, key):
        return sum(r.counts.get(key, 0) for r in self.results)

    @property
    def changed(self):
        return [r for r in self.results if r.status in ("APPLIED", "WOULD_APPLY")]


def horizon_hours():
    return int(getattr(settings, "CYA_OBLIGATION_HORIZON_HOURS", 48))


def lookback_hours():
    return int(getattr(settings, "CYA_OBLIGATION_LOOKBACK_HOURS", 168))


def _instant(value):
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def job_key_for(plan):
    """Compact JSON array of canonical strings naming this unit's exact change."""
    return json.dumps(
        ["obligation-unit", plan.vehicle_id, _instant(plan.due_at), plan.digest],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def lock_unit_scope(organization_id, vehicle_id):
    """Lock the vehicle, then every template bound to it, in a fixed order.

    The order matches the override publisher (vehicle) and the template publisher (template),
    which never take the two in the opposite order, so the locks cannot deadlock. Holding the
    template lock is what makes the later publication read current.
    """
    try:
        vehicle = Vehicle.objects.select_for_update().get(
            organization_id=organization_id, pk=vehicle_id
        )
    except Vehicle.DoesNotExist:
        raise UnitUnavailable("vehicle not found in this organization") from None
    if not vehicle.active:
        raise UnitUnavailable("vehicle is not active")
    template_ids = sorted(
        {
            str(t)
            for t in RequirementBinding.objects.filter(
                organization_id=organization_id, vehicle_id=vehicle_id
            ).values_list("template_id", flat=True)
        }
    )
    list(
        ChecklistTemplate.objects.select_for_update()
        .filter(organization_id=organization_id, pk__in=template_ids)
        .order_by("pk")
    )
    return vehicle


def _evidence(receipt, actor):
    return {
        "command": receipt,
        "recorded_at": receipt.recorded_at,
        "actor_user": None,
        "actor_roles_at_time": [],
        "service_identity": actor.service_identity,
        "job_key": actor.job_key,
    }


def _ensure_requirement_versions(plan, evidence):
    wanted = {a.desired.requirement_id: a.desired for a in plan.actions if a.desired}
    have = set(
        RequirementVersion.objects.filter(
            organization_id=plan.organization_id, pk__in=list(wanted)
        ).values_list("pk", flat=True)
    )
    RequirementVersion.objects.bulk_create(
        RequirementVersion(
            id=rid,
            organization_id=plan.organization_id,
            vehicle_id=plan.vehicle_id,
            item_id=d.item_id,
            item_def_id=d.item_def_id,
            publication_id=d.publication_id,
            override_version_id=d.override_version_id,
            binding_id=d.binding_id,
            schedule_rule_id=d.schedule_rule_id,
            conditional_rule_id=d.conditional_rule_id,
            kit_instance_id=d.kit_id,
            kit_key=d.kit_id or NO_KIT,
            kit_event_id=d.kit_event_id,
            **evidence,
        )
        for rid, d in wanted.items()
        if rid not in have
    )


def _supersede(plan, action, evidence, trigger, publication_id):
    """Append the SUPERSEDED determination of the obligation an event replaces or retires."""
    prior = latest_obligation_determination(
        plan.organization_id, action.old_obligation_id, for_update=True
    )
    determination_id = new_id()
    ObligationDetermination.objects.create(
        id=determination_id,
        organization_id=plan.organization_id,
        obligation_id=action.old_obligation_id,
        vehicle_id=plan.vehicle_id,
        due_at=plan.due_at,
        determination_type="SUPERSEDED",
        prior_determination=prior,
        explanation=(
            "Pre-window obligation superseded because its requirement "
            + ("changed" if action.desired else "no longer applies")
            + " before the early window opened (SPEC 5.7)."
        ),
        evaluated_inputs={
            "reason": action.reason,
            "trigger": trigger,
            "publication_id": publication_id,
            "superseded_requirement_version_id": action.old_requirement_id,
            "replacement_requirement_version_id": (
                action.desired.requirement_id if action.desired else None
            ),
        },
        **evidence,
    )
    return determination_id


def _apply(receipt, actor, plan, trigger, publication_id):
    evidence = _evidence(receipt, actor)
    _ensure_requirement_versions(plan, evidence)

    determinations = {
        a.key: _supersede(plan, a, evidence, trigger, publication_id)
        for a in plan.actions
        if a.old_obligation_id
    }
    obligations = {a.key: new_id() for a in plan.actions if a.desired is not None}
    Obligation.objects.bulk_create(
        Obligation(
            id=obligations[a.key],
            organization_id=plan.organization_id,
            vehicle_id=plan.vehicle_id,
            item_id=a.item_id,
            kit_instance_id=a.desired.kit_id,
            kit_key=a.kit_key,
            requirement_version_id=a.desired.requirement_id,
            due_at=plan.due_at,
            early_at=a.desired.early_at,
            grace_at=a.desired.grace_at,
            routine=True,
            basis_at=a.cutoff or receipt.recorded_at,
            **evidence,
        )
        for a in plan.actions
        if a.desired is not None
    )
    ObligationSelection.objects.bulk_create(
        ObligationSelection(
            id=new_id(),
            organization_id=plan.organization_id,
            vehicle_id=plan.vehicle_id,
            item_id=a.item_id,
            kit_instance_id=None if a.kit_key == NO_KIT else a.kit_key,
            kit_key=a.kit_key,
            due_at=plan.due_at,
            sequence=a.tail_sequence + 1,
            selection_type=a.kind,
            selected_obligation_id=obligations.get(a.key),
            previous_selection_id=a.tail_id,
            supersession_determination_id=determinations.get(a.key),
            **evidence,
        )
        for a in plan.actions
    )
    OdometerObligation.objects.bulk_create(
        OdometerObligation(
            id=new_id(),
            organization_id=plan.organization_id,
            vehicle_id=plan.vehicle_id,
            schedule_rule_id=o.schedule_rule_id,
            scheduled_inspection_key=o.key,
            due_at=o.due_at,
            early_at=o.early_at,
            grace_at=o.grace_at,
            **evidence,
        )
        for o in plan.odometers
    )


def _run(plan, trigger, publication_id):
    job_key = job_key_for(plan)
    organization_id = plan.organization_id

    def authorize(org):
        if not org.active:
            raise CommandDenied()
        lock_unit_scope(organization_id, plan.vehicle_id)
        return Actor(service_identity=SERVICE_IDENTITY, job_key=job_key)

    def prepare(actor):
        fresh = build_plan(organization_id, plan.vehicle_id, plan.due_at, timezone.now())
        if fresh.digest != plan.digest:
            raise PlanChanged("the unit changed between preview and lock")
        counts = fresh.counts()
        return CommandPlan(
            response_body={
                "code": "RECONCILED",
                "vehicle_id": plan.vehicle_id,
                "due_at": _instant(plan.due_at),
                "plan_digest": plan.digest,
                **counts,
            },
            apply=lambda receipt, current: _apply(receipt, current, fresh, trigger, publication_id),
            target_type=TARGET_TYPE,
            target_id=organization_id,
            new_snapshot={
                "schema_version": 1,
                "values": {
                    "vehicle_id": plan.vehicle_id,
                    "due_at": _instant(plan.due_at),
                    "trigger": trigger,
                    "publication_id": publication_id,
                    **counts,
                },
            },
        )

    return run_command(
        organization_id=organization_id,
        operation_id=str(
            worker_operation_id(organization_id, SERVICE_IDENTITY, COMMAND_TYPE, job_key)
        ),
        command_type=COMMAND_TYPE,
        request_digest=plan.digest,
        correlation_id=new_id(),
        request_attempt_id=new_id(),
        authorize=authorize,
        prepare=prepare,
    )


def reconcile_unit(
    organization_id,
    vehicle_id,
    due_at,
    *,
    trigger="GENERATION",
    publication_id=None,
    dry_run=False,
):
    """Bring one (vehicle, due instant) up to date; a unit that is current writes nothing."""
    organization_id, vehicle_id = str(organization_id), str(vehicle_id)
    for attempt in range(MAX_ATTEMPTS):
        try:
            plan = build_plan(organization_id, vehicle_id, due_at, timezone.now())
        except RequirementRevived:
            # The preview holds no lock, so another worker may have committed these very
            # obligations between its reads. A genuine revival is deterministic and survives
            # every attempt; a stale preview does not.
            if attempt == MAX_ATTEMPTS - 1:
                raise
            continue
        if plan.is_empty:
            return UnitResult(vehicle_id, due_at, "UNCHANGED", {})
        if dry_run:
            return UnitResult(vehicle_id, due_at, "WOULD_APPLY", plan.counts())
        try:
            result = _run(plan, trigger, publication_id)
        except PlanChanged:
            continue
        status = "REPLAYED" if result.replayed else "APPLIED"
        return UnitResult(vehicle_id, due_at, status, plan.counts())
    raise ReconcileConflict(f"unit {vehicle_id} {_instant(due_at)} kept changing")


def unit_dues(organization_id, vehicle_id, start, end):
    """Due instants to reconcile: scheduled ones in [start, end] and already-selected ones.

    The second set lets a schedule or binding change retire an obligation whose occurrence no
    longer exists. It includes due instants that have already passed: a change recorded before
    the early window is still applied by a late run (see cya.obligations.planning), so a unit
    whose occurrence was removed must still be visited inside the recovery interval.
    """
    scheduled = {o.due_at for o in occurrences(organization_id, vehicle_id, start, end)}
    selected = set(
        ObligationSelection.objects.filter(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            due_at__gte=start,
            due_at__lte=end,
        )
        .values_list("due_at", flat=True)
        .distinct()
    )
    return sorted(scheduled | selected)


def _active_vehicle_ids(organization_id, vehicle_ids=None):
    queryset = Vehicle.objects.filter(organization_id=organization_id, active=True)
    if vehicle_ids is not None:
        queryset = queryset.filter(pk__in=list(vehicle_ids))
    return sorted(str(v) for v in queryset.values_list("pk", flat=True))


def _reconcile_vehicles(
    organization_id, vehicle_ids, start, end, now, *, trigger, publication_id, dry_run
):
    summary = RunSummary()
    for vehicle_id in vehicle_ids:
        try:
            dues = unit_dues(organization_id, vehicle_id, start, end)
        except Exception as exc:
            summary.failures.append(UnitFailure(vehicle_id, start, _describe(exc)))
            continue
        for due_at in dues:
            try:
                summary.results.append(
                    reconcile_unit(
                        organization_id,
                        vehicle_id,
                        due_at,
                        trigger=trigger,
                        publication_id=publication_id,
                        dry_run=dry_run,
                    )
                )
            except Exception as exc:
                summary.failures.append(UnitFailure(vehicle_id, due_at, _describe(exc)))
    return summary


def _describe(exc):
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def _check_hours(value, maximum, name):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be a whole number of hours from 0 to {maximum}")
    return value


def run_generation(
    organization_id, *, vehicle_ids=None, horizon=None, lookback=None, dry_run=False
):
    """Materialize obligations from `lookback` hours ago through `horizon` hours ahead.

    Idempotent and restart-safe: every unit is its own audited command, a unit that is already
    current writes nothing, and a missed run is repaired by the next one. Failures are collected
    per unit and reported, never hidden; units that did succeed stay committed.
    """
    horizon = _check_hours(
        horizon_hours() if horizon is None else horizon, MAX_HORIZON_HOURS, "horizon"
    )
    lookback = _check_hours(
        lookback_hours() if lookback is None else lookback, MAX_LOOKBACK_HOURS, "lookback"
    )
    now = timezone.now()
    return _reconcile_vehicles(
        str(organization_id),
        _active_vehicle_ids(organization_id, vehicle_ids),
        now - timedelta(hours=lookback),
        now + timedelta(hours=horizon),
        now,
        trigger="GENERATION",
        publication_id=None,
        dry_run=dry_run,
    )


def affected_vehicle_ids(organization_id, publication):
    """Active vehicles a publication can change: its override's vehicle, or all bound to its template."""
    if publication.override_version_id is not None:
        return _active_vehicle_ids(organization_id, [publication.override_version.vehicle_id])
    template_id = publication.checklist_version.template_id
    bound = RequirementBinding.objects.filter(
        organization_id=organization_id, template_id=template_id
    ).values_list("vehicle_id", flat=True)
    return _active_vehicle_ids(organization_id, set(bound))


def reconcile_publication(organization_id, publication_id, *, horizon=None):
    """Reconcile the pre-window obligations a just-committed publication affects (SPEC 5.7).

    Runs in its own transactions after the publication committed. Whatever it misses (a crash,
    a conflict) is repaired by the next run_generation, which uses the same reconciliation.
    """
    horizon = _check_hours(
        horizon_hours() if horizon is None else horizon, MAX_HORIZON_HOURS, "horizon"
    )
    publication = ChecklistPublication.objects.select_related(
        "checklist_version", "override_version"
    ).get(organization_id=organization_id, pk=publication_id)
    now = timezone.now()
    return _reconcile_vehicles(
        str(organization_id),
        affected_vehicle_ids(organization_id, publication),
        now,
        now + timedelta(hours=horizon),
        now,
        trigger="PUBLICATION",
        publication_id=str(publication_id),
        dry_run=False,
    )


def reconcile_vehicle(organization_id, vehicle_id, *, horizon=None, trigger="BINDING"):
    """Reconcile one vehicle's future units, for example after its bindings changed."""
    horizon = _check_hours(
        horizon_hours() if horizon is None else horizon, MAX_HORIZON_HOURS, "horizon"
    )
    now = timezone.now()
    return _reconcile_vehicles(
        str(organization_id),
        _active_vehicle_ids(organization_id, [vehicle_id]),
        now,
        now + timedelta(hours=horizon),
        now,
        trigger=trigger,
        publication_id=None,
        dry_run=False,
    )
