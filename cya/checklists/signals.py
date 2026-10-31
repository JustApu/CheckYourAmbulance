"""Notification that a checklist or override version was just published (after commit).

Consumers that must react to a publication, such as the pre-window obligation reconciliation
(SPEC 5.7), connect to checklist_published. The signal is sent only after the publish command
has committed, in its own transaction boundary, and never on a replay of an earlier publish,
so a receiver failure can neither undo nor delay the publication. Receivers must therefore be
restart-safe: whatever one misses, the scheduled generator repairs on its next run.
"""

import logging

from django.dispatch import Signal

logger = logging.getLogger(__name__)

# Keyword arguments: organization_id, publication_id.
checklist_published = Signal()


def announce_publication(result, organization_id):
    """Send checklist_published for a freshly committed publication; log, never raise."""
    publication_id = result.body.get("publication_id") if isinstance(result.body, dict) else None
    if result.replayed or not publication_id:
        return
    for receiver, outcome in checklist_published.send_robust(
        sender=announce_publication,
        organization_id=organization_id,
        publication_id=publication_id,
    ):
        if isinstance(outcome, Exception):
            logger.error(
                "checklist publication receiver %s failed for publication %s",
                getattr(receiver, "__qualname__", repr(receiver)),
                publication_id,
                exc_info=outcome,
            )
