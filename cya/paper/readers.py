"""Read-only paper-entry facts for the crew screens (SPEC 8.0.0 REC-5, LATE-1 to LATE-8; issue #9 B6).

Nothing here writes. The pages that call these readers authorize the signed-in user first; the
write endpoints authorize every record again (SPEC 3.2).

- own_batch: a batch the user entered (only its entry actor adds records to it, Part B2).
- batch_summary: what the batch header says, for the paper screen's banner.
- batch_effective_checks: the batch's effective (not invalidated) check per checklist row key.
- version_choices: the published versions of a vehicle checklist and of the kit definitions
  each places, for the setup page's "printed version" choices.
- vehicle_batches: a vehicle's recent batches, newest entry first.
- check_paper_sources: performers and entry actor of the paper checks among some checks.
"""

from cya.authorization.backend import canonical_id
from cya.checklists.models import ChecklistPublication, ChecklistTemplate
from cya.checks.models import CheckRecord
from cya.kits.models import ChecklistKitPlacement, KitDefinitionPublication
from cya.odometer.models import EngineHoursEntry, OdometerEntry
from cya.paper.models import (
    PaperEntryBatch,
    PaperEntryInvalidationEvent,
    PaperEntryKitVersion,
    PaperEntryPerformer,
)
from cya.seals.models import SealEvent, SealSubstitution

MAX_VERSIONS = 10  # checklist versions the setup page offers
MAX_KIT_VERSIONS = 10  # kit-definition versions offered per placed definition


def own_batch(user, batch_id, vehicle_id=None):
    """The user's own batch with this id (and vehicle), or None.

    batch_id must already be canonical; any other user's batch is None, so a page or API
    answers it like one that does not exist (as the check command does, Part B2).
    """
    found = PaperEntryBatch.objects.select_related(
        "vehicle", "checklist_version__template", "actor_user"
    ).filter(organization_id=user.organization_id, pk=batch_id, actor_user_id=user.pk)
    if vehicle_id is not None:
        found = found.filter(vehicle_id=vehicle_id)
    return found.first()


def parse_batch_id(value):
    """The canonical batch id, or None when value is not an identifier."""
    try:
        return canonical_id(value)
    except (ValueError, TypeError, AttributeError):
        return None


def is_invalidated(batch):
    return PaperEntryInvalidationEvent.objects.filter(batch=batch).exists()


def has_records(batch):
    """Whether any check, reading or seal record names the batch (LATE-6)."""
    return any(
        model.objects.filter(organization_id=batch.organization_id, paper_entry_batch=batch)
        .only("pk")
        .exists()
        for model in (CheckRecord, OdometerEntry, EngineHoursEntry, SealEvent, SealSubstitution)
    )


def batch_summary(batch):
    """The header facts the paper screen shows (LATE-2 to LATE-4), as plain values."""
    performers = [
        {
            "user_id": str(row.performer_id),
            "display_name": row.performer.display_name,
            "active": row.performer.active,
        }
        for row in PaperEntryPerformer.objects.filter(batch=batch)
        .select_related("performer")
        .order_by("performer__display_name", "performer_id")
    ]
    kits = [
        {
            "version_id": str(row.kit_definition_version_id),
            "definition_id": str(row.kit_definition_version.definition_id),
            "label": row.kit_definition_version.definition.label,
            "revision": row.kit_definition_version.revision,
        }
        for row in PaperEntryKitVersion.objects.filter(batch=batch)
        .select_related("kit_definition_version__definition")
        .order_by("kit_definition_version__definition__label", "kit_definition_version_id")
    ]
    printed = {kit["definition_id"] for kit in kits}
    unprinted = [
        placement.definition.label
        for placement in ChecklistKitPlacement.objects.filter(
            checklist_version_id=batch.checklist_version_id
        )
        .select_related("definition")
        .order_by("display_order", "pk")
        if str(placement.definition_id) not in printed
    ]
    version = batch.checklist_version
    return {
        "batch_id": str(batch.pk),
        "vehicle_id": str(batch.vehicle_id),
        "performed_at": batch.performed_at,
        "performed_local_date": batch.performed_local_date,
        "performed_local_time": batch.performed_local_time,
        "performed_zone": batch.performed_zone,
        "performed_resolution": batch.performed_resolution,
        "recorded_at": batch.recorded_at,
        "checklist_version_id": str(version.pk),
        "template_id": str(version.template_id),
        "template_label": version.template.label,
        "revision": version.revision,
        "performers": performers,
        "kit_versions": kits,
        "unprinted_kit_labels": unprinted,
        "invalidated": is_invalidated(batch),
        "has_records": has_records(batch),
    }


def batch_effective_checks(organization_id, batch_id, as_of):
    """{(item id, unit key): (check id, outcome)}: the batch's effective check per row key.

    Each chain is replayed as the attention reader does (cya.attention.facts), so a correction's
    replacement is the chain's check and an invalidated chain (an Administrator's, or the batch's
    own invalidation, INV-8) is left out. Several chains of one key keep the newest by
    performance time, then server time, then id (SPEC 19.4); a batch has one performance time.
    """
    from cya.attention.facts import _effective_by_root
    from cya.kits.presence import line_unit_keys
    from cya.obligations.projections import check_unit_key

    roots = sorted(
        {
            str(root)
            for root in CheckRecord.objects.filter(
                organization_id=organization_id,
                paper_entry_batch_id=batch_id,
                recorded_at__lte=as_of,
            ).values_list("root_check_id", flat=True)
        }
    )
    if not roots:
        return {}
    effective = _effective_by_root(organization_id, roots, as_of)
    rows = [row for _decision, row in effective.values()]
    units = line_unit_keys({r.kit_requirement_id for r in rows if r.kit_requirement_id})
    found = {}
    for row in rows:
        key = (str(row.item_id), check_unit_key(row, units))
        order = (row.performed_at or row.recorded_at, row.recorded_at, str(row.pk))
        if key not in found or order > found[key][0]:
            found[key] = (order, str(row.pk), row.outcome)
    return {key: (check_id, outcome) for key, (_order, check_id, outcome) in found.items()}


