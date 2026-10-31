"""Bounded, retried email delivery with idempotent attempt records (SPEC 13, AC-28).

deliver_pending(organization_id, sender) is the job: it takes at most `limit` unresolved EMAIL
outbox rows, sends each through the injected sender and records the outcome as a
notification_delivery_event written by a worker command. Job identity follows the data-model
contract: service identity, command type and job key ["<outbox id>","<attempt number>"] derive
the operation id, so recording the same attempt twice is one row. A second worker that reaches
the same attempt with a different outcome collides: the stored receipt wins, nothing is
overwritten, and the collision is logged and counted (never silent).

Delivery is at-least-once. The sender is called before the attempt row is written (a command
callback may not touch the network), so a crash after the transport accepted a message but before
the row commits leaves an ambiguous outcome: the next run repeats the same attempt number and the
message may be sent again. The intent is never lost, and no exactly-once claim is made.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from cya.accounts.models import Organization, User
from cya.authorization.persistence import digest
from cya.core.audit_commands import Actor, CommandDenied, Plan, run_command
from cya.notifications import senders
from cya.notifications.models import NotificationDeliveryEvent, NotificationOutbox
from tools.command_identity import worker_operation_id

logger = logging.getLogger(__name__)
SERVICE = "worker.notifications"
COMMAND = "NOTIFICATION_DELIVERY_ATTEMPT"
SCAN_FACTOR = 5


@dataclass
class DeliveryReport:
    sent: int = 0
    retrying: int = 0
    gave_up: int = 0
    collisions: int = 0
    errors: int = 0
    outbox_ids: list = field(default_factory=list)


def _record(organization_id, outbox, attempt, outcome, error_code):
    """One worker command that appends the attempt row; returns the Result."""
    key = json.dumps([str(outbox.pk), str(attempt)], separators=(",", ":"))
    correlation = str(uuid4())
    body = {"code": outcome, "outbox_id": str(outbox.pk), "attempt_number": attempt}

    def authorize(org):
        org = Organization.objects.select_for_update().get(pk=org.pk)
        if not org.active:
            raise CommandDenied()
        return Actor(service_identity=SERVICE, job_key=key)

    def prepare(actor):
        def apply(receipt, current):
            NotificationDeliveryEvent.objects.create(
                organization_id=organization_id,
                command=receipt,
                outbox=outbox,
                attempt_number=attempt,
                outcome=outcome,
                error_code=error_code,
                recorded_at=receipt.recorded_at,
            )

        return Plan(
            response_body=body,
            apply=apply,
            target_type="ORGANIZATION",
            target_id=str(organization_id),
            new_snapshot={
                "schema_version": 1,
                "values": {"outbox_id": str(outbox.pk), "attempt": attempt, "outcome": outcome},
            },
        )

    return run_command(
        organization_id=str(organization_id),
        operation_id=str(worker_operation_id(str(organization_id), SERVICE, COMMAND, key)),
        command_type=COMMAND,
        request_digest=digest(
            {"outbox": str(outbox.pk), "attempt": attempt, "outcome": outcome, "error": error_code}
        ),
        correlation_id=correlation,
        request_attempt_id=correlation,
        authorize=authorize,
        prepare=prepare,
    )


def _unresolved(organization_id, page, after=None):
    """One page of unresolved EMAIL rows in (recorded_at, id) order, after the `after` key."""
    final = NotificationDeliveryEvent.objects.filter(
        outbox=OuterRef("pk"), outcome__in=("SENT", "GAVE_UP")
    )
    rows = NotificationOutbox.objects.filter(
        organization_id=organization_id, channel="EMAIL"
    ).filter(~Exists(final))
    if after is not None:
        rows = rows.filter(Q(recorded_at__gt=after[0]) | Q(recorded_at=after[0], id__gt=after[1]))
    return list(rows.order_by("recorded_at", "id")[:page])


def _due_rows(organization_id, now, retry_base, limit):
    """Up to `limit` due (row, attempts) pairs. Pages through unresolved rows by key, so rows
    still in backoff cannot occupy a fixed window and starve newer work."""
    due, after = [], None
    while len(due) < limit:
        rows = _unresolved(organization_id, limit * SCAN_FACTOR, after)
        if not rows:
            break
        history = _attempts(rows)
        for outbox in rows:
            events = history.get(outbox.pk, [])
            if _due(events, now, retry_base):
                due.append((outbox, events))
                if len(due) >= limit:
                    break
        after = (rows[-1].recorded_at, rows[-1].pk)
    return due


def _attempts(rows):
    found = {}
    events = NotificationDeliveryEvent.objects.filter(outbox__in=rows).order_by("attempt_number")
    for event in events:
        found.setdefault(event.outbox_id, []).append(event)
    return found


def _due(events, now, retry_base):
    if not events:
        return True
    last = events[-1]
    return last.recorded_at + retry_base * (2 ** (last.attempt_number - 1)) <= now


def _outcome(error_code, attempt, max_attempts):
    if error_code is None:
        return "SENT"
    if error_code in senders.PERMANENT_CODES or attempt >= max_attempts:
        return "GAVE_UP"
    return "RETRY"


def _send(sender, outbox, base_url):
    """None on success, else a safe error code; never raises."""
    user = User.objects.filter(pk=outbox.recipient_user_id, active=True).first()
    if user is None or not user.email:
        return "INVALID_ADDRESS"
    try:
        sender.send(senders.render(outbox, user.email, base_url))
    except senders.SenderError as error:
        return error.code
    except Exception as error:
        logger.error("notification sender failed (%s)", type(error).__name__)
        return "UNKNOWN"
    return None


def deliver_pending(
    organization_id,
    sender,
    *,
    now=None,
    limit=50,
    max_attempts=None,
    retry_base=None,
    base_url=None,
):
    """Attempt due EMAIL deliveries once each; bounded by `limit` rows per call."""
    now = now or timezone.now()
    max_attempts = max_attempts or settings.CYA_NOTIFICATION_MAX_ATTEMPTS
    retry_base = retry_base or timedelta(seconds=settings.CYA_NOTIFICATION_RETRY_SECONDS)
    base_url = settings.CYA_PUBLIC_BASE_URL if base_url is None else base_url
    report = DeliveryReport()
    for outbox, events in _due_rows(organization_id, now, retry_base, limit):
        attempt = len(events) + 1
        error_code = _send(sender, outbox, base_url)
        outcome = _outcome(error_code, attempt, max_attempts)
        report.outbox_ids.append(str(outbox.pk))
        try:
            result = _record(organization_id, outbox, attempt, outcome, error_code)
        except Exception as error:
            logger.error("notification attempt not recorded (%s)", type(error).__name__)
            report.errors += 1
            continue
        if result.operation_reused:
            logger.error("notification job collision: attempt already recorded differently")
            report.collisions += 1
            continue
        if outcome == "SENT":
            report.sent += 1
        elif outcome == "RETRY":
            report.retrying += 1
        else:
            report.gave_up += 1
    return report


def failures(organization_id, limit=200):
    """What administrators see: email intents that failed at least once and are not yet sent."""
    return _unresolved_or_abandoned(organization_id, limit)


def _unresolved_or_abandoned(organization_id, limit):
    sent = NotificationDeliveryEvent.objects.filter(outbox=OuterRef("pk"), outcome="SENT")
    failed = NotificationDeliveryEvent.objects.filter(outbox=OuterRef("pk")).exclude(outcome="SENT")
    outbox = (
        NotificationOutbox.objects.filter(organization_id=organization_id, channel="EMAIL")
        .filter(~Exists(sent), Exists(failed))
        .order_by("recorded_at", "id")[:limit]
    )
    history = _attempts(list(outbox))
    return [
        {
            "outbox_id": str(row.pk),
            "event_type": row.event_type,
            "recipient_user_id": str(row.recipient_user_id),
            "attempts": len(history[row.pk]),
            "last_outcome": history[row.pk][-1].outcome,
            "last_error_code": history[row.pk][-1].error_code,
        }
        for row in outbox
    ]
