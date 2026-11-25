"""Audited seal event commands for cabinets and physical kits."""

from zoneinfo import ZoneInfo

from django.utils import timezone

from cya.accounts.fields import new_id
from cya.accounts.models import Organization
from cya.authorization.backend import canonical_id
from cya.authorization.persistence import digest
from cya.checklists.models import Compartment, KitInstance, Vehicle
from cya.core.audit_commands import Plan, run_command
from cya.kits.definitions import authorizer
from cya.kits.sealable import kit_sealable
from cya.seals.models import SealEvent, SealSubstitution

MAX_EVENTS = 10000
MAX_REASON = 4000


class SealError(ValueError):
    """Seal state or request is invalid."""


class SealConflict(SealError):
    """The seal stream changed since the caller read it."""


class SealAuditOpen(SealError):
    """An open subject must be fully inspected before it can receive an audit request."""


def _subject(organization_id, vehicle_id, kit_id, slot_id, *, require_sealable):
    vehicle = (
        Vehicle.objects.select_for_update()
        .filter(organization_id=organization_id, pk=vehicle_id, active=True)
        .first()
    )
    if vehicle is None:
        raise SealError("vehicle is unavailable")
    if kit_id is not None:
        kit = (
            KitInstance.objects.select_for_update()
            .filter(organization_id=organization_id, pk=kit_id, vehicle_id=vehicle_id, active=True)
            .first()
        )
        now = timezone.now()
        if kit is None or (require_sealable and not kit_sealable(organization_id, [kit_id], now)):
            raise SealError("kit is unavailable or not designated sealable")
    else:
        now = timezone.now()
        compartment = (
            Compartment.objects.filter(
                organization_id=organization_id,
                vehicle_id=vehicle_id,
                stable_slot_id=slot_id,
                effective_at__lte=now,
            )
            .order_by("-effective_at", "-revision")
            .first()
        )
        # SPEC 4.8 CFG-2: a retired cabinet is no seal subject; breaking a seal still left on it
        # (require_sealable False) stays possible.
        if compartment is None or (
            require_sealable and (compartment.retired or not compartment.sealable)
        ):
            raise SealError("compartment is unavailable or not designated sealable")
    return now


def _events(organization_id, vehicle_id, kit_id, slot_id):
    rows = SealEvent.objects.filter(organization_id=organization_id)
    if kit_id is not None:
        rows = rows.filter(kit_id=kit_id)
    else:
        rows = rows.filter(vehicle_id=vehicle_id, compartment_slot_id=slot_id, kit__isnull=True)
    events = list(rows.order_by("sequence")[: MAX_EVENTS + 1])
    if len(events) > MAX_EVENTS:
        raise SealError("seal event history exceeds its limit")
    return events


def _event_state(events):
    seal = None
    audit_pending = False
    for event in events:
        if event.event_type == "RESEAL":
            seal = event
        elif event.event_type == "BREAK":
            seal = None
        elif event.event_type == "AUDIT_REQUEST":
            audit_pending = True
        elif event.event_type == "AUDIT_FULFILLED":
            audit_pending = False
    return seal, audit_pending


def _append_event(
    *,
    event_type,
    vehicle_id,
    kit_id=None,
    slot_id=None,
    expected_tip_id=None,
    reason=None,
    **context,
):
    organization_id = context["organization_id"]
    for value in (organization_id, context["actor_id"], vehicle_id):
        canonical_id(value)
    if (kit_id is None) == (slot_id is None):
        raise SealError("exactly one kit or compartment slot is required")
    canonical_id(kit_id if kit_id is not None else slot_id)
    if expected_tip_id is not None:
        canonical_id(expected_tip_id)
    if event_type == "BREAK":
        if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON:
            raise SealError("a break requires a reason")
        reason = reason.strip()
        command_type, action = "SEAL_BREAK", "verify_break_reseal_kit_seal"
    elif event_type == "AUDIT_REQUEST":
        command_type, action = "SEAL_AUDIT_REQUEST", "request_kit_audit"
    else:
        raise SealError("unsupported seal event")

    request = {
        "vehicle_id": vehicle_id,
        "kit_id": kit_id,
        "slot_id": slot_id,
        "expected_tip_id": expected_tip_id,
        "reason": reason,
    }

    def build(actor):
        now = _subject(
            organization_id, vehicle_id, kit_id, slot_id, require_sealable=event_type != "BREAK"
        )
        events = _events(organization_id, vehicle_id, kit_id, slot_id)
        prior = events[-1] if events else None
        if (str(prior.pk) if prior else None) != expected_tip_id:
            raise SealConflict("seal changed; reload before continuing")
        seal, pending = _event_state(events)
        if event_type == "BREAK" and seal is None:
            raise SealError("there is no intact seal to break")
        if event_type == "AUDIT_REQUEST" and pending:
            raise SealError("a full audit is already pending")
        if event_type == "AUDIT_REQUEST" and seal is None:
            raise SealAuditOpen("the open seal already requires a full inspection before reseal")
        event_id = new_id()

        def apply(receipt, current):
            SealEvent.objects.create(
                id=event_id,
                organization_id=organization_id,
                vehicle_id=vehicle_id,
                kit_id=kit_id,
                compartment_slot_id=slot_id,
                sequence=prior.sequence + 1 if prior else 1,
                prior_event=prior,
                event_type=event_type,
                marking=seal.marking if event_type == "BREAK" else None,
                reason=reason,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current.user,
                actor_roles_at_time=list(current.roles),
            )
            from cya.live_updates.publish import publish_vehicle_update

            publish_vehicle_update(
                organization_id=organization_id,
                vehicle_id=vehicle_id,
                event_id=event_id,
                kind="seal.changed",
            )

        body = {"code": "SEAL_" + event_type, "event_id": event_id}
        return Plan(
            response_body=body,
            apply=apply,
            target_type="ORGANIZATION",
            target_id=organization_id,
            recorded_at=now,
            new_snapshot={"schema_version": 1, "values": body},
        )

    return run_command(
        organization_id=organization_id,
        operation_id=context["operation_id"],
        command_type=command_type,
        request_digest=digest(request),
        correlation_id=context["correlation_id"],
        request_attempt_id=context["request_attempt_id"],
        authorize=authorizer(organization_id, context["actor_id"], action=action),
        prepare=build,
    )


