"""Full fresh inspection and attributed resealing of a physical kit or cabinet."""

from datetime import date
from zoneinfo import ZoneInfo

from cya.accounts.fields import new_id
from cya.accounts.models import Organization
from cya.authorization.backend import canonical_id
from cya.authorization.persistence import digest
from cya.core.audit_commands import Plan, run_command
from cya.core.models import CommandReceipt
from cya.deficiencies import service as deficiency_service
from cya.deficiencies.models import DeficiencyEvidence
from cya.kits.definitions import authorizer
from cya.kits.models import KitDefinition
from cya.kits.sealable import kit_sealable
from cya.seals.blockers import seal_blockers
from cya.seals.commands import SealConflict, SealError, _event_state, _events, _subject
from cya.seals.counts import validate_kit_counts
from cya.seals.inspection import validate_inspection
from cya.seals.links import LinkTarget, persist_inspection_links, plan_links
from cya.seals.locking import lock_seal_inputs, require_item_locks
from cya.seals.models import (
    SealEvent,
    SealInspection,
    SealInspectionItem,
    SealInspectionKitCount,
    SealInspectionKitInstance,
    SealInspectionKitVerification,
    SealSubstitution,
)
from cya.seals.scope import MAX_SCOPE, resolve_kit_lines, resolve_scope
from cya.seals.verification import _eligible_seal, current_certification


def _observations(rows):
    if not isinstance(rows, list) or len(rows) > MAX_SCOPE:
        raise SealError("invalid inspection entries")
    entries, deficiencies = [], {}
    required = {"item_id", "kit_id", "outcome", "count", "expiration", "measurement"}
    for row in rows:
        if (
            not isinstance(row, dict)
            or not required <= set(row)
            or set(row) - required - {"deficiency"}
        ):
            raise SealError("invalid inspection entry fields")
        entry = {key: row[key] for key in required}
        if entry["expiration"] is not None:
            try:
                if not isinstance(entry["expiration"], str):
                    raise ValueError
                entry["expiration"] = date.fromisoformat(entry["expiration"])
            except ValueError:
                raise SealError("expiration must be an ISO date") from None
        key = (entry["item_id"], entry["kit_id"])
        if any(not isinstance(value, str) for value in key if value is not None):
            raise SealError("invalid inspection item identity")
        if key in deficiencies:
            raise SealError("duplicate inspection item")
        selection = deficiency_service.check_selection(row.get("deficiency"))
        if selection is not None and selection[0] != "create":
            raise SealError("a failed inspection requires a new attributed deficiency")
        deficiencies[key] = selection[1] if selection else None
        entries.append(entry)
    return entries, deficiencies


