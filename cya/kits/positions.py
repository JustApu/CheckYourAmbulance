"""Compartment and system positions: add, revise and retire (SPEC 3.0.0 4.8 CFG-2, issue #130).

A position is a stable slot on a vehicle (SPEC 4.2). Its rows are revisions that are never
updated or deleted. add_position gives a vehicle its row in an existing keyed position, or
creates a new position with the key its creator enters (IMP-6, SPEC 3.0.0).
revise_position appends a revision that changes any of label, display order and retired,
keeping the stable slot and a compartment's sealable designation (CAB-1).

A position cannot be retired while anything published still uses it on that vehicle (CFG-2):
- a version, in force or published for later, of any template bound to the vehicle (including
  by a binding recorded to take effect later), or one of the vehicle's overrides, places an
  item there;
- a kit list in force or published for later names it;
- an active kit instance is in it.
retired_blockers lists them. The reverse checks, so that publishing, binding and placing never
use a retired position, are in the writers themselves, through refuse_retired.

Locks: the vehicle row is the shared lock (docs/configuration-management.md). Every command
here locks it before reading anything it checks. add_position also takes the organization
lock first, because position keys are organization-wide.
"""

from datetime import datetime

from django.utils import timezone

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id
from cya.authorization.persistence import digest
from cya.checklists.models import (
    ChecklistItemDef,
    ChecklistPublication,
    Compartment,
    KitInstance,
    Vehicle,
    VehicleSystem,
)
from cya.core.audit_commands import Plan, run_command
from cya.kits import definitions
from cya.kits import keys as key_rules
from cya.kits.models import (
    POSITION_KINDS,
    PositionKey,
    VehicleKitListPublication,
    VehicleKitRequirement,
)

ACTION = "manage_vehicle_positions"
ADD_COMMAND = "POSITION_ADD"
REVISE_COMMAND = "POSITION_REVISE"
SLOT_MODELS = {"COMPARTMENT": Compartment, "SYSTEM": VehicleSystem}
ITEM_FIELD = {"COMPARTMENT": "compartment", "SYSTEM": "system"}
MAX_LABEL = 200
MAX_ORDER = 1_000_000


class PositionError(ValueError):
    """A rule was violated; the command rolls back and is audited as failed."""


def _kind(kind):
    if kind not in POSITION_KINDS:
        raise PositionError(f"kind must be one of {list(POSITION_KINDS)}")
    return kind


