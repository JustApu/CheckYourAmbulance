"""Changed checklist rows carried in pushes (SPEC 8.3.0 §17.3, OQ-88; issue #278).

After a check commits, its item's rows are read once per open session on the vehicle, exactly as
that session's participants would read them (GET .../checklist?view=NORMAL&needs_attention_only=
false&session_id=...&item_ids=<item>), with the read instant. Each set goes only to that session's
group, which a socket joins only for a recorded participant (cya.live_updates.consumer), the same
rule the checklist API applies to session_id. A page applies the rows instead of re-reading only
for its own session and view and only when they are newer than what it shows; otherwise the
vehicle hint that follows still makes it re-read. Pushes remain hints.

Bounded: at most MAX_SESSIONS open sessions, and a set larger than MAX_BYTES is left out (those
pages simply re-read).
"""

import json
import logging

from django.core.serializers.json import DjangoJSONEncoder

from cya.attention.checklist import NORMAL, vehicle_checklist

logger = logging.getLogger(__name__)

MAX_SESSIONS = 8
MAX_BYTES = 48 * 1024  # under the socket's 64 KiB frame bound (CYA_LIVE_MAX_MESSAGE_BYTES)


def session_group(organization_id, session_id):
    return f"session.{organization_id}.{session_id}"


def open_sessions(organization_id, vehicle_id):
    """The vehicle's sessions open now (SES-7 allows one per checklist), bounded.

    Open and before their end instant (SES-9): a session past its end is closed by the session
    job, and until that job runs its projection can still say OPEN. Pushing rows to such a
    session would cost a read for nobody (issue #278: stale sessions multiplied a save's cost).
    """
    from django.utils import timezone

    from cya.inspections.models import InspectionSession, SessionProjection

    open_ids = SessionProjection.objects.filter(
        organization_id=organization_id,
        session__vehicle_id=vehicle_id,
        state="OPEN",
        ends_at__gt=timezone.now(),
    ).values("session_id")
    return list(
        InspectionSession.objects.select_related("checklist_version")
        .filter(organization_id=organization_id, vehicle_id=vehicle_id, pk__in=open_ids)
        .order_by("pk")[:MAX_SESSIONS]
    )


def row_messages(organization_id, vehicle_id, item_id, event_id):
    """[(group, message)] for each open session: the item's rows read after the commit."""
    from cya.attention.api import _stamp, check_authors, checklist_row

    messages = []
    for session in open_sessions(organization_id, vehicle_id):
        result = vehicle_checklist(
            organization_id,
            vehicle_id,
            view=NORMAL,
            needs_attention_only=False,
            session=session,
            item_ids=[item_id],
        )
        authors = check_authors(organization_id, result.rows)
        message = {
            "type": "live.rows",
            "organization_id": organization_id,
            "vehicle_id": vehicle_id,
            "session_id": str(session.pk),
            "event_id": event_id,
            "item_id": item_id,
            "as_of": _stamp(result.as_of),
            # The API's JSON values (ids, decimals, instants) as plain types for the channel layer.
            "items": json.loads(
                json.dumps(
                    [checklist_row(row, authors.get(row.latest_check_id)) for row in result.rows],
                    cls=DjangoJSONEncoder,
                )
            ),
        }
        if len(json.dumps(message, separators=(",", ":"))) > MAX_BYTES:
            logger.warning("pushed rows too large; that session re-reads")
            continue
        messages.append((session_group(organization_id, str(session.pk)), message))
    return messages
