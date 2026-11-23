"""Fleet, location slots and versioned checklists (docs/data-model-contracts.md section 2).

Table kinds follow the contract: vehicle, kit_instance, checklist_template and
item are mutable (M); compartment, vehicle_system, checklist_version,
checklist_item_def, vehicle_override_version, vehicle_override_item and
stocking_deviation are versioned (V); checklist_publication and checklist_cutover
are append-only (A).
Migration 0002 installs the freeze triggers, composite organization foreign
keys and UUID checks. Nothing here is a draft-editing API: a draft is changed by
inserting a new revision (SPEC CHK-2), and published rows are frozen by the
database. Writers go through cya.checklists.commands.

Implementation notes that differ from or complete the contract text are
recorded in docs/checklists-implementation.md.
"""

from uuid import UUID, uuid5

from django.db import models

from cya.accounts.fields import IdentityIDField, new_id

KINDS = ("PRESENCE_CONDITION", "QUANTITY", "MEASUREMENT", "SPECIAL_OBSERVATION", "VEHICLE_READING")
# NOT_APPLICABLE needs an administrator-defined na_rule (SPEC 7.1), which is a
# separate issue; it is deliberately not accepted until that rule exists.
OUTCOMES = ("PASSED", "FAILED", "UNABLE_TO_INSPECT")
OVERRIDE_ACTIONS = ("ADD", "REMOVE", "REPLACE")
# Where a vehicle item requirement keeps its stock (SPEC 6.0.0 CHK-11; Not placed is CHK-9).
PLACE_KINDS = ("COMPARTMENT", "SYSTEM", "NOT_PLACED")


def organization_fk():
    return models.ForeignKey(
        "cya_accounts.Organization", on_delete=models.PROTECT, related_name="+"
    )


def non_blank(column):
    return models.Q(**{f"{column}__regex": r"\S"})


# A VIN: 17 characters, letters and digits except I, O and Q, uppercase (SPEC 2.1.0 IMP-6).
VIN_PATTERN = r"^[A-HJ-NPR-Z0-9]{17}$"
# Position and template keys use the kit definition key format (IMP-1, IMP-6).
KEY_PATTERN = r"^[a-z0-9][a-z0-9._-]*$"
MAX_KEY = 100


class Vehicle(models.Model):
    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    display_name = models.CharField(max_length=200)
    number = models.CharField(max_length=50, null=True, blank=True)  # noqa: DJ001
    active = models.BooleanField(default=True)
    # The vehicle's key is its VIN (SPEC 2.1.0 IMP-6): set once, then never changed (a remount
    # is a new vehicle). Null only for a vehicle created before 2.1.0 whose VIN is not yet set.
    key = models.CharField(  # noqa: DJ001
        max_length=17, null=True, blank=True, db_collation="ascii_bin"
    )

    class Meta:
        db_table = "vehicle"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="vehicle_org_id"),
            models.UniqueConstraint(fields=["organization", "key"], name="vehicle_org_key"),
            models.CheckConstraint(
                condition=models.Q(key__isnull=True) | models.Q(key__regex=VIN_PATTERN),
                name="vehicle_key_vin",
            ),
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
    # SPEC 4.8 CFG-2: order and retirement belong to the revision. An empty order sorts after
    # numbered positions, then by label; rows from before CFG-2 keep it empty (rows are never
    # updated).
    display_order = models.PositiveIntegerField(null=True, blank=True)
    retired = models.BooleanField(default=False)

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
            models.CheckConstraint(
                condition=models.Q(display_order__isnull=True) | models.Q(display_order__gte=1),
                name="%(class)s_display_order",
            ),
            models.CheckConstraint(
                condition=models.Q(retired__in=[True, False]), name="%(class)s_retired_bool"
            ),
        ]


