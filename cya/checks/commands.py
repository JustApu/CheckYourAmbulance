"""Audited record-check command (SPEC 7.1-7.3, REC-1, REC-4; docs/check-records.md).

record_check is the server boundary for POST /api/v1/checks. It takes IDs and intent, never
role snapshots, and runs through cya.core.audit_commands.run_command, which owns the outermost
transaction, the operation-id receipt (a replay returns the original result and writes nothing)
and the success/failure audit. Do not wrap it in a transaction.

Shape errors (types, ids, sizes) are raised before any transaction. Everything that needs the
database is checked in prepare, after authorization, so a rejected write rolls back whole: no
check, no receipt, no deficiency, and the operation id stays reusable. A Failed or Unable to
Inspect check carries its deficiency (an existing one to add evidence to, or a new one) and
commits atomically with it (docs/deficiencies.md).

A verification check (purpose VERIFICATION, verification_for_deficiency_id set; SPEC VER-4 to
VER-6) is an ordinary check for attribution, idempotency, conflict warnings and obligation links.
It is accepted only for a deficiency that is Pending Verification and for an item and kit instance
in that deficiency's newest scope revision. Under the deficiency lock the same transaction decides
the effect: a Pass recorded strictly after the correction instant that satisfies the last open
scope row closes the deficiency (VERIFICATION_CLOSED) unless an administrator invalidated its
supporting check chain (INV-3: only an explicit closure then closes it), and a Fail adds evidence and returns it to
In Progress (VERIFICATION_FAILED). A closure ends the deficiency's check suspensions still in force
and lists them in suspensions_ended (SPEC SUS-3). Every other unsupported field is refused (NOT_YET_SUPPORTED)
rather than silently ignored, because accepting what is not built would fail open.
"""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from uuid import UUID

from django.db.models import Max
from django.utils import timezone

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id, load_state
from cya.authorization.guard import PermissionDenied, Principal, require_permission
from cya.authorization.locking import locked_identities
from cya.authorization.persistence import DatabaseRoleBackend, digest
from cya.authorization.policy import ResourceContext, Role
from cya.checklists.models import ChecklistPublication, ChecklistVersion, Vehicle
from cya.checklists.overlay import ResolutionError, _active_deviations, resolve_checklist
from cya.checks.conflicts import CheckConflict, conflicting_check
from cya.checks.linking import (
    LinkScopeTooLarge,
    choose_links,
    conflict_obligation,
    persist_links,
    response_links,
)
from cya.checks.models import (
    OUTCOMES,
    PURPOSES,
    CheckEvent,
    CheckObligationLink,
    CheckRecord,
    ItemLock,
)
from cya.conditions import check_effects as completion_effects
from cya.core.audit_commands import Actor, CommandDenied, Plan, run_command
from cya.deficiencies import effects as deficiency_effects
from cya.deficiencies import service as deficiency_service
from cya.deficiencies.effects import NO_KIT_KEY
from cya.deficiencies.effects import satisfied_scope_rows as _satisfied_scope_rows
from cya.deficiencies.models import (
    Deficiency,
    DeficiencyEvent,
    DeficiencyEvidence,
    DeficiencyProjection,
    VerificationScopeRow,
)
from cya.deficiencies.service import DeficiencyError
from cya.deficiencies.transitions import (
    CLOSED_VERIFIED,
    IN_PROGRESS,
    PENDING_VERIFICATION,
    validate_transition,
)
from cya.inspections.models import InspectionSession, SessionParticipant
from cya.inspections.projections import current_state, is_expired, rebuild_session_projection
from cya.operations.models import MAX_TEXT

ACTION = "record_check"
COMMAND_TYPE = "CHECK_RECORD"
CORRECTION_COMMAND_TYPE = "CHECK_CORRECTION"
INVALIDATION_COMMAND_TYPE = "CHECK_INVALIDATION"
INVALIDATION_ACTION = "invalidate_others_check"
MAX_QUANTITY = 1_000_000
MEASUREMENT_PLACES = 10
MEASUREMENT_LIMIT = Decimal(10) ** 19
MAX_UNIT = 40
MAX_SCOPE_ROWS = 2000  # rows of one scope revision a verification check will evaluate


class SelfInvalidationForbidden(CommandDenied):
    """An administrator named their own chain: use a correction instead (INV-5, OQ-33 default).

    It is a CommandDenied so run_command keeps the actor and records a DENIED audit event with
    the role snapshot; the code is the wire code the HTTP layer answers with.
    """

    code, status = "SELF_INVALIDATION_FORBIDDEN", 403


def _self_invalidation_forbidden(*, actor_id, author_id):
    """OQ-33 / INV-5 policy point: an administrator may not invalidate their own chain.

    The default is no (they correct it instead); the owner's later decision is a change here.
    """
    return actor_id == author_id


def _correction_time_limit_elapsed(*, check, now):
    """OQ-32 policy point: corrections have no time limit unless the owner changes it."""
    return False


class CheckError(ValueError):
    """A rule was violated; the command rolls back and is audited as failed.

    code is the stable wire code, status the HTTP status it maps to, path a safe schema path.
    """

    def __init__(self, code, message, *, status=422, path=None):
        super().__init__(message)
        self.code, self.status, self.path = code, status, path


def _malformed(path, message):
    return CheckError("MALFORMED_REQUEST", message, status=400, path=path)


def _id(value, path):
    try:
        return canonical_id(value)
    except ValueError:
        raise _malformed(path, f"{path} must be a canonical lowercase UUID") from None


def _operation_id(value):
    _id(value, "operation_id")
    if UUID(value).version != 4:
        raise _malformed("operation_id", "operation_id must be a version 4 UUID")


def _optional_id(value, path):
    return None if value is None else _id(value, path)


def _quantity(value):
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= MAX_QUANTITY:
        raise CheckError(
            "INVALID_OBSERVATION",
            f"quantity must be a whole number from 0 to {MAX_QUANTITY}",
            path="quantity",
        )
    return value


def _measurement(value):
    """(Decimal, unit) from {"value": "12.5", "unit": "psi"}; never rounds, never non-finite."""
    if value is None:
        return None
    bad = CheckError(
        "INVALID_OBSERVATION",
        "measurement must be {value: decimal string, unit: text}",
        path="measurement",
    )
    if not isinstance(value, dict) or set(value) != {"value", "unit"}:
        raise bad
    raw, unit = value["value"], value["unit"]
    if not isinstance(raw, str) or not isinstance(unit, str) or not unit.strip():
        raise bad
    if len(unit) > MAX_UNIT:
        raise bad
    try:
        number = Decimal(raw)
    except InvalidOperation:
        raise bad from None
    exponent = number.as_tuple().exponent
    if not number.is_finite() or abs(number) >= MEASUREMENT_LIMIT or -exponent > MEASUREMENT_PLACES:
        raise bad
    return number, unit


