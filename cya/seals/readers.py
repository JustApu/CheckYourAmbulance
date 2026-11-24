"""Bounded, organization-scoped seal status and fresh-inspection views."""

from collections import defaultdict
from zoneinfo import ZoneInfo

from django.db.models import Q
from django.utils import timezone

from cya.checklists.models import Compartment, Item, KitInstance, Vehicle
from cya.kits.models import KitDefinition
from cya.kits.sealable import kit_sealable
from cya.seals.commands import MAX_EVENTS, _event_state
from cya.seals.models import SealEvent
from cya.seals.scope import MAX_SCOPE, resolve_kit_lines, resolve_kit_presence, resolve_scope


def _bounded(query, limit):
    rows = list(query[: limit + 1])
    if len(rows) > limit:
        raise ValueError("seal view exceeds its supported size")
    return rows


def _vehicle(organization_id, vehicle_id):
    vehicle = (
        Vehicle.objects.select_related("organization")
        .filter(organization_id=organization_id, pk=vehicle_id, active=True)
        .first()
    )
    if vehicle is None:
        raise LookupError("vehicle unavailable")
    return vehicle


def subjects(organization_id, vehicle_id, *, at=None):
    """List current sealable compartments and physical kits, with their current event tips."""
    vehicle = _vehicle(organization_id, vehicle_id)
    at = at or timezone.now()
    compartments = _bounded(
        Compartment.objects.filter(
            organization_id=organization_id, vehicle_id=vehicle_id, effective_at__lte=at
        ).order_by("stable_slot_id", "-effective_at", "-revision"),
        MAX_SCOPE,
    )
    latest = {}
    for row in compartments:
        latest.setdefault(str(row.stable_slot_id), row)
    latest = {slot: row for slot, row in latest.items() if row.sealable and not row.retired}
    kits = _bounded(
        KitInstance.objects.filter(
            organization_id=organization_id, vehicle_id=vehicle_id, active=True
        )
        .select_related("compartment")
        .order_by("pk"),
        MAX_SCOPE,
    )
    sealed_kits = kit_sealable(organization_id, [str(kit.pk) for kit in kits], at)
    kits = [kit for kit in kits if str(kit.pk) in sealed_kits]
    events = _bounded(
        SealEvent.objects.filter(organization_id=organization_id, recorded_at__lte=at)
        .filter(
            Q(kit_id__in=[kit.pk for kit in kits])
            | Q(vehicle_id=vehicle_id, kit__isnull=True, compartment_slot_id__in=latest)
        )
        .order_by("sequence", "pk"),
        MAX_EVENTS,
    )
    streams = defaultdict(list)
    for event in events:
        key = (
            ("kit", str(event.kit_id)) if event.kit_id else ("slot", str(event.compartment_slot_id))
        )
        streams[key].append(event)
    today = at.astimezone(ZoneInfo(vehicle.organization.station_zone)).date()

    def state(kind, subject_id, label, slot_id=None):
        stream = streams[(kind, subject_id)]
        seal, pending = _event_state(stream)
        status = (
            "OPEN"
            if seal is None
            else "AUDIT_PENDING"
            if pending
            else (
                "EXPIRED" if seal.expiration is not None and seal.expiration < today else "SEALED"
            )
        )
        return {
            "kind": kind,
            "id": subject_id,
            "label": label,
            "state": status,
            "tip_id": str(stream[-1].pk) if stream else None,
            "expiration": seal.expiration.isoformat() if seal and seal.expiration else None,
            "no_expiration": bool(seal and seal.no_expiration),
            "audit_pending": pending,
            "slot_id": slot_id,
        }

    result = [state("slot", slot, row.label) for slot, row in latest.items()]
    result.extend(
        state(
            "kit",
            str(kit.pk),
            kit.label,
            str(kit.compartment.stable_slot_id) if kit.compartment_id else None,
        )
        for kit in kits
    )
    return sorted(result, key=lambda row: (row["kind"], row["label"].casefold(), row["id"]))


def inspection_scope(organization_id, vehicle_id, kind, subject_id, *, at=None):
    """Show the current configured requirements, never prior certified observations."""
    _vehicle(organization_id, vehicle_id)
    at = at or timezone.now()
    match = next(
        (
            row
            for row in subjects(organization_id, vehicle_id, at=at)
            if row["kind"] == kind and row["id"] == subject_id
        ),
        None,
    )
    if match is None:
        raise LookupError("seal subject unavailable")
    items, assignments = resolve_scope(
        organization_id,
        vehicle_id,
        at,
        kit_id=subject_id if kind == "kit" else None,
        slot_id=subject_id if kind == "slot" else None,
    )
    labels = dict(
        Item.objects.filter(
            organization_id=organization_id, pk__in={r.item_id for r in items}
        ).values_list("pk", "label")
    )
    kit_labels = dict(
        KitInstance.objects.filter(organization_id=organization_id, pk__in=assignments).values_list(
            "pk", "label"
        )
    )
    if len(labels) != len({r.item_id for r in items}) or len(kit_labels) != len(assignments):
        raise ValueError("seal scope labels are unavailable")
    kit_presence = resolve_kit_presence(organization_id, vehicle_id, assignments, at)
    presence_labels = dict(
        Item.objects.filter(
            organization_id=organization_id, pk__in={row.item_id for row in kit_presence}
        ).values_list("pk", "label")
    )
    if len(presence_labels) != len({row.item_id for row in kit_presence}):
        raise ValueError("kit presence labels are unavailable")
    lines = (
        resolve_kit_lines(organization_id, vehicle_id, subject_id, at, assignments)
        if kind == "slot"
        else ()
    )
    definitions = {
        str(row.pk): row
        for row in KitDefinition.objects.filter(
            organization_id=organization_id, pk__in={line.definition_id for line in lines}
        )
    }
    if len(definitions) != len({line.definition_id for line in lines}):
        raise ValueError("seal kit definitions are unavailable")
    checklist_versions = {}
    if lines:
        from cya.attention.checklist import vehicle_checklist

        for row in vehicle_checklist(organization_id, vehicle_id, as_of=at).rows:
            if row.kit_requirement_id and row.checklist_version_id:
                checklist_versions.setdefault(
                    str(row.kit_requirement_id), str(row.checklist_version_id)
                )
    return {
        "subject": match,
        "items": [
            {
                "item_id": item.item_id,
                "kit_id": item.kit_id,
                "label": labels[item.item_id],
                "kit_label": kit_labels.get(item.kit_id),
                "kind": item.kind,
                "expected_quantity": item.expected_quantity,
                "unit": item.unit,
                "expiration_tracked": item.expiration_tracked,
                "allowed_outcomes": list(item.allowed_outcomes),
            }
            for item in items
        ],
        "kit_presence": [
            {
                "kit_id": row.kit_id,
                "assignment_event_id": row.assignment_event_id,
                "item_id": row.item_id,
                "kit_label": kit_labels[row.kit_id],
                "label": presence_labels[row.item_id],
            }
            for row in kit_presence
        ],
        "kit_lines": [
            {
                "line_id": line.line_id,
                "label": definitions[line.definition_id].label,
                "item_id": str(definitions[line.definition_id].presence_item_id),
                "required_count": line.required_count,
                "checklist_version_id": checklist_versions.get(line.line_id),
            }
            for line in lines
        ],
    }
