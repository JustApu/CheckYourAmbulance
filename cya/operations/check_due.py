"""Scheduled "check due on a vehicle" notices (SPEC 13 NOT-3, 15.4 ASG-7; issue #202 part 3).

A check coming due has no event of its own, so a bounded job looks for routine obligations whose
window opened recently and sends **one notice per vehicle per due instant** (owner decision,
2026-11-18), for example "Medic 7: 14 checks due at 06:00". By default it goes to the vehicle's
current crew when the notice is written; Supervisors and Administrators can be switched on
(cya/notifications/default_recipients.json). A vehicle with nobody assigned then gets the notice
recorded with no recipients, and it is not sent again when someone is assigned later: by then Home
already shows them what is due.

Only selected routine obligations count (ad hoc inspections are ordered by a supervisor who
already knows). A vehicle out of service at the due instant is skipped, because those obligations
are exempt (SPEC 9). A window that opened more than LOOKBACK ago is not announced, so a job that
was down does not send stale notices.

Each notice is a worker command (service worker.vehicle_check_due, command
VEHICLE_CHECK_DUE_NOTICE, job key ["<vehicle id>","<due instant>"]) whose operation id is derived
from the key, so a repeat or a second worker replays without writing.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import uuid4

from django.db import DatabaseError
from django.db.models import Count, Q
from django.utils import timezone

from cya.accounts.models import Organization
from cya.authorization.persistence import digest
from cya.core.audit_commands import Actor, CommandDenied, Plan, run_command
from cya.core.models import CommandReceipt
from cya.notifications.recipients import notify
from cya.obligations.models import Obligation, ObligationProjection
from cya.operations.status import status_at
from tools.command_identity import worker_operation_id

logger = logging.getLogger(__name__)
SERVICE = "worker.vehicle_check_due"
COMMAND = "VEHICLE_CHECK_DUE_NOTICE"
EVENT = "vehicle.check_due"
SOURCE = "vehicle_check_due"
LOOKBACK = timedelta(hours=2)
PAGE = 500  # groups read per query; a pass pages on until `limit` notices are written


@dataclass
class CheckDueReport:
    notified: int = 0
    already: int = 0
    out_of_service: int = 0
    errors: int = 0
    groups: list = field(default_factory=list)


def _key(vehicle_id, due_at):
    return json.dumps([str(vehicle_id), due_at.isoformat()], separators=(",", ":"))


def _operation(organization_id, key):
    return str(worker_operation_id(str(organization_id), SERVICE, COMMAND, key))


def groups(organization_id, now, after=None):
    """One page of [(vehicle id, due instant, number of checks)] whose window opened within
    LOOKBACK, ordered by (due instant, vehicle id) and starting after the `after` group."""
    selected = ObligationProjection.objects.filter(
        organization_id=organization_id, selected=True
    ).values("obligation_id")
    rows = (
        Obligation.objects.filter(
            organization_id=organization_id,
            routine=True,
            early_at__lte=now,
            early_at__gt=now - LOOKBACK,
            grace_at__gt=now,
            vehicle__active=True,
            pk__in=selected,
        )
        .filter(
            Q()
            if after is None
            else Q(due_at__gt=after[1]) | Q(due_at=after[1], vehicle_id__gt=after[0])
        )
        .values("vehicle_id", "due_at")
        .annotate(checks=Count("pk"))
        .order_by("due_at", "vehicle_id")[:PAGE]
    )
    return [(str(r["vehicle_id"]), r["due_at"], r["checks"]) for r in rows]


def _notify_one(organization_id, vehicle_id, due_at, checks):
    key = _key(vehicle_id, due_at)
    correlation = str(uuid4())

    def authorize(org):
        org = Organization.objects.select_for_update().get(pk=org.pk)
        if not org.active:
            raise CommandDenied()
        return Actor(service_identity=SERVICE, job_key=key)

    def prepare(actor):
        values = {"vehicle_id": vehicle_id, "due_at": due_at.isoformat(), "checks": checks}

        def apply(receipt, current):
            notify(
                receipt,
                event_type=EVENT,
                source_event_type=SOURCE,
                source_event_id=receipt.pk,
                deep_link=f"/inspection/vehicles/{vehicle_id}/",
                payload=values,
                crew_vehicle_id=vehicle_id,
            )

        return Plan(
            response_body={"code": "CHECK_DUE_NOTIFIED", **values},
            apply=apply,
            target_type="ORGANIZATION",
            target_id=str(organization_id),
            new_snapshot={"schema_version": 1, "values": values},
        )

    return run_command(
        organization_id=str(organization_id),
        operation_id=_operation(organization_id, key),
        command_type=COMMAND,
        request_digest=digest({"vehicle": vehicle_id, "due_at": due_at.isoformat()}),
        correlation_id=correlation,
        request_attempt_id=correlation,
        authorize=authorize,
        prepare=prepare,
    )


def notify_check_due(organization_id, *, now=None, limit=50):
    """One pass: at most `limit` new notices, oldest due instant first; returns a report.

    Groups already notified, out of service or failing do not use up the pass: it reads further
    pages (keyset on due instant and vehicle) until `limit` notices are written or none remain,
    so a backlog beyond one page is still reached before its window ages out."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer from 1 to 500")
    now = now or timezone.now()
    report = CheckDueReport()
    after = None
    while report.notified < limit:
        found = groups(organization_id, now, after)
        if not found:
            break
        after = found[-1][:2]
        _notify_page(organization_id, found, limit, report)
        if len(found) < PAGE:
            break
    return report


def _notify_page(organization_id, found, limit, report):
    operations = {(v, d): _operation(organization_id, _key(v, d)) for v, d, _n in found}
    done = {
        str(op)
        for op in CommandReceipt.objects.filter(
            organization_id=organization_id, operation_id__in=list(operations.values())
        ).values_list("operation_id", flat=True)
    }
    for vehicle_id, due_at, checks in found:
        if report.notified >= limit:
            return
        if operations[(vehicle_id, due_at)] in done:
            report.already += 1
            continue
        status = status_at(organization_id, vehicle_id, due_at)
        if status is not None and status.status == "OUT_OF_SERVICE":
            report.out_of_service += 1
            continue
        try:
            result = _notify_one(organization_id, vehicle_id, due_at, checks)
        except DatabaseError:
            # One vehicle's failure never blocks the others; its notice stays due.
            logger.error("check-due notice unavailable for one vehicle")
            report.errors += 1
            continue
        if result.replayed:
            report.already += 1
        else:
            report.notified += 1
            report.groups.append((vehicle_id, due_at.isoformat(), checks))