def break_seal(*, reason, **kwargs):
    return _append_event(event_type="BREAK", reason=reason, **kwargs)


def request_seal_audit(**kwargs):
    return _append_event(event_type="AUDIT_REQUEST", **kwargs)


def start_inspection(*, vehicle_id, kit_id=None, slot_id=None, expected_tip_id=None, **context):
    """Mark when a fresh inspection begins, without changing the seal event stream."""
    organization_id = context["organization_id"]
    for value in (organization_id, context["actor_id"], vehicle_id):
        canonical_id(value)
    if (kit_id is None) == (slot_id is None):
        raise SealError("exactly one kit or compartment slot is required")
    canonical_id(kit_id if kit_id is not None else slot_id)
    if expected_tip_id is not None:
        canonical_id(expected_tip_id)
    request = {
        "vehicle_id": vehicle_id,
        "kit_id": kit_id,
        "slot_id": slot_id,
        "expected_tip_id": expected_tip_id,
    }

    def build(actor):
        now = _subject(organization_id, vehicle_id, kit_id, slot_id, require_sealable=True)
        events = _events(organization_id, vehicle_id, kit_id, slot_id)
        prior = events[-1] if events else None
        if (str(prior.pk) if prior else None) != expected_tip_id:
            raise SealConflict("seal changed; reload before continuing")
        seal, pending = _event_state(events)
        if seal is not None:
            raise SealError("break the intact seal before inspecting")
        if pending:
            audit = next(event for event in reversed(events) if event.event_type == "AUDIT_REQUEST")
            broken = next(
                (event for event in reversed(events) if event.event_type == "BREAK"), None
            )
            if broken is None or broken.sequence < audit.sequence:
                raise SealError("the audit requires a break after it was requested")
        body = {
            "code": "SEAL_INSPECTION_STARTED",
            "start_operation_id": context["operation_id"],
            **request,
        }
        return Plan(
            response_body=body,
            apply=lambda _receipt, _current: None,
            target_type="ORGANIZATION",
            target_id=organization_id,
            recorded_at=now,
            new_snapshot={"schema_version": 1, "values": body},
        )

    return run_command(
        organization_id=organization_id,
        operation_id=context["operation_id"],
        command_type="SEAL_INSPECTION_START",
        request_digest=digest(request),
        correlation_id=context["correlation_id"],
        request_attempt_id=context["request_attempt_id"],
        authorize=authorizer(
            organization_id, context["actor_id"], action="verify_break_reseal_kit_seal"
        ),
        prepare=build,
    )


