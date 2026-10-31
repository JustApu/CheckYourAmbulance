"""Prepare stable lock rows for published vehicle/item scopes."""

from cya.accounts.fields import new_id
from cya.checks.models import ItemLock


def ensure_item_locks(organization_id, vehicle_ids, item_ids):
    """Create each missing lock without replacing a row that may already serialize a writer."""
    pairs = sorted(
        {(str(vehicle_id), str(item_id)) for vehicle_id in vehicle_ids for item_id in item_ids}
    )
    for vehicle_id, item_id in pairs:
        ItemLock.objects.get_or_create(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            item_id=item_id,
            defaults={"id": new_id()},
        )
