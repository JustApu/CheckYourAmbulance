"""Keep pre-window obligations current after a checklist or override publication (SPEC 5.7).

The publish commands send checklist_published after they commit (cya.checklists.signals). The
receiver reconciles the affected vehicles in their own transactions. A failure here never
undoes the publication; it is logged, and the scheduled generator repairs whatever was missed.
"""

import logging

from django.dispatch import receiver

from cya.checklists.signals import checklist_published

logger = logging.getLogger(__name__)


@receiver(checklist_published, dispatch_uid="cya.obligations.reconcile_after_publication")
def reconcile_after_publication(sender, organization_id, publication_id, **kwargs):
    from cya.obligations.generation import reconcile_publication

    summary = reconcile_publication(organization_id, publication_id)
    for failure in summary.failures:
        logger.error(
            "obligation reconciliation failed after publication %s: vehicle %s due %s: %s",
            publication_id,
            failure.vehicle_id,
            failure.due_at.isoformat(),
            failure.error,
        )