def _explanation(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT:
        raise _malformed(
            "explanation", f"explanation must be nonblank text of at most {MAX_TEXT} characters"
        )
    return value


def _observed(value):
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise _malformed(
            "client_observed_at", "client_observed_at must be a timezone-aware instant"
        )
    return value


def _unsupported(**fields):
    for path, value in fields.items():
        if value is not None:
            raise CheckError(
                "NOT_YET_SUPPORTED",
                f"{path} is part of the contract but is not available yet",
                path=path,
            )


def _shape(*, vehicle_id, item_id, checklist_version_id, outcome, purpose, **values):
    """Validate everything that needs no database; returns the normalized fields."""
    shape = {
        "vehicle_id": _id(vehicle_id, "vehicle_id"),
        "item_id": _id(item_id, "item_id"),
        "checklist_version_id": _id(checklist_version_id, "checklist_version_id"),
    }
    if outcome is not None and outcome not in OUTCOMES:
        raise CheckError(
            "INVALID_OUTCOME", f"outcome must be one of {list(OUTCOMES)}", path="outcome"
        )
    if purpose not in PURPOSES:
        raise _malformed("purpose", f"purpose must be one of {list(PURPOSES)}")
    shape |= {
        "outcome": outcome,
        "purpose": purpose,
        "quantity": _quantity(values["quantity"]),
        "measurement": _measurement(values["measurement"]),
        "explanation": _explanation(values["explanation"]),
        "client_observed_at": _observed(values["client_observed_at"]),
        "kit_id": _optional_id(values["kit_id"], "kit_id"),
        "session_id": _optional_id(values["session_id"], "session_id"),
    }
    for path in ("basis_check_id", "acknowledges_check_id"):
        shape[path] = _optional_id(values[path], path)
    shape["verification_for_deficiency_id"] = _optional_id(
        values["verification_for_deficiency_id"], "verification_for_deficiency_id"
    )
    if purpose == "VERIFICATION" and shape["verification_for_deficiency_id"] is None:
        raise CheckError(
            "VERIFICATION_TARGET_REQUIRED",
            "a verification check names the deficiency it verifies",
            path="verification_for_deficiency_id",
        )
    if purpose == "ROUTINE" and shape["verification_for_deficiency_id"] is not None:
        raise CheckError(
            "VERIFICATION_TARGET_NOT_ALLOWED",
            "only a verification check names a deficiency to verify",
            path="verification_for_deficiency_id",
        )
    try:
        shape["deficiency"] = deficiency_service.check_selection(values["deficiency"])
    except DeficiencyError as error:
        raise CheckError(error.code, str(error), status=error.status, path=error.path) from None
    return shape


def _vehicle(organization_id, vehicle_id, *, lock=False):
    """With lock, hold the vehicle row: the session's activity clock is read under it."""
    vehicles = Vehicle.objects.filter(organization_id=organization_id, pk=vehicle_id)
    vehicle = (vehicles.select_for_update() if lock else vehicles).first()
    if vehicle is None:
        raise CheckError("NOT_FOUND", "vehicle was not found", status=404, path="vehicle_id")
    if not vehicle.active:
        raise CheckError("VEHICLE_INACTIVE", "vehicle is inactive", path="vehicle_id")
    return vehicle


def _published_version(organization_id, version_id):
    """A version never published is invisible: drafts are management data, not for crews."""
    version = ChecklistVersion.objects.filter(
        organization_id=organization_id, pk=version_id
    ).first()
    published = (
        version is not None
        and ChecklistPublication.objects.filter(
            organization_id=organization_id, checklist_version=version
        ).exists()
    )
    if not published:
        raise CheckError(
            "NOT_FOUND", "checklist version was not found", status=404, path="checklist_version_id"
        )
    return version


def _pinned_session(organization_id, actor, shape):
    """The session whose checklist governs this check, or None to use the current checklist.

    Only a recorded participant on the request's own vehicle gets its session's checklist. Any
    other caller, for a real session or an unknown one, is resolved identically against the
    current checklist, so nothing here reveals whether a session exists; _session_for_check
    then answers both with the same 404.
    """
    if shape["session_id"] is None:
        return None
    session = InspectionSession.objects.filter(
        organization_id=organization_id, pk=shape["session_id"]
    ).first()
    if (
        session is None
        or str(session.vehicle_id) != shape["vehicle_id"]
        or not SessionParticipant.objects.filter(session=session, user=actor.user).exists()
    ):
        return None
    return session


def _resolved_item(organization_id, vehicle_id, version, item_id, now, session=None):
    """The item as the vehicle's governing checklist defines it; no silent version substitution.

    A check in a session is resolved against the checklist the session opened with, including
    the vehicle override it knew then, even after a newer version or override takes effect
    (SPEC CHK-4, 5.7). Only an active stocking deviation is read as of now, so a count
    is judged against the target in force when it is recorded. Outside a session the vehicle's
    current checklist governs.
    """
    pinned = session is not None and str(session.checklist_version_id) == str(version.pk)
    try:
        resolved = resolve_checklist(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            template_id=version.template_id,
            as_of=session.opened_at if pinned else now,
            recorded_before=session.recorded_at + timedelta(microseconds=1) if pinned else None,
        )
    except ResolutionError as error:
        raise CheckError("CHECKLIST_UNAVAILABLE", str(error), status=409) from None
    if resolved.template_version_id != version.pk:
        raise CheckError(
            "CHECKLIST_VERSION_NOT_CURRENT",
            "the vehicle's checklist version has changed; reload the checklist",
            status=409,
            path="checklist_version_id",
        )
    for item in resolved.items:
        if item.requirement.item_id == item_id:
            if pinned and item.requirement.kind == "QUANTITY":
                deviation = _active_deviations(organization_id, vehicle_id, [item_id], now).get(
                    item_id
                )
                item = replace(
                    item,
                    effective_quantity=(
                        deviation.alternate_quantity
                        if deviation
                        else item.requirement.expected_quantity
                    ),
                    deviation_id=deviation.pk if deviation else None,
                )
            return item
    raise CheckError(
        "ITEM_NOT_IN_CHECKLIST",
        "the item is not part of this vehicle's checklist version",
        path="item_id",
    )


def _check_observation(item, shape):
    """Quantity and measurement must match the item kind (SPEC 4.4, contracts Record a check)."""
    requirement, outcome = item.requirement, shape["outcome"]
    quantity, measurement = shape["quantity"], shape["measurement"]
    observed = outcome != "UNABLE_TO_INSPECT"  # nothing can be observed when unable to inspect
    if requirement.kind == "VEHICLE_READING":
        raise CheckError(
            "NOT_YET_SUPPORTED",
            "vehicle readings are recorded as odometer entries, not checks",
            path="item_id",
        )
    if requirement.kind == "QUANTITY":
        if measurement is not None or (quantity is None) == observed:
            raise CheckError(
                "INVALID_OBSERVATION",
                "a quantity item needs a quantity (and none when unable to inspect)",
                path="quantity",
            )
        if outcome == "PASSED" and quantity != item.effective_quantity:
            raise CheckError(
                "INVALID_OBSERVATION",
                f"a Passed quantity must equal the target of {item.effective_quantity}",
                path="quantity",
            )
    elif requirement.kind == "MEASUREMENT":
        if quantity is not None or (measurement is None) == observed:
            raise CheckError(
                "INVALID_OBSERVATION",
                "a measurement item needs a measurement (and none when unable to inspect)",
                path="measurement",
            )
        if measurement is not None and measurement[1] != requirement.unit:
            raise CheckError(
                "INVALID_OBSERVATION",
                f"the measurement unit must be {requirement.unit}",
                path="measurement",
            )
    elif quantity is not None or measurement is not None:
        raise CheckError(
            "INVALID_OBSERVATION",
            "this item kind takes no quantity or measurement",
            path="quantity",
        )


def _outcome(item, shape):
    """The outcome to store: the one stated, or for a quantity item the one its count implies.

    Derived against the exact target (SPEC 4.4, SEAL-6): a count equal to the target, including
    an active stocking deviation's alternate quantity, is Passed; any other count is Failed.
    The crew member may still state Failed for a matching count (condition, not count), but a
    mismatch can never be stated as Passed (_check_observation), so the stated outcome can only
    make the derived one worse.
    """
    if shape["outcome"] is not None:
        return shape["outcome"]
    if item.requirement.kind != "QUANTITY":
        raise CheckError("INVALID_OUTCOME", "the outcome is required for this item", path="outcome")
    if shape["quantity"] is None:
        raise CheckError("INVALID_OBSERVATION", "a quantity item needs a quantity", path="quantity")
    return "PASSED" if shape["quantity"] == item.effective_quantity else "FAILED"


def _check_outcome(item, shape):
    outcome = shape["outcome"]
    if outcome not in item.requirement.allowed_outcomes:
        raise CheckError(
            "INVALID_OUTCOME", "the outcome is not allowed for this item", path="outcome"
        )
    if outcome == "UNABLE_TO_INSPECT" and shape["explanation"] is None:
        raise CheckError(
            "EXPLANATION_REQUIRED", "Unable to Inspect requires a reason", path="explanation"
        )


def _checked_reference(prospective, check_id, path):
    """A basis or acknowledged check must be an earlier check of this same item (CON-3).

    One that is not in this organization looks the same as one that does not exist (404); one
    that belongs to another vehicle, item or kit instance cannot be a basis here (422).
    """
    if check_id is None:
        return None
    row = CheckRecord.objects.filter(
        organization_id=prospective.organization_id, pk=check_id
    ).first()
    if row is None:
        raise CheckError("NOT_FOUND", "check was not found", status=404, path=path)
    if (str(row.vehicle_id), str(row.item_id), row.kit_id) != (
        str(prospective.vehicle_id),
        str(prospective.item_id),
        prospective.kit_id,
    ):
        raise CheckError("INVALID_LINK", "the check is not a check of this item", path=path)
    return row


def _session_for_check(organization_id, actor, shape, version, now):
    """The session a check is made in, or None. The vehicle row is already locked.

    A participant may record into a session on its own vehicle and pinned checklist version
    while it is Open. An unknown session and one the actor did not join answer alike (404), so
    its existence is not revealed. A session idle for six hours has already closed (SES-1): the
    check is refused so late work can never extend or contradict the close; the crew member
    reopens the session and records again.
    """
    if shape["session_id"] is None:
        return None
    session = InspectionSession.objects.filter(
        organization_id=organization_id, pk=shape["session_id"]
    ).first()
    if (
        session is None
        or not SessionParticipant.objects.filter(session=session, user=actor.user).exists()
    ):
        raise CheckError("NOT_FOUND", "session was not found", status=404, path="session_id")
    if str(session.vehicle_id) != shape["vehicle_id"] or str(session.checklist_version_id) != str(
        version.pk
    ):
        raise CheckError(
            "SESSION_MISMATCH",
            "the session is for another vehicle or checklist version",
            path="session_id",
        )
    state = current_state(session)
    if state.state != "OPEN" or is_expired(state, now):
        raise CheckError(
            "SESSION_NOT_OPEN",
            "the session has ended or been idle for six hours; reopen it first",
            status=409,
            path="session_id",
        )
    return session


def chosen_kit_id(in_kit, kits, requested):
    """The vehicle's own kit instance this check covers, frozen on the check.

    A kit is a template; a vehicle keeps zero, one or several instances of it, and each
    instance needs its own check. So a check on an item inside a kit always names its instance
    (KIT_REQUIRED when it does not, even if only one exists, so a stale screen can never be
    credited to a kit it did not show), and that instance must be one of the vehicle's own.
    With no instance there is nothing to check. An item outside a kit takes no kit.
    """
    present = [kit_id for kit_id, _label in kits]
    if not in_kit:
        if requested is not None:
            raise CheckError("KIT_NOT_ON_VEHICLE", "the item is not part of a kit", path="kit_id")
        return None
    if not present:
        raise CheckError("KIT_NOT_ON_VEHICLE", "the vehicle has no kit of this type", path="kit_id")
    if requested is None:
        raise CheckError("KIT_REQUIRED", "say which kit this check covers", path="kit_id")
    if requested not in present:
        raise CheckError(
            "KIT_NOT_ON_VEHICLE",
            "the kit is not one of this vehicle's kits for the item",
            path="kit_id",
        )
    return requested


def _authorize(organization_id, actor_id, *extra_actions):
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
            try:
                for action in (ACTION, *extra_actions):
                    require_permission(principal, organization_id, action)
            except PermissionDenied:
                raise CommandDenied(actor) from None
            return actor

    return authorize


def _needs_deficiency(outcome):
    return outcome in ("FAILED", "UNABLE_TO_INSPECT")


def _verification_plan(organization_id, shape, chosen_kit):
    """Lock the verified deficiency and decide what this verification check does (VER-4, VER-5).

    Runs in prepare after the vehicle and item locks, as the last lock group. The deficiency must
    be on this vehicle and Pending Verification, and the item and kit instance must be a row of
    its newest scope revision (an older revision belongs to a failed attempt). The returned plan
    carries the instant to record, refreshed after the lock so a check that waited behind a
    correction is never dated before it: only a Pass strictly after the row's instant counts. It
    closes when it counts and leaves no row open; a Fail returns the deficiency to work.
    """
    deficiency_id = shape["verification_for_deficiency_id"]
    path = "verification_for_deficiency_id"
    projection = deficiency_service.lock_deficiencies(organization_id, [deficiency_id])[
        deficiency_id
    ]
    vehicle_id = Deficiency.objects.values_list("vehicle_id", flat=True).get(
        organization_id=organization_id, pk=deficiency_id
    )
    if str(vehicle_id) != shape["vehicle_id"]:
        raise DeficiencyError(
            "DEFICIENCY_VEHICLE_MISMATCH", "the deficiency belongs to another vehicle", path=path
        )
    if projection.status != PENDING_VERIFICATION:
        raise CheckError(
            "VERIFICATION_NOT_PENDING",
            "the deficiency is not Pending Verification",
            status=409,
            path=path,
        )
    last = deficiency_service.last_event(deficiency_id)
    if projection.last_applied_event_id != last.pk or last.to_status != PENDING_VERIFICATION:
        raise CheckError("CONFIGURATION_UNAVAILABLE", "deficiency state is unavailable", status=503)
    scope = VerificationScopeRow.objects.filter(
        organization_id=organization_id, deficiency_id=deficiency_id
    )
    revision = scope.aggregate(newest=Max("scope_revision"))["newest"]
    rows = list(scope.filter(scope_revision=revision)[: MAX_SCOPE_ROWS + 1]) if revision else []
    if not rows or len(rows) > MAX_SCOPE_ROWS:
        raise CheckError(
            "CONFIGURATION_UNAVAILABLE", "verification scope is unavailable", status=503
        )
    key = chosen_kit or NO_KIT_KEY
    own = next(
        (r for r in rows if str(r.item_id) == shape["item_id"] and r.instance_key == key), None
    )
    if own is None:
        raise CheckError(
            "NOT_IN_VERIFICATION_SCOPE",
            "this item and kit are not part of the deficiency's verification scope",
            path="item_id",
        )
    now = timezone.now()
    counts = shape["outcome"] == "PASSED" and now > own.required_after
    satisfied = _satisfied_scope_rows(organization_id, deficiency_id, rows)
    if counts:
        satisfied.add(own.pk)
    remaining = len(rows) - len(satisfied)
    # INV-3 (critical): a deficiency whose supporting check chain an administrator invalidated stays
    # open until an explicit closure under 8.8. Verification still records and counts, but it never
    # closes such a deficiency by itself. Invalidation locks every supporting deficiency, so this
    # read is serialized with it by the deficiency lock held here.
    withheld = CheckEvent.objects.filter(
        organization_id=organization_id,
        event_type="INVALIDATION",
        root_check_id__in=DeficiencyEvidence.objects.filter(
            organization_id=organization_id,
            deficiency_id=deficiency_id,
            check_record__isnull=False,
        ).values("check_record__root_check_id"),
    ).exists()
    event_type = to_status = None
    if shape["outcome"] == "FAILED":
        event_type, to_status = "VERIFICATION_FAILED", IN_PROGRESS
    elif counts and remaining == 0 and not withheld:
        event_type, to_status = "VERIFICATION_CLOSED", CLOSED_VERIFIED
    if event_type is not None:
        try:
            validate_transition(PENDING_VERIFICATION, to_status)
        except ValueError:
            raise CheckError(
                "CONFIGURATION_UNAVAILABLE", "verification transition is unavailable", status=503
            ) from None
    # A verified closure ends every check suspension still in force (SUS-3), read under the lock.
    ended = (
        deficiency_service.suspension_ids_in_force(organization_id, deficiency_id)
        if event_type == "VERIFICATION_CLOSED"
        else []
    )
    return SimpleNamespace(
        deficiency_id=deficiency_id,
        last=last,
        revision=revision,
        suspensions_ended=ended,
        counts=counts,
        remaining=remaining,
        closure_withheld=withheld and counts and remaining == 0,
        event_type=event_type,
        to_status=to_status,
        event_id=new_id() if event_type else None,
        now=now,
    )


def _verification_body(plan):
    return {
        "deficiency_id": plan.deficiency_id,
        "scope_revision": plan.revision,
        "counts_toward_scope": plan.counts,
        "remaining_scope_rows": plan.remaining,
        "closure_withheld": plan.closure_withheld,
        "status": plan.to_status or PENDING_VERIFICATION,
        "suspensions_ended": plan.suspensions_ended,
    }


def record_check(
    *,
    vehicle_id,
    item_id,
    checklist_version_id,
    outcome=None,
    purpose="ROUTINE",
    quantity=None,
    measurement=None,
    explanation=None,
    client_observed_at=None,
    kit_id=None,
    session_id=None,
    basis_check_id=None,
    acknowledges_check_id=None,
    verification_for_deficiency_id=None,
    deficiency=None,
    organization_id,
    actor_id,
    operation_id,
    correlation_id,
    request_attempt_id,
):
    """Record one attributed item-level check (REC-1); idempotent on operation_id (REC-4).

    The actor is the authenticated user; nothing here accepts a client-supplied actor or roles.
    """
    for value in (organization_id, actor_id):
        canonical_id(value)
    _operation_id(operation_id)
    shape = _shape(
        vehicle_id=vehicle_id,
        item_id=item_id,
        checklist_version_id=checklist_version_id,
        outcome=outcome,
        purpose=purpose,
        quantity=quantity,
        measurement=measurement,
        explanation=explanation,
        client_observed_at=client_observed_at,
        kit_id=kit_id,
        session_id=session_id,
        basis_check_id=basis_check_id,
        acknowledges_check_id=acknowledges_check_id,
        verification_for_deficiency_id=verification_for_deficiency_id,
        deficiency=deficiency,
    )
    request = {
        "vehicle_id": vehicle_id,
        "item_id": item_id,
        "checklist_version_id": checklist_version_id,
        "outcome": outcome,
        "purpose": purpose,
        "quantity": quantity,
        "measurement": measurement,
        "explanation": explanation,
        "client_observed_at": client_observed_at.isoformat() if client_observed_at else None,
        "kit_id": kit_id,
        "session_id": session_id,
        "basis_check_id": basis_check_id,
        "acknowledges_check_id": acknowledges_check_id,
        "verification_for_deficiency_id": verification_for_deficiency_id,
        "deficiency": deficiency,
    }

    def build(actor):
        _vehicle(organization_id, shape["vehicle_id"], lock=shape["session_id"] is not None)
        # Serialize every check for this vehicle/item, including checks outside a session.
        # Publication creates these rows; a missing row is a configuration failure.
        lock_row = (
            ItemLock.objects.select_for_update()
            .filter(
                organization_id=organization_id,
                vehicle_id=shape["vehicle_id"],
                item_id=shape["item_id"],
            )
            .first()
        )
        if lock_row is None:
            # A lock is provisioned only for an item in the published vehicle checklist.
            # Preserve the normal item-level refusal for an out-of-scope item; if resolution
            # succeeds, the missing lock is a real configuration failure and remains fail-closed.
            version = _published_version(organization_id, shape["checklist_version_id"])
            _resolved_item(
                organization_id,
                shape["vehicle_id"],
                version,
                shape["item_id"],
                timezone.now(),
                _pinned_session(organization_id, actor, shape),
            )
            raise CheckError(
                "CONFIGURATION_UNAVAILABLE",
                "item lock is unavailable",
                status=503,
                path="item_id",
            )
        # Assign the evidence instant only after all check-serialization locks are held. A
        # waiter must not record an earlier instant than a check that committed ahead of it.
        now = timezone.now()
        version = _published_version(organization_id, shape["checklist_version_id"])
        item = _resolved_item(
            organization_id,
            shape["vehicle_id"],
            version,
            shape["item_id"],
            now,
            _pinned_session(organization_id, actor, shape),
        )
        shape["derived"] = shape["outcome"] is None
        shape["outcome"] = _outcome(item, shape)
        _check_outcome(item, shape)
        _check_observation(item, shape)
        # The rules above hold for every outcome; only then refuse what is not built yet.
        _unsupported(
            outcome=None
            if shape["outcome"] in ("PASSED", "FAILED", "UNABLE_TO_INSPECT")
            else shape["outcome"],
        )
        verification_id = shape["verification_for_deficiency_id"]
        selection = shape["deficiency"]
        if verification_id is not None:
            if shape["outcome"] not in ("PASSED", "FAILED"):
                raise CheckError(
                    "INVALID_OUTCOME", "a verification check is Passed or Failed", path="outcome"
                )
            if shape["outcome"] == "FAILED":
                # A failed attempt is evidence for the deficiency it verifies and for no other.
                if selection not in (None, ("existing", verification_id)):
                    raise CheckError(
                        "DEFICIENCY_SELECTION_CONFLICT",
                        "a failed verification adds evidence only to the deficiency it verifies",
                        path="deficiency",
                    )
                selection = ("existing", verification_id)
        if _needs_deficiency(shape["outcome"]) and selection is None:
            raise CheckError(
                "DEFICIENCY_REQUIRED",
                "a Failed or Unable to Inspect check names its deficiency",
                path="deficiency",
            )
        if not _needs_deficiency(shape["outcome"]) and selection is not None:
            raise CheckError(
                "DEFICIENCY_NOT_ALLOWED",
                "only a Failed or Unable to Inspect check takes a deficiency",
                path="deficiency",
            )
        session = _session_for_check(organization_id, actor, shape, version, now)
        chosen_kit = chosen_kit_id(item.requirement.kit_id is not None, item.kits, shape["kit_id"])
        check_id = new_id()
        prospective_check = CheckRecord(
            id=check_id,
            organization_id=organization_id,
            vehicle_id=shape["vehicle_id"],
            item_id=shape["item_id"],
            kit_id=chosen_kit,
        )
        try:
            links = choose_links(prospective_check, recorded_at=now)
        except LinkScopeTooLarge:
            raise CheckError(
                "CONFIGURATION_UNAVAILABLE",
                "obligation overlap exceeds the supported resolver scope",
                status=503,
                path="item_id",
            ) from None
        _checked_reference(prospective_check, shape["basis_check_id"], "basis_check_id")
        _checked_reference(
            prospective_check, shape["acknowledges_check_id"], "acknowledges_check_id"
        )
        scope_obligation = conflict_obligation(prospective_check, recorded_at=now)
        newer = conflicting_check(
            prospective_check,
            scope_obligation,
            actor_id=actor.user.pk,
            basis_check_id=shape["basis_check_id"],
            acknowledges_check_id=shape["acknowledges_check_id"],
        )
        if newer is not None:
            # A warning is not a commit: it rolls back whole, so it writes no receipt and the
            # operation id stays free; an identical retry is evaluated again (REC-4, CON-3).
            raise CheckConflict(newer, scope_obligation)
        # Deficiencies are the last lock group, after the item lock held above (contract order).
        try:
            category = severity = None
            if verification_id is not None:
                pass  # _verification_plan below locks and checks the one deficiency it names
            elif selection is not None and selection[0] == "existing":
                projections = deficiency_service.lock_deficiencies(organization_id, [selection[1]])
                deficiency_service.require_active(projections[selection[1]])
                if (
                    str(
                        Deficiency.objects.values_list("vehicle_id", flat=True).get(pk=selection[1])
                    )
                    != shape["vehicle_id"]
                ):
                    raise DeficiencyError(
                        "DEFICIENCY_VEHICLE_MISMATCH",
                        "the deficiency belongs to another vehicle",
                        path="deficiency.existing_id",
                    )
            elif selection is not None:
                category = deficiency_service.resolve_category(
                    organization_id, selection[1]["category_id"], now
                )
                severity = deficiency_service.resolve_severity_level(
                    organization_id,
                    selection[1]["severity"],
                    now,
                    path="deficiency.create.severity",
                )
        except DeficiencyError as error:
            raise CheckError(error.code, str(error), status=error.status, path=error.path) from None
        verification = None
        if verification_id is not None:
            try:
                verification = _verification_plan(organization_id, shape, chosen_kit)
            except DeficiencyError as error:
                raise CheckError(
                    error.code, str(error), status=error.status, path=error.path
                ) from None
            now = verification.now
        deficiency_id = selection[1] if selection and selection[0] == "existing" else new_id()
        creation_event_id = new_id() if category is not None else None
        measured = shape["measurement"]

        def apply(receipt, current):
            check = CheckRecord.objects.create(
                id=check_id,
                root_check_id=check_id,
                organization_id=organization_id,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current.user,
                actor_roles_at_time=list(current.roles),
                operation_id=operation_id,
                vehicle_id=shape["vehicle_id"],
                item_id=shape["item_id"],
                item_def_id=item.requirement.definition_id,
                checklist_version_id=version.pk,
                kit_id=chosen_kit,
                session=session,
                basis_check_id=shape["basis_check_id"],
                acknowledges_check_id=shape["acknowledges_check_id"],
                outcome=shape["outcome"],
                quantity=shape["quantity"],
                measurement_value=measured[0] if measured else None,
                measurement_unit=measured[1] if measured else None,
                explanation=shape["explanation"],
                client_observed_at=shape["client_observed_at"],
                purpose=shape["purpose"],
                verification_for_deficiency_id=verification_id,
                verification_scope_revision=verification.revision if verification else None,
            )
            persist_links(check, receipt, current, links)
            if selection is not None:
                if category is not None:
                    deficiency_service.insert_deficiency(
                        receipt,
                        current,
                        organization_id=organization_id,
                        deficiency_id=deficiency_id,
                        event_id=creation_event_id,
                        operation_id=operation_id,
                        vehicle_id=shape["vehicle_id"],
                        category=category,
                        severity=severity,
                        fields=selection[1],
                        affected=[(shape["item_id"], item.requirement.definition_id)],
                        item_id=shape["item_id"],
                        kit_id=chosen_kit,
                    )
                deficiency_service.add_check_evidence(
                    receipt,
                    current,
                    organization_id=organization_id,
                    deficiency_id=deficiency_id,
                    check_id=check_id,
                )
            if verification is not None and verification.event_type is not None:
                event = DeficiencyEvent.objects.create(
                    id=verification.event_id,
                    organization_id=organization_id,
                    deficiency_id=verification.deficiency_id,
                    sequence=verification.last.sequence + 1,
                    predecessor_event=verification.last,
                    event_type=verification.event_type,
                    from_status=PENDING_VERIFICATION,
                    to_status=verification.to_status,
                    verification_check_id=check_id,
                    suspensions_ended=verification.suspensions_ended,
                    **deficiency_service.stamp(receipt, current),
                )
                deficiency_service.end_suspensions(
                    receipt, current, organization_id=organization_id, event=event
                )
                DeficiencyProjection.objects.filter(
                    organization_id=organization_id, deficiency_id=verification.deficiency_id
                ).update(status=verification.to_status, last_applied_event=event)
            if session is not None:
                rebuild_session_projection(session)  # the new check is persisted activity

            from cya.live_updates.publish import publish_vehicle_update

            publish_vehicle_update(
                organization_id=organization_id,
                vehicle_id=shape["vehicle_id"],
                event_id=check_id,
                kind="check.recorded",
            )
            if selection is not None or (verification and verification.event_type):
                publish_vehicle_update(
                    organization_id=organization_id,
                    vehicle_id=shape["vehicle_id"],
                    event_id=creation_event_id
                    or (verification.event_id if verification else None)
                    or check_id,
                    kind="deficiency.changed",
                )

        # No explanation text in the audit snapshot: it is free text from the crew member.
        snapshot = {
            "schema_version": 1,
            "values": {
                "check_id": check_id,
                "vehicle_id": shape["vehicle_id"],
                "item_id": shape["item_id"],
                "checklist_version_id": version.pk,
                "outcome": shape["outcome"],
                "outcome_source": "DERIVED" if shape["derived"] else "STATED",
                "purpose": shape["purpose"],
                "deficiency_ids": [deficiency_id] if selection is not None else [],
                "deficiency_created": category is not None,
            },
        }
        body = {
            "code": "CHECK_RECORDED",
            "check_id": check_id,
            "outcome": shape["outcome"],
            "obligation_links": response_links(links),
            "deficiency_ids": [deficiency_id] if selection is not None else [],
            "event_ids": [creation_event_id] if creation_event_id else [],
        }
        if verification is not None:
            # Only a verification check reports its effect, so a routine response is unchanged.
            body["verification"] = _verification_body(verification)
            if verification.event_id:
                body["event_ids"].append(verification.event_id)
            snapshot["values"] |= {
                "verification_for_deficiency_id": verification_id,
                "verification_counts": verification.counts,
                "verification_event_id": verification.event_id,
            }
        return Plan(
            response_body=body,
            apply=apply,
            response_status=201,
            target_type="ORGANIZATION",
            target_id=organization_id,
            new_snapshot=snapshot,
            recorded_at=now,
        )

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type=COMMAND_TYPE,
        request_digest=digest(request),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=_authorize(
            organization_id,
            actor_id,
            *(
                ("create_deficiency",)
                if shape["deficiency"] and shape["deficiency"][0] == "create"
                else ()
            ),
        ),
        prepare=build,
    )


