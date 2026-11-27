"""Invalidate a paper-entry batch and its linked records (SPEC 8.0.0 INV-8, LATE-6, LATE-7;
AC-43 (7), (8); issue #9 Part B5).

invalidate_paper_entry_batch is the audited command behind
POST /api/v1/paper-entry-batches/{batch_id}/invalidations. A wrong paper time or performer is
never edited in place: a Supervisor or Administrator other than the batch's entry actor
invalidates the whole batch with a mandatory explanation, and the form is entered again as a new
batch. In one transaction (cya.core.audit_commands.run_command) it appends, and never modifies:

- an INVALIDATION check_event for every check chain of the batch not already invalidated, with
  the ordinary chain consequences (INV-2a, INV-3, INV-4, INV-7; UTI-3 completions voided and
  their supervisors notified), exactly as an administrator's chain invalidation;
- an INVALIDATION reading_event for every odometer and engine-hours chain of the batch not
  already invalidated, so the vehicle's reading sequence and odometer compliance are recomputed
  without it (ODO-5, ODO-6);
- last, one paper_entry_invalidation_event. Its insert trigger (migration 0003) refuses it
  unless every check, odometer and engine-hours chain of the batch is invalidated by then.

Paper seal records are not given events of their own; readers apply the batch event. A seal
substitution of the batch, and the inspection of a paper reseal of the batch, are void from it
(obligation state, linking and later-evidence rules read them so), so they satisfy nothing
(LATE-7). A paper RESEAL of the batch no longer supports reliance and its paper AUDIT_FULFILLED
no longer ends a pending audit: the seal reads as open. A paper break stays in force. Either
way a new full inspection and reseal are required before reliance resumes (INV-8, fail closed).

Every row keeps its original performance and server-entry times and stays visible; derived state
is recomputed without it. The batch takes no further record (409 PAPER_BATCH_INVALIDATED), and an
invalidation is never undone (INV-6). The per-chain events identify the batch through the
receipt's response body (batch_id), which the database guards read.

Lock order: vehicle, batch, the item locks of the batch's checks (sorted), deficiencies (sorted),
as for paper record commands, so a concurrent record of the same batch serializes behind it.
"""

from django.db.models import F
from django.utils import timezone

from cya.authorization.backend import canonical_id, load_state
from cya.authorization.guard import PermissionDenied, Principal, require_permission
from cya.authorization.locking import locked_identities
from cya.authorization.persistence import DatabaseRoleBackend, digest
from cya.authorization.policy import ResourceContext, Role
from cya.checklists.models import Vehicle
from cya.checks.commands import (
    SelfInvalidationForbidden,
    _apply_chain_invalidation,
    _chain_invalidated,
    _plan_chain_invalidation,
    _self_invalidation_forbidden,
)
from cya.checks.models import CheckRecord, ItemLock
from cya.core.audit_commands import Actor, CommandDenied, Plan, run_command
from cya.deficiencies.effects import supporting_deficiency_ids
from cya.deficiencies.service import lock_deficiencies
from cya.operations.models import MAX_TEXT
from cya.paper.commands import PaperEntryError, _id, _malformed, _operation_id
from cya.paper.models import PaperEntryBatch, PaperEntryInvalidationEvent

COMMAND_TYPE = "PAPER_ENTRY_BATCH_INVALIDATION"
ACTION = "invalidate_paper_entry_batch"
MAX_CHAINS = 1000  # check and reading chains one batch invalidation appends events for
READING_KINDS = ("odometer", "engine-hours")


def _explanation(value):
    if not isinstance(value, str) or not value.strip():
        raise PaperEntryError(
            "EXPLANATION_REQUIRED",
            "a paper-entry batch invalidation needs an explanation",
            path="explanation",
        )
    if len(value) > MAX_TEXT:
        raise _malformed("explanation", f"explanation must be at most {MAX_TEXT} characters")
    return value


