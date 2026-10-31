"""Fleet, location slots and versioned checklists (docs/data-model-contracts.md section 2).

Table kinds follow the contract: vehicle, kit_instance, checklist_template and
item are mutable (M); compartment, vehicle_system, checklist_version,
checklist_item_def, vehicle_override_version, vehicle_override_item and
stocking_deviation are versioned (V); checklist_publication is append-only (A).
Migration 0002 installs the freeze triggers, composite organization foreign
keys and UUID checks. Nothing here is a draft-editing API: a draft is changed by
inserting a new revision (SPEC CHK-2), and published rows are frozen by the
database. Writers go through cya.checklists.commands.

Implementation notes that differ from or complete the contract text are
recorded in docs/checklists-implementation.md.
"""

from django.db import models

from cya.accounts.fields import IdentityIDField, new_id

KINDS = ("PRESENCE_CONDITION", "QUANTITY", "MEASUREMENT", "SPECIAL_OBSERVATION", "VEHICLE_READING")
# NOT_APPLICABLE needs an administrator-defined na_rule (SPEC 7.1), which is a
# separate issue; it is deliberately not accepted until that rule exists.
OUTCOMES = ("PASSED", "FAILED", "UNABLE_TO_INSPECT")
OVERRIDE_ACTIONS = ("ADD", "REMOVE", "REPLACE")


def organization_fk():
    return models.ForeignKey(
        "cya_accounts.Organization", on_delete=models.PROTECT, related_name="+"
    )


def non_blank(column):
    return models.Q(**{f"{column}__regex": r"\S"})


class Vehicle(models.Model):
    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    display_name = models.CharField(max_length=200)
    number = models.CharField(max_length=50, null=True, blank=True)  # noqa: DJ001
    active = models.BooleanField(default=True)

    class Meta:
        db_table = "vehicle"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="vehicle_org_id"),
            models.CheckConstraint(
                condition=models.Q(active__in=[True, False]), name="vehicle_bool"
            ),
        ]

    def __str__(self):
        return f"Vehicle {self.pk}"


class SlotRevision(models.Model):
    """One effective-dated revision of a vehicle's location slot (V table)."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    vehicle = models.ForeignKey(Vehicle, on_delete=models.PROTECT, related_name="+")
    # Names the slot across revisions and, for the same physical position, across
    # vehicles: a template definition references one row and a vehicle's own row
    # for that slot is found by stable_slot_id (overlay.py).
    stable_slot_id = IdentityIDField()
    revision = models.PositiveIntegerField()
    label = models.CharField(max_length=200)
    effective_at = models.DateTimeField()

    class Meta:
        abstract = True
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="%(class)s_org_id"),
            models.UniqueConstraint(fields=["vehicle", "id"], name="%(class)s_vehicle_id"),
            models.UniqueConstraint(
                fields=["vehicle", "stable_slot_id", "revision"], name="%(class)s_slot_revision"
            ),
            models.CheckConstraint(condition=models.Q(revision__gte=1), name="%(class)s_revision"),
            models.CheckConstraint(condition=non_blank("label"), name="%(class)s_label"),
        ]


class Compartment(SlotRevision):
    class Meta(SlotRevision.Meta):
        db_table = "compartment"

    def __str__(self):
        return f"Compartment {self.pk}"


class VehicleSystem(SlotRevision):
    class Meta(SlotRevision.Meta):
        db_table = "vehicle_system"

    def __str__(self):
        return f"VehicleSystem {self.pk}"


class Item(models.Model):
    """Stable logical item identity reused across checklist versions."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    label = models.CharField(max_length=200)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = "item"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="item_org_id"),
            models.CheckConstraint(condition=models.Q(active__in=[True, False]), name="item_bool"),
            models.CheckConstraint(condition=non_blank("label"), name="item_label"),
        ]

    def __str__(self):
        return f"Item {self.pk}"