class Compartment(SlotRevision):
    # SPEC 10.2 CAB-1: a compartment may be designated sealable. The flag belongs to the
    # revision, so a change is a new effective-dated revision and history keeps the old value.
    sealable = models.BooleanField(default=False)

    class Meta(SlotRevision.Meta):
        db_table = "compartment"
        constraints = [
            *SlotRevision.Meta.constraints,
            models.CheckConstraint(
                condition=models.Q(sealable__in=[True, False]), name="compartment_sealable_bool"
            ),
        ]

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
    """A checklist: a vehicle checklist (SPEC 5.0.0 CHK-1) or a legacy shared template.

    With vehicle set, this row is one vehicle-owned checklist (the logical vehicle_checklist of
    SPEC 18): its versions, item rows and publications are the ordinary checklist_version,
    checklist_item_def and checklist_publication rows, and a schedule assignment is a
    requirement_binding row naming the same vehicle. With vehicle null it is a template from
    before 5.0.0, read-only once the organization's cutover is recorded (ChecklistCutover).
    The owner and the precedence ordinal are set at creation and never changed (trigger).
    """

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    label = models.CharField(max_length=200)
    active = models.BooleanField(default=True)
    # The template key files name (SPEC 2.1.0 IMP-6): set once, then never changed. Vehicle
    # checklists have none (5.0.0 IMP-6: a template key is no longer used).
    key = models.CharField(  # noqa: DJ001
        max_length=MAX_KEY, null=True, blank=True, db_collation="ascii_bin"
    )
    # SPEC 5.0.0 CHK-1: the owning vehicle; null for a legacy shared template.
    vehicle = models.ForeignKey(Vehicle, null=True, on_delete=models.PROTECT, related_name="+")
    # SPEC 5.0.0 CHK-10: the stable checklist-precedence ordinal within the vehicle. It orders
    # checklists that require the same routine anchor; a new checklist gets the next one.
    precedence = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        db_table = "checklist_template"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="template_org_id"),
            models.UniqueConstraint(fields=["organization", "key"], name="template_org_key"),
            models.UniqueConstraint(fields=["vehicle", "precedence"], name="template_precedence"),
            # Targets of the requirement_binding (vehicle, template) owner check.
            models.UniqueConstraint(fields=["vehicle", "id"], name="template_vehicle_id"),
            models.CheckConstraint(
                condition=(
                    models.Q(vehicle__isnull=True, precedence__isnull=True)
                    # IS NOT NULL is explicit: a CHECK passes when it evaluates to NULL.
                    | models.Q(
                        vehicle__isnull=False,
                        precedence__isnull=False,
                        precedence__gte=1,
                        key__isnull=True,
                    )
                ),
                name="template_owner_precedence",
            ),
            models.CheckConstraint(
                condition=models.Q(key__isnull=True) | models.Q(key__regex=KEY_PATTERN),
                name="template_key_format",
            ),
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
    # Published item definitions retain whether a seal inspection needs an expiration date.
    expiration_tracked = models.BooleanField(default=False)
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
            models.CheckConstraint(
                condition=models.Q(expiration_tracked__in=[True, False]),
                name="itemdef_expiration_bool",
            ),
            # At most one of compartment or ambulance-wide system; a kit sits in a compartment.
            # Neither is a vehicle checklist's Not placed item (SPEC 5.0.0 CHK-9); the owner
            # trigger of migration 0008 allows that only on a vehicle checklist version.
            models.CheckConstraint(
                condition=(
                    models.Q(compartment__isnull=False, system__isnull=True)
                    | models.Q(compartment__isnull=True, system__isnull=False, kit__isnull=True)
                    | models.Q(compartment__isnull=True, system__isnull=True, kit__isnull=True)
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


class ChecklistCutover(models.Model):
    """The instant an organization moved to vehicle-owned checklists (SPEC 5.0.0 18.2).

    Append-only, at most one per organization, written by the migration command. Before it, the
    legacy templates, overrides and bindings govern; from it on, only vehicle checklists do, and
    the legacy rows are read-only (cya.checklists.ownership).
    """

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    command = models.ForeignKey(
        "cya_core.CommandReceipt", on_delete=models.PROTECT, related_name="+"
    )
    recorded_at = models.DateTimeField()
    actor_user = models.ForeignKey("cya_accounts.User", on_delete=models.PROTECT, related_name="+")
    actor_roles_at_time = models.JSONField(default=list)

    class Meta:
        db_table = "checklist_cutover"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="cutover_org_id"),
            models.UniqueConstraint(fields=["organization"], name="cutover_one_per_org"),
        ]

    def __str__(self):
        return f"ChecklistCutover {self.pk}"


