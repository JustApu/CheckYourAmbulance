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

MAX_EXPLANATION = 4000
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
):
    for value in (organization_id, actor_id):
        canonical_id(value)
    backend = DatabaseRoleBackend()

    def authorize(org):
        # Only the actor is locked (no role change here); current roles are read after the lock.
        with locked_identities(organization_id, [actor_id], lock_organization=False) as (
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


def save_template_draft(*, template_id, items, **context):
    """Create the next draft revision of a template with its complete item set (CHK-2)."""
    canonical_id(template_id)
    org = context["organization_id"]

    def build(actor):
        template = _locked(ChecklistTemplate, org, template_id, "template")
        if not template.active:
            raise ChecklistValidationError("template is inactive")
        definitions = validate_definitions(org, items)
        revision = _next_revision(ChecklistVersion.objects.filter(template=template))
        version_id = new_id()

        def apply(receipt, current_actor):
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

        return _plan(
            org,
            {"version_id": version_id, "revision": revision, "item_count": len(definitions)},
            apply,
            action="TEMPLATE_DRAFT_SAVED",
            template_id=template_id,
            version_id=version_id,
            revision=revision,
            item_count=len(definitions),
        )

    return _execute(
        action="draft_edit_template",
        command_type="CHECKLIST_DRAFT_SAVE",
        intent={"template_id": template_id, "items": items},
        build=build,
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


def _active_vehicle_ids(org):
    return list(
        Vehicle.objects.filter(organization_id=org, active=True).values_list("pk", flat=True)
    )


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
        _locked(ChecklistTemplate, org, version.template_id, "checklist template")
        publication_id = new_id()

        def apply(receipt, current_actor):
            final = _final_effective_at(requested, receipt)
            _require_after_latest(
                ChecklistPublication.objects.filter(checklist_version__template=version.template),
                final,
            )
            requirements = [requirement_from_definition(d, "TEMPLATE") for d in definitions]
            _require_slots(requirements, _active_vehicle_ids(org), final)
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
                _active_vehicle_ids(org),
                [definition.item_id for definition in definitions],
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


def _validated_entry(org, entry):
    if not isinstance(entry, dict) or set(entry) - {"item_id", "action", "definition"}:
        raise ChecklistValidationError("override entries take item_id, action and definition")
    action, definition = entry.get("action"), entry.get("definition")
    if action not in OVERRIDE_ACTIONS:
        raise ChecklistValidationError(f"action must be one of {list(OVERRIDE_ACTIONS)}")
    if (action == "REMOVE") != (definition is None):
        raise ChecklistValidationError("only REMOVE omits the definition; ADD and REPLACE need one")
    item_id = reference(entry, "item_id", Item, org)
    if item_id is None:
        raise ChecklistValidationError("item_id is required")
    fields = validate_definition(org, definition) if definition is not None else None
    if fields is not None and fields["item_id"] != item_id:
        raise ChecklistValidationError("a definition must name the entry's item")
    return item_id, action, fields


def _validated_entries(org, entries, template_requirements):
    """Validate override entries and check that they overlay the template version cleanly."""
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ITEMS:
        raise ChecklistValidationError(f"provide 1-{MAX_ITEMS} override entries")
    validated = [_validated_entry(org, entry) for entry in entries]
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


def save_override_draft(*, vehicle_id, template_version_id, entries, **context):
    """Create the next draft override revision for a vehicle over a published template version."""
    canonical_id(vehicle_id)
    canonical_id(template_version_id)
    org = context["organization_id"]

    def build(actor):
        vehicle = _locked(Vehicle, org, vehicle_id, "vehicle")
        template_version = _locked(ChecklistVersion, org, template_version_id, "template version")
        if not _is_published(checklist_version=template_version):
            raise ChecklistValidationError("an override must overlay a published template version")
        template_requirements = [
            requirement_from_definition(d, "TEMPLATE")
            for d in ChecklistItemDef.objects.filter(checklist_version=template_version)
        ]
        validated = _validated_entries(org, entries, template_requirements)
        revision = _next_revision(VehicleOverrideVersion.objects.filter(vehicle=vehicle))
        override_id = new_id()

        def apply(receipt, current_actor):
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

        return _plan(
            org,
            {"override_version_id": override_id, "revision": revision},
            apply,
            action="OVERRIDE_DRAFT_SAVED",
            vehicle_id=vehicle_id,
            template_version_id=template_version_id,
            override_version_id=override_id,
            revision=revision,
            entry_count=len(validated),
        )

    return _execute(
        action="draft_edit_template",
        command_type="OVERRIDE_DRAFT_SAVE",
        intent={
            "vehicle_id": vehicle_id,
            "template_version_id": template_version_id,
            "entries": entries,
        },
        build=build,
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
