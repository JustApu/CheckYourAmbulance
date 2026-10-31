"""Choose and record links from a check to selected, unsatisfied obligations (SPEC §5.3–§5.5)."""

from django.db.models import Exists, F, OuterRef, Q, Subquery

from cya.accounts.fields import new_id
from cya.checks.models import CheckEvent, CheckObligationLink
from cya.obligations.models import (
    Obligation,
    ObligationSelection,
    SharedSatisfactionRule,
    SharedSatisfactionRuleMember,
)

MAX_LINK_CANDIDATES = 10_000


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
    if late:
        candidates = candidates.filter(grace_at__lt=recorded_at)
    else:
        candidates = candidates.filter(early_at__lte=recorded_at, grace_at__gte=recorded_at)
    candidates = candidates.filter(Q(routine=False) | Q(pk=F("selected_id")))
    if not include_satisfied:
        candidates = candidates.filter(~Exists(already_satisfied))
    return list(
        candidates.select_related("requirement_version").order_by("due_at", "pk")[
            : MAX_LINK_CANDIDATES + 1
        ]
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
    kit_key = str(check.kit_id) if check.kit_id else "00000000-0000-0000-0000-000000000000"
    return {
        "organization_id": str(check.organization_id),
        "vehicle_id": str(check.vehicle_id),
        "item_id": str(check.item_id),
        "kit_key": kit_key,
        "recorded_at": recorded_at,
    }


def conflict_obligation(check, *, recorded_at):
    """The obligation whose on-time window holds the instant, satisfied or not, or None.

    This anchors the conflict warning (SPEC CON-3). A second crew member about to repeat a check
    someone just recorded is exactly the case to warn about, even though linking sends the repeat
    elsewhere because the obligation is already satisfied. The caller holds the vehicle/item lock.
    """
    on_time = _eligible(**_scope(check, recorded_at), late=False, include_satisfied=True)
    return on_time[0] if on_time else None


def choose_links(check, *, recorded_at, ignore_root_check_id=None):
    """Return primary and explicitly shared links in deterministic order.

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
