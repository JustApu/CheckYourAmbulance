"""Deficiency writers shared by the record-check and report commands (SPEC 8.1-8.3, DEF-1..3).

Nothing here opens a transaction or authorizes: the calling command (cya.core.audit_commands
run_command) owns both, and every writer below runs inside its apply step under the deficiency
lock protocol. Locks follow the contract order (vehicles, kits, items, deficiencies); a
deficiency is locked through its deficiency_lock row, and several are locked sorted by id.
"""

from datetime import datetime

from django.utils import timezone

from cya.authorization.backend import canonical_id
from cya.checklists.models import ChecklistTemplate, Compartment, KitInstance, VehicleSystem
from cya.checklists.overlay import (
    NoEffectiveVersionError,
    ResolutionError,
    resolve_checklist,
)
from cya.deficiencies import deadlines, notices
from cya.deficiencies.models import (
    CheckSuspension,
    Deficiency,
    DeficiencyAffectedItem,
    DeficiencyAgeRule,
    DeficiencyCategory,
    DeficiencyEvent,
    DeficiencyEvidence,
    DeficiencyLink,
    DeficiencyLock,
    DeficiencyProjection,
    DeficiencyScopeExpansion,
)
from cya.deficiencies.severity import resolve_severity
from cya.deficiencies.transitions import (
    ACTIVE_STATUSES,
    AWAITING_EXTERNAL_DEPENDENCY,
    OPEN,
    validate_transition,
)
from cya.kits.history import active_kits, slot_of_reference_kit
from cya.operations.models import MAX_TEXT

MAX_SUMMARY = 200
MAX_SEVERITY_CODE = 30
MAX_RELATED = 50
MAX_SYSTEMS = 50
MAX_EXPANSIONS = 2000  # supervisor-expanded items one correction will include
MAX_SUGGESTIONS = 20
MAX_REGRADES = 1000
MAX_ASSIGNMENT_EVENTS = 5000
MAX_DEPENDENCY_EVENTS = 5000
MAX_PROGRESS_NOTES = 5000
MAX_DEADLINE_EVENTS = 5000
MAX_SUSPENSIONS_IN_FORCE = 200  # check suspensions one deficiency may hold at once (SPEC 8.9)


class DeficiencyError(ValueError):
    """A rule was violated; the calling command rolls back whole.

    code is the stable wire code, status the HTTP status, path a safe schema path.
    """

    def __init__(self, code, message, *, status=422, path=None):
        super().__init__(message)
        self.code, self.status, self.path = code, status, path


def malformed(path, message):
    return DeficiencyError("MALFORMED_REQUEST", message, status=400, path=path)


def canonical(value, path):
    try:
        return canonical_id(value)
    except ValueError:
        raise malformed(path, f"{path} must be a canonical lowercase UUID") from None