def verify_seal(
    *, vehicle_id, marking, attested, kit_id=None, slot_id=None, expected_tip_id=None, **context
):
    """Record attributed substitution only when current seal and complete scope remain valid."""
    from cya.kits.models import KitDefinition
    from cya.kits.seal_rules import SealVerificationFacts, verification_refusal
    from cya.seals.blockers import seal_blockers
    from cya.seals.links import LinkTarget, persist_substitution_links, plan_links
    from cya.seals.locking import lock_seal_inputs, require_item_locks
    from cya.seals.scope import resolve_kit_lines, resolve_kit_presence, resolve_scope
    from cya.seals.verification import current_certification

    organization_id = context["organization_id"]
    for value in (organization_id, context["actor_id"], vehicle_id):
        canonical_id(value)
    if (kit_id is None) == (slot_id is None):
        raise SealError("exactly one kit or compartment slot is required")
    canonical_id(kit_id if kit_id is not None else slot_id)
    if expected_tip_id is not None:
        canonical_id(expected_tip_id)
    if not isinstance(marking, str) or not marking.strip() or len(marking) > 200:
        raise SealError("the seal marking is required")
    if attested is not True:
        raise SealError("the intact seal must be attested")
    request = {
        "vehicle_id": vehicle_id,
        "kit_id": kit_id,
        "slot_id": slot_id,
        "expected_tip_id": expected_tip_id,
        "marking": marking,
        "attested": attested,
    }

    def build(actor):
        _subject(organization_id, vehicle_id, kit_id, slot_id, require_sealable=True)
        now, item_locks, deficiencies = lock_seal_inputs(organization_id, vehicle_id)
        # A scheduled designation can take effect while another lock is being acquired.
        now = _subject(organization_id, vehicle_id, kit_id, slot_id, require_sealable=True)
        events = _events(organization_id, vehicle_id, kit_id, slot_id)
        tip = events[-1] if events else None
        if (str(tip.pk) if tip else None) != expected_tip_id:
            raise SealConflict("seal changed; reload before continuing")
        seal, pending = _event_state(events)
        if seal is None:
            raise SealError("there is no intact seal to verify")
        scope, assignments = resolve_scope(
            organization_id, vehicle_id, now, kit_id=kit_id, slot_id=slot_id
        )
        lines = (
            resolve_kit_lines(organization_id, vehicle_id, slot_id, now, assignments)
            if slot_id is not None
            else ()
        )
        kit_presence = resolve_kit_presence(organization_id, vehicle_id, assignments, now)
        definitions = {
            str(row.pk): str(row.presence_item_id)
            for row in KitDefinition.objects.filter(
                organization_id=organization_id, pk__in={line.definition_id for line in lines}
            )
        }
        if len(definitions) != len({line.definition_id for line in lines}):
            raise SealError("a covered kit definition is unavailable")
        require_item_locks(scope, item_locks)
        if any(row.item_id not in item_locks for row in kit_presence):
            raise SealError("a kit-presence item lock is unavailable")
        if any(definitions[line.definition_id] not in item_locks for line in lines):
            raise SealError("a kit-presence item lock is unavailable")
        covered = {item.key for item in scope}
        covered.update((row.item_id, row.kit_id) for row in kit_presence)
        presence_lines = {(definitions[line.definition_id], line.line_id) for line in lines}
        open_deficiency, suspended = seal_blockers(
            organization_id,
            deficiencies,
            covered,
            at=now,
            presence_lines=presence_lines,
            covered_kit_ids=assignments,
        )
        zone = Organization.objects.values_list("station_zone", flat=True).get(pk=organization_id)
        today = now.astimezone(ZoneInfo(zone)).date()
        refusal = verification_refusal(
            SealVerificationFacts(
                sealable=True,
                intact=True,
                marking=seal.marking,
                expires_on=seal.expiration,
                no_expiration=seal.no_expiration,
                audit_pending=pending,
                open_deficiency=open_deficiency,
                suspended=suspended,
            ),
            marking=marking,
            attested=attested,
            today=today,
        )
        if refusal:
            raise SealError(refusal)
        refusal = current_certification(
            organization_id,
            vehicle_id,
            seal,
            scope,
            assignments,
            at=now,
            today=today,
            open_deficiency=open_deficiency,
            suspended=suspended,
        )
        if refusal:
            raise SealError(refusal)
        targets = [
            LinkTarget(
                item.item_id,
                item.kit_id,
                item_requirement_version_id=item.item_requirement_version_id,
            )
            for item in scope
        ]
        targets.extend(
            LinkTarget(definitions[line.definition_id], kit_requirement_id=line.line_id)
            for line in lines
        )
        targets.extend(LinkTarget(row.item_id, row.kit_id) for row in kit_presence)
        planned = plan_links(organization_id, vehicle_id, targets, recorded_at=now)
        substitution_id = new_id()
        count = sum(len(links) for _, links in planned)
        body = {"code": "SEAL_SUBSTITUTED", "substitution_id": substitution_id, "link_count": count}

        def apply(receipt, current):
            substitution = SealSubstitution.objects.create(
                id=substitution_id,
                organization_id=organization_id,
                vehicle_id=vehicle_id,
                seal_event=seal,
                entered_marking=marking,
                intact_attestation=True,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current.user,
                actor_roles_at_time=list(current.roles),
            )
            for target, links in planned:
                persist_substitution_links(substitution, target, links, receipt, current)
            from cya.live_updates.publish import publish_vehicle_update

            publish_vehicle_update(
                organization_id=organization_id,
                vehicle_id=vehicle_id,
                event_id=substitution_id,
                kind="obligations.changed",
            )

        return Plan(
            response_body=body,
            apply=apply,
            target_type="ORGANIZATION",
            target_id=organization_id,
            recorded_at=now,
            new_snapshot={"schema_version": 1, "values": body},
        )

    return run_command(
        organization_id=organization_id,
        operation_id=context["operation_id"],
        command_type="SEAL_SUBSTITUTE",
        request_digest=digest(request),
        correlation_id=context["correlation_id"],
        request_attempt_id=context["request_attempt_id"],
        authorize=authorizer(
            organization_id, context["actor_id"], action="verify_break_reseal_kit_seal"
        ),
        prepare=build,
    )
