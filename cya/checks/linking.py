"""Choose and record links from a check to selected, unsatisfied obligations (SPEC §5.3–§5.5).

SPEC 6.0.0 (SCH-2, SCH-5, SCH-6; issue #256 Part B): a check links to every unsatisfied
obligation of its item for which it is on time or late within that obligation's L, whichever
checklist created it. Shared-satisfaction rules no longer create links; links recorded under them
stay in history. Seal evidence keeps its earlier selection (choose_seal_links), which SPEC 6.0.0
does not change.
"""

import json
from datetime import timedelta

from django.conf import settings
from django.db.models import Exists, F, OuterRef, Q, Subquery

from cya.accounts.fields import new_id
from cya.checks.models import CheckEvent, CheckObligationLink
from cya.kits.presence import line_unit_keys
from cya.obligations.models import (
    Obligation,
    ObligationSelection,
    SharedSatisfactionRule,
    SharedSatisfactionRuleMember,
)

MAX_LINK_CANDIDATES = 10_000


def late_window():
    """The current w_late setting (SPEC SCH-3).

    It applies only to obligations created from now on: each obligation captures it as
    late_seconds when created, and its late limit L never moves (SCH-2, issue #80).
    """
    return timedelta(hours=settings.CYA_LATE_COMPLETION_HOURS)


def captured_late_seconds():
    """The current w_late in whole seconds, as a creator stores it on a new obligation."""
    return int(late_window().total_seconds())


class LinkScopeTooLarge(ValueError):
    """A single check has more eligible obligations than the bounded resolver permits."""


def _eligible(
    *,
    organization_id,
    vehicle_id,
    item_id,
    kit_key,
    recorded_at,
    late,
    include_satisfied=False,
    ignore_root_check_id=None,
    kit_requirement_id=None,
):
    selected = (
        ObligationSelection.objects.filter(
            organization_id=OuterRef("organization_id"),
            vehicle_id=OuterRef("vehicle_id"),
            item_id=OuterRef("item_id"),
            kit_key=OuterRef("kit_key"),
            due_at=OuterRef("due_at"),
            recorded_at__lte=recorded_at,
        )
        .order_by("-sequence")
        .values("selected_obligation_id")[:1]
    )
    already_satisfied = CheckObligationLink.objects.filter(
        organization_id=OuterRef("organization_id"),
        obligation_id=OuterRef("pk"),
        check_record__recorded_at__lte=recorded_at,
        check_record__outcome__in=("PASSED", "FAILED"),
    ).exclude(
        # An invalidated chain no longer satisfies anything (INV-2): a fresh check may take over.
        Exists(
            CheckEvent.objects.filter(
                root_check_id=OuterRef("check_record__root_check_id"),
                event_type="INVALIDATION",
                recorded_at__lte=recorded_at,
            )
        )
    )
    if ignore_root_check_id is not None:
        already_satisfied = already_satisfied.exclude(
            check_record__root_check_id=ignore_root_check_id
        )
    candidates = Obligation.objects.filter(
        organization_id=organization_id,
        vehicle_id=vehicle_id,
        item_id=item_id,
        kit_key=kit_key,
        recorded_at__lte=recorded_at,
    ).annotate(selected_id=Subquery(selected))
    if kit_requirement_id is not None:
        candidates = candidates.filter(requirement_version__kit_requirement_id=kit_requirement_id)
    if late:
        # SCH-2: only up to the obligation's pinned late limit L (inclusive); older ones stay
        # missed. The range on late_limit_at keeps the read off the item's settled history
        # (issue #61), and grace_at < t keeps a late completion apart from an on-time one.
        candidates = candidates.filter(grace_at__lt=recorded_at, late_limit_at__gte=recorded_at)
    else:
        candidates = candidates.filter(early_at__lte=recorded_at, grace_at__gte=recorded_at)
    candidates = candidates.filter(Q(routine=False) | Q(pk=F("selected_id")))
    if not include_satisfied:
        # A seal substitution is separate evidence but has already satisfied the
        # linked obligation just as a qualifying check has.
        from cya.seals.models import SealInspectionObligation, SealSubstitutionObligation

        already_substituted = SealSubstitutionObligation.objects.filter(
            organization_id=OuterRef("organization_id"),
            obligation_id=OuterRef("pk"),
            recorded_at__lte=recorded_at,
        )
        already_inspected = SealInspectionObligation.objects.filter(
            organization_id=OuterRef("organization_id"),
            obligation_id=OuterRef("pk"),
            recorded_at__lte=recorded_at,
            inspection_item__outcome__in=("PASSED", "FAILED"),
        )
        candidates = candidates.filter(
            ~Exists(already_satisfied), ~Exists(already_substituted), ~Exists(already_inspected)
        )
    return list(
        candidates.select_related(
            "requirement_version__item_def__compartment",
            "requirement_version__item_def__system",
            "requirement_version__kit_definition_item",
            "requirement_version__placement",
        ).order_by("due_at", "pk")[: MAX_LINK_CANDIDATES + 1]
    )


