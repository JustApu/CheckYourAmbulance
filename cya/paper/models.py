"""Paper-entry batch headers (SPEC 8.0.0 §7.9 LATE-1 to LATE-4, §18.4; issue #9 Part B).

A paper-entry batch is the header under which records copied from an outage paper form are
entered. It holds what the paper says once for the whole form: the vehicle, the printed
vehicle-checklist version, any printed kit-definition versions, the original performance
instant and the attributed performer(s). Each row also keeps who entered it and when
(the Evidence columns), which are never replaced by the paper facts (§7.2, TIME-4).

All four tables are append-only (A). The header rows are written only by
cya.paper.commands.record_paper_entry_batch, inside one command receipt; migration 0002
installs the guards that repeat its rules in the database. The records a batch groups (checks,
readings and seal records) reference it from their own tables. A batch is never edited: wrong
metadata is corrected by invalidating the batch (PaperEntryInvalidationEvent, written only by
cya.paper.invalidation; guards in migration 0003) and entering a new one (LATE-6, INV-8).
"""

from django.db import models

from cya.accounts.fields import IdentityIDField, new_id
from cya.checklists.models import ChecklistVersion, Vehicle, non_blank, organization_fk
from cya.kits.models import KitDefinitionVersion
from cya.operations.models import MAX_TEXT, Evidence

# How the entered local date-time became an instant (SPEC TIME-3, LATE-2): an ordinary local
# time, a time inside a spring-forward gap (moved to the first valid instant after it), or a
# repeated fall-back time (its first occurrence).
RESOLUTIONS = ("EXACT", "GAP_FORWARD", "AMBIGUOUS_FIRST")
MAX_ZONE = 100  # Organization.station_zone length


class PaperEntryBatch(Evidence):
    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    vehicle = models.ForeignKey(Vehicle, on_delete=models.PROTECT, related_name="+")
    # The vehicle-checklist version printed on the paper form (LATE-4). One per batch: a vehicle
    # with several checklists prints several forms, and each is entered as its own batch.
    checklist_version = models.ForeignKey(
        ChecklistVersion, on_delete=models.PROTECT, related_name="+"
    )
    # The original performance instant from the paper, in UTC (LATE-2). recorded_at, from
    # Evidence, is the separate server entry instant.
    performed_at = models.DateTimeField()
    # What was entered and how it was converted, so the conversion can be explained (LATE-2).
    # The wall-clock date and time as written on the paper, before zone conversion.
    performed_local_date = models.DateField()
    performed_local_time = models.TimeField()
    performed_zone = models.CharField(max_length=MAX_ZONE)
    performed_utc_offset_minutes = models.SmallIntegerField()
    performed_resolution = models.CharField(max_length=20)

    class Meta:
        db_table = "paper_entry_batch"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="paperbatch_org_id"),
            models.CheckConstraint(
                condition=models.Q(performed_at__lte=models.F("recorded_at")),
                name="paperbatch_not_future",
            ),
            models.CheckConstraint(
                condition=models.Q(performed_resolution__in=RESOLUTIONS),
                name="paperbatch_resolution",
            ),
            models.CheckConstraint(
                condition=models.Q(performed_utc_offset_minutes__gte=-1080)
                & models.Q(performed_utc_offset_minutes__lte=1080),
                name="paperbatch_offset_range",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "vehicle", "performed_at"],
                name="paperbatch_vehicle_time",
            ),
        ]

    def __str__(self):
        return f"PaperEntryBatch {self.pk}"


class PaperEntryPerformer(Evidence):
    """One attributed performer of a batch (LATE-3): a child row, never a list field."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    batch = models.ForeignKey(PaperEntryBatch, on_delete=models.PROTECT, related_name="+")
    performer = models.ForeignKey("cya_accounts.User", on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "paper_entry_performer"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="paperperformer_org_id"),
            models.UniqueConstraint(fields=["batch", "performer"], name="paperperformer_once"),
        ]

    def __str__(self):
        return f"PaperEntryPerformer {self.pk}"


class PaperEntryKitVersion(Evidence):
    """A kit-definition version printed on the separate kit reference used (LATE-4)."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    batch = models.ForeignKey(PaperEntryBatch, on_delete=models.PROTECT, related_name="+")
    kit_definition_version = models.ForeignKey(
        KitDefinitionVersion, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "paper_entry_kit_version"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="paperkitver_org_id"),
            models.UniqueConstraint(
                fields=["batch", "kit_definition_version"], name="paperkitver_once"
            ),
        ]

    def __str__(self):
        return f"PaperEntryKitVersion {self.pk}"


class PaperEntryInvalidationEvent(Evidence):
    """The invalidation of a whole batch and its linked records (SPEC 8.0.0 INV-8, LATE-6, 18.4).

    Appended last by cya.paper.invalidation.invalidate_paper_entry_batch, after an INVALIDATION
    event for every check and reading chain of the batch; its insert trigger (migration 0003)
    refuses it otherwise. Its actor is a Supervisor or Administrator other than the batch's entry
    actor. Nothing is edited: the batch, its records and their times stay as recorded and are
    read as invalidated from this row on. A batch is invalidated at most once (INV-6).
    """

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    batch = models.OneToOneField(PaperEntryBatch, on_delete=models.PROTECT, related_name="+")
    explanation = models.CharField(max_length=MAX_TEXT)

    class Meta:
        db_table = "paper_entry_invalidation_event"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="paperinvalid_org_id"),
            models.CheckConstraint(
                condition=non_blank("explanation"), name="paperinvalid_explanation"
            ),
        ]

    def __str__(self):
        return f"PaperEntryInvalidationEvent {self.pk}"
