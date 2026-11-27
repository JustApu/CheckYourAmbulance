"""Revalidate stored seal certifications against the locked current vehicle scope."""

from cya.kits.seal_rules import SealVerificationFacts, verification_refusal
from cya.kits.sealable import kit_sealable
from cya.seals.certification import certification_refusal, stored_certification
from cya.seals.models import SealSubstitution
from cya.seals.scope import resolve_kit_lines, resolve_kit_presence, resolve_scope


def _eligible_seal(seal, pending, *, marking, today, open_deficiency, suspended):
    if seal is None:
        return "SEAL_OPEN"
    return verification_refusal(
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
        attested=True,
        today=today,
    )


def current_certification(
    organization_id,
    vehicle_id,
    seal,
    scope,
    assignments,
    *,
    at,
    today,
    open_deficiency,
    suspended,
    events_at=None,
):
    """Return a refusal code when a recorded seal no longer covers its current contents.

    The caller holds the vehicle, kit, configuration, item and deficiency locks. This also
    rechecks nested kit seals against their own current event and certification; recording a
    prior nested verification cannot hide a subsequent break, audit or configuration change.
    events_at judges each nested kit seal by its events up to that instant: a paper
    verification's performance instant (SPEC 8.0.0 LATE-7).
    """
    from cya.seals.commands import _event_state, _events, _events_at

    saved = stored_certification(organization_id, seal)
    nested_ids = []
    for kit_id, substitution_id in saved.nested:
        if seal.kit_id is not None or kit_id not in assignments:
            return "KIT_SCOPE_CHANGED"
        substitution = (
            SealSubstitution.objects.filter(
                organization_id=organization_id,
                pk=substitution_id,
                vehicle_id=vehicle_id,
                seal_event__kit_id=kit_id,
            )
            .select_related("seal_event")
            .first()
        )
        if substitution is None or kit_id not in kit_sealable(organization_id, [kit_id], at):
            return "NESTED_SEAL_INVALID"
        nested_events = _events(organization_id, vehicle_id, kit_id, None)
        if events_at is not None:
            nested_events = _events_at(nested_events, events_at)
        nested_seal, pending = _event_state(nested_events)
        if nested_seal is None or nested_seal.pk != substitution.seal_event_id:
            return "NESTED_SEAL_INVALID"
        refusal = _eligible_seal(
            nested_seal,
            pending,
            marking=substitution.entered_marking,
            today=today,
            open_deficiency=open_deficiency,
            suspended=suspended,
        )
        if refusal:
            return "NESTED_SEAL_INVALID"
        nested_scope, nested_assignments = resolve_scope(
            organization_id, vehicle_id, at, kit_id=kit_id
        )
        nested_saved = stored_certification(organization_id, nested_seal)
        if nested_saved.nested or certification_refusal(
            nested_scope,
            nested_saved.items,
            (),
            nested_saved.lines,
            current_presence=resolve_kit_presence(
                organization_id, vehicle_id, nested_assignments, at
            ),
            certified_presence=nested_saved.presence,
        ):
            return "NESTED_SEAL_INVALID"
        nested_ids.append(kit_id)
    lines = (
        resolve_kit_lines(
            organization_id, vehicle_id, str(seal.compartment_slot_id), at, assignments
        )
        if seal.kit_id is None
        else ()
    )
    return certification_refusal(
        scope,
        saved.items,
        lines,
        saved.lines,
        current_presence=resolve_kit_presence(organization_id, vehicle_id, assignments, at),
        certified_presence=saved.presence,
        nested_kit_ids=nested_ids,
    )
