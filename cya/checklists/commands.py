"""Audited checklist commands: draft, publish, override and stocking deviation.

Each function is the server boundary for a future UI handler. It takes IDs and
intent, never client role snapshots, and runs through cya.core.audit_commands
.run_command, which owns the outermost transaction, the operation-id receipt
(replays return the original result) and the success/failure audit. Do not wrap
them in a transaction or authorized_request. Permission comes from the current
database roles (SPEC 3.3: draft_edit_template, publish_template,
approve_stock_deviation). Audit evidence targets the organization and carries a
bounded snapshot of what changed.

Locks are taken in prepare, before validation: the template, version, override
or vehicle row is locked FOR UPDATE so concurrent drafts get distinct revisions
and a publish cannot race an edit of the same version. Time-dependent rules run
in apply against the receipt's recorded_at, because that instant is assigned
after prepare. Validation failures raise ChecklistValidationError, roll the
command back and are audited by run_command as a failed command.
"""

from datetime import datetime

from django.db.models import Max
from django.utils import timezone

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id, load_state
from cya.authorization.guard import PermissionDenied, Principal, require_permission
from cya.authorization.locking import locked_identities
from cya.authorization.persistence import DatabaseRoleBackend, digest
from cya.authorization.policy import Role
from cya.checklists.models import (
    ChecklistItemDef,
    ChecklistPublication,
    ChecklistTemplate,
    ChecklistVersion,
    Item,
    StockingDeviation,
    Vehicle,
    VehicleOverrideItem,
    VehicleOverrideVersion,
)
from cya.checklists.overlay import (
    OverrideEntry,
    apply_overlay,
    requirement_from_definition,
    requirement_from_fields,
    resolve_checklist,
    vehicle_slots,
)
from cya.checklists.ownership import require_legacy_writable, require_template_writable
from cya.checklists.signals import announce_publication
from cya.checklists.validation import (
    MAX_ITEMS,
    ChecklistValidationError,
    is_count,
    reference,
    validate_definition,
    validate_definitions,
)
from cya.core.audit_commands import Actor, CommandDenied, Plan, run_command
from cya.kits.models import ChecklistKitPlacement
from cya.kits.placements import (
    check_publishable,
    placed_content_item_ids,
    placements_of,
    validate_placements,
)

MAX_EXPLANATION = 4000
MAX_ITEM_LABEL = 200  # item.label
OVERRIDE_ACTIONS = ("ADD", "REMOVE", "REPLACE")


def _snapshot(**values):
    return {"schema_version": 1, "values": values}


def _locked(model, organization_id, pk, what):
    try:
        return model.objects.select_for_update().get(organization_id=organization_id, pk=pk)
    except model.DoesNotExist:
        raise ChecklistValidationError(f"{what} {pk} does not exist in the organization") from None


def _aware(value, field):
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ChecklistValidationError(f"{field} must be a timezone-aware datetime")
    return value


def _execute(
    *,
    organization_id,
    actor_id,
    operation_id,
    correlation_id,
    request_attempt_id,
    action,
    command_type,
    intent,
    build,
    lock_organization=False,
):
    for value in (organization_id, actor_id):
        canonical_id(value)
    backend = DatabaseRoleBackend()

    def authorize(org):
        # The actor is locked (no role change here), after the organization when a rule spans
        # it; current roles are read after the lock.
        with locked_identities(
            organization_id, [actor_id], lock_organization=lock_organization
        ) as (
            current_org,
            users,
        ):
            actor_user = users[actor_id]
            state = load_state(backend, actor_user)
            roles = frozenset(Role.CREW if r == Role.EMT else r for r in state.grants)
            actor = Actor(user=actor_user, roles=tuple(sorted(r.value for r in roles)))
            principal = Principal(
                organization_id, actor_id, roles, actor_user.active and current_org.active
            )
            try:
                require_permission(principal, organization_id, action)
            except PermissionDenied:
                raise CommandDenied(actor) from None
            return actor

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type=command_type,
        request_digest=digest(intent),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=authorize,
        prepare=build,
    )


def _plan(organization_id, body, apply, **snapshot):
    return Plan(
        response_body={"code": "SAVED", **body},
        apply=apply,
        target_type="ORGANIZATION",
        target_id=organization_id,
        new_snapshot=_snapshot(**snapshot),
    )


def _next_revision(queryset):
    return (queryset.aggregate(top=Max("revision"))["top"] or 0) + 1


def _is_published(**target):
    return ChecklistPublication.objects.filter(**target).exists()


# --- template drafts -------------------------------------------------------------------------