def text(value, path, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise malformed(path, f"{path} must be nonblank text of at most {limit} characters")
    return value


def severity_code(value, path):
    """Shape of a severity level code; whether it names an active level is checked in prepare."""
    if not isinstance(value, str) or not 0 < len(value) <= MAX_SEVERITY_CODE:
        raise malformed(path, f"{path} must be a severity level code")
    return value


def report_fields(value, path):
    """The validated {category_id, severity, summary, description} of a new deficiency."""
    if not isinstance(value, dict) or set(value) != {
        "category_id",
        "severity",
        "summary",
        "description",
    }:
        raise malformed(path, "create takes category_id, severity, summary and description")
    return {
        "category_id": canonical(value["category_id"], f"{path}.category_id"),
        "severity": severity_code(value["severity"], f"{path}.severity"),
        "summary": text(value["summary"], f"{path}.summary", MAX_SUMMARY),
        "description": text(value["description"], f"{path}.description", MAX_TEXT),
    }


def check_selection(value):
    """Shape of a check's `deficiency`: ("existing", id) or ("create", fields); None if absent."""
    if value is None:
        return None
    if not isinstance(value, dict) or len(value) != 1:
        raise malformed("deficiency", "deficiency is exactly one of existing_id or create")
    if "existing_id" in value:
        return "existing", canonical(value["existing_id"], "deficiency.existing_id")
    if "create" in value:
        return "create", report_fields(value["create"], "deficiency.create")
    raise malformed("deficiency", "deficiency is exactly one of existing_id or create")


def resolve_severity_level(organization_id, code, now, path="severity"):
    """The active severity revision in force for code; a retired or unknown code is refused."""
    level = resolve_severity(organization_id, code, now)
    if level is None:
        raise DeficiencyError(
            "INVALID_SEVERITY", "choose one of the current severity levels", path=path
        )
    return level


def resolve_category(organization_id, stable_category_id, now):
    """The category revision in force at now; its supply flag is never client-supplied."""
    category = (
        DeficiencyCategory.objects.filter(
            organization_id=organization_id,
            stable_category_id=stable_category_id,
            effective_at__lte=now,
        )
        .order_by("-revision")
        .first()
    )
    if category is None:
        raise DeficiencyError(
            "UNKNOWN_CATEGORY", "the deficiency category was not found", path="category_id"
        )
    return category


def lock_deficiencies(organization_id, deficiency_ids):
    """Lock each named deficiency (sorted by id) and return {id: projection}.

    A deficiency of another organization looks the same as one that does not exist (404). A
    deficiency without its lock row or projection is a configuration failure, never created
    lazily.
    """
    ids = sorted(set(deficiency_ids))
    locked = {
        str(row)
        for row in DeficiencyLock.objects.select_for_update()
        .filter(organization_id=organization_id, deficiency_id__in=ids)
        .order_by("deficiency_id")
        .values_list("deficiency_id", flat=True)
    }
    for deficiency_id in ids:
        exists = Deficiency.objects.filter(organization_id=organization_id, pk=deficiency_id)
        if deficiency_id not in locked:
            if exists.exists():
                raise DeficiencyError(
                    "CONFIGURATION_UNAVAILABLE", "deficiency lock is unavailable", status=503
                )
            raise DeficiencyError("NOT_FOUND", "deficiency was not found", status=404)
    projections = {
        str(p.deficiency_id): p
        for p in DeficiencyProjection.objects.filter(
            organization_id=organization_id, deficiency_id__in=ids
        )
    }
    if set(projections) != set(ids):
        raise DeficiencyError(
            "CONFIGURATION_UNAVAILABLE", "deficiency state is unavailable", status=503
        )
    return projections


def require_active(projection):
    """Evidence attaches only to a deficiency that is still being worked."""
    if projection.status not in ACTIVE_STATUSES:
        raise DeficiencyError(
            "DEFICIENCY_NOT_ACTIVE",
            "the deficiency is closed or cancelled; report a new one",
            path="deficiency",
        )


def stamp(receipt, current):
    return {
        "command": receipt,
        "recorded_at": receipt.recorded_at,
        "actor_user": current.user,
        "actor_roles_at_time": list(current.roles),
    }


def insert_deficiency(
    receipt,
    current,
    *,
    organization_id,
    deficiency_id,
    event_id,
    operation_id,
    vehicle_id,
    category,
    severity,
    fields,
    affected,
    item_id=None,
    kit_id=None,
    compartment_id=None,
    assignee_id=None,
    deadline=None,
):
    """Create the header, its lock, scope snapshot, initial Open event and projection.

    affected is a list of (item_id, definition_id) pairs. Returns the header. The projection is
    written last because it points at the event it summarizes.
    """
    validate_transition(None, OPEN)
    fields_of_command = stamp(receipt, current)
    header = Deficiency.objects.create(
        id=deficiency_id,
        organization_id=organization_id,
        operation_id=operation_id,
        reported_at=receipt.recorded_at,
        reporter=current.user,
        category_version=category,
        severity_version=severity,
        summary=fields["summary"],
        description=fields["description"],
        vehicle_id=vehicle_id,
        item_id=item_id,
        kit_id=kit_id,
        compartment_id=compartment_id,
        **fields_of_command,
    )
    DeficiencyLock.objects.create(organization_id=organization_id, deficiency=header)
    for scope_item_id, definition_id in affected:
        DeficiencyAffectedItem.objects.create(
            organization_id=organization_id,
            deficiency=header,
            item_id=scope_item_id,
            definition_id=definition_id,
            **fields_of_command,
        )
    event = DeficiencyEvent.objects.create(
        id=event_id,
        organization_id=organization_id,
        deficiency=header,
        sequence=1,
        event_type="REPORTED",
        to_status=OPEN,
        assignee_id=assignee_id,
        deadline=deadline,
        **fields_of_command,
    )
    DeficiencyProjection.objects.create(
        organization_id=organization_id,
        deficiency=header,
        status=OPEN,
        assignee_id=assignee_id,
        deadline=deadline,
        severity_version=severity,
        last_applied_event=event,
    )
    return header


def add_check_evidence(receipt, current, *, organization_id, deficiency_id, check_id):
    """Record that a Failed or Unable check supports the deficiency (DEF-3); evidence stays."""
    return DeficiencyEvidence.objects.create(
        organization_id=organization_id,
        deficiency_id=deficiency_id,
        check_record_id=check_id,
        **stamp(receipt, current),
    )


def canonical_pair(first_id, second_id):
    """The two ids low first; the stored order that makes one pair one set of rows."""
    return (first_id, second_id) if first_id < second_id else (second_id, first_id)


def latest_link(organization_id, lower_id, upper_id):
    """The newest link row of a pair (the one nothing follows), or None if never linked."""
    rows = list(
        DeficiencyLink.objects.filter(
            organization_id=organization_id, lower_id=lower_id, upper_id=upper_id
        )
    )
    followed = {row.prior_link_event_id for row in rows}
    tips = [row for row in rows if row.pk not in followed]
    if len(tips) > 1:
        raise DeficiencyError(
            "CONFIGURATION_UNAVAILABLE", "link history is unavailable", status=503
        )
    return tips[0] if tips else None


def is_linked(organization_id, lower_id, upper_id):
    tip = latest_link(organization_id, lower_id, upper_id)
    return tip is not None and tip.link_type == "ADD"


def check_link_change(organization_id, first_id, second_id, link_type):
    """The pair's newest link row, after refusing an ADD of a linked pair or REMOVE of an unlinked."""
    if first_id == second_id:
        raise DeficiencyError("SELF_LINK", "a deficiency cannot be linked to itself")
    lower_id, upper_id = canonical_pair(first_id, second_id)
    tip = latest_link(organization_id, lower_id, upper_id)
    linked = tip is not None and tip.link_type == "ADD"
    if link_type == "ADD" and linked:
        raise DeficiencyError("ALREADY_LINKED", "the deficiencies are already linked", status=409)
    if link_type == "REMOVE" and not linked:
        raise DeficiencyError("NOT_LINKED", "the deficiencies are not linked", status=409)
    return tip


def insert_link(receipt, current, *, organization_id, first_id, second_id, link_type, link_id):
    """Append an ADD or REMOVE for the pair. Informational only: no other row is touched."""
    tip = check_link_change(organization_id, first_id, second_id, link_type)
    lower_id, upper_id = canonical_pair(first_id, second_id)
    return DeficiencyLink.objects.create(
        id=link_id,
        organization_id=organization_id,
        lower_id=lower_id,
        upper_id=upper_id,
        link_type=link_type,
        prior_link_event=tip,
        **stamp(receipt, current),
    )


def _effective_items(organization_id, vehicle_id, now):
    """Items the vehicle's published checklists require now, first template by id wins.

    A template with no version in force does not apply to this vehicle. Any other resolution
    failure, a missing vehicle slot included, is surfaced as CHECKLIST_UNAVAILABLE: skipping it
    could leave the scope snapshot incomplete.
    """
    items = {}
    templates = ChecklistTemplate.objects.filter(organization_id=organization_id).order_by("pk")
    for template in templates:
        try:
            resolved = resolve_checklist(
                organization_id=organization_id,
                vehicle_id=vehicle_id,
                template_id=template.pk,
                as_of=now,
            )
        except NoEffectiveVersionError:
            continue
        except ResolutionError as error:
            raise DeficiencyError(
                "CHECKLIST_UNAVAILABLE", str(error), status=409, path="target"
            ) from None
        for item in resolved.items:
            # The checklist overlay's kit list is a mutable current projection. Correction scope
            # is an immutable snapshot at this instant, so resolve physical instances from their
            # append-only assignment history instead.
            requirement = item.requirement
            if requirement.kit_id:
                slot_id = slot_of_reference_kit(requirement.kit_id)
                active = active_kits(organization_id, vehicle_id, slot_id, now) if slot_id else ()
                labels = {
                    str(pk): label
                    for pk, label in KitInstance.objects.filter(
                        organization_id=organization_id,
                        pk__in=[kit.kit_id for kit in active],
                    ).values_list("pk", "label")
                }
                item = item.__class__(
                    requirement=item.requirement,
                    compartment_id=item.compartment_id,
                    compartment_label=item.compartment_label,
                    system_id=item.system_id,
                    system_label=item.system_label,
                    effective_quantity=item.effective_quantity,
                    deviation_id=item.deviation_id,
                    kits=tuple((kit.kit_id, labels[kit.kit_id]) for kit in active),
                )
            items.setdefault(str(requirement.item_id), item)
    return items


def lock_checklist_templates(organization_id):
    """Serialize correction-time resolution with publication of existing templates."""
    return list(
        ChecklistTemplate.objects.select_for_update()
        .filter(organization_id=organization_id)
        .order_by("pk")
        .values_list("pk", flat=True)
    )


def parse_target(target):
    """("item"|"kit"|"compartment"|"systems", value) from exactly one target form."""
    bad = malformed(
        "target", "target is exactly one of item_id, kit_id, compartment_id or vehicle+system_ids"
    )
    if not isinstance(target, dict):
        raise bad
    keys = set(target)
    if keys == {"item_id"}:
        return "item", canonical(target["item_id"], "target.item_id")
    if keys == {"compartment_id"}:
        return "compartment", canonical(target["compartment_id"], "target.compartment_id")
    if keys == {"kit_id"}:
        return "kit", canonical(target["kit_id"], "target.kit_id")
    if keys == {"vehicle", "system_ids"} and target["vehicle"] is True:
        ids = target["system_ids"]
        if not isinstance(ids, list) or not 0 < len(ids) <= MAX_SYSTEMS:
            raise malformed("target.system_ids", f"system_ids is a list of 1 to {MAX_SYSTEMS} ids")
        ordered = sorted({canonical(v, "target.system_ids") for v in ids})
        if len(ordered) != len(ids):
            raise malformed("target.system_ids", "system_ids must not repeat")
        return "systems", ordered
    raise bad


def resolve_scope(organization_id, vehicle_id, target, now):
    """Server-resolved header targets and affected-item snapshot for an independent report.

    Returns ({item_id, compartment_id}, [(item_id, definition_id)]). An empty scope is refused:
    a deficiency that affects nothing could never be verified.
    """
    kind, value = target
    items = _effective_items(organization_id, vehicle_id, now)
    header = {"item_id": None, "kit_id": None, "compartment_id": None}
    if kind == "item":
        match = items.get(value)
        selected = [match] if match is not None else []
        header["item_id"] = value
    elif kind == "compartment":
        row = Compartment.objects.filter(
            organization_id=organization_id, vehicle_id=vehicle_id, pk=value
        ).first()
        if row is None:
            raise DeficiencyError(
                "NOT_FOUND", "compartment was not found", status=404, path="target.compartment_id"
            )
        slots = {
            str(pk): slot
            for pk, slot in Compartment.objects.filter(
                organization_id=organization_id, vehicle_id=vehicle_id
            ).values_list("pk", "stable_slot_id")
        }
        selected = [
            i for i in items.values() if slots.get(str(i.compartment_id)) == row.stable_slot_id
        ]
        header["compartment_id"] = value
    elif kind == "kit":
        kit = KitInstance.objects.filter(organization_id=organization_id, pk=value).first()
        if kit is None or not kit.active or str(kit.vehicle_id) != vehicle_id:
            raise DeficiencyError(
                "NOT_FOUND", "kit was not found", status=404, path="target.kit_id"
            )
        header["kit_id"] = value
        slot = kit.compartment.stable_slot_id if kit.compartment_id else None
        selected = [
            i
            for i in items.values()
            if i.requirement.kit_id
            and slot_of_reference_kit(i.requirement.kit_id) == slot
            and any(kit_id == value for kit_id, _label in i.kits)
        ]
    else:
        rows = list(
            VehicleSystem.objects.filter(
                organization_id=organization_id, vehicle_id=vehicle_id, pk__in=value
            )
        )
        if len(rows) != len(value):
            raise DeficiencyError(
                "NOT_FOUND", "vehicle system was not found", status=404, path="target.system_ids"
            )
        wanted = {row.stable_slot_id for row in rows}
        slots = {
            str(pk): slot
            for pk, slot in VehicleSystem.objects.filter(
                organization_id=organization_id, vehicle_id=vehicle_id
            ).values_list("pk", "stable_slot_id")
        }
        selected = [i for i in items.values() if slots.get(str(i.system_id)) in wanted]
    if not selected:
        raise DeficiencyError(
            "EMPTY_SCOPE",
            "the target covers no item of the vehicle's checklist",
            path="target",
        )
    affected = sorted(
        (str(i.requirement.item_id), str(i.requirement.definition_id)) for i in selected
    )
    return header, affected


def correction_scope(organization_id, deficiency, now):
    """Resolve correction-time rows as (item, definition, physical kit instance or None).

    Vehicle-system scope intentionally uses the report-time affected-item rows. Kit and
    compartment scopes use today's effective checklist and assignments; an item added after
    reporting is therefore included. An item in two physical kits produces two rows.
    """
    current = _effective_items(organization_id, str(deficiency.vehicle_id), now)
    selected = []
    if deficiency.kit_id:
        kit = KitInstance.objects.filter(
            organization_id=organization_id, pk=deficiency.kit_id, vehicle_id=deficiency.vehicle_id
        ).first()
        if kit is None or not kit.active:
            return []
        slot = kit.compartment.stable_slot_id if kit.compartment_id else None
        for item in current.values():
            requirement = item.requirement
            if requirement.kit_id and slot_of_reference_kit(requirement.kit_id) == slot:
                if any(kit_id == str(kit.pk) for kit_id, _label in item.kits):
                    selected.append((item, requirement.definition_id, str(kit.pk)))
    elif deficiency.compartment_id:
        target = Compartment.objects.filter(
            organization_id=organization_id,
            pk=deficiency.compartment_id,
            vehicle_id=deficiency.vehicle_id,
        ).first()
        if target is None:
            raise DeficiencyError(
                "CONFIGURATION_UNAVAILABLE", "deficiency compartment is unavailable", status=503
            )
        slots = {
            str(pk): slot
            for pk, slot in Compartment.objects.filter(
                organization_id=organization_id, vehicle_id=deficiency.vehicle_id
            ).values_list("pk", "stable_slot_id")
        }
        for item in current.values():
            if slots.get(str(item.compartment_id)) != target.stable_slot_id:
                continue
            if item.requirement.kit_id:
                selected.extend(
                    (item, item.requirement.definition_id, kit_id) for kit_id, _label in item.kits
                )
            else:
                selected.append((item, item.requirement.definition_id, None))
    elif deficiency.item_id:
        item = current.get(str(deficiency.item_id))
        if item is not None:
            if item.requirement.kit_id:
                selected.extend(
                    (item, item.requirement.definition_id, kit_id) for kit_id, _label in item.kits
                )
            else:
                selected.append((item, item.requirement.definition_id, None))
    else:
        reported = list(
            DeficiencyAffectedItem.objects.filter(
                organization_id=organization_id, deficiency_id=deficiency.pk
            ).select_related("definition")
        )
        for affected in reported:
            # The report-time definition is part of the immutable affected-item snapshot. Keep
            # that definition even if later publications remove or re-place the item.
            if affected.definition.kit_id:
                slot = slot_of_reference_kit(affected.definition.kit_id)
                kits = (
                    active_kits(organization_id, str(deficiency.vehicle_id), slot, now)
                    if slot
                    else ()
                )
                selected.extend((affected, str(affected.definition_id), kit.kit_id) for kit in kits)
            else:
                selected.append((affected, str(affected.definition_id), None))
    rows = {}
    for item, definition_id, kit_id in selected:
        item_id = str(item.requirement.item_id if hasattr(item, "requirement") else item.item_id)
        rows[(item_id, str(definition_id), kit_id)] = None
    # Supervisor expansions stay with the deficiency (VER-3, amendment 1.22.0): each expanded item
    # joins, resolved like the rest of the correction-time scope, unless the scope above already
    # holds the same item and instance.
    present = {(item_id, kit_id) for item_id, _definition, kit_id in rows}
    for item_id, definition_id, kit_id in _expansions(organization_id, deficiency, current):
        if (item_id, kit_id) not in present:
            rows[(item_id, definition_id, kit_id)] = None
            present.add((item_id, kit_id))
    return sorted(rows, key=lambda row: (row[0], row[1], row[2] or ""))


def _expansions(organization_id, deficiency, current):
    """(item, definition, kit or None) of the deficiency's supervisor expansions that apply now.

    Like every correction-time row (VER-3), an expanded pair is resolved against today's checklist
    and kit placement: the item must still be required on the vehicle, with its current definition,
    and a kit instance must still hold that item there (active, in the slot the definition names).
    A kit moved elsewhere or retired therefore drops out instead of blocking the correction.
    """
    rows = list(
        DeficiencyScopeExpansion.objects.filter(
            organization_id=organization_id, deficiency_id=deficiency.pk
        )
        .order_by("recorded_at", "pk")
        .values_list("item_id", "kit_instance_id")[: MAX_EXPANSIONS + 1]
    )
    if len(rows) > MAX_EXPANSIONS:
        raise DeficiencyError(
            "CONFIGURATION_UNAVAILABLE", "the scope expansions are too many to use", status=503
        )
    applicable = []
    for item_id, kit_id in rows:
        entry = current.get(str(item_id))
        if entry is None:
            continue
        kits = {str(k) for k, _label in entry.kits}
        kit = None if kit_id is None else str(kit_id)
        if (kit is None) != (not entry.requirement.kit_id) or (kit is not None and kit not in kits):
            continue
        applicable.append((str(item_id), str(entry.requirement.definition_id), kit))
    return applicable


def suggest_related(organization_id, vehicle_id, item_id=None):
    """Possible matches for a new deficiency: read-only, the crew member decides (DEF-1).

    Active deficiencies of the vehicle, newest first, narrowed to those whose scope covers the
    item when one is named. Nothing here links, attaches or selects anything.
    """
    rows = DeficiencyProjection.objects.filter(
        organization_id=organization_id,
        deficiency__vehicle_id=vehicle_id,
        status__in=ACTIVE_STATUSES,
    )
    if item_id is not None:
        covered = DeficiencyAffectedItem.objects.filter(
            organization_id=organization_id, item_id=item_id
        ).values("deficiency_id")
        rows = rows.filter(deficiency_id__in=covered)
    rows = rows.select_related("deficiency", "severity_version").order_by(
        "-deficiency__reported_at", "-deficiency_id"
    )
    return [
        {
            "deficiency_id": str(row.deficiency_id),
            "summary": row.deficiency.summary,
            "severity": row.severity_version.code,
            "status": row.status,
            "reported_at": row.deficiency.reported_at,
        }
        for row in rows[:MAX_SUGGESTIONS]
    ]


def suspensions_in_force(organization_id, deficiency_id):
    """The deficiency's check suspension STARTs that no END names yet, oldest first.

    Call under the deficiency lock, which every writer of a START or an END holds. The command that
    starts suspensions keeps the number in force within MAX_SUSPENSIONS_IN_FORCE, so more is
    corrupt state and refused rather than cut, because a closing event must list every one.
    """
    ended = CheckSuspension.objects.filter(
        organization_id=organization_id, deficiency_id=deficiency_id, action="END"
    ).values("prior_suspension_id")
    rows = list(
        CheckSuspension.objects.filter(
            organization_id=organization_id, deficiency_id=deficiency_id, action="START"
        )
        .exclude(pk__in=ended)
        .order_by("recorded_at", "id")[: MAX_SUSPENSIONS_IN_FORCE + 1]
    )
    if len(rows) > MAX_SUSPENSIONS_IN_FORCE:
        raise DeficiencyError(
            "CONFIGURATION_UNAVAILABLE", "check suspensions are unavailable", status=503
        )
    return rows


def suspension_ids_in_force(organization_id, deficiency_id):
    """What a closing event lists in suspensions_ended: the ids in force, sorted (SUS-3, SUS-4)."""
    return sorted(str(row.pk) for row in suspensions_in_force(organization_id, deficiency_id))


def end_suspensions(receipt, current, *, organization_id, event):
    """Write one END per START the closing event lists, in the closing command's transaction."""
    ids = list(event.suspensions_ended)
    if not ids:
        return []
    starts = list(
        CheckSuspension.objects.filter(
            organization_id=organization_id,
            deficiency_id=event.deficiency_id,
            action="START",
            pk__in=ids,
        ).order_by("id")
    )
    if len(starts) != len(ids):
        raise DeficiencyError(
            "CONFIGURATION_UNAVAILABLE", "check suspensions are unavailable", status=503
        )
    ends = [
        CheckSuspension(
            organization_id=organization_id,
            deficiency_id=start.deficiency_id,
            vehicle_id=start.vehicle_id,
            item_id=start.item_id,
            definition_id=start.definition_id,
            kit_instance_id=start.kit_instance_id,
            instance_key=start.instance_key,
            action="END",
            deficiency_event=event,
            prior_suspension=start,
            **stamp(receipt, current),
        )
        for start in starts
    ]
    CheckSuspension.objects.bulk_create(ends)
    return ends


def last_event(deficiency_id):
    """The deficiency's newest event: what a client's expected event is compared with."""
    event = (
        DeficiencyEvent.objects.filter(deficiency_id=deficiency_id).order_by("-sequence").first()
    )
    if event is None:
        raise DeficiencyError(
            "CONFIGURATION_UNAVAILABLE", "deficiency state is unavailable", status=503
        )
    return event


def insert_regrade(
    receipt,
    current,
    *,
    organization_id,
    event_id,
    last,
    previous,
    severity,
    explanation,
):
    """Append a SEVERITY_CHANGED event after the last one and move the projection's level.

    The status, assignee, deadline, links and evidence are not touched (DEF-8). The projection
    points at the new event, so it is updated last.
    """
    event = DeficiencyEvent.objects.create(
        id=event_id,
        organization_id=organization_id,
        deficiency_id=last.deficiency_id,
        sequence=last.sequence + 1,
        predecessor_event=last,
        event_type="SEVERITY_CHANGED",
        from_status=last.to_status,
        to_status=last.to_status,
        explanation=explanation,
        previous_severity_version=previous,
        severity_version=severity,
        **stamp(receipt, current),
    )
    DeficiencyProjection.objects.filter(
        organization_id=organization_id, deficiency_id=last.deficiency_id
    ).update(severity_version=severity, last_applied_event=event)
    return event


def insert_lifecycle(
    receipt,
    current,
    *,
    organization_id,
    event_id,
    last,
    event_type,
    to_status,
    assignee_id=None,
    deadline=None,
):
    """Append an ASSIGNED, DEADLINE_SET or WORK_STARTED event and move the projection (DEF-4).

    Only the field the event carries changes in the projection, which points at the new event and
    is updated last. A status change goes through the one SPEC 8.2 table first.
    """
    if to_status != last.to_status:
        validate_transition(last.to_status, to_status)
    event = DeficiencyEvent.objects.create(
        id=event_id,
        organization_id=organization_id,
        deficiency_id=last.deficiency_id,
        sequence=last.sequence + 1,
        predecessor_event=last,
        event_type=event_type,
        from_status=last.to_status,
        to_status=to_status,
        assignee_id=assignee_id,
        deadline=deadline,
        **stamp(receipt, current),
    )
    changes = {"last_applied_event": event, "status": to_status}
    if event_type == "ASSIGNED":
        changes["assignee_id"] = assignee_id
    if event_type == "DEADLINE_SET":
        changes["deadline"] = deadline
    DeficiencyProjection.objects.filter(
        organization_id=organization_id, deficiency_id=last.deficiency_id
    ).update(**changes)
    notices.notify_event(receipt, event)  # ASSIGNED only (issue 14)
    return event


def insert_dependency_event(
    receipt,
    current,
    *,
    organization_id,
    event_id,
    last,
    event_type,
    to_status,
    explanation=None,
    follow_up_at=None,
):
    """Append a dependency or progress-note event and move the projection (SPEC 8.5, 8.1).

    Only the status can change in the projection, which points at the new event and is updated
    last; the assignee and the deadline are never touched, so a dependency update cannot move
    the deadline or the escalation (SPEC 8.5). A status change goes through the one SPEC 8.2
    table first.
    """
    if to_status != last.to_status:
        validate_transition(last.to_status, to_status)
    event = DeficiencyEvent.objects.create(
        id=event_id,
        organization_id=organization_id,
        deficiency_id=last.deficiency_id,
        sequence=last.sequence + 1,
        predecessor_event=last,
        event_type=event_type,
        from_status=last.to_status,
        to_status=to_status,
        explanation=explanation,
        follow_up_at=follow_up_at,
        **stamp(receipt, current),
    )
    DeficiencyProjection.objects.filter(
        organization_id=organization_id, deficiency_id=last.deficiency_id
    ).update(last_applied_event=event, status=to_status)
    return event


def insert_extension_event(
    receipt,
    current,
    *,
    organization_id,
    event_id,
    last,
    event_type,
    explanation,
    proposed_deadline=None,
    deadline=None,
    decision=None,
    request_event_id=None,
):
    """Append an extension request or decision (SPEC DEF-5); the status does not change.

    The projection's status, assignee and deadline stay as they are: the base deadline is still the
    last revision's, and the governing deadline is derived from the events (cya.deficiencies
    .deadlines), so only the pointer to the newest event moves.
    """
    event = DeficiencyEvent.objects.create(
        id=event_id,
        organization_id=organization_id,
        deficiency_id=last.deficiency_id,
        sequence=last.sequence + 1,
        predecessor_event=last,
        event_type=event_type,
        from_status=last.to_status,
        to_status=last.to_status,
        explanation=explanation,
        proposed_deadline=proposed_deadline,
        deadline=deadline,
        decision=decision,
        request_event_id=request_event_id,
        **stamp(receipt, current),
    )
    DeficiencyProjection.objects.filter(
        organization_id=organization_id, deficiency_id=last.deficiency_id
    ).update(last_applied_event=event)
    notices.notify_event(receipt, event)  # extension requested or decided (issue 14)
    return event


def replay_deadlines(events):
    """deadline_state, with a history no command could have written refused as unavailable."""
    try:
        return deadlines.deadline_state(events)
    except deadlines.InconsistentHistory:
        raise DeficiencyError(
            "CONFIGURATION_UNAVAILABLE", "deadline state is unavailable", status=503
        ) from None


def highlight_of(rule, age_seconds, extension_count):
    """deadlines.highlight, with a stored rule no command could have written refused as 503."""
    try:
        return deadlines.highlight(rule, age_seconds, extension_count)
    except deadlines.InconsistentHistory:
        raise DeficiencyError(
            "CONFIGURATION_UNAVAILABLE", "the age rule is unavailable", status=503
        ) from None


DEADLINE_EVENT_TYPES = (
    "REPORTED",
    "DEADLINE_SET",
    "VERIFICATION_ADDED",
    "EXTENSION_REQUESTED",
    "EXTENSION_DECIDED",
)
DEPENDENCY_EVENT_TYPES = ("DEPENDENCY_ENTERED", "DEPENDENCY_REVISED", "DEPENDENCY_ENDED")


def deadline_events(organization_id, deficiency_id, as_of=None):
    """The newest event and the deadline-bearing and extension events up to as_of, oldest first."""
    return _events(
        organization_id, deficiency_id, as_of, MAX_DEADLINE_EVENTS, types=DEADLINE_EVENT_TYPES
    )


def extension_state(organization_id, deficiency_id, as_of=None, now=None):
    """Deadline, extensions, overdue state and age of one deficiency, replayed (DEF-5, DEF-6).

    as_of (an aware instant) answers for that moment, from the events recorded by then; without it
    the answer is for now. The projection's status is a cache of the status this returns.
    last_event_id is what a lifecycle command must name as expected. Bounded: past the cap the
    answer is refused, never truncated.
    """
    newest, events = deadline_events(organization_id, deficiency_id, as_of)
    at = as_of if as_of is not None else (now or timezone.now())
    header = Deficiency.objects.get(organization_id=organization_id, pk=deficiency_id)
    state = replay_deadlines(events)
    age_seconds = deadlines.age_seconds(header.reported_at, at)
    rule = age_rule_at(organization_id, at)
    return {
        "deficiency_status": newest.to_status,
        "last_event_id": str(newest.pk),
        "as_of": at,
        "reported_at": header.reported_at,
        "age_seconds": age_seconds,
        "overdue": deadlines.is_overdue(state["governing_deadline"], newest.to_status, at),
        **highlight_of(rule, age_seconds, state["extension_count"]),
        **state,
    }


def age_rule_at(organization_id, at):
    """The age-highlight revision in force at an instant, or None when none was configured."""
    return (
        DeficiencyAgeRule.objects.filter(organization_id=organization_id, recorded_at__lte=at)
        .order_by("-revision")
        .first()
    )


def entering_event(deficiency_id):
    """The event that put the deficiency into Awaiting External Dependency, or None.

    It is the newest DEPENDENCY_ENTERED event: that type is the only way into the status, and a
    later one is possible only after the dependency ended. Its from_status is the status the
    deficiency returns to when the caller names none.
    """
    return (
        DeficiencyEvent.objects.filter(deficiency_id=deficiency_id, event_type="DEPENDENCY_ENTERED")
        .order_by("-sequence")
        .first()
    )


def _events(organization_id, deficiency_id, as_of, cap, types=None):
    """The deficiency's events up to as_of, oldest first, refused (503) past cap, never cut."""
    header = Deficiency.objects.filter(organization_id=organization_id, pk=deficiency_id).first()
    if header is None or (as_of is not None and header.reported_at > as_of):
        raise DeficiencyError("NOT_FOUND", "deficiency was not found", status=404)
    events = DeficiencyEvent.objects.filter(
        organization_id=organization_id, deficiency_id=deficiency_id
    )
    if as_of is not None:
        events = events.filter(recorded_at__lte=as_of)
    # The newest event is read first so a concurrent append can only make last_event_id older
    # than the rows, never newer (a command that names it then fails closed with a 409).
    newest = events.order_by("-sequence").first()
    if newest is None:
        raise DeficiencyError(
            "CONFIGURATION_UNAVAILABLE", "deficiency state is unavailable", status=503
        )
    rows = events.filter(sequence__lte=newest.sequence)
    if types is not None:
        rows = rows.filter(event_type__in=types)
    rows = list(rows.order_by("sequence")[: cap + 1])
    if len(rows) > cap:
        raise DeficiencyError(
            "CONFIGURATION_UNAVAILABLE", "the history is too long to return", status=503
        )
    return newest, rows


def replay_dependency(events):
    """(the dependency record after these events, their history), oldest first (SPEC 8.5).

    events are this deficiency's DEPENDENCY_* events. The record only applies while the
    deficiency's newest status is Awaiting External Dependency; the caller checks that.
    """
    record = None
    history = []
    for event in events:
        if event.event_type == "DEPENDENCY_ENTERED":
            record = {
                "explanation": event.explanation,
                "follow_up_at": event.follow_up_at,
                "entered_event_id": str(event.pk),
                "entered_at": event.recorded_at,
                "entered_by": str(event.actor_user_id),
                "returns_to": event.from_status,
            }
        elif event.event_type == "DEPENDENCY_REVISED":
            if record is None:
                raise DeficiencyError(
                    "CONFIGURATION_UNAVAILABLE", "dependency state is unavailable", status=503
                )
            record.update(
                explanation=event.explanation,
                follow_up_at=event.follow_up_at,
                revised_event_id=str(event.pk),
                revised_at=event.recorded_at,
                revised_by=str(event.actor_user_id),
            )
        else:
            record = None
        history.append(
            {
                "event_id": str(event.pk),
                "event_type": event.event_type,
                "recorded_at": event.recorded_at,
                "actor_user_id": str(event.actor_user_id),
                "from_status": event.from_status,
                "to_status": event.to_status,
                "explanation": event.explanation,
                "follow_up_at": event.follow_up_at,
            }
        )
    return record, history


def dependency_state(organization_id, deficiency_id, as_of=None):
    """The current Awaiting External Dependency record and its history, replayed (SPEC 8.5).

    The record is the entering event as revised by the later DEPENDENCY_REVISED events, and exists
    only while the deficiency awaits: a DEPENDENCY_ENDED event clears it, as does any later event
    that leaves the status (the final status decides), and every earlier record
    stays in the history. The projection's status is a cache of the status this returns. as_of (an
    aware instant) answers for that moment. Bounded: past the cap the answer is refused, never
    truncated. last_event_id is the event a lifecycle command must name as expected.
    """
    newest, events = _events(
        organization_id,
        deficiency_id,
        as_of,
        MAX_DEPENDENCY_EVENTS,
        types=DEPENDENCY_EVENT_TYPES,
    )
    record, history = replay_dependency(events)
    return {
        "deficiency_status": newest.to_status,
        "awaiting_external_dependency": newest.to_status == AWAITING_EXTERNAL_DEPENDENCY,
        # The record exists only while the deficiency awaits: a later close or reopen by an event
        # that does not mention the dependency (issue 152 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/152)) leaves it in the history, never current.
        "dependency": record if newest.to_status == AWAITING_EXTERNAL_DEPENDENCY else None,
        "last_event_id": str(newest.pk),
        "history": history,
    }


def progress_notes(organization_id, deficiency_id, as_of=None):
    """Every progress note of one deficiency, oldest first, with who wrote it and when (SPEC 8.1).

    Notes are append-only events; none is edited or removed. as_of answers for that moment.
    Bounded: past the cap the answer is refused, never truncated.
    """
    newest, events = _events(
        organization_id, deficiency_id, as_of, MAX_PROGRESS_NOTES, types=("PROGRESS_NOTE",)
    )
    return {
        "deficiency_status": newest.to_status,
        "last_event_id": str(newest.pk),
        "notes": [
            {
                "event_id": str(event.pk),
                "recorded_at": event.recorded_at,
                "actor_user_id": str(event.actor_user_id),
                "note": event.explanation,
            }
            for event in events
        ],
    }


def assignment_state(organization_id, deficiency_id, as_of=None):
    """Assignee, deadline and status of one deficiency, replayed from its events (DEF-4, AC-27).

    The creation event may carry the first assignee and deadline; each ASSIGNED and DEADLINE_SET
    event after it replaces one (and a VERIFICATION_ADDED reopening sets a new deadline, SPEC 8.7), and every earlier value stays in the history. as_of (an aware
    instant) answers for that moment: later events and a deficiency not yet reported are ignored
    (404). The projection's assignee, deadline and status are a cache of exactly this answer.
    last_event_id is the event a lifecycle command must name as expected; it is read first, so a
    concurrent append can only make it older than the history, never newer. Bounded: past the cap
    the answer is refused, never truncated. An unassigned deficiency reports null, never a guess.
    """
    header = Deficiency.objects.filter(organization_id=organization_id, pk=deficiency_id).first()
    if header is None or (as_of is not None and header.reported_at > as_of):
        raise DeficiencyError("NOT_FOUND", "deficiency was not found", status=404)
    events = DeficiencyEvent.objects.filter(
        organization_id=organization_id, deficiency_id=deficiency_id
    )
    if as_of is not None:
        events = events.filter(recorded_at__lte=as_of)
    events = list(events.order_by("sequence")[: MAX_ASSIGNMENT_EVENTS + 1])
    if len(events) > MAX_ASSIGNMENT_EVENTS:
        raise DeficiencyError(
            "CONFIGURATION_UNAVAILABLE", "assignment history is too long to return", status=503
        )
    if not events or events[0].event_type != "REPORTED":
        raise DeficiencyError(
            "CONFIGURATION_UNAVAILABLE", "deficiency state is unavailable", status=503
        )
    assignee_id = deadline = None
    history = []
    for event in events:
        if event.event_type == "REPORTED":
            assignee_id, deadline = event.assignee_id, event.deadline
        elif event.event_type == "ASSIGNED":
            assignee_id = event.assignee_id
        elif event.event_type in ("DEADLINE_SET", "VERIFICATION_ADDED"):
            # Added verification after closure sets the new resolution deadline (SPEC 8.7).
            deadline = event.deadline
        elif event.event_type in (
            "ADMINISTRATOR_CLOSED",
            "ADMINISTRATOR_REOPENED",
            "EMPTY_SCOPE_CLOSED",
        ):
            history.append(
                {
                    "event_id": str(event.pk),
                    "event_type": event.event_type,
                    "recorded_at": event.recorded_at,
                    "actor_user_id": str(event.actor_user_id),
                    "from_status": event.from_status,
                    "to_status": event.to_status,
                    "assignee_id": None if assignee_id is None else str(assignee_id),
                    "deadline": deadline,
                    "explanation": event.explanation,
                    "suspensions_ended": event.suspensions_ended,
                }
            )
            continue
        else:
            continue
        if event.event_type != "REPORTED" or assignee_id or deadline:
            history.append(
                {
                    "event_id": str(event.pk),
                    "event_type": event.event_type,
                    "recorded_at": event.recorded_at,
                    "actor_user_id": str(event.actor_user_id),
                    "assignee_id": None if event.assignee_id is None else str(event.assignee_id),
                    "deadline": event.deadline,
                }
            )
    return {
        "assignee_id": None if assignee_id is None else str(assignee_id),
        "deadline": deadline,
        "deficiency_status": events[-1].to_status,
        "last_event_id": str(events[-1].pk),
        "history": history,
    }


def severity_level(row):
    return {
        "code": row.code,
        "revision": row.revision,
        "label": row.label,
        "description": row.description,
        "rank": row.rank,
    }


def severity_history(organization_id, deficiency_id):
    """The current level and every grade of one deficiency, replayed from its events (DEF-8).

    The first entry is the grade it was reported under; each re-grade follows with the level it
    left, the one it set, the explanation, the actor and the instant. The projection's level is a
    cache of exactly this answer. Bounded: past the cap the answer is refused, never truncated.
    last_event_id is what a re-grade must name as its expected event; it is read before the
    history, so a concurrent append can only make that id older than the history (the re-grade
    is then refused as stale), never newer.
    """
    header = (
        Deficiency.objects.filter(organization_id=organization_id, pk=deficiency_id)
        .select_related("severity_version")
        .first()
    )
    if header is None:
        raise DeficiencyError("NOT_FOUND", "deficiency was not found", status=404)
    newest = last_event(deficiency_id)
    events = list(
        DeficiencyEvent.objects.filter(
            organization_id=organization_id,
            deficiency_id=deficiency_id,
            event_type="SEVERITY_CHANGED",
        )
        .select_related("previous_severity_version", "severity_version")
        .order_by("sequence")[: MAX_REGRADES + 1]
    )
    if len(events) > MAX_REGRADES:
        raise DeficiencyError(
            "CONFIGURATION_UNAVAILABLE", "severity history is too long to return", status=503
        )
    history = [
        {
            "event_id": None,
            "recorded_at": header.recorded_at,
            "actor_user_id": str(header.actor_user_id),
            "previous": None,
            "severity": severity_level(header.severity_version),
            "explanation": None,
        }
    ]
    history += [
        {
            "event_id": str(e.pk),
            "recorded_at": e.recorded_at,
            "actor_user_id": str(e.actor_user_id),
            "previous": severity_level(e.previous_severity_version),
            "severity": severity_level(e.severity_version),
            "explanation": e.explanation,
        }
        for e in events
    ]
    return {
        "severity": history[-1]["severity"],
        "deficiency_status": newest.to_status,
        "last_event_id": str(newest.pk),
        "history": history,
    }


def instant(value, path):
    """A timezone-aware instant, or None."""
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise malformed(path, f"{path} must be a timezone-aware instant")
    return value
