"""Transaction-tail routing from immutable command effects to affected projection rows.

Inspect committed-intent rows by their receipt, including secondary effects (withdrawn Unable
completions and suspension ends). Bulk inserts are included; no ORM signals/on_commit cache
updates are used. Existing publication/kit/binding reconciliation still appends selections in
its own audited transactions; those transactions refresh their replacements and retirements.
"""

from cya.checks.models import CheckEvent, CheckObligationLink, CheckRecord
from cya.conditions.models import ObligationDetermination
from cya.deficiencies.models import CheckSuspension
from cya.obligations.current import refresh_obligations
from cya.obligations.models import Obligation, ObligationSelection
from cya.operations.models import VehicleStatusEvent

CHECK_COMMANDS = frozenset(("CHECK_RECORD", "CHECK_CORRECTION", "CHECK_INVALIDATION"))
DETERMINATION_COMMANDS = frozenset(
    (
        "RECORD_OBLIGATION_DETERMINATION",
        "SUPERVISOR_COMPLETE_UNABLE",
        "SUPERVISOR_COMPLETION_INVALIDATION",
        "INGEST_ROSTER_SNAPSHOT",
        "INGEST_ROSTER_UNAVAILABLE",
        "RECORD_SESSION_ROSTER_CORRECTION",
    )
)
STATUS_COMMANDS = frozenset(("VEHICLE_STATUS_SET", "STATUS_REQUEST_DECIDE"))
OBLIGATION_COMMANDS = frozenset(("OBLIGATIONS_RECONCILE", "AD_HOC_INSPECTION_REQUEST"))


def refresh_for_receipt(receipt):
    """Run after ALL domain effects, before success audit/COMMIT. Any failure rolls back both."""
    kind, org = receipt.command_type, str(receipt.organization_id)
    ids = set()
    if kind in CHECK_COMMANDS:
        roots = set(
            CheckRecord.objects.filter(command=receipt).values_list("root_check_id", flat=True)
        )
        roots.update(
            CheckEvent.objects.filter(command=receipt).values_list("root_check_id", flat=True)
        )
        ids.update(
            CheckObligationLink.objects.filter(
                organization_id=org,
                check_record__root_check_id__in=roots,
            ).values_list("obligation_id", flat=True)
        )
    if kind in CHECK_COMMANDS | DETERMINATION_COMMANDS | OBLIGATION_COMMANDS:
        ids.update(
            ObligationDetermination.objects.filter(command=receipt).values_list(
                "obligation_id", flat=True
            )
        )
    if kind in OBLIGATION_COMMANDS:
        ids.update(Obligation.objects.filter(command=receipt).values_list("pk", flat=True))
        for current, previous in ObligationSelection.objects.filter(command=receipt).values_list(
            "selected_obligation_id",
            "previous_selection__selected_obligation_id",
        ):
            ids.update(i for i in (current, previous) if i)
    if kind in STATUS_COMMANDS:
        for event in VehicleStatusEvent.objects.filter(command=receipt):
            ids.update(
                Obligation.objects.filter(
                    organization_id=org,
                    vehicle_id=event.vehicle_id,
                    routine=True,
                    due_at__gte=event.recorded_at,
                ).values_list("pk", flat=True)
            )
    if kind in CHECK_COMMANDS or kind.startswith("DEFICIENCY_"):
        for suspension in CheckSuspension.objects.filter(command=receipt).select_related(
            "prior_suspension"
        ):
            start = suspension if suspension.action == "START" else suspension.prior_suspension
            ids.update(
                Obligation.objects.filter(
                    organization_id=org,
                    vehicle_id=start.vehicle_id,
                    item_id=start.item_id,
                    kit_key=start.instance_key,
                    routine=True,
                    grace_at__gte=start.recorded_at,
                ).values_list("pk", flat=True)
            )
    if ids:
        # Some early conditional-rule records deliberately name not-yet-materialized obligations.
        actual = Obligation.objects.filter(organization_id=org, pk__in=ids).values_list(
            "pk", flat=True
        )
        refresh_obligations(org, actual)