def _planned(call, *args):
    """Run a deficiency-effect planner, showing its refusals as the command's own errors."""
    try:
        return call(*args)
    except DeficiencyError as error:
        raise CheckError(error.code, str(error), status=error.status, path=error.path) from None


def _chain_tip(root):
    """The one row of a correction chain nothing has corrected; a forked chain fails closed."""
    children = CheckRecord.objects.filter(
        root_check=root, predecessor_check__isnull=False
    ).values_list("predecessor_check_id", flat=True)
    tips = list(
        CheckRecord.objects.filter(root_check=root)
        .exclude(pk__in=children)
        .order_by("recorded_at", "id")
        .values_list("pk", flat=True)[:2]
    )
    if len(tips) != 1:
        raise CheckError("CONFIGURATION_UNAVAILABLE", "check chain is unavailable", status=503)
    return CheckRecord.objects.get(pk=tips[0])


def _completion_effects(organization_id, root_id):
    try:
        return completion_effects.plan_check_effects(organization_id, root_id)
    except completion_effects.CompletionEffectLimit:
        raise CheckError(
            "CONFIGURATION_UNAVAILABLE", "completion effect scope is too large", status=503
        ) from None


def _verification_observation(quantity, measurement):
    """The observation fields of a corrected verification row, as validated."""
    return {
        "quantity": quantity,
        "measurement_value": measurement[0] if measurement is not None else None,
        "measurement_unit": measurement[1] if measurement is not None else None,
    }