class KitInstance(models.Model):
    """A physical kit bound to a vehicle compartment slot (SPEC 4.2); retained on checks."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    label = models.CharField(max_length=200)
    active = models.BooleanField(default=True)
    vehicle = models.ForeignKey(Vehicle, null=True, on_delete=models.PROTECT, related_name="+")
    compartment = models.ForeignKey(
        Compartment, null=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "kit_instance"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="kit_org_id"),
            models.CheckConstraint(condition=models.Q(active__in=[True, False]), name="kit_bool"),
            models.CheckConstraint(condition=non_blank("label"), name="kit_label"),
            models.CheckConstraint(
                condition=models.Q(compartment__isnull=True) | models.Q(vehicle__isnull=False),
                name="kit_compartment_needs_vehicle",
            ),
        ]

    def __str__(self):
        return f"KitInstance {self.pk}"


class ChecklistTemplate(models.Model):
    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    label = models.CharField(max_length=200)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = "checklist_template"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="template_org_id"),
            models.CheckConstraint(
                condition=models.Q(active__in=[True, False]), name="template_bool"
            ),
            models.CheckConstraint(condition=non_blank("label"), name="template_label"),
        ]

    def __str__(self):
        return f"ChecklistTemplate {self.pk}"


class ChecklistVersion(models.Model):
    """A template revision. Published iff a checklist_publication row names it."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    template = models.ForeignKey(ChecklistTemplate, on_delete=models.PROTECT, related_name="+")
    revision = models.PositiveIntegerField()
    created_at = models.DateTimeField()

    class Meta:
        db_table = "checklist_version"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="version_org_id"),
            models.UniqueConstraint(fields=["template", "revision"], name="version_revision"),
            models.CheckConstraint(
                condition=models.Q(revision__gte=1), name="version_revision_min"
            ),
        ]

    def __str__(self):
        return f"ChecklistVersion {self.pk}"


class VehicleOverrideVersion(models.Model):
    """A vehicle overlay on one template version. Published iff a publication names it."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    vehicle = models.ForeignKey(Vehicle, on_delete=models.PROTECT, related_name="+")
    template_version = models.ForeignKey(
        ChecklistVersion, on_delete=models.PROTECT, related_name="+"
    )
    revision = models.PositiveIntegerField()
    created_at = models.DateTimeField()

    class Meta:
        db_table = "vehicle_override_version"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="override_org_id"),
            models.UniqueConstraint(fields=["vehicle", "revision"], name="override_revision"),
            models.CheckConstraint(
                condition=models.Q(revision__gte=1), name="override_revision_min"
            ),
        ]

    def __str__(self):
        return f"VehicleOverrideVersion {self.pk}"


class ChecklistPublication(models.Model):
    """Append-only publication of exactly one template version or one override version."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    command = models.ForeignKey(
        "cya_core.CommandReceipt", on_delete=models.PROTECT, related_name="+"
    )
    recorded_at = models.DateTimeField()
    actor_user = models.ForeignKey("cya_accounts.User", on_delete=models.PROTECT, related_name="+")
    actor_roles_at_time = models.JSONField(default=list)
    checklist_version = models.OneToOneField(
        ChecklistVersion, null=True, on_delete=models.PROTECT, related_name="+"
    )
    override_version = models.OneToOneField(
        VehicleOverrideVersion, null=True, on_delete=models.PROTECT, related_name="+"
    )
    effective_at = models.DateTimeField()

    class Meta:
        db_table = "checklist_publication"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="publication_org_id"),
            models.CheckConstraint(
                condition=(
                    models.Q(checklist_version__isnull=False, override_version__isnull=True)
                    | models.Q(checklist_version__isnull=True, override_version__isnull=False)
                ),
                name="publication_one_target",
            ),
            models.CheckConstraint(
                condition=models.Q(effective_at__gte=models.F("recorded_at")),
                name="publication_not_retroactive",
            ),
        ]

    def __str__(self):
        return f"ChecklistPublication {self.pk}"


