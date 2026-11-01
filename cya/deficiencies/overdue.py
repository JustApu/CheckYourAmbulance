"""Scheduled deadline-overdue notices (SPEC 13 NOT-3, DEF-5, 8.4; issue 14).

An overdue deadline has no event of its own, so a bounded job finds the deficiencies whose
governing deadline (cya.deficiencies.deadlines) has passed while they are still being worked and
notifies each once per governing deadline: the default recipients are the assignee and all
supervisors (recipients.py). An active escalation acknowledgement (SPEC 8.4) suppresses the notice;
a later pass sends it once the acknowledgement has expired, if the deficiency is still overdue. A
new governing deadline (a revision or an extension decision) is a new notice when it passes in
turn. There is no escalation beyond this single notice until the owner decides OQ-20.

Each notice is a worker command (service worker.deficiency_overdue, command
DEFICIENCY_OVERDUE_NOTICE, job key ["<deficiency id>","<governing event id>"]). Its operation id is
derived from that key, so a deficiency already notified for its governing deadline replays
without writing anything, and two workers cannot both notify. The command locks the deficiency and
checks again under the lock; a deficiency that stopped being overdue or was acknowledged meanwhile
is refused without a receipt (and counted as skipped), so a later pass can still notify it.
"""

import json
import logging
from dataclasses import dataclass, field
from uuid import uuid4

from django.db import DatabaseError
from django.db.models import OuterRef, Q, Subquery
from django.utils import timezone

from cya.accounts.models import Organization
from cya.authorization.persistence import digest
from cya.core.audit_commands import Actor, CommandDenied, Plan, run_command
from cya.core.models import CommandReceipt
from cya.deficiencies import service
from cya.deficiencies.acknowledgement import acknowledgement_state
from cya.deficiencies.models import DeficiencyEvent, DeficiencyProjection
from cya.deficiencies.notices import deficiency_link, people
from cya.deficiencies.service import DeficiencyError
from cya.deficiencies.transitions import ACTIVE_STATUSES
from cya.notifications.recipients import notify
from tools.command_identity import worker_operation_id

logger = logging.getLogger(__name__)
SERVICE = "worker.deficiency_overdue"
COMMAND = "DEFICIENCY_OVERDUE_NOTICE"
EVENT = "deficiency.deadline_overdue"
SOURCE = "deficiency_overdue"
PAGE = 200


class NotDue(Exception):
    """Under the lock the deficiency is no longer overdue, or an acknowledgement suppresses it."""


@dataclass
class OverdueReport:
    notified: int = 0
    already: int = 0
    suppressed: int = 0
    skipped: int = 0
    errors: int = 0
    deficiency_ids: list = field(default_factory=list)


def _key(deficiency_id, governing_event_id):
    return json.dumps([str(deficiency_id), str(governing_event_id)], separators=(",", ":"))


def _operation(organization_id, key):
    return str(worker_operation_id(str(organization_id), SERVICE, COMMAND, key))


def _due(organization_id, deficiency_id, now):
    """(governing event id, governing deadline) if overdue and not acknowledged, else a reason."""
    state = service.extension_state(organization_id, deficiency_id, now=now)
    if not state["overdue"]:
        return None, "not_overdue"
    acknowledged = acknowledgement_state(organization_id, deficiency_id, as_of=now)[
        "acknowledgement"
    ]
    if acknowledged is not None and acknowledged["active"]:
        return None, "suppressed"
    return (state["governing_event_id"], state["governing_deadline"]), None


