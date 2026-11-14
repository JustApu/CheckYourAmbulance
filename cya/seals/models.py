"""Immutable evidence shared by cabinet and kit seal workflows."""

from django.db import models

from cya.accounts.fields import IdentityIDField, new_id
from cya.checklists.models import non_blank, organization_fk
from cya.operations.models import Evidence


class SealEvent(Evidence):
    """One event in a kit or vehicle-compartment seal stream."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    vehicle = models.ForeignKey(
        "cya_checklists.Vehicle", on_delete=models.PROTECT, related_name="+"
    )
    kit = models.ForeignKey(
        "cya_checklists.KitInstance", null=True, on_delete=models.PROTECT, related_name="+"
    )
    compartment_slot_id = IdentityIDField(null=True)
    sequence = models.PositiveBigIntegerField()
    prior_event = models.OneToOneField(
        "self", null=True, on_delete=models.PROTECT, related_name="+"
    )
    audit_request = models.OneToOneField(
        "self", null=True, on_delete=models.PROTECT, related_name="+"
    )
    event_type = models.CharField(max_length=20)
    marking = models.CharField(max_length=200, null=True)  # noqa: DJ001
    expiration = models.DateField(null=True)
    no_expiration = models.BooleanField(default=False)
    reason = models.TextField(null=True)  # noqa: DJ001

    class Meta:
        db_table = "seal_event"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="seal_event_org_id"),
            models.UniqueConstraint(fields=["kit", "sequence"], name="seal_event_kit_seq"),
            models.UniqueConstraint(
                fields=["vehicle", "compartment_slot_id", "sequence"], name="seal_event_slot_seq"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(kit__isnull=False, compartment_slot_id__isnull=True)
                    | models.Q(kit__isnull=True, compartment_slot_id__isnull=False)
                ),
                name="seal_event_subject",
            ),
            models.CheckConstraint(condition=models.Q(sequence__gte=1), name="seal_event_seq_min"),
            models.CheckConstraint(
                condition=models.Q(no_expiration__in=[True, False]), name="seal_event_exp_bool"
            ),
            models.CheckConstraint(
                condition=models.Q(prior_event__isnull=False) | models.Q(sequence=1),
                name="seal_event_first",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    event_type__in=["BREAK", "RESEAL", "AUDIT_REQUEST", "AUDIT_FULFILLED"]
                ),
                name="seal_event_type",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(event_type="AUDIT_FULFILLED", audit_request__isnull=False)
                    | (
                        ~models.Q(event_type="AUDIT_FULFILLED")
                        & models.Q(audit_request__isnull=True)
                    )
                ),
                name="seal_event_audit_ref",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(event_type="BREAK")
                    | (models.Q(reason__isnull=False) & non_blank("reason"))
                ),
                name="seal_event_break_reason",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(event_type="RESEAL")
                    | (
                        models.Q(marking__isnull=False)
                        & non_blank("marking")
                        & (
                            models.Q(expiration__isnull=False, no_expiration=False)
                            | models.Q(expiration__isnull=True, no_expiration=True)
                        )
                    )
                ),
                name="seal_event_reseal_marking",
            ),
        ]


class SealSubstitution(Evidence):
    """An intact-seal attestation relying on a specific reseal event."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    vehicle = models.ForeignKey(
        "cya_checklists.Vehicle", on_delete=models.PROTECT, related_name="+"
    )
    seal_event = models.ForeignKey(SealEvent, on_delete=models.PROTECT, related_name="+")
    entered_marking = models.CharField(max_length=200)
    intact_attestation = models.BooleanField()

    class Meta:
        db_table = "seal_substitution"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="sealsub_org_id"),
            models.CheckConstraint(
                condition=models.Q(intact_attestation=True), name="sealsub_attest"
            ),
            models.CheckConstraint(condition=non_blank("entered_marking"), name="sealsub_marking"),
        ]


class SealSubstitutionObligation(Evidence):
    """A substitution's explicit link to one obligation; never a fabricated check."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    substitution = models.ForeignKey(SealSubstitution, on_delete=models.PROTECT, related_name="+")
    obligation = models.ForeignKey(
        "cya_obligations.Obligation", on_delete=models.PROTECT, related_name="+"
    )
    item = models.ForeignKey("cya_checklists.Item", on_delete=models.PROTECT, related_name="+")
    kit_key = IdentityIDField()
    ordinal = models.PositiveIntegerField()
    shared_rule = models.ForeignKey(
        "cya_obligations.SharedSatisfactionRule",
        null=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    conditional_rule = models.ForeignKey(
        "cya_conditions.ConditionalRule", null=True, on_delete=models.PROTECT, related_name="+"
    )
    late_eligible = models.BooleanField(default=False)

    class Meta:
        db_table = "seal_substitution_obligation"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="sealsublink_org_id"),
            models.UniqueConstraint(fields=["substitution", "obligation"], name="sealsublink_pair"),
            models.UniqueConstraint(
                fields=["substitution", "item", "kit_key", "ordinal"], name="sealsublink_ordinal"
            ),
            models.CheckConstraint(
                condition=models.Q(ordinal=0, shared_rule__isnull=True)
                | models.Q(ordinal__gt=0, shared_rule__isnull=False),
                name="sealsublink_shared_shape",
            ),
            models.CheckConstraint(
                condition=models.Q(late_eligible__in=[True, False]), name="sealsublink_late_bool"
            ),
        ]


class SealInspection(Evidence):
    """A full inspection used for a reseal; results are retained per physical item."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    reseal = models.OneToOneField(SealEvent, on_delete=models.PROTECT, related_name="+")
    break_event = models.ForeignKey(
        SealEvent, null=True, on_delete=models.PROTECT, related_name="+"
    )
    start_receipt = models.ForeignKey(
        "cya_core.CommandReceipt", null=True, on_delete=models.PROTECT, related_name="+"
    )
    fresh_entry_attestation = models.BooleanField()
    written_marking_attestation = models.BooleanField()

    class Meta:
        db_table = "seal_inspection"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="sealinspect_org_id"),
            models.CheckConstraint(
                condition=models.Q(fresh_entry_attestation=True, written_marking_attestation=True),
                name="sealinspect_attest",
            ),
        ]