def _label(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_LABEL:
        raise PositionError(f"label must be nonblank text of at most {MAX_LABEL} characters")
    return value.strip()


def _order(value):
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= MAX_ORDER:
        raise PositionError(f"display_order must be a whole number from 1 to {MAX_ORDER}")
    return value


def tail(kind, vehicle_id, stable_slot_id):
    """The vehicle's newest revision of a slot, including one scheduled for later, or None."""
    return (
        SLOT_MODELS[kind]
        .objects.filter(vehicle_id=vehicle_id, stable_slot_id=stable_slot_id)
        .order_by("-revision")
        .first()
    )


def retired_slots(kind, vehicle_id, stable_slot_ids):
    """The subset of slots whose newest revision on the vehicle is retired."""
    newest = {}
    for row in (
        SLOT_MODELS[kind]
        .objects.filter(vehicle_id=vehicle_id, stable_slot_id__in=set(stable_slot_ids))
        .order_by("stable_slot_id", "-revision")
        .only("stable_slot_id", "retired")
    ):
        newest.setdefault(str(row.stable_slot_id), row.retired)
    return {slot for slot, retired in newest.items() if retired}


def refuse_retired(kind, vehicle_id, row_ids, error, what):
    """Raise `error` when any referenced slot row's position is retired on the vehicle.

    row_ids are slot rows of any vehicle (a template definition references one vehicle's row);
    each names a stable slot, and the vehicle's newest revision of that slot decides.
    """
    slot_of = dict(
        SLOT_MODELS[kind]
        .objects.filter(pk__in=[r for r in row_ids if r])
        .values_list("pk", "stable_slot_id")
    )
    retired = retired_slots(kind, vehicle_id, {str(s) for s in slot_of.values()})
    if retired:
        labels = sorted(
            SLOT_MODELS[kind]
            .objects.filter(vehicle_id=vehicle_id, stable_slot_id__in=retired)
            .order_by("stable_slot_id", "-revision")
            .values_list("label", flat=True)
            .distinct()
        )
        raise error(f"{what} uses a retired {kind.lower()} on vehicle {vehicle_id}: {labels}")


def _in_force_and_later(publications, now, group):
    """The publications in force at `now` (latest effective per group) plus every later one."""
    current, later = {}, []
    for publication in publications:
        if publication.effective_at > now:
            later.append(publication)
        else:
            key = group(publication)
            best = current.get(key)
            if best is None or publication.effective_at > best.effective_at:
                current[key] = publication
    return list(current.values()) + later


def _bound_now_or_later(revisions, now):
    """True when the binding is in force at `now` or a later revision schedules a BIND.

    revisions are one (vehicle, template, slot) chain in revision order. The revision in force
    is the latest one effective by `now` (as cya.obligations.requirements._bound_at), so an
    UNBIND recorded for later does not hide a BIND still in force (PR #157 review).
    """
    in_force = None
    for row in revisions:
        if row.effective_at <= now:
            in_force = row
    if in_force is not None and in_force.action == "BIND":
        return True
    return any(row.effective_at > now and row.action == "BIND" for row in revisions)


def _binding_chains(**filters):
    from cya.obligations.models import RequirementBinding

    chains = {}
    for binding in RequirementBinding.objects.filter(**filters).order_by(
        "vehicle_id", "template_id", "slot", "revision"
    ):
        key = (str(binding.vehicle_id), str(binding.template_id), binding.slot)
        chains.setdefault(key, []).append(binding)
    return chains


def templates_bound_now_or_later(organization_id, vehicle_id, now=None):
    """Templates bound to the vehicle at `now` or by a binding scheduled to take effect later."""
    now = now or timezone.now()
    chains = _binding_chains(organization_id=organization_id, vehicle_id=vehicle_id)
    return sorted(
        {template for (_v, template, _s), rows in chains.items() if _bound_now_or_later(rows, now)}
    )


def retired_blockers(organization_id, vehicle_id, kind, stable_slot_id, now):
    """What published still uses the position on the vehicle (CFG-2), as readable reasons."""
    org, slot = str(organization_id), str(stable_slot_id)
    reasons = []
    rows = set(
        SLOT_MODELS[kind]
        .objects.filter(organization_id=org, stable_slot_id=slot)
        .values_list("pk", flat=True)
    )
    templates = templates_bound_now_or_later(org, vehicle_id, now)
    versions = _in_force_and_later(
        ChecklistPublication.objects.filter(
            checklist_version__template_id__in=templates
        ).select_related("checklist_version"),
        now,
        lambda p: str(p.checklist_version.template_id),
    )
    overrides = _in_force_and_later(
        ChecklistPublication.objects.filter(override_version__vehicle_id=vehicle_id).select_related(
            "override_version__template_version"
        ),
        now,
        lambda p: str(p.override_version.template_version.template_id),
    )
    field = f"{ITEM_FIELD[kind]}_id__in"
    for publication in versions:
        version = publication.checklist_version
        if ChecklistItemDef.objects.filter(checklist_version=version, **{field: rows}).exists():
            reasons.append(
                f"checklist template {version.template_id} version {version.revision} "
                f"(effective {publication.effective_at:%Y-%m-%d %H:%M} UTC) places items there"
            )
    for publication in overrides:
        override = publication.override_version
        if ChecklistItemDef.objects.filter(override_version=override, **{field: rows}).exists():
            reasons.append(
                f"vehicle override revision {override.revision} "
                f"(effective {publication.effective_at:%Y-%m-%d %H:%M} UTC) places items there"
            )
    if kind == "COMPARTMENT":
        kit_lists = _in_force_and_later(
            VehicleKitListPublication.objects.filter(version__vehicle_id=vehicle_id).select_related(
                "version"
            ),
            now,
            lambda p: "kit list",
        )
        for publication in kit_lists:
            if VehicleKitRequirement.objects.filter(
                version=publication.version, compartment_id__in=rows
            ).exists():
                reasons.append(
                    f"kit list revision {publication.version.revision} "
                    f"(effective {publication.effective_at:%Y-%m-%d %H:%M} UTC) names it"
                )
        kits = sorted(
            KitInstance.objects.filter(
                vehicle_id=vehicle_id, active=True, compartment_id__in=rows
            ).values_list("label", flat=True)
        )
        if kits:
            reasons.append(f"active kits are in it: {kits}")
    return reasons


def _locked_vehicle(organization_id, vehicle_id):
    vehicle = (
        Vehicle.objects.select_for_update()
        .filter(organization_id=organization_id, pk=vehicle_id)
        .first()
    )
    if vehicle is None:
        raise PositionError(f"vehicle {vehicle_id} does not exist in the organization")
    return vehicle


def _run(context, command_type, request, build, *, lock_organization=False):
    canonical_id(context["actor_id"])
    return run_command(
        organization_id=context["organization_id"],
        operation_id=context["operation_id"],
        command_type=command_type,
        request_digest=digest(request),
        correlation_id=context["correlation_id"],
        request_attempt_id=context["request_attempt_id"],
        authorize=definitions.authorizer(
            context["organization_id"],
            context["actor_id"],
            ACTION,
            lock_organization=lock_organization,
        ),
        prepare=build,
    )


def _plan(organization_id, body, apply):
    return Plan(
        response_body=body,
        apply=apply,
        target_type="ORGANIZATION",
        target_id=organization_id,
        new_snapshot={"schema_version": 1, "values": body},
    )


def add_position(*, vehicle_id, kind, label, key, display_order=None, **context):
    """Give the vehicle its row in the position `key`, creating the position if it is new.

    The creator enters the key of a new position (IMP-6, SPEC 3.0.0); an existing key adds
    this vehicle's row in that position. The row is revision 1, effective when recorded.
    """
    canonical_id(vehicle_id)
    kind, label, order = _kind(kind), _label(label), _order(display_order)
    key = key_rules.check_key(key, "position key")
    org = context["organization_id"]

    def build(actor):
        vehicle = _locked_vehicle(org, vehicle_id)
        existing = PositionKey.objects.filter(organization_id=org, kind=kind, key=key).first()
        slot = str(existing.stable_slot_id) if existing else new_id()
        if (
            existing
            and SLOT_MODELS[kind].objects.filter(vehicle=vehicle, stable_slot_id=slot).exists()
        ):
            raise PositionError(
                f"the vehicle already has position {key!r}; revise it instead of adding it"
            )
        row_id, key_id = new_id(), None if existing else new_id()

        def apply(receipt, current):
            # The row first: the position_key insert guard requires the slot to exist.
            SLOT_MODELS[kind].objects.create(
                id=row_id,
                organization_id=org,
                vehicle=vehicle,
                stable_slot_id=slot,
                revision=1,
                label=label,
                effective_at=receipt.recorded_at,
                display_order=order,
            )
            if key_id is not None:
                PositionKey.objects.create(
                    id=key_id,
                    organization_id=org,
                    kind=kind,
                    stable_slot_id=slot,
                    key=key,
                    command=receipt,
                    recorded_at=receipt.recorded_at,
                    actor_user=current.user,
                    actor_roles_at_time=list(current.roles),
                )

        body = {
            "code": ADD_COMMAND,
            "vehicle_id": vehicle_id,
            "kind": kind,
            "key": key,
            "stable_slot_id": slot,
            "row_id": row_id,
            "new_position": existing is None,
            "label": label,
            "display_order": order,
        }
        return _plan(org, body, apply)

    request = {
        "vehicle_id": vehicle_id,
        "kind": kind,
        "label": label,
        "key": key,
        "display_order": order,
    }
    return _run(context, ADD_COMMAND, request, build, lock_organization=True)


UNSET = object()


def revise_position(
    *,
    vehicle_id,
    kind,
    stable_slot_id,
    label=UNSET,
    display_order=UNSET,
    retired=UNSET,
    **context,
):
    """Append a revision changing any of label, display order and retired (CFG-2).

    The revision takes effect when recorded and keeps the slot and, for a compartment, its
    sealable designation. It is refused while a later revision of the slot is scheduled, when
    it changes nothing, and, when it retires the position, while anything published uses it.
    """
    canonical_id(vehicle_id)
    canonical_id(stable_slot_id)
    kind = _kind(kind)
    wanted = {}
    if label is not UNSET:
        wanted["label"] = _label(label)
    if display_order is not UNSET:
        wanted["display_order"] = _order(display_order)
    if retired is not UNSET:
        if type(retired) is not bool:
            raise PositionError("retired must be true or false")
        wanted["retired"] = retired
    if not wanted:
        raise PositionError("name at least one of label, display_order and retired")
    org = context["organization_id"]

    def build(actor):
        _locked_vehicle(org, vehicle_id)
        now = timezone.now()
        newest = tail(kind, vehicle_id, stable_slot_id)
        if newest is None:
            raise PositionError("the vehicle has no such position")
        if newest.effective_at > now:
            raise PositionError("a later revision of this position is already scheduled")
        values = {
            "label": newest.label,
            "display_order": newest.display_order,
            "retired": newest.retired,
            **wanted,
        }
        if all(getattr(newest, name) == value for name, value in values.items()):
            raise PositionError("the position already has those values")
        if values["retired"] and not newest.retired:
            reasons = retired_blockers(org, vehicle_id, kind, stable_slot_id, now)
            if reasons:
                raise PositionError("the position is still in use: " + "; ".join(reasons))
        row_id, revision = new_id(), newest.revision + 1
        extra = {"sealable": newest.sealable} if kind == "COMPARTMENT" else {}

        def apply(receipt, _current):
            if receipt.recorded_at < newest.effective_at:
                raise PositionError("a later revision of this position is already scheduled")
            SLOT_MODELS[kind].objects.create(
                id=row_id,
                organization_id=org,
                vehicle_id=vehicle_id,
                stable_slot_id=stable_slot_id,
                revision=revision,
                effective_at=receipt.recorded_at,
                **values,
                **extra,
            )

        body = {
            "code": REVISE_COMMAND,
            "vehicle_id": vehicle_id,
            "kind": kind,
            "stable_slot_id": stable_slot_id,
            "row_id": row_id,
            "revision": revision,
            **values,
        }
        return _plan(org, body, apply)

    request = {
        "vehicle_id": vehicle_id,
        "kind": kind,
        "stable_slot_id": stable_slot_id,
        **wanted,
    }
    return _run(context, REVISE_COMMAND, request, build)


def positions(organization_id, vehicle_id, kind, at=None):
    """The vehicle's positions of one kind in effect at `at`, in display order.

    Each is its latest revision effective by then; retired positions are included and flagged,
    so callers decide whether to show them. Order: display_order (empty last), then label.
    """
    at = at or timezone.now()
    if not isinstance(at, datetime):
        raise PositionError("at must be a datetime")
    latest = {}
    for row in (
        SLOT_MODELS[kind]
        .objects.filter(
            organization_id=organization_id, vehicle_id=vehicle_id, effective_at__lte=at
        )
        .order_by("stable_slot_id", "-revision")
    ):
        latest.setdefault(str(row.stable_slot_id), row)
    keys = dict(
        PositionKey.objects.filter(organization_id=organization_id, kind=kind).values_list(
            "stable_slot_id", "key"
        )
    )
    rows = sorted(
        latest.values(),
        key=lambda r: (r.display_order is None, r.display_order or 0, r.label, str(r.pk)),
    )
    return [(row, keys.get(row.stable_slot_id)) for row in rows]


def vehicles_bound_now_or_later(organization_id, template_id, now=None, vehicle_ids=None):
    """Vehicles bound to the template at `now` or by a binding scheduled to take effect later.

    Inactive vehicles are included: a binding survives deactivation, and reactivation would
    make a retired position required again. vehicle_ids, when given, limits the check to those
    vehicles with a bounded read (bound_vehicles_among, SPEC NFR-6).
    """
    now = now or timezone.now()
    if vehicle_ids is not None:
        return bound_vehicles_among(organization_id, template_id, vehicle_ids, now)
    chains = _binding_chains(organization_id=organization_id, template_id=template_id)
    return sorted(
        {vehicle for (vehicle, _t, _s), rows in chains.items() if _bound_now_or_later(rows, now)}
    )


def bound_vehicles_among(organization_id, template_id, vehicle_ids, now=None):
    """Which of vehicle_ids are bound to the template at `now` or by a later BIND (SPEC NFR-6).

    The same rule as _bound_now_or_later: the revision in force on a (vehicle, template, slot)
    chain is its highest revision effective by `now`, and a later BIND also counts. The database
    does the per-chain work, so the rows read are at most the distinct vehicle ids given, however
    long the binding history is.
    """
    ids = [str(v) for v in vehicle_ids]
    if not ids:
        return []
    return _bound_vehicle_ids(organization_id, template_id, now, vehicle_ids=ids)


def bound_vehicles_first(organization_id, template_id, limit, now=None):
    """(up to `limit` vehicles bound to the template now or later, whether there are more).

    Bounded (SPEC NFR-6): the database finds bound vehicles directly, and at most limit + 1 ids
    of each kind (in force, later) are read, however many vehicles or revisions there are.
    """
    ids = _bound_vehicle_ids(organization_id, template_id, now, limit=limit + 1)
    return ids[:limit], len(ids) > limit


def _bound_vehicle_ids(organization_id, template_id, now=None, vehicle_ids=None, limit=None):
    from django.db.models import F, OuterRef, Subquery

    from cya.obligations.models import RequirementBinding

    now = now or timezone.now()
    chains = RequirementBinding.objects.filter(
        organization_id=organization_id, template_id=template_id
    )
    if vehicle_ids is not None:
        chains = chains.filter(vehicle_id__in=vehicle_ids)
    newest = (
        RequirementBinding.objects.filter(
            vehicle_id=OuterRef("vehicle_id"),
            template_id=OuterRef("template_id"),
            slot=OuterRef("slot"),
            effective_at__lte=now,
        )
        .order_by("-revision")
        .values("revision")[:1]
    )
    in_force = (
        chains.filter(effective_at__lte=now, action="BIND")
        .annotate(newest=Subquery(newest))
        .filter(revision=F("newest"))
        .order_by("vehicle_id")
        .values_list("vehicle_id", flat=True)
        .distinct()
    )
    later = (
        chains.filter(effective_at__gt=now, action="BIND")
        .order_by("vehicle_id")
        .values_list("vehicle_id", flat=True)
        .distinct()
    )
    if limit is not None:
        in_force, later = in_force[:limit], later[:limit]
    found = sorted({str(v) for v in in_force} | {str(v) for v in later})
    return found[:limit] if limit is not None else found


def template_slot_rows(organization_id, template_id, now):
    """{kind: slot row ids} placed by the template's versions in force at `now` or later."""
    versions = _in_force_and_later(
        ChecklistPublication.objects.filter(
            organization_id=organization_id, checklist_version__template_id=template_id
        ),
        now,
        lambda p: "template",
    )
    defs = ChecklistItemDef.objects.filter(
        checklist_version_id__in=[p.checklist_version_id for p in versions]
    )
    return {
        "COMPARTMENT": set(
            defs.exclude(compartment_id=None).values_list("compartment_id", flat=True)
        ),
        "SYSTEM": set(defs.exclude(system_id=None).values_list("system_id", flat=True)),
    }