def _notify_one(organization_id, deficiency_id, governing_event_id, now):
    key = _key(deficiency_id, governing_event_id)
    correlation = str(uuid4())

    def authorize(org):
        org = Organization.objects.select_for_update().get(pk=org.pk)
        if not org.active:
            raise CommandDenied()
        return Actor(service_identity=SERVICE, job_key=key)

    def prepare(actor):
        service.lock_deficiencies(organization_id, [deficiency_id])
        due, _reason = _due(organization_id, deficiency_id, now)
        if due is None or due[0] != governing_event_id:
            raise NotDue(deficiency_id)
        deadline = due[1]

        def apply(receipt, current):
            assignee, reporter, vehicle = people(organization_id, deficiency_id)
            notify(
                receipt,
                event_type=EVENT,
                source_event_type=SOURCE,
                source_event_id=governing_event_id,
                deep_link=deficiency_link(deficiency_id),
                payload={
                    "deficiency_id": str(deficiency_id),
                    "vehicle_id": str(vehicle),
                    "governing_deadline": deadline.isoformat(),
                },
                assignee_id=assignee,
                reporter_id=reporter,
            )

        return Plan(
            response_body={
                "code": "OVERDUE_NOTIFIED",
                "deficiency_id": str(deficiency_id),
                "governing_event_id": str(governing_event_id),
            },
            apply=apply,
            target_type="ORGANIZATION",
            target_id=str(organization_id),
            new_snapshot={
                "schema_version": 1,
                "values": {
                    "deficiency_id": str(deficiency_id),
                    "governing_event_id": str(governing_event_id),
                },
            },
        )

    return run_command(
        organization_id=str(organization_id),
        operation_id=_operation(organization_id, key),
        command_type=COMMAND,
        request_digest=digest(
            {"deficiency": str(deficiency_id), "governing": str(governing_event_id)}
        ),
        correlation_id=correlation,
        request_attempt_id=correlation,
        authorize=authorize,
        prepare=prepare,
    )


def candidates(organization_id, now):
    """Active deficiencies whose governing deadline has passed, annotated with it.

    The governing deadline is the deadline of the newest event that carries one: a report or
    revision, or an extension decision made after it (deadlines.deadline_state replays the same
    rule). The projection's deadline is not used, because an extension decision leaves it at the
    base deadline.
    """
    newest = DeficiencyEvent.objects.filter(
        organization_id=OuterRef("organization_id"),
        deficiency_id=OuterRef("deficiency_id"),
        deadline__isnull=False,
    ).order_by("-sequence")
    return (
        DeficiencyProjection.objects.filter(
            organization_id=organization_id, status__in=ACTIVE_STATUSES
        )
        .annotate(
            governing=Subquery(newest.values("deadline")[:1]),
            governing_event=Subquery(newest.values("pk")[:1]),
        )
        .filter(governing__lt=now)
    )


def _pages(organization_id, now):
    """Candidates oldest governing deadline first, PAGE rows per query (keyset, no offset)."""
    query = candidates(organization_id, now).order_by("governing", "deficiency_id")
    after = None
    while True:
        page = query
        if after is not None:
            page = page.filter(
                Q(governing__gt=after[0]) | Q(governing=after[0], deficiency_id__gt=after[1])
            )
        rows = list(page.values_list("deficiency_id", "governing", "governing_event")[:PAGE])
        if not rows:
            return
        yield rows
        if len(rows) < PAGE:
            return
        after = (rows[-1][1], rows[-1][0])


def notify_overdue(organization_id, *, now=None, limit=50):
    """One pass: notify at most `limit` newly overdue deficiencies; returns a report.

    Candidates are active deficiencies whose governing deadline has passed, oldest first, read in
    pages of PAGE. Those already notified for their governing deadline are recognized from their
    worker receipts with one query per page and do not count toward the limit, so neither they
    nor deficiencies extended past now can keep a later overdue deficiency from being reached.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer from 1 to 500")
    now = now or timezone.now()
    report = OverdueReport()
    for rows in _pages(organization_id, now):
        operations = {row[0]: _operation(organization_id, _key(row[0], row[2])) for row in rows}
        done = {
            str(operation)
            for operation in CommandReceipt.objects.filter(
                organization_id=organization_id, operation_id__in=list(operations.values())
            ).values_list("operation_id", flat=True)
        }
        for deficiency_id, _governing, _event in rows:
            if report.notified >= limit:
                return report
            if operations[deficiency_id] in done:
                report.already += 1
                continue
            deficiency_id = str(deficiency_id)
            try:
                due, reason = _due(organization_id, deficiency_id, now)
                if due is None:
                    if reason == "suppressed":
                        report.suppressed += 1
                    continue
                operation = _operation(organization_id, _key(deficiency_id, due[0]))
                if CommandReceipt.objects.filter(
                    organization_id=organization_id, operation_id=operation
                ).exists():
                    report.already += 1
                    continue
                result = _notify_one(organization_id, deficiency_id, due[0], now)
            except NotDue:
                report.skipped += 1
                continue
            except (DeficiencyError, DatabaseError):
                # One deficiency's failure never blocks the others; its notice stays due.
                logger.error("overdue notice unavailable for one deficiency")
                report.errors += 1
                continue
            if result.replayed:
                report.already += 1
            else:
                report.notified += 1
                report.deficiency_ids.append(deficiency_id)
    return report