def _authorizer(organization_id, actor_id, batch_id):
    """SPEC 3.3 and INV-8: a Supervisor or Administrator, never the batch's entry actor.

    The entry actor is read under the identity locks, so the matrix's "other only" rule sees a
    trusted fact. A Supervisor or Administrator naming their own batch gets a distinct, audited
    denial; a missing batch passes here and is answered 404 by the command.
    """
    backend = DatabaseRoleBackend()

    def authorize(_org):
        with locked_identities(organization_id, [actor_id], lock_organization=False) as (
            current_org,
            users,
        ):
            user = users[actor_id]
            roles = frozenset(
                Role.CREW if r == Role.EMT else r for r in load_state(backend, user).grants
            )
            actor = Actor(user=user, roles=tuple(sorted(r.value for r in roles)))
            principal = Principal(
                organization_id, actor_id, roles, user.active and current_org.active
            )
            entry_actor = (
                PaperEntryBatch.objects.filter(organization_id=organization_id, pk=batch_id)
                .values_list("actor_user_id", flat=True)
                .first()
            )
            author_id = str(canonical_id(entry_actor)) if entry_actor is not None else ""
            if (
                Role.SUPERVISOR in roles or Role.ADMINISTRATOR in roles
            ) and _self_invalidation_forbidden(actor_id=actor_id, author_id=author_id):
                raise SelfInvalidationForbidden(actor)
            try:
                require_permission(
                    principal,
                    organization_id,
                    ACTION,
                    context=ResourceContext(author_id=author_id),
                )
            except PermissionDenied:
                raise CommandDenied(actor) from None
            return actor

    return authorize


def _locked_batch(organization_id, batch_id):
    """The batch, with its vehicle and then itself locked; or a refusal."""
    found = (
        PaperEntryBatch.objects.filter(organization_id=organization_id, pk=batch_id)
        .values_list("vehicle_id", flat=True)
        .first()
    )
    if found is None:
        raise PaperEntryError(
            "NOT_FOUND", "paper-entry batch was not found", status=404, path="batch_id"
        )
    Vehicle.objects.select_for_update().filter(organization_id=organization_id, pk=found).first()
    batch = PaperEntryBatch.objects.select_for_update().get(
        organization_id=organization_id, pk=batch_id
    )
    if PaperEntryInvalidationEvent.objects.filter(batch=batch).exists():
        raise PaperEntryError(
            "BATCH_INVALIDATED", "the paper-entry batch is already invalidated", status=409
        )
    return batch


def _check_roots(organization_id, batch):
    return list(
        CheckRecord.objects.filter(
            organization_id=organization_id, paper_entry_batch=batch, pk=F("root_check_id")
        ).order_by("item_id", "id")[: MAX_CHAINS + 1]
    )


def _reading_roots(organization_id, batch):
    """[(kind, model, root)] of the batch's reading chains not yet invalidated."""
    from cya.odometer.corrections import MODELS, _invalidated

    found = []
    for kind in READING_KINDS:
        model = MODELS[kind]
        rows = model.objects.filter(
            organization_id=organization_id, paper_entry_batch=batch, pk=F("root_entry_id")
        ).order_by("id")[: MAX_CHAINS + 1]
        found += [(kind, model, row) for row in rows if not _invalidated(kind, row)]
    return found


def _too_many():
    return PaperEntryError(
        "CONFIGURATION_UNAVAILABLE",
        "the paper-entry batch has more records than one invalidation supports",
        status=503,
    )


def _lock_items(organization_id, vehicle_id, item_ids):
    """Each check's item lock, in item order (the order every check command takes one)."""
    for item_id in sorted(item_ids):
        locked = (
            ItemLock.objects.select_for_update()
            .filter(organization_id=organization_id, vehicle_id=vehicle_id, item_id=item_id)
            .first()
        )
        if locked is None:
            raise PaperEntryError(
                "CONFIGURATION_UNAVAILABLE", "item lock is unavailable", status=503
            )