class MigrationRecord(models.Model):
    """Receipt and actor of a row written by the 5.0.0 migration (its CHECKLIST_CUTOVER command)."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    command = models.ForeignKey(
        "cya_core.CommandReceipt", on_delete=models.PROTECT, related_name="+"
    )
    recorded_at = models.DateTimeField()
    actor_user = models.ForeignKey("cya_accounts.User", on_delete=models.PROTECT, related_name="+")
    actor_roles_at_time = models.JSONField(default=list)

    class Meta:
        abstract = True


class LegacyChecklistMap(MigrationRecord):
    """(vehicle, legacy template) -> the vehicle checklist the migration made of it (SPEC 18.2).

    Append-only and kept for good: a historical record that names a legacy template is read
    after the cutover through this map (cya.obligations.legacy).
    """

    vehicle = models.ForeignKey(Vehicle, on_delete=models.PROTECT, related_name="+")
    legacy_template = models.ForeignKey(
        ChecklistTemplate, on_delete=models.PROTECT, related_name="+"
    )
    checklist = models.OneToOneField(ChecklistTemplate, on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "legacy_checklist_map"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="legacymap_org_id"),
            models.UniqueConstraint(
                fields=["vehicle", "legacy_template"], name="legacymap_vehicle_template"
            ),
        ]

    def __str__(self):
        return f"LegacyChecklistMap {self.pk}"


class LegacyVersionMap(MigrationRecord):
    """A migrated vehicle checklist version and the legacy versions it was resolved from.

    The version is the legacy template version with the vehicle's override, if any, applied
    (the effective checklist of SPEC 4.4.0 CHK-6), resolved at one boundary instant.
    """

    checklist_version = models.OneToOneField(
        ChecklistVersion, on_delete=models.PROTECT, related_name="+"
    )
    legacy_template_version = models.ForeignKey(
        ChecklistVersion, on_delete=models.PROTECT, related_name="+"
    )
    legacy_override_version = models.ForeignKey(
        VehicleOverrideVersion, null=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "legacy_version_map"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="legacyversion_org_id"),
        ]

    def __str__(self):
        return f"LegacyVersionMap {self.pk}"


class LegacyItemDefMap(MigrationRecord):
    """A migrated item row and the legacy template or override row it copies."""

    item_def = models.OneToOneField(ChecklistItemDef, on_delete=models.PROTECT, related_name="+")
    legacy_item_def = models.ForeignKey(
        ChecklistItemDef, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "legacy_item_def_map"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="legacyitem_org_id"),
        ]

    def __str__(self):
        return f"LegacyItemDefMap {self.pk}"


class LegacyPositionMap(MigrationRecord):
    """(vehicle, legacy stable slot) -> the vehicle-local position (SPEC 18.2 step 1).

    A position's identity is the pair (vehicle, stable slot): every position row is keyed by
    its vehicle, so a vehicle's position already never names another vehicle's. The migration
    keeps each pair as it is, so the new identity's stable slot equals the legacy one; the map
    records that, for every pair, so no reader has to assume it.
    """

    vehicle = models.ForeignKey(Vehicle, on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=12)
    legacy_stable_slot_id = IdentityIDField()
    stable_slot_id = IdentityIDField()

    class Meta:
        db_table = "legacy_position_map"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="legacypos_org_id"),
            models.UniqueConstraint(
                fields=["vehicle", "kind", "legacy_stable_slot_id"], name="legacypos_slot"
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=("COMPARTMENT", "SYSTEM")), name="legacypos_kind"
            ),
        ]

    def __str__(self):
        return f"LegacyPositionMap {self.pk}"


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


class CommandRecord(models.Model):
    """Receipt and actor of an append-only row written by one audited command."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    command = models.ForeignKey(
        "cya_core.CommandReceipt", on_delete=models.PROTECT, related_name="+"
    )
    recorded_at = models.DateTimeField()
    actor_user = models.ForeignKey("cya_accounts.User", on_delete=models.PROTECT, related_name="+")
    actor_roles_at_time = models.JSONField(default=list)

    class Meta:
        abstract = True


# Item requirement ids are version-5 UUIDs (kit instance ids are version 4): a requirement id is
# the unit key of its obligation stream (issue #256 C2), and readers tell a requirement key from
# a kit key by its version alone, as they do kit presence line keys (cya.kits.presence). The
# database holds the version (migration 0014, cya_vehicle_item_requirement_v5).
REQUIREMENT_ID_NAMESPACE = UUID("6a1f3b0e-6c56-5d0e-9a5b-2f6c1d8e4b70")


def new_requirement_id():
    """A fresh, unpredictable item requirement id of UUID version 5."""
    return str(uuid5(REQUIREMENT_ID_NAMESPACE, new_id()))


class VehicleItemRequirement(CommandRecord):
    """One stock of an item on one vehicle (SPEC 6.0.0 CHK-11). Append-only (A).

    Its identity holds no place: a move publishes a new version of the same requirement, and an
    item kept in several places at once has one requirement for each place.
    """

    id = IdentityIDField(primary_key=True, default=new_requirement_id, editable=False)
    vehicle = models.ForeignKey(Vehicle, on_delete=models.PROTECT, related_name="+")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "vehicle_item_requirement"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="itemreq_org_id"),
            # Targets of the version's composite keys: same vehicle and item as its requirement.
            models.UniqueConstraint(
                fields=["vehicle", "item", "id"], name="itemreq_vehicle_item_id"
            ),
        ]

    def __str__(self):
        return f"VehicleItemRequirement {self.pk}"