def _with_new_items(org, items):
    """(specs with item ids, {new item id: label}) for specs naming a new item by its label.

    A spec may give "new_item": a label, in place of "item_id"; the command creates that item,
    as a vehicle-file import creates an item by label (issue #130 part 6). A label that an active
    item already has is refused, so the same thing does not get two identities.
    """
    if not isinstance(items, list):
        return items, {}
    labels = [s["new_item"] for s in items if isinstance(s, dict) and "new_item" in s]
    if not labels:
        return items, {}
    created, specs = {}, []
    for spec in items:
        if not isinstance(spec, dict) or "new_item" not in spec:
            specs.append(spec)
            continue
        if "item_id" in spec:
            raise ChecklistValidationError("give either item_id or new_item, not both")
        label = spec["new_item"]
        if not isinstance(label, str) or not label.strip() or len(label.strip()) > MAX_ITEM_LABEL:
            raise ChecklistValidationError(
                f"a new item's name must be nonblank text of at most {MAX_ITEM_LABEL} characters"
            )
        label = label.strip()
        if label in created.values():
            raise ChecklistValidationError(f"the new item {label!r} is named twice")
        item_id = new_id()
        created[item_id] = label
        specs.append({**{k: v for k, v in spec.items() if k != "new_item"}, "item_id": item_id})
    from cya.kits.library_import import _active_items_by_label

    taken = sorted(
        label
        for label, rows in _active_items_by_label(org, list(created.values())).items()
        if any(active for _pk, active in rows)
    )
    if taken:
        raise ChecklistValidationError(
            f"an active item is already named {taken}; choose it instead of a new item"
        )
    return specs, created


def save_template_draft(*, template_id, items, placements=None, base_revision=None, **context):
    """Create the next draft revision of a template with its complete item set (CHK-2).

    placements (optional) names the kit definitions this version inspects (SPEC 4.6 KDEF-4).
    An item spec may name a new item by label ("new_item") instead of an item_id; the command
    creates it. base_revision (optional) is the template's latest revision the edit started
    from (0 for none): if another version was saved since, the save is refused rather than
    silently replacing that change.
    """
    canonical_id(template_id)
    org = context["organization_id"]
    if base_revision is not None and not is_count(base_revision):
        raise ChecklistValidationError("base_revision must be a nonnegative integer")
    creates_items = isinstance(items, list) and any(
        isinstance(s, dict) and "new_item" in s for s in items
    )

    def build(actor):
        template = _locked(ChecklistTemplate, org, template_id, "template")
        if not template.active:
            raise ChecklistValidationError("template is inactive")
        # SPEC 5.0.0: a legacy template is read-only after the cutover; a vehicle checklist
        # places its items on its own vehicle or leaves them Not placed (CHK-8, CHK-9).
        require_template_writable(org, template, error=ChecklistValidationError)
        latest = _next_revision(ChecklistVersion.objects.filter(template=template)) - 1
        if base_revision is not None and base_revision != latest:
            raise ChecklistValidationError(
                "the template was changed since this edit started; reload it and try again"
            )
        specs, new_items = _with_new_items(org, items)
        definitions = validate_definitions(
            org,
            specs,
            frozenset(new_items),
            str(template.vehicle_id) if template.vehicle_id else None,
        )
        placed = validate_placements(org, placements) if placements is not None else []
        revision = latest + 1
        version_id = new_id()

        def apply(receipt, current_actor):
            Item.objects.bulk_create(
                Item(id=item_id, organization_id=org, label=label, active=True)
                for item_id, label in new_items.items()
            )
            ChecklistVersion.objects.create(
                id=version_id,
                organization_id=org,
                template=template,
                revision=revision,
                created_at=receipt.recorded_at,
            )
            ChecklistItemDef.objects.bulk_create(
                ChecklistItemDef(
                    id=new_id(), organization_id=org, checklist_version_id=version_id, **fields
                )
                for fields in definitions
            )
            if placed:
                ChecklistKitPlacement.objects.bulk_create(
                    ChecklistKitPlacement(
                        id=new_id(), organization_id=org, checklist_version_id=version_id, **fields
                    )
                    for fields in placed
                )

        body = {"version_id": version_id, "revision": revision, "item_count": len(definitions)}
        extra = {}
        if new_items:
            # Only when there are any, so an existing kind of save keeps its result and snapshot.
            body["new_item_ids"] = sorted(new_items)
            extra["new_items"] = [{"item_id": k, "label": v} for k, v in sorted(new_items.items())]
        return _plan(
            org,
            body,
            apply,
            action="TEMPLATE_DRAFT_SAVED",
            template_id=template_id,
            version_id=version_id,
            revision=revision,
            item_count=len(definitions),
            placement_count=len(placed),
            **extra,
        )

    intent = {"template_id": template_id, "items": items}
    # Each only when given, so the digest of an existing kind of request is unchanged.
    if placements is not None:
        intent["placements"] = placements
    if base_revision is not None:
        intent["base_revision"] = base_revision
    return _execute(
        action="draft_edit_template",
        command_type="CHECKLIST_DRAFT_SAVE",
        intent=intent,
        build=build,
        # New items: an active item label must stay unique, and that rule spans the
        # organization, so take its lock first (docs/configuration-management.md, level 1).
        lock_organization=creates_items,
        **context,
    )


