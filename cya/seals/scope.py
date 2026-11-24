"""Resolve a seal's physical contents from server-side configuration and history."""

from dataclasses import dataclass
from datetime import timedelta

from django.db.models import OuterRef, Subquery

from cya.checklists.models import ChecklistTemplate, Compartment, KitInstance
from cya.checklists.overlay import NoEffectiveVersionError, ResolutionError, resolve_checklist
from cya.kits.history import slot_of_reference_kit
from cya.kits.kit_lists import kit_list_effective_at
from cya.kits.models import (
    KitAssignmentEvent,
    KitDefinition,
    KitDefinitionItem,
    KitInstanceDefinition,
    VehicleKitRequirement,
)
from cya.kits.placements import definition_version_at
from cya.obligations.requirements import bound_template_ids

MAX_SCOPE = 2000


class SealScopeError(ValueError):
    """The complete physical scope cannot be resolved safely."""


@dataclass(frozen=True)
class ScopeItem:
    item_id: str
    kit_id: str | None
    definition_id: str
    source: str
    kind: str
    expected_quantity: int | None
    unit: str | None
    expiration_tracked: bool
    allowed_outcomes: tuple[str, ...]
    # SPEC 6.0.0 CHK-11: the item requirement version of a loose item's row (None for kit
    # content and before the item-requirement cutover).
    item_requirement_version_id: str | None = None

    @property
    def key(self):
        return self.item_id, self.kit_id


@dataclass(frozen=True)
class ScopeKitLine:
    line_id: str
    definition_id: str
    required_count: int
    assignments: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ScopeKitPresence:
    kit_id: str
    assignment_event_id: str
    item_id: str
    checklist_version_id: str


def _bounded(query):
    rows = list(query[: MAX_SCOPE + 1])
    if len(rows) > MAX_SCOPE:
        raise SealScopeError("seal scope exceeds its item limit")
    return rows


def _active_assignments(organization_id, vehicle_id, instant):
    latest = KitAssignmentEvent.objects.filter(
        organization_id=organization_id,
        kit_id=OuterRef("pk"),
        effective_at__lte=instant,
        recorded_at__lte=instant,
    ).order_by("-sequence")
    kits = (
        KitInstance.objects.filter(organization_id=organization_id)
        .annotate(
            current_vehicle=Subquery(latest.values("vehicle_id")[:1]),
            current_active=Subquery(latest.values("active")[:1]),
            current_slot=Subquery(latest.values("compartment__stable_slot_id")[:1]),
            current_assignment=Subquery(latest.values("pk")[:1]),
        )
        .filter(current_vehicle=vehicle_id, current_active=True)
        .order_by("pk")
    )
    found = {}
    for kit in _bounded(kits):
        if kit.current_slot is None or kit.current_assignment is None:
            raise SealScopeError("an active kit has an incomplete assignment")
        found[str(kit.pk)] = (str(kit.current_slot), str(kit.current_assignment))
    return found


def _add(found, item):
    previous = found.get(item.key)
    if previous is not None:
        prior_values = (
            previous.kind,
            previous.expected_quantity,
            previous.unit,
            previous.expiration_tracked,
            previous.allowed_outcomes,
        )
        new_values = (
            item.kind,
            item.expected_quantity,
            item.unit,
            item.expiration_tracked,
            item.allowed_outcomes,
        )
        if prior_values != new_values:
            raise SealScopeError("multiple incompatible definitions cover one physical item")
        return
    found[item.key] = item
    if len(found) > MAX_SCOPE:
        raise SealScopeError("seal scope exceeds its item limit")


def _resolved_items(organization_id, vehicle_id, instant):
    template_ids = bound_template_ids(organization_id, vehicle_id, instant)
    if len(template_ids) > MAX_SCOPE:
        raise SealScopeError("too many bound checklists")
    templates = _bounded(
        ChecklistTemplate.objects.filter(
            organization_id=organization_id, pk__in=template_ids
        ).order_by("pk")
    )
    if len(templates) != len(template_ids):
        raise SealScopeError("a bound checklist is unavailable")
    chosen = {}
    for order, template in enumerate(templates):
        try:
            resolved = resolve_checklist(
                organization_id=organization_id,
                vehicle_id=vehicle_id,
                template_id=template.pk,
                as_of=instant,
                recorded_before=instant + timedelta(microseconds=1),
            )
        except (NoEffectiveVersionError, ResolutionError) as error:
            raise SealScopeError("a bound checklist cannot be resolved") from error
        for item in resolved.items:
            chosen.setdefault(
                (str(item.requirement.item_id), str(template.pk)),
                (order, item, resolved.template_version_id),
            )
            if len(chosen) > MAX_SCOPE:
                raise SealScopeError("checklist scope exceeds its item limit")
    return chosen