class VehicleItemRequirementVersion(CommandRecord):
    """An effective-dated version of a vehicle item requirement (SPEC 6.0.0 CHK-11). Append-only.

    It is in force from effective_at until its requirement's next version. Its place is a
    position kind and the position's stable slot id; the slot revision named when it was
    published is kept for display. Vehicle and item repeat the requirement's (a composite key
    holds them equal) so that SPEC 18.1 constraint 8 can be checked on insert.
    """

    requirement = models.ForeignKey(
        VehicleItemRequirement, on_delete=models.PROTECT, related_name="+"
    )
    vehicle = models.ForeignKey(Vehicle, on_delete=models.PROTECT, related_name="+")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")
    revision = models.PositiveIntegerField()
    effective_at = models.DateTimeField()
    place_kind = models.CharField(max_length=12)
    place_slot_id = IdentityIDField(null=True)
    compartment = models.ForeignKey(
        Compartment, null=True, on_delete=models.PROTECT, related_name="+"
    )
    system = models.ForeignKey(VehicleSystem, null=True, on_delete=models.PROTECT, related_name="+")
    instructions = models.CharField(max_length=4000, blank=True)
    kind = models.CharField(max_length=30)
    expected_quantity = models.PositiveIntegerField(null=True)
    unit = models.CharField(max_length=40, null=True)  # noqa: DJ001
    allowed_outcomes = models.JSONField()
    expiration_tracked = models.BooleanField(default=False)
    bulk_eligible = models.BooleanField(default=False)

    class Meta:
        db_table = "vehicle_item_requirement_version"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="itemreqver_org_id"),
            models.UniqueConstraint(
                fields=["requirement", "revision"], name="itemreqver_requirement_revision"
            ),
            models.CheckConstraint(
                condition=models.Q(place_kind__in=PLACE_KINDS), name="itemreqver_place_kind"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        place_kind="COMPARTMENT",
                        place_slot_id__isnull=False,
                        compartment__isnull=False,
                        system__isnull=True,
                    )
                    | models.Q(
                        place_kind="SYSTEM",
                        place_slot_id__isnull=False,
                        compartment__isnull=True,
                        system__isnull=False,
                    )
                    | models.Q(
                        place_kind="NOT_PLACED",
                        place_slot_id__isnull=True,
                        compartment__isnull=True,
                        system__isnull=True,
                    )
                ),
                name="itemreqver_place",
            ),
            models.CheckConstraint(condition=models.Q(kind__in=KINDS), name="itemreqver_kind"),
            models.CheckConstraint(
                condition=(
                    models.Q(kind="QUANTITY", expected_quantity__isnull=False)
                    | (~models.Q(kind="QUANTITY") & models.Q(expected_quantity__isnull=True))
                ),
                name="itemreqver_quantity_by_kind",
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
                name="itemreqver_unit_by_kind",
            ),
            models.CheckConstraint(
                condition=models.Q(bulk_eligible=False), name="itemreqver_bulk_reserved"
            ),
        ]

    def __str__(self):
        return f"VehicleItemRequirementVersion {self.pk}"


class ItemRequirementCutover(CommandRecord):
    """The instant an organization moved to vehicle item requirements (SPEC 6.0.0 18.3).

    Append-only, at most one per organization, written by migrate_item_requirements. From it
    on, publishing a vehicle checklist version links each item row outside a kit to the item
    requirement version it inspects.
    """

    class Meta:
        db_table = "item_requirement_cutover"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="itemreqcut_org_id"),
            models.UniqueConstraint(fields=["organization"], name="itemreqcut_one_per_org"),
        ]

    def __str__(self):
        return f"ItemRequirementCutover {self.pk}"


class ItemRequirementMap(CommandRecord):
    """An item row of a published vehicle checklist version -> its item requirement and version.

    SPEC 6.0.0 CHK-5, CHK-11, 18.3 item 2. The migration writes one for every row of a version
    published before the cutover; from the cutover on, publishing a vehicle checklist version
    writes one for each of its rows (item rows are versioned and never updated). An earlier
    record is resolved through the row it pinned, never through its item and place alone. Kit
    content has no item requirement (CHK-11); its row is recorded as such, so every published
    row resolves. Append-only and kept for good.
    """

    vehicle = models.ForeignKey(Vehicle, on_delete=models.PROTECT, related_name="+")
    checklist_version = models.ForeignKey(
        ChecklistVersion, on_delete=models.PROTECT, related_name="+"
    )
    item_def = models.OneToOneField(ChecklistItemDef, on_delete=models.PROTECT, related_name="+")
    kit_content = models.BooleanField(default=False)
    requirement = models.ForeignKey(
        VehicleItemRequirement, null=True, on_delete=models.PROTECT, related_name="+"
    )
    requirement_version = models.ForeignKey(
        VehicleItemRequirementVersion, null=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "item_requirement_map"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="itemreqmap_org_id"),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        kit_content=False,
                        requirement__isnull=False,
                        requirement_version__isnull=False,
                    )
                    | models.Q(
                        kit_content=True,
                        requirement__isnull=True,
                        requirement_version__isnull=True,
                    )
                ),
                name="itemreqmap_target",
            ),
        ]

    def __str__(self):
        return f"ItemRequirementMap {self.pk}"