def _seal_records(organization_id, batch):
    """The batch's seal records, by what the invalidation does to them, and their obligations.

    Substitutions and the inspections of paper reseals are void (their obligations are
    re-derived); a paper RESEAL no longer supports reliance and its AUDIT_FULFILLED no longer
    ends an audit; a paper BREAK stays in force (LATE-7, INV-8).
    """
    from cya.seals.models import (
        SealEvent,
        SealInspection,
        SealInspectionObligation,
        SealSubstitution,
        SealSubstitutionObligation,
    )

    def ids(rows):
        return sorted(str(pk) for pk in rows.values_list("pk", flat=True))

    substitutions = ids(
        SealSubstitution.objects.filter(organization_id=organization_id, paper_entry_batch=batch)
    )
    events = SealEvent.objects.filter(organization_id=organization_id, paper_entry_batch=batch)
    inspections = ids(
        SealInspection.objects.filter(
            organization_id=organization_id, reseal__paper_entry_batch=batch
        )
    )
    obligations = {
        str(pk)
        for pk in SealSubstitutionObligation.objects.filter(
            organization_id=organization_id, substitution_id__in=substitutions
        ).values_list("obligation_id", flat=True)
    }
    obligations.update(
        str(pk)
        for pk in SealInspectionObligation.objects.filter(
            organization_id=organization_id, inspection_item__inspection_id__in=inspections
        ).values_list("obligation_id", flat=True)
    )
    return {
        "voided_substitution_ids": substitutions,
        "voided_inspection_ids": inspections,
        # A paper reseal cannot support reliance: a new full inspection and reseal are required.
        "voided_reseal_event_ids": ids(events.filter(event_type="RESEAL")),
        "voided_audit_fulfillment_event_ids": ids(events.filter(event_type="AUDIT_FULFILLED")),
        # A paper break stays in force: the seal stays open.
        "seal_break_event_ids": ids(events.filter(event_type="BREAK")),
    }, obligations


def _reading_plan(organization_id, kind, model, root):
    from cya.accounts.fields import new_id
    from cya.odometer.corrections import _tip
    from cya.odometer.models import OdometerObligationLink

    affected = set()
    if kind == "odometer":
        affected = {
            str(pk)
            for pk in OdometerObligationLink.objects.filter(entry=root).values_list(
                "obligation_id", flat=True
            )
        }
    return kind, root, _tip(model, root), new_id(), affected


def _apply_reading(plan, receipt, current, *, organization_id, explanation):
    from cya.live_updates.publish import publish_vehicle_update
    from cya.odometer.corrections import _event_fields
    from cya.odometer.models import ReadingEvent

    kind, root, tip, event_id, _affected = plan
    ReadingEvent.objects.create(
        id=event_id,
        organization_id=organization_id,
        command=receipt,
        recorded_at=receipt.recorded_at,
        actor_user=current.user,
        actor_roles_at_time=list(current.roles),
        event_type="INVALIDATION",
        explanation=explanation,
        **_event_fields(kind, root, tip),
    )
    publish_vehicle_update(
        organization_id=organization_id,
        vehicle_id=root.vehicle_id,
        event_id=event_id,
        kind="odometer.invalidated",
    )


def _plans(organization_id, batch):
    """Lock what the batch's records touch and plan every chain's INVALIDATION."""
    roots = _check_roots(organization_id, batch)
    readings = _reading_roots(organization_id, batch)
    if len(roots) + len(readings) > MAX_CHAINS:
        raise _too_many()
    _lock_items(organization_id, batch.vehicle_id, {str(r.item_id) for r in roots})
    # A chain an administrator already invalidated (INV-1) keeps its own event.
    open_roots = [root for root in roots if not _chain_invalidated(root)]
    # The chains' deficiencies are locked together in one sorted pass before any is planned.
    deficiency_ids = set()
    for root in open_roots:
        deficiency_ids.update(
            supporting_deficiency_ids(organization_id, root.pk, root.verification_for_deficiency_id)
        )
    lock_deficiencies(organization_id, sorted(deficiency_ids))
    checks = [_plan_chain_invalidation(organization_id, root) for root in open_roots]
    reading_plans = [_reading_plan(organization_id, *found) for found in readings]
    return checks, reading_plans