def _final_effective_at(requested, receipt):
    """Effective instant: the request, or the publication instant; never in the past."""
    final = requested or receipt.recorded_at
    if final < receipt.recorded_at:
        raise ChecklistValidationError("effective_at must not be earlier than the publication")
    return final


def _require_after_latest(publications, final):
    """Strict ordering within one stream; callers hold the stream's parent lock.

    The locking read is a current read, so a publication committed after this transaction's
    snapshot is still seen once the parent lock is ours.
    """
    latest = (
        publications.select_for_update()
        .order_by("-effective_at")
        .values_list("effective_at", flat=True)
        .first()
    )
    if latest is not None and final <= latest:
        raise ChecklistValidationError(
            f"effective_at must be later than the latest published effective instant {latest}"
        )


def _require_slots(requirements, vehicle_ids, as_of):
    """Every vehicle must have an effective slot row for each placement (full resolved set)."""
    compartments = {r.compartment_id for r in requirements if r.compartment_id}
    systems = {r.system_id for r in requirements if r.system_id}
    for vehicle_id in vehicle_ids:
        slots = vehicle_slots("compartment", vehicle_id, compartments, as_of)
        slots.update(vehicle_slots("system", vehicle_id, systems, as_of))
        absent = sorted(ref for ref, row in slots.items() if row is None)
        if absent:
            raise ChecklistValidationError(
                f"vehicle {vehicle_id} has no effective slot for {absent} at {as_of}"
            )


def _refuse_retired(requirements, vehicle_ids):
    """No placement may use a position retired on a vehicle it applies to (SPEC 4.8 CFG-2)."""
    from cya.kits.positions import refuse_retired

    compartments = [r.compartment_id for r in requirements if r.compartment_id]
    systems = [r.system_id for r in requirements if r.system_id]
    for vehicle_id in vehicle_ids:
        refuse_retired("COMPARTMENT", vehicle_id, compartments, ChecklistValidationError, "it")
        refuse_retired("SYSTEM", vehicle_id, systems, ChecklistValidationError, "it")


def _lock_vehicles(org, vehicle_ids):
    """Lock vehicle rows in id order: the shared configuration lock, taken before templates."""
    list(
        Vehicle.objects.select_for_update()
        .filter(organization_id=org, pk__in=sorted({str(v) for v in vehicle_ids}))
        .order_by("pk")
    )


def _active_vehicle_ids(org):
    return list(
        Vehicle.objects.filter(organization_id=org, active=True).values_list("pk", flat=True)
    )


def _link_item_requirements(org, owner, version, definitions, effective_at, receipt, actor):
    """SPEC 6.0.0 CHK-11: link the rows to their vehicle item requirements (item_requirements)."""
    from cya.checklists import item_requirements

    if not item_requirements.has_cutover(org):
        return
    try:
        item_requirements.link_rows(
            organization_id=org,
            vehicle_id=str(owner),
            template_id=version.template_id,
            rows=definitions,
            effective_at=effective_at,
            receipt=receipt,
            actor=actor,
        )
    except item_requirements.ItemRequirementError as error:
        raise ChecklistValidationError(str(error)) from error