def reseal(
    *,
    vehicle_id,
    marking,
    inspected_attestation,
    written_attestation,
    observations,
    kit_id=None,
    slot_id=None,
    expected_tip_id=None,
    start_operation_id=None,
    nested_substitution_ids=None,
    kit_count_checks=None,
    **context,
):
    """Create one atomic inspection, discrepancy evidence and seal event.

    Each cabinet kit-list count is an ordinary, fresh presence check made after inspection
    start; its immutable record is referenced by the seal's count certification.
    """
    organization_id = context["organization_id"]
    for value in (organization_id, context["actor_id"], vehicle_id):
        canonical_id(value)
    if (kit_id is None) == (slot_id is None):
        raise SealError("exactly one kit or compartment slot is required")
    canonical_id(kit_id if kit_id is not None else slot_id)
    if expected_tip_id is not None:
        canonical_id(expected_tip_id)
    if start_operation_id is not None:
        canonical_id(start_operation_id)
    nested_substitution_ids = [] if nested_substitution_ids is None else nested_substitution_ids
    if not isinstance(nested_substitution_ids, list) or len(nested_substitution_ids) > MAX_SCOPE:
        raise SealError("invalid nested seal verifications")
    for substitution_id in nested_substitution_ids:
        canonical_id(substitution_id)
    if len(set(nested_substitution_ids)) != len(nested_substitution_ids):
        raise SealError("duplicate nested seal verification")
    if nested_substitution_ids and (slot_id is None or start_operation_id is None):
        raise SealError("nested seals require a started cabinet inspection")
    kit_count_checks = [] if kit_count_checks is None else kit_count_checks
    if not isinstance(marking, str) or not marking.strip() or len(marking) > 200:
        raise SealError("the new seal marking is required")
    if inspected_attestation is not True or written_attestation is not True:
        raise SealError("inspection and written marking attestations are required")
    entries, deficiency_fields = _observations(observations)
    request = {
        "vehicle_id": vehicle_id,
        "kit_id": kit_id,
        "slot_id": slot_id,
        "expected_tip_id": expected_tip_id,
        "marking": marking,
        "inspected_attestation": inspected_attestation,
        "written_attestation": written_attestation,
        "observations": observations,
        "start_operation_id": start_operation_id,
        "nested_substitution_ids": nested_substitution_ids,
        "kit_count_checks": kit_count_checks,
    }

    def build(actor):
        _subject(organization_id, vehicle_id, kit_id, slot_id, require_sealable=True)
        _, item_locks, deficiencies = lock_seal_inputs(organization_id, vehicle_id)
        now = _subject(organization_id, vehicle_id, kit_id, slot_id, require_sealable=True)
        events = _events(organization_id, vehicle_id, kit_id, slot_id)
        prior = events[-1] if events else None
        if (str(prior.pk) if prior else None) != expected_tip_id:
            raise SealConflict("seal changed; reload before continuing")
        start = None
        if start_operation_id is not None:
            start = CommandReceipt.objects.filter(
                organization_id=organization_id,
                operation_id=start_operation_id,
                actor_user_id=context["actor_id"],
                command_type="SEAL_INSPECTION_START",
                recorded_at__lte=now,
            ).first()
            expected_start = {
                "vehicle_id": vehicle_id,
                "kit_id": kit_id,
                "slot_id": slot_id,
                "expected_tip_id": expected_tip_id,
            }
            if start is None or any(
                start.response_body.get(key) != value for key, value in expected_start.items()
            ):
                raise SealError("inspection start does not match this seal")
        seal, audit_pending = _event_state(events)
        if seal is not None:
            raise SealError("break the intact seal before inspecting")
        prior_break = next(
            (event for event in reversed(events) if event.event_type == "BREAK"), None
        )
        last_reseal = next(
            (event for event in reversed(events) if event.event_type == "RESEAL"), None
        )
        if last_reseal is not None and (
            prior_break is None or prior_break.sequence < last_reseal.sequence
        ):
            raise SealError("a new break is required before resealing")
        audit_request = (
            next((event for event in reversed(events) if event.event_type == "AUDIT_REQUEST"), None)
            if audit_pending
            else None
        )
        if audit_pending and (prior_break is None or prior_break.sequence < audit_request.sequence):
            raise SealError("the audit requires a break after it was requested")
        if audit_pending and slot_id is not None and nested_substitution_ids:
            raise SealError("a full-content audit requires inspecting every nested kit item")
        scope, assignments = resolve_scope(
            organization_id, vehicle_id, now, kit_id=kit_id, slot_id=slot_id
        )
        lines = (
            resolve_kit_lines(organization_id, vehicle_id, slot_id, now, assignments)
            if slot_id is not None
            else ()
        )
        definition_items = {
            str(row.pk): str(row.presence_item_id)
            for row in KitDefinition.objects.filter(
                organization_id=organization_id, pk__in={line.definition_id for line in lines}
            )
        }
        if len(definition_items) != len({line.definition_id for line in lines}) or any(
            definition_items[line.definition_id] not in item_locks for line in lines
        ):
            raise SealError("a kit-presence item lock is unavailable")
        count_checks = validate_kit_counts(
            organization_id,
            vehicle_id,
            context["actor_id"],
            lines,
            kit_count_checks,
            start=start,
            at=now,
        )
        require_item_locks(scope, item_locks)
        zone = Organization.objects.values_list("station_zone", flat=True).get(pk=organization_id)
        today = now.astimezone(ZoneInfo(zone)).date()
        nested = []
        nested_ids = set()
        if slot_id is not None:
            substitutions = list(
                SealSubstitution.objects.filter(
                    organization_id=organization_id,
                    pk__in=nested_substitution_ids,
                    actor_user_id=context["actor_id"],
                    vehicle_id=vehicle_id,
                ).select_related("seal_event")
            )
            if len(substitutions) != len(nested_substitution_ids):
                raise SealError("a nested seal verification is unavailable")
            for substitution in substitutions:
                covered_kit = str(substitution.seal_event.kit_id)
                if (
                    covered_kit not in assignments
                    or covered_kit in nested_ids
                    or substitution.recorded_at <= start.recorded_at
                ):
                    raise SealError("nested seal verification is outside this inspection")
                nested_seal, pending = _event_state(
                    _events(organization_id, vehicle_id, covered_kit, None)
                )
                if (
                    nested_seal is None
                    or nested_seal.pk != substitution.seal_event_id
                    or covered_kit not in kit_sealable(organization_id, [covered_kit], now)
                ):
                    raise SealError("a nested kit seal has changed")
                nested_scope, nested_assignments = resolve_scope(
                    organization_id, vehicle_id, now, kit_id=covered_kit
                )
                blocked, suspended = seal_blockers(
                    organization_id,
                    deficiencies,
                    {item.key for item in nested_scope},
                    at=now,
                    covered_kit_ids=nested_assignments,
                )
                if _eligible_seal(
                    nested_seal,
                    pending,
                    marking=substitution.entered_marking,
                    today=today,
                    open_deficiency=blocked,
                    suspended=suspended,
                ):
                    raise SealError("a nested kit seal cannot substitute")
                if current_certification(
                    organization_id,
                    vehicle_id,
                    nested_seal,
                    nested_scope,
                    nested_assignments,
                    at=now,
                    today=today,
                    open_deficiency=blocked,
                    suspended=suspended,
                ):
                    raise SealError("a nested kit certification has changed")
                nested.append((covered_kit, substitution, nested_seal))
                nested_ids.add(covered_kit)
            for covered_kit in set(assignments) - nested_ids:
                nested_seal, _ = _event_state(
                    _events(organization_id, vehicle_id, covered_kit, None)
                )
                if nested_seal is not None:
                    raise SealError("break or verify each nested kit seal before resealing")
        fresh_scope = tuple(item for item in scope if item.kit_id not in nested_ids)
        inspected, expiration, _ = validate_inspection(fresh_scope, entries, today=today)
        dates = [
            value
            for value in [expiration, *(seal.expiration for _, _, seal in nested)]
            if value is not None
        ]
        expiration = min(dates, default=None)
        targets = [LinkTarget(row.scope.item_id, row.scope.kit_id) for row in inspected]
        planned = plan_links(organization_id, vehicle_id, targets, recorded_at=now)
        discrepancies = []
        for row in inspected:
            fields = deficiency_fields[row.scope.key]
            if (row.outcome == "FAILED") != (fields is not None):
                raise SealError(
                    "each failed item requires a deficiency; passed items must not have one"
                )
            if fields is not None:
                category = deficiency_service.resolve_category(
                    organization_id, fields["category_id"], now
                )
                severity = deficiency_service.resolve_severity_level(
                    organization_id, fields["severity"], now
                )
                discrepancies.append(
                    {
                        "id": new_id(),
                        "event_id": new_id(),
                        "ordinal": len(discrepancies) + 1,
                        "key": row.scope.key,
                        "category": category,
                        "severity": severity,
                        "fields": fields,
                    }
                )
        by_key = {row["key"]: row for row in discrepancies}
        seal_id, inspection_id = new_id(), new_id()
        fulfillment_id = new_id() if audit_pending else None
        body = {
            "code": "SEAL_RESEALED",
            "event_id": seal_id,
            "inspection_id": inspection_id,
            "expiration": expiration.isoformat() if expiration else None,
            "no_expiration": expiration is None,
            "start_operation_id": start_operation_id,
            "deficiencies": [{"id": row["id"], "ordinal": row["ordinal"]} for row in discrepancies],
        }

        def apply(receipt, current):
            stamp = {
                "organization_id": organization_id,
                "command": receipt,
                "recorded_at": receipt.recorded_at,
                "actor_user": current.user,
                "actor_roles_at_time": list(current.roles),
            }
            reseal_event = SealEvent.objects.create(
                id=seal_id,
                **stamp,
                vehicle_id=vehicle_id,
                kit_id=kit_id,
                compartment_slot_id=slot_id,
                sequence=prior.sequence + 1 if prior else 1,
                prior_event=prior,
                event_type="RESEAL",
                marking=marking,
                expiration=expiration,
                no_expiration=expiration is None,
            )
            inspection = SealInspection.objects.create(
                id=inspection_id,
                **stamp,
                reseal=reseal_event,
                break_event=prior_break,
                fresh_entry_attestation=True,
                written_marking_attestation=True,
                start_receipt=start,
            )
            for line, check in count_checks:
                saved_count = SealInspectionKitCount.objects.create(
                    **stamp,
                    inspection=inspection,
                    kit_requirement_id=line.line_id,
                    check_record=check,
                    count=check.quantity,
                )
                for covered_kit, assignment_id in line.assignments:
                    SealInspectionKitInstance.objects.create(
                        **stamp,
                        kit_count=saved_count,
                        kit_id=covered_kit,
                        assignment_id=assignment_id,
                    )
            for covered_kit, substitution, _ in nested:
                SealInspectionKitVerification.objects.create(
                    **stamp, inspection=inspection, kit_id=covered_kit, substitution=substitution
                )
            for row, (_, links) in zip(inspected, planned, strict=True):
                source = (
                    {"kit_definition_item_id": row.scope.definition_id}
                    if row.scope.source == "KIT_DEFINITION"
                    else {"item_def_id": row.scope.definition_id}
                )
                item = SealInspectionItem.objects.create(
                    **stamp,
                    inspection=inspection,
                    item_id=row.scope.item_id,
                    kit_id=row.scope.kit_id,
                    instance_key=row.scope.kit_id or "00000000-0000-0000-0000-000000000000",
                    outcome=row.outcome,
                    count=row.count,
                    expiration=row.expiration,
                    measurement_value=row.measurement[0] if row.measurement else None,
                    measurement_unit=row.measurement[1] if row.measurement else None,
                    **source,
                )
                persist_inspection_links(item, links, receipt, current)
                problem = by_key.get(row.scope.key)
                if problem is not None:
                    deficiency_service.insert_deficiency(
                        receipt,
                        current,
                        organization_id=organization_id,
                        deficiency_id=problem["id"],
                        event_id=problem["event_id"],
                        operation_id=context["operation_id"],
                        operation_ordinal=problem["ordinal"],
                        vehicle_id=vehicle_id,
                        category=problem["category"],
                        severity=problem["severity"],
                        fields=problem["fields"],
                        affected=[(row.scope.item_id, row.scope.definition_id)],
                        item_id=row.scope.item_id,
                        kit_id=row.scope.kit_id,
                    )
                    DeficiencyEvidence.objects.create(
                        **stamp,
                        deficiency_id=problem["id"],
                        seal_inspection_item=item,
                    )
            if fulfillment_id:
                SealEvent.objects.create(
                    id=fulfillment_id,
                    **stamp,
                    vehicle_id=vehicle_id,
                    kit_id=kit_id,
                    compartment_slot_id=slot_id,
                    sequence=reseal_event.sequence + 1,
                    prior_event=reseal_event,
                    audit_request=audit_request,
                    event_type="AUDIT_FULFILLED",
                )
            from cya.live_updates.publish import publish_vehicle_update

            publish_vehicle_update(
                organization_id=organization_id,
                vehicle_id=vehicle_id,
                event_id=seal_id,
                kind="obligations.changed",
            )
            for problem in discrepancies:
                publish_vehicle_update(
                    organization_id=organization_id,
                    vehicle_id=vehicle_id,
                    event_id=problem["event_id"],
                    kind="deficiency.changed",
                )

        return Plan(
            response_body=body,
            apply=apply,
            target_type="ORGANIZATION",
            target_id=organization_id,
            recorded_at=now,
            new_snapshot={
                "schema_version": 1,
                "values": {
                    "event_id": seal_id,
                    "inspection_id": inspection_id,
                    "deficiency_count": len(discrepancies),
                },
            },
        )

    return run_command(
        organization_id=organization_id,
        operation_id=context["operation_id"],
        command_type="SEAL_RESEAL",
        request_digest=digest(request),
        correlation_id=context["correlation_id"],
        request_attempt_id=context["request_attempt_id"],
        authorize=authorizer(
            organization_id, context["actor_id"], action="verify_break_reseal_kit_seal"
        ),
        prepare=build,
    )
