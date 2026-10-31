"""Which kit instances a vehicle had in a slot, as the assignment records stood (TIME-1a, REG-6).

Reads only kit_assignment_event, never the mutable kit_instance row, so the answer for a past
instant cannot change. With recorded_before, only events recorded strictly before that instant
count ("as the records stood then", the same cutoff cya.obligations uses).

A slot is a compartment's stable_slot_id: the vehicle's own compartment row differs per vehicle,
but the slot an item's kit lives in is the same across them. An instance is in the slot at an
instant when its latest event effective by then is active, on this vehicle, in that slot.

since_event_id is the first event of the instance's current uninterrupted run on this vehicle
and slot. Requirements pin it, so a kit that leaves and comes back gets a new requirement and
its retired obligations are never revived, while an unrelated later event (say, a move inside
the same slot) changes nothing.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from cya.kits.models import KitAssignmentEvent


@dataclass(frozen=True)
class ActiveKit:
    kit_id: str
    since_event_id: str
    since_at: datetime  # the run's first event takes effect (TIME-1a: an open window is not owed)


def _in_slot(event, vehicle_id, slot_id):
    return (
        event.active
        and str(event.vehicle_id) == vehicle_id
        and event.compartment is not None
        and str(event.compartment.stable_slot_id) == slot_id
    )


def active_kits(organization_id, vehicle_id, slot_id, at, recorded_before=None):
    """Kit instances in `slot_id` on the vehicle at `at`, ordered by kit id."""
    organization_id, vehicle_id, slot_id = str(organization_id), str(vehicle_id), str(slot_id)
    events = KitAssignmentEvent.objects.filter(
        organization_id=organization_id, effective_at__lte=at
    )
    if recorded_before is not None:
        events = events.filter(recorded_at__lt=recorded_before)
    # Only a kit that was ever on this vehicle can be on it now; load just those kits' events.
    candidates = set(events.filter(vehicle_id=vehicle_id).values_list("kit_id", flat=True))
    if not candidates:
        return ()
    chains = defaultdict(list)
    for event in (
        events.filter(kit_id__in=candidates)
        .select_related("compartment")
        .order_by("kit_id", "sequence")
    ):
        chains[str(event.kit_id)].append(event)
    found = []
    for kit_id, chain in chains.items():
        if not _in_slot(chain[-1], vehicle_id, slot_id):
            continue
        run = chain[-1]
        for event in reversed(chain):
            if not _in_slot(event, vehicle_id, slot_id):
                break
            run = event
        found.append(ActiveKit(kit_id, str(run.pk), run.effective_at))
    return tuple(sorted(found, key=lambda k: k.kit_id))


def slot_of_reference_kit(kit_id):
    """The stable slot a checklist definition's template kit stands for, or None if unplaced.

    A definition names a kit only to name a slot (SPEC 4.2). That kit may later move or be
    deactivated, and its mutable row would then lose the slot, so the slot is the one the kit
    was first placed in (its first assignment event). A kit that predates any history falls back
    to its row's compartment.
    """
    from cya.checklists.models import KitInstance

    first = (
        KitAssignmentEvent.objects.filter(kit_id=kit_id, sequence=1)
        .select_related("compartment")
        .first()
    )
    if first is not None:
        return str(first.compartment.stable_slot_id) if first.compartment_id else None
    kit = KitInstance.objects.select_related("compartment").get(pk=kit_id)
    return str(kit.compartment.stable_slot_id) if kit.compartment_id else None