def resolve_scope(organization_id, vehicle_id, instant, *, kit_id=None, slot_id=None):
    """Return (items, kit assignments) for one kit or one cabinet at ``instant``.

    The caller holds the vehicle and relevant configuration locks and authorizes access.
    Physical kits are resolved from assignment events, never from mutable projections.
    """
    if (kit_id is None) == (slot_id is None):
        raise SealScopeError("exactly one kit or compartment slot is required")
    if instant.utcoffset() is None:
        raise SealScopeError("instant must be timezone-aware")
    organization_id, vehicle_id = str(organization_id), str(vehicle_id)
    assignments = _active_assignments(organization_id, vehicle_id, instant)
    if kit_id is not None:
        kit_id = str(kit_id)
        if kit_id not in assignments:
            raise SealScopeError("kit is not active on this vehicle")
        selected = {kit_id: assignments[kit_id]}
    else:
        slot_id = str(slot_id)
        selected = {k: v for k, v in assignments.items() if v[0] == slot_id}

    mappings = _bounded(
        KitInstanceDefinition.objects.filter(
            organization_id=organization_id, kit_id__in=selected, recorded_at__lte=instant
        ).order_by("kit_id")
    )
    definitions = {str(row.kit_id): str(row.definition_id) for row in mappings}
    items = {}
    recorded_before = instant + timedelta(microseconds=1)
    versions = {}
    for definition_id in sorted(set(definitions.values())):
        version = definition_version_at(organization_id, definition_id, instant, recorded_before)
        if version is None:
            raise SealScopeError("a covered kit has no published definition")
        versions[definition_id] = version
    rows = _bounded(
        KitDefinitionItem.objects.filter(
            organization_id=organization_id, version_id__in=versions.values()
        ).order_by("version_id", "item_id")
    )
    by_version = {}
    for row in rows:
        by_version.setdefault(str(row.version_id), []).append(row)
    for selected_kit, definition_id in definitions.items():
        for row in by_version.get(versions[definition_id], ()):
            _add(
                items,
                ScopeItem(
                    str(row.item_id),
                    selected_kit,
                    str(row.pk),
                    "KIT_DEFINITION",
                    row.kind,
                    row.expected_quantity,
                    row.unit,
                    row.expiration_tracked,
                    tuple(row.allowed_outcomes),
                ),
            )

    resolved = _resolved_items(organization_id, vehicle_id, instant)
    if len(resolved) > MAX_SCOPE:
        raise SealScopeError("checklist scope exceeds its item limit")
    compartment_ids = {
        item.compartment_id for _, item, _ in resolved.values() if item.compartment_id
    }
    slots = {
        str(pk): str(slot)
        for pk, slot in Compartment.objects.filter(
            organization_id=organization_id, vehicle_id=vehicle_id, pk__in=compartment_ids
        ).values_list("pk", "stable_slot_id")
    }
    for _, item, _ in resolved.values():
        requirement = item.requirement
        if requirement.kit_id is None:
            if kit_id is None and slots.get(str(item.compartment_id)) == slot_id:
                _add(
                    items,
                    ScopeItem(
                        str(requirement.item_id),
                        None,
                        str(requirement.definition_id),
                        requirement.source,
                        requirement.kind,
                        item.effective_quantity,
                        requirement.unit,
                        requirement.expiration_tracked,
                        requirement.allowed_outcomes,
                        getattr(requirement, "item_requirement_version_id", None),
                    ),
                )
        else:
            reference_slot = slot_of_reference_kit(requirement.kit_id)
            for selected_kit, (assigned_slot, _) in selected.items():
                # Old slot-based checklist versions remain meaningful after kit definitions.
                # Incompatible definitions for the same physical item fail closed in _add.
                if assigned_slot == reference_slot:
                    _add(
                        items,
                        ScopeItem(
                            str(requirement.item_id),
                            selected_kit,
                            str(requirement.definition_id),
                            requirement.source,
                            requirement.kind,
                            item.effective_quantity,
                            requirement.unit,
                            requirement.expiration_tracked,
                            requirement.allowed_outcomes,
                        ),
                    )
    for selected_kit in set(selected) - set(definitions):
        if not any(item.kit_id == selected_kit for item in items.values()):
            raise SealScopeError("a covered legacy kit has no resolvable contents")
    return tuple(
        sorted(items.values(), key=lambda item: (item.item_id, item.kit_id or ""))
    ), selected