def publish_template_version(*, version_id, effective_at=None, **context):
    """Publish a draft: validate the resolved set, assign effective_at, freeze (CHK-2, CHK-3).

    effective_at None means effective at the publication instant.
    """
    canonical_id(version_id)
    org = context["organization_id"]
    requested = _aware(effective_at, "effective_at")

    def build(actor):
        version = _locked(ChecklistVersion, org, version_id, "checklist version")
        if _is_published(checklist_version=version):
            raise ChecklistValidationError("checklist version is already published")
        definitions = list(ChecklistItemDef.objects.filter(checklist_version=version))
        if not definitions:
            raise ChecklistValidationError("cannot publish a version without items")
        from cya.kits.positions import vehicles_bound_now_or_later

        owner = (
            ChecklistTemplate.objects.filter(pk=version.template_id)
            .values_list("vehicle_id", flat=True)
            .get()
        )
        # SPEC 4.8 CFG-2: the version applies to the vehicles bound to its template, now or by a
        # binding recorded for later, active or not. Lock them before the template, as
        # bind_requirement does (vehicle, then template), so a retirement and this
        # publication serialize on the vehicle row. A vehicle checklist applies to its own
        # vehicle only, assigned or not (SPEC 5.0.0 CHK-8, CFG-9).
        applies_to = (
            [str(owner)] if owner else vehicles_bound_now_or_later(org, version.template_id)
        )
        _lock_vehicles(org, applies_to)
        template = _locked(ChecklistTemplate, org, version.template_id, "checklist template")
        require_template_writable(org, template, error=ChecklistValidationError)
        # Issue #164: a binding committed after that first read added a vehicle we did not
        # lock or check (bind_requirement takes the template lock after the vehicle, so it has
        # finished by now). Locking that vehicle here would invert the vehicle-then-template
        # order, so refuse; publishing again reads the new set from the start.
        added = {str(v) for v in vehicles_bound_now_or_later(org, version.template_id)} - {
            str(v) for v in applies_to
        }
        if added and not owner:
            raise ChecklistValidationError(
                "a vehicle was bound to this template while it was being published; publish again"
            )
        placements = placements_of(version.pk)
        check_publishable(placements)
        publication_id = new_id()

        def apply(receipt, current_actor):
            final = _final_effective_at(requested, receipt)
            _require_after_latest(
                ChecklistPublication.objects.filter(checklist_version__template=version.template),
                final,
            )
            requirements = [requirement_from_definition(d, "TEMPLATE") for d in definitions]
            # A shared template must fit every active vehicle; a vehicle checklist its own.
            _require_slots(requirements, applies_to if owner else _active_vehicle_ids(org), final)
            _refuse_retired(requirements, applies_to)
            if owner:
                _link_item_requirements(
                    org, owner, version, definitions, final, receipt, current_actor
                )
            ChecklistPublication.objects.create(
                id=publication_id,
                organization_id=org,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current_actor.user,
                actor_roles_at_time=list(current_actor.roles),
                checklist_version=version,
                effective_at=final,
            )
            from cya.checks.locks import ensure_item_locks

            ensure_item_locks(
                org,
                applies_to if owner else _active_vehicle_ids(org),
                [definition.item_id for definition in definitions]
                + placed_content_item_ids(placements),
            )

        return _plan(
            org,
            {
                "publication_id": publication_id,
                "version_id": version_id,
                "effective_immediately": requested is None,
            },
            apply,
            action="TEMPLATE_PUBLISHED",
            version_id=version_id,
            publication_id=publication_id,
        )

    result = _execute(
        action="publish_template",
        command_type="CHECKLIST_PUBLISH",
        intent={
            "version_id": version_id,
            "effective_at": requested.isoformat() if requested else None,
        },
        build=build,
        **context,
    )
    announce_publication(result, org)
    return result


# --- vehicle overrides ---------------------------------------------------------------------


def _validated_entry(org, entry, new_item_ids=frozenset()):
    if not isinstance(entry, dict) or set(entry) - {"item_id", "action", "definition"}:
        raise ChecklistValidationError("override entries take item_id, action and definition")
    action, definition = entry.get("action"), entry.get("definition")
    if action not in OVERRIDE_ACTIONS:
        raise ChecklistValidationError(f"action must be one of {list(OVERRIDE_ACTIONS)}")
    if (action == "REMOVE") != (definition is None):
        raise ChecklistValidationError("only REMOVE omits the definition; ADD and REPLACE need one")
    if isinstance(entry.get("item_id"), str) and entry["item_id"] in new_item_ids:
        item_id = entry["item_id"]  # created by this command (_override_new_items)
    else:
        item_id = reference(entry, "item_id", Item, org)
    if item_id is None:
        raise ChecklistValidationError("item_id is required")
    fields = validate_definition(org, definition, new_item_ids) if definition is not None else None
    if fields is not None and fields["item_id"] != item_id:
        raise ChecklistValidationError("a definition must name the entry's item")
    return item_id, action, fields