def _current_rule(organization_id, recorded_at):
    return (
        SharedSatisfactionRule.objects.filter(
            organization_id=organization_id,
            recorded_at__lte=recorded_at,
            effective_at__lte=recorded_at,
        )
        .order_by("-effective_at", "-revision", "-id")
        .first()
    )


def _scope(check, recorded_at):
    """The obligation stream a check can satisfy: its item and its unit key.

    The unit key is the check's kit, or for a kit presence check its line's unit key, which
    obligations of that line carry as kit_key (SPEC 2.0.0 KDEF-12). A presence check is then
    eligible only for obligations whose requirement version pins that same line.
    """
    kit_requirement = getattr(check, "kit_requirement_id", None)
    if check.kit_id:
        kit_key = str(check.kit_id)
    elif kit_requirement:
        kit_key = line_unit_keys([kit_requirement])[str(kit_requirement)]
    else:
        kit_key = "00000000-0000-0000-0000-000000000000"
    return {
        "organization_id": str(check.organization_id),
        "vehicle_id": str(check.vehicle_id),
        "item_id": str(check.item_id),
        "kit_key": kit_key,
        "kit_requirement_id": str(kit_requirement) if kit_requirement else None,
        "recorded_at": recorded_at,
    }


def conflict_obligation(check, *, recorded_at):
    """Of the obligations whose on-time window holds the instant, the one opened first, or None.

    This anchors the conflict warning (SPEC CON-3). A second crew member about to repeat a check
    someone just recorded is exactly the case to warn about, even though linking sends the repeat
    elsewhere because the obligation is already satisfied. The comparison covers every obligation
    of the item from any checklist (CON-7), so the anchor is the window that opened first and
    every check since then is in scope. The caller holds the vehicle/item lock.
    """
    on_time = _eligible(**_scope(check, recorded_at), late=False, include_satisfied=True)
    return min(on_time, key=lambda o: (o.early_at, o.due_at, o.pk)) if on_time else None


def _outcomes(value):
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(sorted(value))
    return json.dumps(value, sort_keys=True)


def _requirement_key(version):
    """What a check of this requirement checks, and where (interim guard, issue #256 Part B).

    Until vehicle item requirements exist (SPEC 6.0.0 CHK-11, Part C), one check is shared only
    between obligations whose pinned requirement versions check the item the same way in the
    same place. A kit item's place is its kit instance, and a kit presence check's place is its
    kit list line; both are already part of the link scope (KDEF-12).
    """
    if version.kit_requirement_id is not None:
        return ("PRESENCE", str(version.kit_requirement_id))
    content = version.item_def or version.kit_definition_item
    properties = (
        content.kind,
        content.expected_quantity,
        content.unit,
        _outcomes(content.allowed_outcomes),
        bool(content.expiration_tracked),
    )
    if version.item_def is None:
        return ("KIT", properties)
    item_def = version.item_def
    place = (
        str(item_def.compartment.stable_slot_id) if item_def.compartment_id else None,
        str(item_def.system.stable_slot_id) if item_def.system_id else None,
        str(item_def.kit_id) if item_def.kit_id else None,
    )
    return ("ITEM", properties, place)


def _source_checklist_version(version):
    if version.item_def is not None:
        return version.item_def.checklist_version_id
    if version.placement is not None:
        return version.placement.checklist_version_id
    return None


def _anchor(check, candidates):
    """The obligation of the checklist the check was recorded from, else the first candidate."""
    own = getattr(check, "checklist_version_id", None)
    if own is not None:
        for obligation in candidates:
            source = _source_checklist_version(obligation.requirement_version)
            if source is not None and str(source) == str(own):
                return obligation
    return candidates[0]