class SealInspectionItem(Evidence):
    """A fresh observation for one covered item and its physical kit, if any."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    inspection = models.ForeignKey(SealInspection, on_delete=models.PROTECT, related_name="+")
    item = models.ForeignKey("cya_checklists.Item", on_delete=models.PROTECT, related_name="+")
    kit = models.ForeignKey(
        "cya_checklists.KitInstance", null=True, on_delete=models.PROTECT, related_name="+"
    )
    # A non-null key makes the no-kit case unique in MariaDB as well.
    instance_key = IdentityIDField()
    item_def = models.ForeignKey(
        "cya_checklists.ChecklistItemDef", null=True, on_delete=models.PROTECT, related_name="+"
    )
    kit_definition_item = models.ForeignKey(
        "cya_kits.KitDefinitionItem", null=True, on_delete=models.PROTECT, related_name="+"
    )
    outcome = models.CharField(max_length=20)
    count = models.PositiveIntegerField(null=True)
    expiration = models.DateField(null=True)
    measurement_value = models.DecimalField(max_digits=30, decimal_places=10, null=True)
    measurement_unit = models.CharField(max_length=40, null=True)  # noqa: DJ001

    class Meta:
        db_table = "seal_inspection_item"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="sealinspectitem_org_id"),
            models.UniqueConstraint(
                fields=["inspection", "item", "instance_key"], name="sealinspectitem_scope"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(item_def__isnull=False, kit_definition_item__isnull=True)
                    | models.Q(
                        item_def__isnull=True, kit_definition_item__isnull=False, kit__isnull=False
                    )
                ),
                name="sealinspectitem_source",
            ),
            models.CheckConstraint(
                condition=models.Q(outcome__in=["PASSED", "FAILED", "NOT_APPLICABLE"]),
                name="sealinspectitem_outcome",
            ),
        ]


class SealInspectionKitCount(Evidence):
    """The freshly counted kit-list line certified when a cabinet is sealed."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    inspection = models.ForeignKey(SealInspection, on_delete=models.PROTECT, related_name="+")
    kit_requirement = models.ForeignKey(
        "cya_kits.VehicleKitRequirement", on_delete=models.PROTECT, related_name="+"
    )
    check_record = models.ForeignKey(
        "cya_checks.CheckRecord", null=True, on_delete=models.PROTECT, related_name="+"
    )
    count = models.PositiveIntegerField()

    class Meta:
        db_table = "seal_inspection_kit_count"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="sealkitcount_org_id"),
            models.UniqueConstraint(
                fields=["inspection", "kit_requirement"], name="sealkitcount_line"
            ),
        ]


class SealInspectionKitInstance(Evidence):
    """An instance and assignment included in a certified kit count."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    kit_count = models.ForeignKey(
        SealInspectionKitCount, on_delete=models.PROTECT, related_name="+"
    )
    kit = models.ForeignKey(
        "cya_checklists.KitInstance", on_delete=models.PROTECT, related_name="+"
    )
    assignment = models.ForeignKey(
        "cya_kits.KitAssignmentEvent", on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "seal_inspection_kit_instance"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="sealkitinst_org_id"),
            models.UniqueConstraint(fields=["kit_count", "kit"], name="sealkitinst_count_kit"),
        ]


class SealInspectionKitVerification(Evidence):
    """A nested kit's own seal substitution used during a cabinet inspection (OQ-65)."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    inspection = models.ForeignKey(SealInspection, on_delete=models.PROTECT, related_name="+")
    kit = models.ForeignKey(
        "cya_checklists.KitInstance", on_delete=models.PROTECT, related_name="+"
    )
    substitution = models.ForeignKey(SealSubstitution, on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "seal_inspection_kit_verification"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="sealkitverify_org_id"),
            models.UniqueConstraint(
                fields=["inspection", "kit"], name="sealkitverify_inspection_kit"
            ),
        ]


class SealInspectionObligation(Evidence):
    """A freshly inspected item's explicit link to an obligation."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    inspection_item = models.ForeignKey(
        SealInspectionItem, on_delete=models.PROTECT, related_name="+"
    )
    obligation = models.ForeignKey(
        "cya_obligations.Obligation", on_delete=models.PROTECT, related_name="+"
    )
    ordinal = models.PositiveIntegerField()
    shared_rule = models.ForeignKey(
        "cya_obligations.SharedSatisfactionRule",
        null=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    late_eligible = models.BooleanField(default=False)

    class Meta:
        db_table = "seal_inspection_obligation"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="sealinspob_org_id"),
            models.UniqueConstraint(
                fields=["inspection_item", "obligation"], name="sealinspob_pair"
            ),
            models.UniqueConstraint(
                fields=["inspection_item", "ordinal"], name="sealinspob_ordinal"
            ),
            models.CheckConstraint(
                condition=models.Q(ordinal=0, shared_rule__isnull=True)
                | models.Q(ordinal__gt=0, shared_rule__isnull=False),
                name="sealinspob_shared_shape",
            ),
            models.CheckConstraint(
                condition=models.Q(late_eligible__in=[True, False]), name="sealinspob_late_bool"
            ),
        ]
