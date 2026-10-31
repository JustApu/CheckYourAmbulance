"""Append-only check records (SPEC section 7.2, docs/data-model-contracts.md section 2).

check_record is an A table: rows are never updated or deleted by application code, and the
database rejects it. Migration 0002 installs the organization-pair foreign keys, UUID/role
checks, the append-only triggers and the insert trigger that ties a row to its receipt,
published checklist version and item definition. Writers go through cya.checks.commands.

CheckRecord rows form immutable correction chains rooted at the first check. CheckEvent records
the correction decision; the check row itself remains an immutable snapshot.
"""

from django.db import models

from cya.accounts.fields import IdentityIDField, new_id
from cya.checklists.models import (
    ChecklistItemDef,
    ChecklistVersion,
    Item,
    KitInstance,
    Vehicle,
    non_blank,
    organization_fk,
)
from cya.operations.models import MAX_TEXT, Evidence

OUTCOMES = ("PASSED", "FAILED", "UNABLE_TO_INSPECT", "NOT_APPLICABLE")
PURPOSES = ("ROUTINE", "VERIFICATION")


class CheckRecord(Evidence):
    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    operation_id = IdentityIDField()
    vehicle = models.ForeignKey(Vehicle, on_delete=models.PROTECT, related_name="+")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")
    item_def = models.ForeignKey(ChecklistItemDef, on_delete=models.PROTECT, related_name="+")
    checklist_version = models.ForeignKey(
        ChecklistVersion, on_delete=models.PROTECT, related_name="+"
    )
    # The vehicle's own kit that was present, frozen at check time; null when none or ambiguous.
    kit = models.ForeignKey(KitInstance, null=True, on_delete=models.PROTECT, related_name="+")
    # The inspection session this check was made in (SPEC 6.1); null for a check outside one.
    # It is activity for the session's inactivity timer (SPEC 6.3 SES-2) and never compliance.
    session = models.ForeignKey(
        "cya_inspections.InspectionSession", null=True, on_delete=models.PROTECT, related_name="+"
    )
    outcome = models.CharField(max_length=20)
    quantity = models.PositiveIntegerField(null=True)
    measurement_value = models.DecimalField(max_digits=30, decimal_places=10, null=True)
    measurement_unit = models.CharField(max_length=40, null=True)  # noqa: DJ001
    explanation = models.CharField(max_length=MAX_TEXT, null=True)  # noqa: DJ001
    # What the client claims it observed; informational only, never used for compliance.
    client_observed_at = models.DateTimeField(null=True)
    root_check = models.ForeignKey("self", on_delete=models.PROTECT, related_name="+")
    # CON-3: the newest check the client had displayed, and the newer one it chose to proceed
    # past after a conflict warning. Both name an earlier check of the same item.
    basis_check = models.ForeignKey("self", null=True, on_delete=models.PROTECT, related_name="+")
    acknowledges_check = models.ForeignKey(
        "self", null=True, on_delete=models.PROTECT, related_name="+"
    )
    predecessor_check = models.OneToOneField(
        "self", null=True, on_delete=models.PROTECT, related_name="replacement_check"
    )
    purpose = models.CharField(max_length=12)
    # The Pending Verification deficiency this check verifies (SPEC VER-4, VER-6); set exactly
    # when purpose is VERIFICATION. A correction row repeats its chain's value.
    verification_for_deficiency = models.ForeignKey(
        "cya_deficiencies.Deficiency", null=True, on_delete=models.PROTECT, related_name="+"
    )
    # The scope revision of that deficiency this check was recorded against (VER-4): a Pass counts
    # only for rows of the same revision, so a Pass never carries over to a later revision.
    verification_scope_revision = models.PositiveIntegerField(null=True)

    class Meta:
        db_table = "check_record"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="check_org_id"),
            models.UniqueConstraint(
                fields=["organization", "operation_id"], name="check_org_operation"
            ),
            models.CheckConstraint(condition=models.Q(outcome__in=OUTCOMES), name="check_outcome"),
            models.CheckConstraint(condition=models.Q(purpose__in=PURPOSES), name="check_purpose"),
            models.CheckConstraint(
                condition=models.Q(explanation__isnull=True) | non_blank("explanation"),
                name="check_explanation",
            ),
            # Only a verification check names a deficiency, and every one does (VER-4, VER-6).
            models.CheckConstraint(
                condition=models.Q(
                    purpose="ROUTINE",
                    verification_for_deficiency__isnull=True,
                    verification_scope_revision__isnull=True,
                )
                | models.Q(
                    purpose="VERIFICATION",
                    verification_for_deficiency__isnull=False,
                    verification_scope_revision__isnull=False,
                ),
                name="check_verification_pairing",
            ),
            # Unable to Inspect always carries its reason (SPEC 7.1, UTI-1).
            models.CheckConstraint(
                condition=~models.Q(outcome="UNABLE_TO_INSPECT")
                | models.Q(explanation__isnull=False),
                name="check_unable_reason",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(measurement_value__isnull=True, measurement_unit__isnull=True)
                    | (
                        models.Q(measurement_value__isnull=False, measurement_unit__isnull=False)
                        & non_blank("measurement_unit")
                    )
                ),
                name="check_measurement_pair",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__isnull=True)
                | models.Q(measurement_value__isnull=True),
                name="check_one_observation",
            ),
            models.CheckConstraint(
                condition=~models.Q(basis_check=models.F("id"))
                & ~models.Q(acknowledges_check=models.F("id")),
                name="check_basis_not_self",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(predecessor_check__isnull=True, root_check=models.F("id"))
                    | models.Q(predecessor_check__isnull=False)
                ),
                name="check_root_shape",
            ),
        ]

    def __str__(self):
        return f"CheckRecord {self.pk}"