def correct_check(
    *,
    check_id,
    expected_tip_check_id,
    outcome,
    operation_id,
    organization_id,
    actor_id,
    correlation_id,
    request_attempt_id,
    quantity=None,
    measurement=None,
    explanation=None,
):
    """Append an author-only correction; the expected tip prevents concurrent forks."""
    for value in (organization_id, actor_id):
        canonical_id(value)
    check_id = _id(check_id, "check_id")
    expected_tip_check_id = _id(expected_tip_check_id, "expected_tip_check_id")
    _operation_id(operation_id)
    if outcome not in OUTCOMES:
        raise CheckError("INVALID_OUTCOME", "outcome is not supported", path="outcome")
    quantity = _quantity(quantity)
    measurement = _measurement(measurement)
    explanation = _explanation(explanation)

    def build(actor):
        root = (
            CheckRecord.objects.filter(organization_id=organization_id, pk=check_id)
            .select_related("item_def")
            .first()
        )
        if root is None:
            raise CheckError("NOT_FOUND", "check was not found", status=404, path="check_id")
        if root.actor_user_id != actor.user.pk:
            raise CheckError("AUTHOR_REQUIRED", "only the check author may correct it", status=403)
        # Session corrections count as activity and must serialize with the auto-close
        # worker, which takes this same vehicle lock before deciding whether to close.
        if root.session_id is not None:
            vehicle = (
                Vehicle.objects.select_for_update()
                .filter(organization_id=organization_id, pk=root.vehicle_id)
                .first()
            )
            if vehicle is None:
                raise CheckError(
                    "CONFIGURATION_UNAVAILABLE", "vehicle lock is unavailable", status=503
                )
        lock = (
            ItemLock.objects.select_for_update()
            .filter(
                organization_id=organization_id, vehicle_id=root.vehicle_id, item_id=root.item_id
            )
            .first()
        )
        if lock is None:
            raise CheckError("CONFIGURATION_UNAVAILABLE", "item lock is unavailable", status=503)
        root = CheckRecord.objects.get(organization_id=organization_id, pk=root.root_check_id)
        now = timezone.now()
        if root.session_id is not None:
            session = InspectionSession.objects.filter(
                organization_id=organization_id, pk=root.session_id
            ).first()
            if session is None:
                raise CheckError(
                    "CONFIGURATION_UNAVAILABLE", "inspection session is unavailable", status=503
                )
            session_state = current_state(session)
            if session_state.state != "OPEN" or is_expired(session_state, now):
                raise CheckError(
                    "SESSION_NOT_OPEN",
                    "the session has ended or been idle for six hours; reopen it first",
                    status=409,
                    path="session_id",
                )
        if _correction_time_limit_elapsed(check=root, now=now):
            raise CheckError("CORRECTION_EXPIRED", "the correction period has ended", status=409)
        if CheckEvent.objects.filter(
            root_check=root, event_type="INVALIDATION", recorded_at__lte=now
        ).exists():
            raise CheckError(
                "CHAIN_INVALIDATED", "an invalidated check cannot be corrected", status=409
            )
        tip = _chain_tip(root)
        if tip.pk != expected_tip_check_id:
            raise CheckError("CHAIN_CHANGED", "the check chain changed", status=409)
        unable_to_failed = tip.outcome == "UNABLE_TO_INSPECT" and outcome == "FAILED"
        verification = root.purpose == "VERIFICATION"
        if verification:
            # issue 155 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/155): an author may withdraw a verification Pass (to Failed or Unable to
            # Inspect); it stops counting toward scope and may reopen a closure, never close one.
            # Any other verification correction would need its own closure or return rules.
            if tip.outcome != "PASSED" or outcome not in ("FAILED", "UNABLE_TO_INSPECT"):
                raise CheckError(
                    "NOT_YET_SUPPORTED",
                    "a verification check can only be corrected from Passed to Failed or Unable",
                    path="outcome",
                )
        elif outcome != "PASSED" and not unable_to_failed:
            raise CheckError(
                "NOT_YET_SUPPORTED",
                "this correction outcome is not available yet",
                path="outcome",
            )
        if (
            unable_to_failed
            and not DeficiencyEvidence.objects.filter(
                organization_id=organization_id, check_record__root_check_id=root.pk
            ).exists()
        ):
            raise CheckError(
                "CONFIGURATION_UNAVAILABLE", "Unable check has no deficiency evidence", status=503
            )
        deviations = _active_deviations(
            organization_id, root.vehicle_id, [root.item_id], root.recorded_at
        )
        deviation = deviations.get(root.item_id)
        item = SimpleNamespace(
            requirement=SimpleNamespace(
                kind=root.item_def.kind,
                unit=root.item_def.unit,
                expected_quantity=root.item_def.expected_quantity,
                allowed_outcomes=tuple(root.item_def.allowed_outcomes),
            ),
            effective_quantity=(
                deviation.alternate_quantity if deviation else root.item_def.expected_quantity
            ),
        )
        shape = {
            "outcome": outcome,
            "quantity": quantity,
            "measurement": measurement,
            "explanation": explanation,
        }
        if outcome not in item.requirement.allowed_outcomes:
            raise CheckError(
                "INVALID_OUTCOME", "the outcome is not allowed for this item", path="outcome"
            )
        verified_quantity = verified_measurement = None
        if verification:
            # The stored row keeps the Pass's observation unless replaced; nothing is observed
            # when unable to inspect, which also needs a reason (UTI-1).
            if outcome == "UNABLE_TO_INSPECT":
                shape["explanation"] = explanation if explanation is not None else root.explanation
            elif quantity is None and measurement is None:
                shape["quantity"] = root.quantity
                if root.measurement_value is not None:
                    shape["measurement"] = (root.measurement_value, root.measurement_unit)
            _check_outcome(item, shape)
            _check_observation(item, shape)
            verified_quantity, verified_measurement = shape["quantity"], shape["measurement"]
        elif tip.outcome == "UNABLE_TO_INSPECT" or quantity is not None or measurement is not None:
            _check_observation(item, shape)
        # A Failed to Passed Correct-entry acts on the deficiencies the chain supports (COR-3,
        # COR-4), and a withdrawn verification Pass on the deficiency it verified (INV-4), under
        # their locks, which come after the item lock held above.
        effects = []
        if verification:
            effects = _planned(
                deficiency_effects.plan_verification_correction_effects, organization_id, root
            )
        elif tip.outcome == "FAILED":
            effects = _planned(deficiency_effects.plan_correction_effects, organization_id, root.pk)
        completions = _completion_effects(organization_id, root.pk)
        replacement_id, event_id = new_id(), new_id()
        replacement = CheckRecord(
            id=replacement_id,
            organization_id=organization_id,
            vehicle_id=root.vehicle_id,
            item_id=root.item_id,
            kit_id=root.kit_id,
        )
        try:
            links = choose_links(
                replacement,
                recorded_at=root.recorded_at,
                ignore_root_check_id=str(root.pk),
            )
        except LinkScopeTooLarge:
            raise CheckError(
                "CONFIGURATION_UNAVAILABLE",
                "obligation overlap exceeds the supported resolver scope",
                status=503,
            ) from None

        def apply(receipt, current):
            row = CheckRecord.objects.create(
                id=replacement_id,
                organization_id=organization_id,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current.user,
                actor_roles_at_time=list(current.roles),
                operation_id=operation_id,
                vehicle_id=root.vehicle_id,
                item_id=root.item_id,
                item_def_id=root.item_def_id,
                checklist_version_id=root.checklist_version_id,
                kit_id=root.kit_id,
                session=root.session,
                outcome=outcome,
                **(
                    _verification_observation(verified_quantity, verified_measurement)
                    if verification
                    else {
                        "quantity": quantity if quantity is not None else root.quantity,
                        "measurement_value": (
                            measurement[0] if measurement is not None else root.measurement_value
                        ),
                        "measurement_unit": (
                            measurement[1] if measurement is not None else root.measurement_unit
                        ),
                    }
                ),
                explanation=explanation if explanation is not None else root.explanation,
                client_observed_at=root.client_observed_at,
                purpose=root.purpose,
                verification_for_deficiency_id=root.verification_for_deficiency_id,
                verification_scope_revision=root.verification_scope_revision,
                root_check=root,
                predecessor_check=tip,
            )
            CheckEvent.objects.create(
                id=event_id,
                organization_id=organization_id,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current.user,
                actor_roles_at_time=list(current.roles),
                root_check=root,
                selected_check=tip,
                event_type="CORRECTION",
                replacement_check=row,
                explanation=explanation,
            )
            persist_links(row, receipt, current, links)
            completion_effects.append_check_effects(
                completions,
                receipt,
                current,
                root_id=root.pk,
                event_id=event_id,
                determination_type="SUPERSEDED",
            )
            for effect in effects:
                deficiency_effects.append_effect(
                    receipt,
                    current,
                    organization_id=organization_id,
                    effect=effect,
                    root_id=root.pk,
                )
            if root.session_id:
                rebuild_session_projection(root.session)
            from cya.live_updates.publish import publish_vehicle_update

            for effect in effects:
                publish_vehicle_update(
                    organization_id=organization_id,
                    vehicle_id=root.vehicle_id,
                    event_id=effect.event_id,
                    kind="deficiency.changed",
                )

            publish_vehicle_update(
                organization_id=organization_id,
                vehicle_id=root.vehicle_id,
                event_id=event_id,
                kind="check.corrected",
            )

        return Plan(
            response_body={
                "code": "CHECK_CORRECTED",
                "check_id": replacement_id,
                "root_check_id": root.pk,
                "supersedes_check_id": tip.pk,
                "outcome": outcome,
                "event_id": event_id,
                "event_ids": [event_id],
                "deficiency_event_ids": [e.event_id for e in effects],
                "deficiency_effects": [
                    {
                        "deficiency_id": e.deficiency_id,
                        "event_id": e.event_id,
                        "event_type": e.event_type,
                    }
                    for e in effects
                ],
                "obligation_links": response_links(links),
            },
            apply=apply,
            response_status=201,
            target_type="ORGANIZATION",
            target_id=organization_id,
            new_snapshot={
                "schema_version": 1,
                "values": {
                    "event_id": event_id,
                    "root_check_id": root.pk,
                    "selected_check_id": tip.pk,
                    "replacement_check_id": replacement_id,
                    "outcome": outcome,
                    "deficiency_event_ids": [e.event_id for e in effects],
                    "superseded_determination_ids": [str(e.prior.pk) for e in completions],
                },
            },
            recorded_at=now,
        )

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type=CORRECTION_COMMAND_TYPE,
        request_digest=digest(
            {
                "check_id": check_id,
                "expected_tip_check_id": expected_tip_check_id,
                "outcome": outcome,
                "quantity": quantity,
                "measurement": ((str(measurement[0]), measurement[1]) if measurement else None),
                "explanation": explanation,
            }
        ),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=_authorize(organization_id, actor_id),
        prepare=build,
    )