def _validated_entries(org, entries, template_requirements, new_item_ids=frozenset()):
    """Validate override entries and check that they overlay the template version cleanly."""
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ITEMS:
        raise ChecklistValidationError(f"provide 1-{MAX_ITEMS} override entries")
    validated = [_validated_entry(org, entry, new_item_ids) for entry in entries]
    # Dry-run the merge so a draft cannot be saved in a state that would never resolve.
    try:
        apply_overlay(
            template_requirements,
            [
                OverrideEntry(
                    item_id,
                    action,
                    requirement_from_fields(fields, "OVERRIDE") if fields else None,
                )
                for item_id, action, fields in validated
            ],
        )
    except ValueError as error:
        raise ChecklistValidationError(str(error)) from None
    return validated


def _override_new_items(org, entries):
    """(entries with item ids, {new item id: label}) for ADD entries naming a new item by label.

    An entry may give "new_item" (a label) in place of "item_id" for an ADD; its definition then
    names no item. The command creates the item, under the same rules as save_template_draft.
    """
    if not isinstance(entries, list):
        return entries, {}
    marked = [e for e in entries if isinstance(e, dict) and "new_item" in e]
    if not marked:
        return entries, {}
    for entry in marked:
        definition = entry.get("definition")
        if (
            entry.get("action") != "ADD"
            or "item_id" in entry
            or not isinstance(definition, dict)
            or "item_id" in definition
        ):
            raise ChecklistValidationError(
                "a new item can only be added: give new_item, action ADD and a definition "
                "without item_id"
            )
    specs, created = _with_new_items(
        org, [{"new_item": e["new_item"], **e["definition"]} for e in marked]
    )
    ids = iter(spec["item_id"] for spec in specs)
    resolved = []
    for entry in entries:
        if isinstance(entry, dict) and "new_item" in entry:
            item_id = next(ids)
            resolved.append(
                {
                    "item_id": item_id,
                    "action": "ADD",
                    "definition": {**entry["definition"], "item_id": item_id},
                }
            )
        else:
            resolved.append(entry)
    return resolved, created


def save_override_draft(*, vehicle_id, template_version_id, entries, base_revision=None, **context):
    """Create the next draft override revision for a vehicle over a published template version.

    An ADD entry may name a new item by label ("new_item"); the command creates it.
    base_revision (optional) is the vehicle's latest override revision the edit started from
    (0 for none; revisions count per vehicle across templates): a save is refused if another
    override revision was saved since.
    """
    canonical_id(vehicle_id)
    canonical_id(template_version_id)
    org = context["organization_id"]
    if base_revision is not None and not is_count(base_revision):
        raise ChecklistValidationError("base_revision must be a nonnegative integer")
    creates_items = isinstance(entries, list) and any(
        isinstance(e, dict) and "new_item" in e for e in entries
    )

    def build(actor):
        # SPEC 5.0.0: overrides are withdrawn (CHK-6); no new one after the cutover.
        require_legacy_writable(org, ChecklistValidationError)
        vehicle = _locked(Vehicle, org, vehicle_id, "vehicle")
        template_version = _locked(ChecklistVersion, org, template_version_id, "template version")
        if ChecklistTemplate.objects.filter(
            pk=template_version.template_id, vehicle__isnull=False
        ).exists():
            raise ChecklistValidationError("a vehicle checklist has no overrides")
        if not _is_published(checklist_version=template_version):
            raise ChecklistValidationError("an override must overlay a published template version")
        latest = _next_revision(VehicleOverrideVersion.objects.filter(vehicle=vehicle)) - 1
        if base_revision is not None and base_revision != latest:
            raise ChecklistValidationError(
                "the vehicle's override was changed since this edit started; reload it and try "
                "again"
            )
        template_requirements = [
            requirement_from_definition(d, "TEMPLATE")
            for d in ChecklistItemDef.objects.filter(checklist_version=template_version)
        ]
        resolved, new_items = _override_new_items(org, entries)
        validated = _validated_entries(org, resolved, template_requirements, frozenset(new_items))
        revision = latest + 1
        override_id = new_id()

        def apply(receipt, current_actor):
            Item.objects.bulk_create(
                Item(id=item_id, organization_id=org, label=label, active=True)
                for item_id, label in new_items.items()
            )
            VehicleOverrideVersion.objects.create(
                id=override_id,
                organization_id=org,
                vehicle=vehicle,
                template_version=template_version,
                revision=revision,
                created_at=receipt.recorded_at,
            )
            for item_id, action, fields in validated:
                definition_id = None
                if fields is not None:
                    definition_id = new_id()
                    ChecklistItemDef.objects.create(
                        id=definition_id,
                        organization_id=org,
                        override_version_id=override_id,
                        **fields,
                    )
                VehicleOverrideItem.objects.create(
                    id=new_id(),
                    organization_id=org,
                    override_version_id=override_id,
                    item_id=item_id,
                    action=action,
                    definition_id=definition_id,
                )

        body = {"override_version_id": override_id, "revision": revision}
        extra = {}
        if new_items:
            # Only when there are any, so an existing kind of save keeps its result and snapshot.
            body["new_item_ids"] = sorted(new_items)
            extra["new_items"] = [{"item_id": k, "label": v} for k, v in sorted(new_items.items())]
        return _plan(
            org,
            body,
            apply,
            action="OVERRIDE_DRAFT_SAVED",
            vehicle_id=vehicle_id,
            template_version_id=template_version_id,
            override_version_id=override_id,
            revision=revision,
            entry_count=len(validated),
            **extra,
        )

    intent = {
        "vehicle_id": vehicle_id,
        "template_version_id": template_version_id,
        "entries": entries,
    }
    if base_revision is not None:
        # Only when given, so the digest of an existing kind of request is unchanged.
        intent["base_revision"] = base_revision
    return _execute(
        action="draft_edit_template",
        command_type="OVERRIDE_DRAFT_SAVE",
        intent=intent,
        build=build,
        lock_organization=creates_items,
        **context,
    )