def _current(publications, now):
    """The publication in force at now among publications ordered newest effective first."""
    return next((p for p in publications if p.effective_at <= now), None)


def version_choices(organization_id, vehicle_id, template_id, now):
    """The printed-version choices for one of the vehicle's own checklists, or None.

    Only a checklist of this vehicle itself can be named on a batch (VERSION_NOT_FOR_VEHICLE,
    Part B1), so another template answers None. At most MAX_VERSIONS published versions, newest
    effective first; for each, the kit definitions it places, each with at most MAX_KIT_VERSIONS
    published versions. The one in force now is marked current: it is the default, since most
    sheets in the rig were printed from it. The command judges the choice at the paper time.
    """
    template = ChecklistTemplate.objects.filter(
        organization_id=organization_id, pk=template_id, vehicle_id=vehicle_id, active=True
    ).first()
    if template is None:
        return None
    publications = list(
        ChecklistPublication.objects.filter(
            organization_id=organization_id,
            checklist_version__template_id=template.pk,
            recorded_at__lte=now,
        )
        .select_related("checklist_version")
        .order_by("-effective_at", "-recorded_at", "-pk")[:MAX_VERSIONS]
    )
    current = _current(publications, now)
    placements = {}
    for placement in (
        ChecklistKitPlacement.objects.filter(
            checklist_version_id__in=[p.checklist_version_id for p in publications]
        )
        .select_related("definition")
        .order_by("display_order", "pk")
    ):
        placements.setdefault(str(placement.checklist_version_id), []).append(placement)
    kit_choices = {}
    for definition_id in {str(p.definition_id) for rows in placements.values() for p in rows}:
        kit_publications = list(
            KitDefinitionPublication.objects.filter(
                organization_id=organization_id,
                version__definition_id=definition_id,
                recorded_at__lte=now,
            )
            .select_related("version")
            .order_by("-effective_at", "-recorded_at", "-pk")[:MAX_KIT_VERSIONS]
        )
        kit_current = _current(kit_publications, now)
        kit_choices[definition_id] = [
            {
                "version_id": str(p.version_id),
                "revision": p.version.revision,
                "effective_at": p.effective_at,
                "current": p is kit_current,
            }
            for p in kit_publications
        ]
    return {
        "template_id": str(template.pk),
        "template_label": template.label,
        "versions": [
            {
                "version_id": str(p.checklist_version_id),
                "revision": p.checklist_version.revision,
                "effective_at": p.effective_at,
                "current": p is current,
                "kits": [
                    {
                        "definition_id": str(placement.definition_id),
                        "label": placement.definition.label,
                        "versions": kit_choices.get(str(placement.definition_id), []),
                    }
                    for placement in placements.get(str(p.checklist_version_id), [])
                ],
            }
            for p in publications
        ],
    }


def vehicle_batches(organization_id, vehicle_id, since, limit=20, actor_id=None, valid_only=False):
    """The vehicle's batches entered at or after since, newest entry first, at most limit.

    actor_id keeps only that entry actor's batches; valid_only leaves out invalidated ones.
    """
    found = PaperEntryBatch.objects.select_related("checklist_version__template").filter(
        organization_id=organization_id, vehicle_id=vehicle_id, recorded_at__gte=since
    )
    if actor_id is not None:
        found = found.filter(actor_user_id=actor_id)
    if valid_only:
        found = found.exclude(
            pk__in=PaperEntryInvalidationEvent.objects.filter(
                organization_id=organization_id
            ).values("batch_id")
        )
    return list(found.order_by("-recorded_at", "-pk")[:limit])


def check_paper_sources(organization_id, check_ids):
    """{check id: {performers, entered_by, entered_at}} for the paper checks among check_ids.

    performers are display names in name order (LATE-3: credited separately from the entry
    actor); live checks are absent. Two bulk reads, whatever the number of checks.
    """
    ids = sorted({str(i) for i in check_ids if i})
    if not ids:
        return {}
    rows = list(
        CheckRecord.objects.filter(
            organization_id=organization_id, pk__in=ids, paper_entry_batch__isnull=False
        ).values_list("pk", "paper_entry_batch_id", "recorded_at", "actor_user__display_name")
    )
    if not rows:
        return {}
    names = {}
    for batch_id, name in (
        PaperEntryPerformer.objects.filter(batch_id__in={row[1] for row in rows})
        .order_by("performer__display_name", "performer_id")
        .values_list("batch_id", "performer__display_name")
    ):
        names.setdefault(str(batch_id), []).append(name)
    return {
        str(pk): {
            "performers": names.get(str(batch_id), []),
            "entered_by": entered_by,
            "entered_at": recorded_at,
        }
        for pk, batch_id, recorded_at, entered_by in rows
    }
