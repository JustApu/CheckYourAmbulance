"""Bounded serialization of configuration and evidence for seal commands."""

from django.utils import timezone

from cya.checklists.models import ChecklistTemplate, KitInstance
from cya.checks.models import ItemLock
from cya.deficiencies.models import Deficiency
from cya.deficiencies.service import lock_deficiencies
from cya.kits.models import KitDefinition

MAX_LOCK_ROWS = 10000


class SealLockError(ValueError):
    """The complete lock set cannot be acquired safely."""


def _locked_ids(query, field="pk"):
    ids = list(query.order_by(field).values_list(field, flat=True)[: MAX_LOCK_ROWS + 1])
    if len(ids) > MAX_LOCK_ROWS:
        raise SealLockError("seal scope has too many lock rows")
    return {str(pk) for pk in ids}


def lock_seal_inputs(organization_id, vehicle_id):
    """Lock existing configuration, check streams and deficiencies for one vehicle.

    The caller already holds the vehicle row. Kit locks serialize designation changes,
    including those for nested kits. Existing template and kit-definition locks serialize
    publication. The vehicle lock prevents new bindings,
    assignments and vehicle-specific publication while this set is collected. Locking all
    of the vehicle's item rows covers concurrent checks, including items that become effective
    while waiting; deficiency locks also cover concurrent reopening of a closed deficiency.
    The command must resolve its scope *after* these locks and verify each resolved item has
    an item lock. run_command retries deadlocks, including publication's inverse FK order.
    """
    _locked_ids(
        KitInstance.objects.select_for_update().filter(
            organization_id=organization_id, vehicle_id=vehicle_id, active=True
        )
    )
    _locked_ids(
        ChecklistTemplate.objects.select_for_update().filter(organization_id=organization_id)
    )
    _locked_ids(KitDefinition.objects.select_for_update().filter(organization_id=organization_id))
    item_ids = _locked_ids(
        ItemLock.objects.select_for_update().filter(
            organization_id=organization_id, vehicle_id=vehicle_id
        ),
        "item_id",
    )
    deficiency_ids = list(
        Deficiency.objects.filter(organization_id=organization_id, vehicle_id=vehicle_id)
        .order_by("pk")
        .values_list("pk", flat=True)[: MAX_LOCK_ROWS + 1]
    )
    if len(deficiency_ids) > MAX_LOCK_ROWS:
        raise SealLockError("vehicle has too many deficiencies to evaluate")
    projections = lock_deficiencies(organization_id, [str(pk) for pk in deficiency_ids])
    return timezone.now(), item_ids, projections


def require_item_locks(scope, locked_item_ids):
    """Fail closed if any covered physical item lacks the vehicle's stable lock row."""
    if any(str(item.item_id) not in locked_item_ids for item in scope):
        raise SealLockError("a covered item has no serialization lock")
