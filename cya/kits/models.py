"""Append-only kit assignment history (SPEC 4.2, TIME-1a, 18; OQ-54).

kit_assignment_event records where a kit instance was and whether it was active, one event at
a time per kit. It is the source for every as-of question about which kit instances a vehicle
had (obligation generation reads it, never the mutable kit_instance row), so the answer for a
past instant cannot change. kit_instance's vehicle, compartment and active flag are only a
projection of the latest event, updated by the same command (cya.kits.commands).

An assignment takes effect at the instant it is recorded and is never backdated (migration 0002
refuses effective_at before recorded_at). Writers go through cya.kits.commands; the reader is
cya.kits.history. Full movement workflows (transfers between stations) are R2 (kit_movement).
"""

from django.db import models

from cya.accounts.fields import IdentityIDField, new_id
from cya.checklists.models import Compartment, KitInstance, Vehicle, organization_fk
from cya.operations.models import Evidence


class KitAssignmentEvent(Evidence):
    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    kit = models.ForeignKey(KitInstance, on_delete=models.PROTECT, related_name="+")
    sequence = models.PositiveBigIntegerField()
    prior_event = models.OneToOneField(
        "self", null=True, on_delete=models.PROTECT, related_name="+"
    )
    # Where the kit is while active; both null once it is deactivated (not on any vehicle).
    vehicle = models.ForeignKey(Vehicle, null=True, on_delete=models.PROTECT, related_name="+")
    compartment = models.ForeignKey(
        Compartment, null=True, on_delete=models.PROTECT, related_name="+"
    )
    active = models.BooleanField()
    effective_at = models.DateTimeField()

    class Meta:
        db_table = "kit_assignment_event"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="kitassign_org_id"),
            models.UniqueConstraint(fields=["kit", "sequence"], name="kitassign_sequence"),
            models.CheckConstraint(condition=models.Q(sequence__gte=1), name="kitassign_seq_min"),
            models.CheckConstraint(
                condition=models.Q(active__in=[True, False]), name="kitassign_active_bool"
            ),
            models.CheckConstraint(
                condition=models.Q(prior_event__isnull=False) | models.Q(sequence=1),
                name="kitassign_first",
            ),
            # Never backdated: an assignment takes effect when recorded, or later.
            models.CheckConstraint(
                condition=models.Q(effective_at__gte=models.F("recorded_at")),
                name="kitassign_not_retroactive",
            ),
            # An active kit is in a compartment slot of a vehicle; a compartment implies a vehicle.
            models.CheckConstraint(
                condition=(
                    models.Q(active=True, vehicle__isnull=False, compartment__isnull=False)
                    | models.Q(active=False, vehicle__isnull=True, compartment__isnull=True)
                ),
                name="kitassign_placement",
            ),
        ]

    def __str__(self):
        return f"KitAssignmentEvent {self.pk}"