class ChecklistItemDef(models.Model):
    """One item requirement. Owned by exactly one template version or override version."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    checklist_version = models.ForeignKey(
        ChecklistVersion, null=True, on_delete=models.PROTECT, related_name="+"
    )
    override_version = models.ForeignKey(
        VehicleOverrideVersion, null=True, on_delete=models.PROTECT, related_name="+"
    )
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")
    compartment = models.ForeignKey(
        Compartment, null=True, on_delete=models.PROTECT, related_name="+"
    )
    system = models.ForeignKey(VehicleSystem, null=True, on_delete=models.PROTECT, related_name="+")
    kit = models.ForeignKey(KitInstance, null=True, on_delete=models.PROTECT, related_name="+")
    display_order = models.PositiveIntegerField()
    instructions = models.CharField(max_length=4000, blank=True)
    kind = models.CharField(max_length=30)
    expected_quantity = models.PositiveIntegerField(null=True)
    unit = models.CharField(max_length=40, null=True)  # noqa: DJ001
    allowed_outcomes = models.JSONField()
    # Reserved: bulk pass is deferred from R1 (SPEC 7.6); the database holds it false.
    bulk_eligible = models.BooleanField(default=False)

    class Meta:
        db_table = "checklist_item_def"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="itemdef_org_id"),
            models.UniqueConstraint(
                fields=["checklist_version", "item"], name="itemdef_version_item"
            ),
            models.UniqueConstraint(
                fields=["override_version", "item"], name="itemdef_override_item"
            ),
            # Targets of the override-entry composite foreign keys.
            models.UniqueConstraint(fields=["override_version", "id"], name="itemdef_override_id"),
            models.UniqueConstraint(fields=["id", "item"], name="itemdef_id_item"),
            models.CheckConstraint(
                condition=(
                    models.Q(checklist_version__isnull=False, override_version__isnull=True)
                    | models.Q(checklist_version__isnull=True, override_version__isnull=False)
                ),
                name="itemdef_one_owner",
            ),
            models.CheckConstraint(condition=models.Q(kind__in=KINDS), name="itemdef_kind"),
            # Exactly one of compartment or ambulance-wide system; a kit sits in a compartment.
            models.CheckConstraint(
                condition=(
                    models.Q(compartment__isnull=False, system__isnull=True)
                    | models.Q(compartment__isnull=True, system__isnull=False, kit__isnull=True)
                ),
                name="itemdef_one_placement",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(kind="QUANTITY", expected_quantity__isnull=False)
                    | (~models.Q(kind="QUANTITY") & models.Q(expected_quantity__isnull=True))
                ),
                name="itemdef_quantity_by_kind",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        kind__in=["MEASUREMENT", "VEHICLE_READING"],
                        unit__isnull=False,
                        unit__regex=r"\S",
                    )
                    | (
                        ~models.Q(kind__in=["MEASUREMENT", "VEHICLE_READING"])
                        & models.Q(unit__isnull=True)
                    )
                ),
                name="itemdef_unit_by_kind",
            ),
            models.CheckConstraint(
                condition=models.Q(bulk_eligible=False), name="itemdef_bulk_reserved"
            ),
        ]

    def __str__(self):
        return f"ChecklistItemDef {self.pk}"


class VehicleOverrideItem(models.Model):
    """One overlay entry. ADD and REPLACE carry a replacement definition; REMOVE does not."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    override_version = models.ForeignKey(
        VehicleOverrideVersion, on_delete=models.PROTECT, related_name="+"
    )
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")
    action = models.CharField(max_length=7)
    definition = models.ForeignKey(
        ChecklistItemDef, null=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "vehicle_override_item"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="overrideitem_org_id"),
            models.UniqueConstraint(fields=["override_version", "item"], name="overrideitem_item"),
            models.CheckConstraint(
                condition=(
                    models.Q(action="REMOVE", definition__isnull=True)
                    | models.Q(action__in=["ADD", "REPLACE"], definition__isnull=False)
                ),
                name="overrideitem_action_definition",
            ),
        ]

    def __str__(self):
        return f"VehicleOverrideItem {self.pk}"


class StockingDeviation(models.Model):
    """One immutable revision of a temporary stocking deviation (SPEC 4.5)."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    command = models.ForeignKey(
        "cya_core.CommandReceipt", on_delete=models.PROTECT, related_name="+"
    )
    recorded_at = models.DateTimeField()
    vehicle = models.ForeignKey(Vehicle, on_delete=models.PROTECT, related_name="+")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")
    revision = models.PositiveIntegerField()
    normal_quantity = models.PositiveIntegerField()
    alternate_quantity = models.PositiveIntegerField()
    valid_from = models.DateTimeField()
    valid_until = models.DateTimeField()
    approver = models.ForeignKey("cya_accounts.User", on_delete=models.PROTECT, related_name="+")
    explanation = models.CharField(max_length=4000)
    prior_revision = models.OneToOneField(
        "self", null=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "stocking_deviation"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="deviation_org_id"),
            models.UniqueConstraint(
                fields=["vehicle", "item", "revision"], name="deviation_revision"
            ),
            models.CheckConstraint(
                condition=models.Q(revision__gte=1), name="deviation_revision_min"
            ),
            models.CheckConstraint(
                condition=models.Q(valid_from__lt=models.F("valid_until")), name="deviation_window"
            ),
            models.CheckConstraint(
                condition=~models.Q(alternate_quantity=models.F("normal_quantity")),
                name="deviation_differs",
            ),
            models.CheckConstraint(
                condition=non_blank("explanation"), name="deviation_explanation"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(revision=1, prior_revision__isnull=True)
                    | models.Q(revision__gt=1, prior_revision__isnull=False)
                ),
                name="deviation_prior_by_revision",
            ),
        ]

    def __str__(self):
        return f"StockingDeviation {self.pk}"