def invalidate_paper_entry_batch(*, batch_id, explanation, **context):
    """Invalidate one paper-entry batch as a unit; returns the run_command Result (status 201)."""
    organization_id = context["organization_id"]
    actor_id = canonical_id(context["actor_id"])
    canonical_id(organization_id)
    batch_id = _id(batch_id, "batch_id")
    _operation_id(context["operation_id"])
    explanation = _explanation(explanation)

    def build(actor):
        from cya.accounts.fields import new_id

        batch = _locked_batch(organization_id, batch_id)
        checks, readings = _plans(organization_id, batch)
        seals, seal_obligations = _seal_records(organization_id, batch)
        now = timezone.now()  # after every lock wait
        event_id = new_id()

        def apply(receipt, current):
            for plan in checks:
                _apply_chain_invalidation(
                    plan,
                    receipt,
                    current,
                    organization_id=organization_id,
                    explanation=explanation,
                )
            for plan in readings:
                _apply_reading(
                    plan,
                    receipt,
                    current,
                    organization_id=organization_id,
                    explanation=explanation,
                )
            # Last: its guard requires every check and reading chain of the batch invalidated.
            PaperEntryInvalidationEvent.objects.create(
                id=event_id,
                organization_id=organization_id,
                batch=batch,
                explanation=explanation,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current.user,
                actor_roles_at_time=list(current.roles),
            )
            from cya.live_updates.publish import publish_vehicle_update

            if seal_obligations:
                publish_vehicle_update(
                    organization_id=organization_id,
                    vehicle_id=batch.vehicle_id,
                    event_id=event_id,
                    kind="obligations.changed",
                )
            if seals["voided_reseal_event_ids"]:  # the seal now reads as open
                publish_vehicle_update(
                    organization_id=organization_id,
                    vehicle_id=batch.vehicle_id,
                    event_id=event_id,
                    kind="seal.changed",
                )

        affected = set(seal_obligations)
        for plan in checks:
            affected.update(plan.affected)
        for plan in readings:
            affected.update(plan[4])
        values = {
            "batch_id": batch_id,
            "invalidation_event_id": event_id,
            "check_invalidation_event_ids": [plan.event_id for plan in checks],
            "invalidated_check_root_ids": [str(plan.root.pk) for plan in checks],
            "reading_invalidation_event_ids": [plan[3] for plan in readings],
            "invalidated_reading_root_ids": [str(plan[1].pk) for plan in readings],
            **seals,
            "affected_obligation_ids": sorted(affected),
            "voided_determination_ids": sorted(
                str(e.prior.pk) for plan in checks for e in plan.completions
            ),
            "reopened_deficiency_ids": sorted(
                e.deficiency_id for plan in checks for e in plan.effects
            ),
        }
        # No explanation text in the audit snapshot: it is free text from the invalidator.
        return Plan(
            response_body={"code": "PAPER_ENTRY_BATCH_INVALIDATED", **values},
            response_status=201,
            apply=apply,
            target_type="ORGANIZATION",
            target_id=organization_id,
            new_snapshot={"schema_version": 1, "values": values},
            recorded_at=now,
        )

    return run_command(
        organization_id=organization_id,
        operation_id=context["operation_id"],
        command_type=COMMAND_TYPE,
        request_digest=digest({"batch_id": batch_id, "explanation": explanation}),
        correlation_id=context["correlation_id"],
        request_attempt_id=context["request_attempt_id"],
        authorize=_authorizer(organization_id, actor_id, batch_id),
        prepare=build,
    )
