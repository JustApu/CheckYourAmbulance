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
from cya.checklists.models import (
    KINDS,
    Compartment,
    Item,
    KitInstance,
    Vehicle,
    non_blank,
    organization_fk,
)
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


class KitSealDesignation(Evidence):
    """Append-only history of whether a kit instance is sealed (SPEC 10.1 SEAL-1, issue #131).

    Kept apart from kit_assignment_event, which drives obligation generation: designating a kit
    sealed changes nothing about where it is or which checks it owes. A designation takes
    effect when recorded (effective_at = recorded_at). The latest row recorded by an instant is
    the kit's designation then; a kit with no row is not sealed.
    """

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    kit = models.ForeignKey(KitInstance, on_delete=models.PROTECT, related_name="+")
    sequence = models.PositiveBigIntegerField()
    prior_designation = models.OneToOneField(
        "self", null=True, on_delete=models.PROTECT, related_name="+"
    )
    sealable = models.BooleanField()
    effective_at = models.DateTimeField()

    class Meta:
        db_table = "kit_seal_designation"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="kitsealdes_org_id"),
            models.UniqueConstraint(fields=["kit", "sequence"], name="kitsealdes_sequence"),
            models.CheckConstraint(condition=models.Q(sequence__gte=1), name="kitsealdes_seq_min"),
            models.CheckConstraint(
                condition=models.Q(sealable__in=[True, False]), name="kitsealdes_sealable_bool"
            ),
            models.CheckConstraint(
                condition=models.Q(prior_designation__isnull=False) | models.Q(sequence=1),
                name="kitsealdes_first",
            ),
            models.CheckConstraint(
                condition=models.Q(effective_at=models.F("recorded_at")),
                name="kitsealdes_effective_when_recorded",
            ),
        ]

    def __str__(self):
        return f"KitSealDesignation {self.pk}"


# --- kit definitions (SPEC 4.6 KDEF-1 to KDEF-8, issue #129) ---------------------------------

# A kit holds items only (KDEF-5); a vehicle reading is never a kit content.
KIT_KINDS = tuple(kind for kind in KINDS if kind != "VEHICLE_READING")
MAX_KEY = 100


class KitDefinition(models.Model):
    """A kit type: what a complete kit contains, versioned (KDEF-1, KDEF-2).

    The key is the stable, administrator-maintained name files use (SPEC 4.7 IMP-1). Label and
    active flag change through audited commands; the contents live only in versions.
    """

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    key = models.CharField(max_length=MAX_KEY)
    label = models.CharField(max_length=200)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = "kit_definition"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="kitdef_org_id"),
            models.UniqueConstraint(fields=["organization", "key"], name="kitdef_org_key"),
            models.CheckConstraint(
                condition=models.Q(active__in=[True, False]), name="kitdef_bool"
            ),
            models.CheckConstraint(condition=non_blank("label"), name="kitdef_label"),
            models.CheckConstraint(
                condition=models.Q(key__regex=r"^[a-z0-9][a-z0-9._-]*$"), name="kitdef_key_format"
            ),
        ]

    def __str__(self):
        return f"KitDefinition {self.pk}"


class KitDefinitionVersion(models.Model):
    """One revision of a kit definition's contents. Published iff a publication names it."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    definition = models.ForeignKey(KitDefinition, on_delete=models.PROTECT, related_name="+")
    revision = models.PositiveIntegerField()
    created_at = models.DateTimeField()

    class Meta:
        db_table = "kit_definition_version"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="kitdefver_org_id"),
            models.UniqueConstraint(fields=["definition", "revision"], name="kitdefver_revision"),
            models.CheckConstraint(
                condition=models.Q(revision__gte=1), name="kitdefver_revision_min"
            ),
        ]

    def __str__(self):
        return f"KitDefinitionVersion {self.pk}"


class KitDefinitionItem(models.Model):
    """One content entry of a kit definition version (KDEF-1); frozen once published."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    version = models.ForeignKey(KitDefinitionVersion, on_delete=models.PROTECT, related_name="+")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")
    display_order = models.PositiveIntegerField()
    instructions = models.CharField(max_length=4000, blank=True)
    kind = models.CharField(max_length=30)
    expected_quantity = models.PositiveIntegerField(null=True)
    unit = models.CharField(max_length=40, null=True)  # noqa: DJ001
    allowed_outcomes = models.JSONField()
    expiration_tracked = models.BooleanField(default=False)

    class Meta:
        db_table = "kit_definition_item"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="kitdefitem_org_id"),
            models.UniqueConstraint(fields=["version", "item"], name="kitdefitem_version_item"),
            models.CheckConstraint(condition=models.Q(kind__in=KIT_KINDS), name="kitdefitem_kind"),
            models.CheckConstraint(
                condition=(
                    models.Q(kind="QUANTITY", expected_quantity__isnull=False)
                    | (~models.Q(kind="QUANTITY") & models.Q(expected_quantity__isnull=True))
                ),
                name="kitdefitem_quantity_by_kind",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(kind="MEASUREMENT", unit__isnull=False)
                    | (~models.Q(kind="MEASUREMENT") & models.Q(unit__isnull=True))
                ),
                name="kitdefitem_unit_by_kind",
            ),
            models.CheckConstraint(
                condition=models.Q(expiration_tracked__in=[True, False]),
                name="kitdefitem_expiration_bool",
            ),
        ]

    def __str__(self):
        return f"KitDefinitionItem {self.pk}"


class KitDefinitionPublication(Evidence):
    """Append-only publication of one kit definition version (KDEF-2, CHK-3)."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    version = models.OneToOneField(KitDefinitionVersion, on_delete=models.PROTECT, related_name="+")
    effective_at = models.DateTimeField()

    class Meta:
        db_table = "kit_definition_publication"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="kitdefpub_org_id"),
            models.CheckConstraint(
                condition=models.Q(effective_at__gte=models.F("recorded_at")),
                name="kitdefpub_not_retroactive",
            ),
        ]

    def __str__(self):
        return f"KitDefinitionPublication {self.pk}"


class KitInstanceDefinition(Evidence):
    """The kit definition a kit instance has, fixed when the instance is created (KDEF-3).

    One append-only row per kit instance; kit_instance itself is not changed, so instances made
    before kit definitions (KDEF-8) simply have no row.
    """

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    kit = models.OneToOneField(KitInstance, on_delete=models.PROTECT, related_name="+")
    definition = models.ForeignKey(KitDefinition, on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "kit_instance_definition"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="kitinstdef_org_id"),
        ]

    def __str__(self):
        return f"KitInstanceDefinition {self.pk}"
