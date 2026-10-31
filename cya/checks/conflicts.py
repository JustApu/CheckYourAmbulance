"""The conflict warning for concurrent checks (SPEC 7.4, CON-1..CON-5; issue 75 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/75)).

Crew members check different items freely (CON-1). When a crew member is about to record an
item that someone else has recorded since the screen they were looking at, the server refuses
that attempt with a warning that names the newer check (CON-3). Proceeding is a new decision
with a new operation id and acknowledges_check_id; if yet another check landed meanwhile the
server warns again and commits nothing, so a Fail is never dropped because a Pass raced it
(CON-4). Both records are kept when the crew member proceeds (CON-2).

The comparison and the insert are atomic because record_check holds the ItemLock row for the
vehicle/item (CON-5) and reads its instant only after taking it; this module runs under that lock.
"""

from django.db.models import Exists, OuterRef

from cya.checks.models import CheckEvent, CheckObligationLink, CheckRecord


class CheckConflict(ValueError):
    """A newer check by someone else exists; nothing is committed and no receipt is written.

    Carries only fields every signed-in crew member may already see (contracts: Record a check).
    """

    def __init__(self, newest, obligation):
        super().__init__("a newer check exists for this item")
        self.code, self.status = "CHECK_CONFLICT", 409
        self.newest = {
            "check_id": str(newest.pk),
            "actor": {
                "user_id": str(newest.actor_user_id),
                "display_name": newest.actor_user.display_name,
            },
            "server_recorded_at": newest.recorded_at,
            "outcome": newest.outcome,
        }
        self.obligation_id = str(obligation.pk) if obligation else None


def newest_effective_check(check, obligation):
    """The newest effective check in the comparison scope, or None.

    Scope (contracts: Record a check). With an obligation: every check of the item (the kit
    instance, for a kit item) recorded since that obligation's window opened, whether or not it
    was linked to it; a repeat check is kept but linked elsewhere once the obligation is
    satisfied, so linkage alone would hide it from the next crew member. With no obligation: the
    item's unlinked scope, meaning checks that did not satisfy an on-time window. Every check is
    effective except those of an invalidated chain (issue 77 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/77)); corrections add rows that are
    always newer than the row they supersede.
    """
    rows = CheckRecord.objects.filter(
        organization_id=check.organization_id,
        vehicle_id=check.vehicle_id,
        item_id=check.item_id,
    )
    rows = rows.filter(kit_id=check.kit_id) if check.kit_id else rows.filter(kit_id__isnull=True)
    # A chain an administrator invalidated is not effective (INV-2a); a superseded row is always
    # older than its chain's tip, so it can never be the newest effective check on its own.
    rows = rows.exclude(
        Exists(
            CheckEvent.objects.filter(
                root_check_id=OuterRef("root_check_id"), event_type="INVALIDATION"
            )
        )
    )
    if obligation is not None:
        rows = rows.filter(recorded_at__gte=obligation.early_at)
    else:
        on_time = CheckObligationLink.objects.filter(
            check_record=OuterRef("pk"), obligation__grace_at__gte=OuterRef("recorded_at")
        )
        rows = rows.filter(~Exists(on_time))
    return rows.select_related("actor_user").order_by("-recorded_at", "-id").first()


def conflicting_check(check, obligation, *, actor_id, basis_check_id, acknowledges_check_id):
    """The check to warn about, or None when the attempt may commit.

    No warning when nothing newer exists, when the newest check is the actor's own, or when the
    client's basis or acknowledgement is that very check (the viewer saw it, or already chose to
    proceed past it).
    """
    newest = newest_effective_check(check, obligation)
    if newest is None or str(newest.actor_user_id) == str(actor_id):
        return None
    if str(newest.pk) in (basis_check_id, acknowledges_check_id):
        return None
    return newest