def _override_requirements(override):
    template_requirements = [
        requirement_from_definition(d, "TEMPLATE")
        for d in ChecklistItemDef.objects.filter(checklist_version_id=override.template_version_id)
    ]
    definitions = {d.pk: d for d in ChecklistItemDef.objects.filter(override_version=override)}
    entries = [
        OverrideEntry(
            e.item_id,
            e.action,
            requirement_from_definition(definitions[e.definition_id], "OVERRIDE")
            if e.definition_id
            else None,
        )
        for e in VehicleOverrideItem.objects.filter(override_version=override)
    ]
    return apply_overlay(template_requirements, entries)


def publish_override_version(*, override_version_id, effective_at=None, **context):
    """Publish a vehicle override: it takes effect with or after the template version it overlays."""
    canonical_id(override_version_id)
    org = context["organization_id"]
    requested = _aware(effective_at, "effective_at")

    def build(actor):
        require_legacy_writable(org, ChecklistValidationError)
        override = _locked(VehicleOverrideVersion, org, override_version_id, "override version")
        if _is_published(override_version=override):
            raise ChecklistValidationError("override version is already published")
        if not VehicleOverrideItem.objects.filter(override_version=override).exists():
            raise ChecklistValidationError("cannot publish an override without entries")
        template_publication = ChecklistPublication.objects.filter(
            checklist_version_id=override.template_version_id
        ).first()
        if template_publication is None:
            raise ChecklistValidationError("the overlaid template version is not published")
        _locked(Vehicle, org, override.vehicle_id, "vehicle")
        publication_id = new_id()

        def apply(receipt, current_actor):
            final = _final_effective_at(requested, receipt)
            if final < template_publication.effective_at:
                raise ChecklistValidationError(
                    "an override cannot take effect before the template version it overlays"
                )
            _require_after_latest(
                ChecklistPublication.objects.filter(override_version__vehicle=override.vehicle),
                final,
            )
            _require_slots(_override_requirements(override), [override.vehicle_id], final)
            _refuse_retired(_override_requirements(override), [override.vehicle_id])
            ChecklistPublication.objects.create(
                id=publication_id,
                organization_id=org,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current_actor.user,
                actor_roles_at_time=list(current_actor.roles),
                override_version=override,
                effective_at=final,
            )
            from cya.checks.locks import ensure_item_locks

            resolved = _override_requirements(override)
            ensure_item_locks(org, [override.vehicle_id], [row.item_id for row in resolved])

        return _plan(
            org,
            {
                "publication_id": publication_id,
                "override_version_id": override_version_id,
                "effective_immediately": requested is None,
            },
            apply,
            action="OVERRIDE_PUBLISHED",
            override_version_id=override_version_id,
            publication_id=publication_id,
        )

    result = _execute(
        action="publish_template",
        command_type="OVERRIDE_PUBLISH",
        intent={
            "override_version_id": override_version_id,
            "effective_at": requested.isoformat() if requested else None,
        },
        build=build,
        **context,
    )
    announce_publication(result, org)
    return result


# --- stocking deviations -------------------------------------------------------------------