def resolve_kit_lines(organization_id, vehicle_id, slot_id, instant, assignments):
    """Resolve the published kit-list lines and physical assignments for one cabinet."""
    if instant.utcoffset() is None:
        raise SealScopeError("instant must be timezone-aware")
    version = kit_list_effective_at(
        organization_id, vehicle_id, instant, instant + timedelta(microseconds=1)
    )
    if version is None:
        return ()
    lines = _bounded(
        VehicleKitRequirement.objects.filter(
            organization_id=organization_id,
            version=version,
            compartment__vehicle_id=vehicle_id,
            compartment__stable_slot_id=slot_id,
        ).order_by("id")
    )
    if len(assignments) > MAX_SCOPE:
        raise SealScopeError("too many kit assignments")
    instances = _bounded(
        KitInstanceDefinition.objects.filter(
            organization_id=organization_id, kit_id__in=assignments, recorded_at__lte=instant
        ).order_by("kit_id")
    )
    by_definition = {}
    for instance in instances:
        kit_id = str(instance.kit_id)
        assigned_slot, event_id = assignments[kit_id]
        if str(assigned_slot) != str(slot_id):
            raise SealScopeError("kit assignment is outside the cabinet")
        by_definition.setdefault(str(instance.definition_id), []).append((kit_id, event_id))
    return tuple(
        ScopeKitLine(
            str(line.pk),
            str(line.definition_id),
            line.required_count,
            tuple(sorted(by_definition.get(str(line.definition_id), ()))),
        )
        for line in lines
    )


def resolve_kit_presence(organization_id, vehicle_id, assignments, instant):
    """Resolve the presence item and assignment event for each defined physical unit."""
    if instant.utcoffset() is None or len(assignments) > MAX_SCOPE:
        raise SealScopeError("invalid or oversized physical kit presence scope")
    mappings = _bounded(
        KitInstanceDefinition.objects.filter(
            organization_id=organization_id,
            kit_id__in=assignments,
            recorded_at__lte=instant,
        ).order_by("kit_id")
    )
    by_kit = {str(row.kit_id): str(row.definition_id) for row in mappings}
    definitions = {
        str(pk): str(item_id)
        for pk, item_id in KitDefinition.objects.filter(
            organization_id=organization_id, pk__in=set(by_kit.values())
        ).values_list("pk", "presence_item_id")
    }
    if set(definitions) != set(by_kit.values()):
        raise SealScopeError("a physical kit definition is unavailable")
    placed_definition_versions = {}
    templates = ChecklistTemplate.objects.filter(
        organization_id=organization_id,
        pk__in=bound_template_ids(organization_id, vehicle_id, instant),
    ).order_by("pk")
    for template in _bounded(templates):
        try:
            resolved = resolve_checklist(
                organization_id=organization_id,
                vehicle_id=vehicle_id,
                template_id=template.pk,
                as_of=instant,
                recorded_before=instant + timedelta(microseconds=1),
            )
        except NoEffectiveVersionError:
            # A scheduled binding can precede its first effective version. It contributes no
            # presence target at this instant, matching obligation generation and the checklist.
            continue
        except ResolutionError as error:
            raise SealScopeError("a bound checklist cannot be resolved") from error
        for row in resolved.placements:
            placed_definition_versions.setdefault(
                str(row.definition_id), str(resolved.template_version_id)
            )
    return tuple(
        ScopeKitPresence(
            kit,
            str(assignments[kit][1]),
            definitions[by_kit[kit]],
            placed_definition_versions[by_kit[kit]],
        )
        for kit in sorted(by_kit)
        if by_kit[kit] in placed_definition_versions
    )