class CheckEvent(Evidence):
    """Append-only decision event for a correction or invalidation of a check chain."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    root_check = models.ForeignKey(CheckRecord, on_delete=models.PROTECT, related_name="+")
    selected_check = models.ForeignKey(CheckRecord, on_delete=models.PROTECT, related_name="+")
    event_type = models.CharField(max_length=16)
    replacement_check = models.ForeignKey(
        CheckRecord, null=True, on_delete=models.PROTECT, related_name="+"
    )
    explanation = models.CharField(max_length=MAX_TEXT, null=True)  # noqa: DJ001

    class Meta:
        db_table = "check_event"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="check_event_org_id"),
            models.CheckConstraint(
                condition=models.Q(explanation__isnull=True) | non_blank("explanation"),
                name="check_event_explanation",
            ),
            models.CheckConstraint(
                condition=models.Q(event_type__in=("CORRECTION", "INVALIDATION")),
                name="check_event_type",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(event_type="CORRECTION", replacement_check__isnull=False)
                    | models.Q(event_type="INVALIDATION", replacement_check__isnull=True)
                ),
                name="check_event_replacement_shape",
            ),
            models.UniqueConstraint(
                fields=["replacement_check"], name="check_event_replacement_once"
            ),
        ]

    def __str__(self):
        return f"CheckEvent {self.pk}"


class ItemLock(models.Model):
    """A mutable, empty row used to serialize checks for one vehicle and item."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    vehicle = models.ForeignKey(Vehicle, on_delete=models.PROTECT, related_name="+")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "item_lock"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "vehicle", "item"], name="item_lock_scope"
            ),
            models.UniqueConstraint(fields=["organization", "id"], name="item_lock_org_id"),
        ]

    def __str__(self):
        return f"ItemLock {self.vehicle_id}/{self.item_id}"


class CheckObligationLink(Evidence):
    """Immutable evidence that a check was applied to one selected obligation."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    # Check links inherit attribution data, but this user-only command has no service actor.
    actor_user = models.ForeignKey("cya_accounts.User", on_delete=models.PROTECT, related_name="+")
    service_identity = None
    job_key = None
    check_record = models.ForeignKey(
        CheckRecord,
        db_column="check_id",
        on_delete=models.PROTECT,
        related_name="obligation_links",
    )
    obligation = models.ForeignKey(
        "cya_obligations.Obligation", on_delete=models.PROTECT, related_name="check_links"
    )
    ordinal = models.PositiveIntegerField()
    shared_rule = models.ForeignKey(
        "cya_obligations.SharedSatisfactionRule",
        null=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta:
        db_table = "check_obligation_link"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="check_link_org_id"),
            models.UniqueConstraint(fields=["check_record", "obligation"], name="check_link_pair"),
            models.UniqueConstraint(fields=["check_record", "ordinal"], name="check_link_ordinal"),
            models.CheckConstraint(
                condition=models.Q(ordinal__gte=0), name="check_link_ordinal_min"
            ),
            models.CheckConstraint(
                condition=models.Q(ordinal=0, shared_rule__isnull=True)
                | models.Q(ordinal__gt=0, shared_rule__isnull=False),
                name="check_link_shared_shape",
            ),
        ]

    def __str__(self):
        return f"CheckObligationLink {self.check_record_id}/{self.obligation_id} ({self.ordinal})"


class DeterminationCheck(Evidence):
    """An immutable check that supports a supervisor completion of an obligation."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    actor_user = models.ForeignKey("cya_accounts.User", on_delete=models.PROTECT, related_name="+")
    determination = models.ForeignKey(
        "cya_conditions.ObligationDetermination",
        on_delete=models.PROTECT,
        related_name="supporting_checks",
    )
    check_record = models.ForeignKey(
        CheckRecord,
        db_column="check_id",
        on_delete=models.PROTECT,
        related_name="supervisor_determinations",
    )

    class Meta:
        db_table = "determination_check"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="detcheck_org_id"),
            models.UniqueConstraint(fields=["determination", "check_record"], name="detcheck_pair"),
        ]

    def __str__(self):
        return f"DeterminationCheck {self.determination_id}/{self.check_record_id}"