def _deviation_window(tail, valid_from, valid_until, recorded_at):
    """Return the window start. Windows may not rewrite history (SPEC 4.5).

    A deviation active at recorded_at keeps its start (extend, shorten or terminate only);
    otherwise a new window starts at valid_from, or at recorded_at when valid_from is None.
    """
    active = tail is not None and tail.valid_from <= recorded_at < tail.valid_until
    if active:
        if valid_from not in (None, tail.valid_from):
            raise ChecklistValidationError("an active deviation keeps its original start")
        if valid_until < recorded_at:
            raise ChecklistValidationError("valid_until must not be in the past")
        start = tail.valid_from
    else:
        start = recorded_at if valid_from is None else valid_from
        if start < recorded_at:
            raise ChecklistValidationError("valid_from must not be in the past")
    if not start < valid_until:
        raise ChecklistValidationError("valid_from must be earlier than valid_until")
    return start


def _check_deviation_input(alternate_quantity, valid_until, explanation):
    if valid_until is None:
        raise ChecklistValidationError("valid_until is required")
    if not is_count(alternate_quantity):
        raise ChecklistValidationError("alternate_quantity must be a nonnegative integer")
    if (
        not isinstance(explanation, str)
        or not explanation.strip()
        or len(explanation) > MAX_EXPLANATION
    ):
        raise ChecklistValidationError("an explanation of 1-4000 characters is required")


def _normal_quantity(org, vehicle, template_id, item_id):
    """The vehicle's normal (undeviated) target for a QUANTITY item on its current checklist."""
    resolved = resolve_checklist(
        organization_id=org, vehicle_id=vehicle.pk, template_id=template_id, as_of=timezone.now()
    )
    match = [i for i in resolved.items if i.requirement.item_id == item_id]
    if not match or match[0].requirement.kind != "QUANTITY":
        raise ChecklistValidationError("a deviation applies to a QUANTITY item on the vehicle")
    return match[0].requirement.expected_quantity


def record_stocking_deviation(
    *,
    vehicle_id,
    item_id,
    template_id,
    alternate_quantity,
    valid_from,
    valid_until,
    explanation,
    **context,
):
    """Create a deviation or append a revision (extend, shorten, terminate) for a vehicle item.

    The approver is the authorized actor. valid_from None starts the window at the
    recording instant (or keeps the start of an active deviation). Termination is a
    revision whose valid_until is the termination instant.
    """
    for value in (vehicle_id, item_id, template_id):
        canonical_id(value)
    org = context["organization_id"]
    valid_from = _aware(valid_from, "valid_from")
    valid_until = _aware(valid_until, "valid_until")

    def build(actor):
        _check_deviation_input(alternate_quantity, valid_until, explanation)
        vehicle = _locked(Vehicle, org, vehicle_id, "vehicle")
        normal = _normal_quantity(org, vehicle, template_id, item_id)
        if alternate_quantity == normal:
            raise ChecklistValidationError("the alternate quantity must differ from the normal one")
        tail = (
            StockingDeviation.objects.filter(vehicle=vehicle, item_id=item_id)
            .order_by("-revision")
            .first()
        )
        deviation_id = new_id()
        revision = tail.revision + 1 if tail else 1

        def apply(receipt, current_actor):
            start = _deviation_window(tail, valid_from, valid_until, receipt.recorded_at)
            StockingDeviation.objects.create(
                id=deviation_id,
                organization_id=org,
                command=receipt,
                recorded_at=receipt.recorded_at,
                vehicle=vehicle,
                item_id=item_id,
                revision=revision,
                normal_quantity=normal,
                alternate_quantity=alternate_quantity,
                valid_from=start,
                valid_until=valid_until,
                approver=current_actor.user,
                explanation=explanation,
                prior_revision=tail,
            )

        return _plan(
            org,
            {"deviation_id": deviation_id, "revision": revision},
            apply,
            action="STOCKING_DEVIATION_RECORDED",
            vehicle_id=vehicle_id,
            item_id=item_id,
            deviation_id=deviation_id,
            revision=revision,
            normal_quantity=normal,
            alternate_quantity=alternate_quantity,
            valid_from=valid_from.isoformat() if valid_from else None,
            valid_until=valid_until.isoformat(),
        )

    return _execute(
        action="approve_stock_deviation",
        command_type="STOCKING_DEVIATION_RECORD",
        intent={
            "vehicle_id": vehicle_id,
            "item_id": item_id,
            "template_id": template_id,
            "alternate_quantity": alternate_quantity,
            "valid_from": valid_from.isoformat() if valid_from else None,
            "valid_until": valid_until.isoformat() if valid_until else None,
            "explanation": explanation,
        },
        build=build,
        **context,
    )


# --- vehicle item requirements (SPEC 6.0.0 CHK-11) ------------------------------------------