def _invalidation_authorizer(organization_id, actor_id, check_id):
    """Authorize with the author of the targeted chain loaded under the identity locks.

    Only administrators may invalidate (SPEC 3.3, INV-1); a refusal is audited as a denial. The
    author is read here, after the role check inputs are fixed, so the matrix's "other only"
    rule sees a trusted fact; an administrator naming their own chain gets a distinct error.
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
            target = (
                CheckRecord.objects.filter(organization_id=organization_id, pk=check_id)
                .values_list("actor_user_id", flat=True)
                .first()
            )
            author_id = str(canonical_id(target)) if target is not None else ""
            if Role.ADMINISTRATOR in roles and _self_invalidation_forbidden(
                actor_id=actor_id, author_id=author_id
            ):
                # A denial that carries the trusted actor, so it is audited as the administrator's
                # denied action (AUD-1, AUD-3) rather than as an anonymous failure.
                raise SelfInvalidationForbidden(actor)
            try:
                require_permission(
                    principal,
                    organization_id,
                    INVALIDATION_ACTION,
                    context=ResourceContext(author_id=author_id),
                )
            except PermissionDenied:
                raise CommandDenied(actor) from None
            return actor

    return authorize


def invalidate_chain(
    *,
    check_id,
    explanation,
    operation_id,
    organization_id,
    actor_id,
    correlation_id,
    request_attempt_id,
):
    """Append an administrator's INVALIDATION of a whole correction chain (SPEC 7.8, INV-1..INV-6).

    Any row of the chain resolves to its root. The chain is excluded from compliance and no
    earlier outcome revives (INV-2a); nothing is modified, the event is appended. Not undoable.
    """
    for value in (organization_id, actor_id):
        canonical_id(value)
    check_id = _id(check_id, "check_id")
    _operation_id(operation_id)
    if not isinstance(explanation, str) or not explanation.strip():
        raise CheckError(
            "EXPLANATION_REQUIRED", "an invalidation needs an explanation", path="explanation"
        )
    explanation = _explanation(explanation)

    def build(actor):
        target = CheckRecord.objects.filter(organization_id=organization_id, pk=check_id).first()
        if target is None:
            raise CheckError("NOT_FOUND", "check was not found", status=404, path="check_id")
        lock = (
            ItemLock.objects.select_for_update()
            .filter(
                organization_id=organization_id,
                vehicle_id=target.vehicle_id,
                item_id=target.item_id,
            )
            .first()
        )
        if lock is None:
            raise CheckError("CONFIGURATION_UNAVAILABLE", "item lock is unavailable", status=503)
        root = CheckRecord.objects.get(organization_id=organization_id, pk=target.root_check_id)
        if CheckEvent.objects.filter(root_check=root, event_type="INVALIDATION").exists():
            raise CheckError(
                "CHAIN_INVALIDATED", "the check chain is already invalidated", status=409
            )
        tip = _chain_tip(root)
        affected = sorted(
            str(obligation_id)
            for obligation_id in CheckObligationLink.objects.filter(check_record=tip).values_list(
                "obligation_id", flat=True
            )
        )
        event_id = new_id()
        # INV-3, INV-7, INV-4: nothing is closed or cancelled; a deficiency this chain's correction
        # reported in error, or whose automatic closure this verification chain supported, is
        # reopened in this transaction.
        effects = _planned(deficiency_effects.plan_invalidation_effects, organization_id, root)
        completions = _completion_effects(organization_id, root.pk)

        def apply(receipt, current):
            CheckEvent.objects.create(
                id=event_id,
                organization_id=organization_id,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current.user,
                actor_roles_at_time=list(current.roles),
                root_check=root,
                selected_check=tip,
                event_type="INVALIDATION",
                replacement_check=None,
                explanation=explanation,
            )
            completion_effects.append_check_effects(
                completions,
                receipt,
                current,
                root_id=root.pk,
                event_id=event_id,
                determination_type="INVALIDATED",
            )
            for effect in effects:
                deficiency_effects.append_effect(
                    receipt,
                    current,
                    organization_id=organization_id,
                    effect=effect,
                    root_id=root.pk,
                )
            from cya.live_updates.publish import publish_vehicle_update

            publish_vehicle_update(
                organization_id=organization_id,
                vehicle_id=root.vehicle_id,
                event_id=event_id,
                kind="check.invalidated",
            )
            for effect in effects:
                publish_vehicle_update(
                    organization_id=organization_id,
                    vehicle_id=root.vehicle_id,
                    event_id=effect.event_id,
                    kind="deficiency.changed",
                )

        # No explanation text in the audit snapshot: it is free text from the administrator.
        return Plan(
            response_body={
                "code": "CHECK_CHAIN_INVALIDATED",
                "root_check_id": root.pk,
                "invalidation_event_id": event_id,
                "affected_obligation_ids": affected,
                "voided_determination_ids": [str(e.prior.pk) for e in completions],
                "reopened_deficiency_ids": [e.deficiency_id for e in effects],
            },
            apply=apply,
            response_status=201,
            target_type="ORGANIZATION",
            target_id=organization_id,
            new_snapshot={
                "schema_version": 1,
                "values": {
                    "event_id": event_id,
                    "root_check_id": root.pk,
                    "selected_check_id": tip.pk,
                    "affected_obligation_ids": affected,
                    "reopened_deficiency_ids": [e.deficiency_id for e in effects],
                    "voided_determination_ids": [str(e.prior.pk) for e in completions],
                },
            },
        )

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type=INVALIDATION_COMMAND_TYPE,
        request_digest=digest({"check_id": check_id, "explanation": explanation}),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=_invalidation_authorizer(organization_id, actor_id, check_id),
        prepare=build,
    )