def choose_links(check, *, recorded_at, ignore_root_check_id=None):
    """Return a link to every eligible obligation, on-time ones first (SPEC 6.0.0 SCH-6).

    The caller holds the vehicle/item lock and supplies the exact timestamp that will be stored
    on the receipt and check. Only the latest selected obligation for each due instant is
    considered, and satisfied obligations are skipped. Every unsatisfied obligation whose on-time
    window holds the instant, and every one past its grace deadline but not past its late limit
    L, is linked, whichever checklist created it. On-time links come first, then late ones, each
    by due instant and stable obligation ID; the first is the primary link (ordinal 0). The
    interim guard (_requirement_key) keeps only obligations of the same requirement and place as
    the checklist the check was recorded from.
    """
    scope = _scope(check, recorded_at)
    scope["ignore_root_check_id"] = ignore_root_check_id
    on_time = _eligible(**scope, late=False)
    late = _eligible(**scope, late=True)
    if len(on_time) + len(late) > MAX_LINK_CANDIDATES:
        raise LinkScopeTooLarge("too many eligible obligations")
    candidates = [(o, "ON_TIME") for o in on_time] + [(o, "LATE") for o in late]
    if not candidates:
        return []
    anchor = _anchor(check, [o for o, _ in candidates])
    key = _requirement_key(anchor.requirement_version)
    chosen = [(o, t) for o, t in candidates if _requirement_key(o.requirement_version) == key]
    return [
        {"id": new_id(), "obligation": o, "ordinal": n, "shared_rule": None, "timing": t}
        for n, (o, t) in enumerate(chosen)
    ]


def choose_seal_links(check, *, recorded_at, ignore_root_check_id=None):
    """Return primary and explicitly shared links for seal evidence, as before SPEC 6.0.0.

    The caller holds the vehicle/item lock and supplies the exact timestamp that will be stored
    on the receipt and check. Only the latest selected obligation for each due instant is
    considered. Completed obligations are skipped; on-time windows always take precedence over
    late completion, then due instant and stable obligation ID decide the primary link.
    """
    scope = _scope(check, recorded_at)
    scope["ignore_root_check_id"] = ignore_root_check_id
    on_time = _eligible(**scope, late=False)
    if len(on_time) > MAX_LINK_CANDIDATES:
        raise LinkScopeTooLarge("too many overlapping eligible obligations")
    timing = "ON_TIME"
    candidates = on_time
    if not candidates:
        candidates = _eligible(**scope, late=True)
        if len(candidates) > MAX_LINK_CANDIDATES:
            raise LinkScopeTooLarge("too many eligible late obligations")
        timing = "LATE"
    if not candidates:
        return []

    primary = candidates[0]
    links = [
        {
            "id": new_id(),
            "obligation": primary,
            "ordinal": 0,
            "shared_rule": None,
            "timing": timing,
        }
    ]
    rule = _current_rule(str(check.organization_id), recorded_at)
    if rule is None:
        return links

    members = SharedSatisfactionRuleMember.objects.filter(
        organization_id=check.organization_id,
        rule=rule,
        source_requirement_version_id=primary.requirement_version_id,
        additional_requirement_version_id__in=[o.requirement_version_id for o in candidates[1:]],
    )
    allowed = set(members.values_list("additional_requirement_version_id", flat=True))
    for obligation in candidates[1:]:
        if obligation.requirement_version_id not in allowed:
            continue
        ordinal = len(links)
        links.append(
            {
                "id": new_id(),
                "obligation": obligation,
                "ordinal": ordinal,
                "shared_rule": rule,
                "timing": timing,
            }
        )
    return links


def persist_links(check, receipt, actor, links):
    """Append the prevalidated links with the same receipt and attribution as their check."""
    CheckObligationLink.objects.bulk_create(
        CheckObligationLink(
            id=link["id"],
            organization_id=check.organization_id,
            check_record=check,
            obligation=link["obligation"],
            ordinal=link["ordinal"],
            shared_rule=link["shared_rule"],
            command=receipt,
            recorded_at=receipt.recorded_at,
            actor_user=actor.user,
            actor_roles_at_time=list(actor.roles),
        )
        for link in links
    )


def response_links(links):
    return [
        {
            "obligation_id": str(link["obligation"].pk),
            "shared_rule_id": str(link["shared_rule"].pk) if link["shared_rule"] else None,
            "timing": link["timing"],
        }
        for link in links
    ]