def publish_item_requirement_version(
    *,
    requirement_id,
    kind,
    allowed_outcomes,
    instructions="",
    expected_quantity=None,
    unit=None,
    expiration_tracked=False,
    compartment_id=None,
    system_id=None,
    effective_at=None,
    **context,
):
    """Publish a new version of one vehicle item requirement (SPEC 6.0.0 CHK-11, AC-42 (4)).

    It changes the item's place or requirements for every checklist that inspects that stock,
    from effective_at (None: the publication instant) on; earlier obligations, checks and
    checklist versions keep what they pinned. A compartment or system moves the stock (the
    requirement keeps its identity); neither leaves it Not placed. Refused before the
    organization's item-requirement cutover, at an instant not after the requirement's latest
    version, or where another requirement of the item is in force at that place (18.1 item 8).
    """
    from types import SimpleNamespace

    from cya.checklists import item_requirements
    from cya.checklists.models import VehicleItemRequirement, VehicleItemRequirementVersion

    canonical_id(requirement_id)
    org = context["organization_id"]
    requested = _aware(effective_at, "effective_at")
    intent = {
        "requirement_id": requirement_id,
        "kind": kind,
        "allowed_outcomes": allowed_outcomes,
        "instructions": instructions,
        "expected_quantity": expected_quantity,
        "unit": unit,
        "expiration_tracked": expiration_tracked,
        "compartment_id": compartment_id,
        "system_id": system_id,
        "effective_at": requested.isoformat() if requested else None,
    }

    def build(actor):
        if not item_requirements.has_cutover(org):
            raise ChecklistValidationError(
                "the organization has not moved to item requirements (migrate_item_requirements)"
            )
        found = (
            VehicleItemRequirement.objects.filter(organization_id=org, pk=requirement_id)
            .values_list("vehicle_id", "item_id")
            .first()
        )
        if found is None:
            raise ChecklistValidationError(f"item requirement {requirement_id} does not exist")
        vehicle_id, item_id = str(found[0]), str(found[1])
        _lock_vehicles(org, [vehicle_id])
        (spec,) = validate_definitions(
            org,
            [
                {
                    "item_id": item_id,
                    "display_order": 1,
                    "kind": kind,
                    "instructions": instructions,
                    "expected_quantity": expected_quantity,
                    "unit": unit,
                    "allowed_outcomes": allowed_outcomes,
                    "expiration_tracked": expiration_tracked,
                    "compartment_id": compartment_id,
                    "system_id": system_id,
                }
            ],
            vehicle_id=vehicle_id,
        )
        row = SimpleNamespace(
            compartment_id=spec.get("compartment_id"),
            system_id=spec.get("system_id"),
            **{name: spec.get(name) for name in item_requirements.FIELDS},
        )
        row.instructions = row.instructions or ""
        row.expiration_tracked = bool(row.expiration_tracked)

        version_id = new_id()

        def apply(receipt, current_actor):
            final = _final_effective_at(requested, receipt)
            versions = list(
                VehicleItemRequirementVersion.objects.filter(vehicle_id=vehicle_id, item_id=item_id)
            )
            own = [v for v in versions if str(v.requirement_id) == str(requirement_id)]
            if own and final <= max(v.effective_at for v in own):
                raise ChecklistValidationError(
                    "effective_at must be after the requirement's latest version"
                )
            place = item_requirements.place_of(row)
            for other in {str(v.requirement_id) for v in versions} - {str(requirement_id)}:
                theirs = sorted(
                    (v for v in versions if str(v.requirement_id) == other),
                    key=lambda v: v.effective_at,
                )
                # Another requirement occupies the place over [final, ...) when its version in
                # force at final, or any later one, is there (18.1 item 8).
                current = [v for v in theirs if v.effective_at <= final][-1:]
                later = [v for v in theirs if v.effective_at > final]
                if any(item_requirements._version_place(v) == place for v in current + later):
                    raise ChecklistValidationError(
                        "another item requirement of this item is in force at that place"
                    )
            writer = item_requirements._Writer(org, vehicle_id, receipt, current_actor)
            writer.version(requirement_id, item_id, final, row, version_id)

        body = {"requirement_id": requirement_id, "requirement_version_id": version_id}
        return _plan(
            org,
            body,
            apply,
            action="ITEM_REQUIREMENT_PUBLISHED",
            requirement_id=requirement_id,
        )

    return _execute(
        action="publish_template",
        command_type="ITEM_REQUIREMENT_PUBLISH",
        intent=intent,
        build=build,
        **context,
    )
