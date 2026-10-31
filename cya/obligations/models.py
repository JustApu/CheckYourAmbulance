"""Obligations, their selection stream and requirement pins (SPEC 5.1, 5.7, 5.8, 6.4).

All five tables are append-only (A) in docs/data-model-contracts.md section 2. An obligation
row is never edited: whether it is the effective one for a (vehicle, item, due instant) is
decided only by the latest obligation_selection event for that anchor, and replacing or
retiring a pre-window obligation appends a SUPERSEDED determination, a new obligation and a
selection. Migration 0002 installs the organization-pair foreign keys, UUID checks, the
append-only triggers and the insert triggers that enforce the selection stream and attribution.
Routine writers go through cya.obligations.generation, explicit orders through
cya.obligations.adhoc; readers through cya.obligations.queries. Non-routine orders have no
selection stream and remain effective from creation.
"""

from django.db import models

from cya.accounts.fields import IdentityIDField, new_id
from cya.checklists.models import non_blank, organization_fk
from cya.obligations.state import NO_KIT

BINDING_ACTIONS = ("BIND", "UNBIND")
SELECTION_TYPES = ("SELECT", "REPLACE", "RETIRE")


# SPEC 1.8.0 (OQ-53): an item inside a kit yields one obligation per kit instance. On each anchor
# table kit_instance is the real foreign key (null for an item outside a kit) and kit_key is the
# same id, or NO_KIT, as a NOT NULL value that unique keys and composite foreign keys can compare,
# because MariaDB UNIQUE treats NULLs as distinct. kit_key_matches keeps the two in step.


def kit_key_matches(name):
    return models.CheckConstraint(
        condition=(
            models.Q(kit_instance__isnull=True, kit_key=NO_KIT)
            # Explicitly non-null: a NULL CHECK result counts as satisfied, which would let a
            # missing instance carry any key.
            | models.Q(kit_instance__isnull=False, kit_instance=models.F("kit_key"))
        ),
        name=name,
    )


def actor_kind(name):
    """Exactly the receipt's two cases: a human actor, or a named worker with its job key."""
    return models.CheckConstraint(
        condition=(
            models.Q(actor_user__isnull=False, service_identity__isnull=True, job_key__isnull=True)
            | models.Q(
                actor_user__isnull=True, service_identity__isnull=False, job_key__isnull=False
            )
        ),
        name=name,
    )


class Evidence(models.Model):
    """Receipt, recorded time and actor shared by every historical row (contracts section 2).

    A human administrator has an actor_user and no service identity; a named worker has both
    service_identity and job_key and no user (the receipt CHECK allows exactly those two cases).
    """

    command = models.ForeignKey(
        "cya_core.CommandReceipt", on_delete=models.PROTECT, related_name="+"
    )
    recorded_at = models.DateTimeField()
    actor_user = models.ForeignKey(
        "cya_accounts.User", null=True, on_delete=models.PROTECT, related_name="+"
    )
    actor_roles_at_time = models.JSONField(default=list)
    service_identity = models.CharField(max_length=200, null=True)  # noqa: DJ001
    job_key = models.CharField(max_length=200, null=True)  # noqa: DJ001

    class Meta:
        abstract = True


class RequirementBinding(Evidence):
    """Which checklist template and schedule slot govern a vehicle's routine inspections.

    The contract names no table for this: the checklist definitions carry no schedule and the
    schedule rules are not tied to vehicles, so something must say that "Ambulance 1" follows
    slots first-due-0600 and first-due-1800 over the ambulance template. One revision per
    (vehicle, template, slot); the latest revision effective at an instant decides whether it
    is bound then. Appended by administrators; nothing is updated or deleted.
    """

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    vehicle = models.ForeignKey(
        "cya_checklists.Vehicle", on_delete=models.PROTECT, related_name="+"
    )
    template = models.ForeignKey(
        "cya_checklists.ChecklistTemplate", on_delete=models.PROTECT, related_name="+"
    )
    slot = models.CharField(max_length=100, db_collation="utf8mb4_bin")
    revision = models.PositiveIntegerField()
    action = models.CharField(max_length=6)
    effective_at = models.DateTimeField()

    class Meta:
        db_table = "requirement_binding"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="binding_org_id"),
            models.UniqueConstraint(
                fields=["vehicle", "template", "slot", "revision"], name="binding_revision"
            ),
            models.CheckConstraint(
                condition=models.Q(revision__gte=1), name="binding_revision_min"
            ),
            models.CheckConstraint(
                condition=models.Q(action__in=BINDING_ACTIONS), name="binding_action"
            ),
            models.CheckConstraint(condition=non_blank("slot"), name="binding_slot"),
            actor_kind("binding_actor_kind"),
            # Bindings are administrator decisions, never produced by a worker.
            models.CheckConstraint(
                condition=models.Q(actor_user__isnull=False), name="binding_human_only"
            ),
        ]

    def __str__(self):
        return f"RequirementBinding {self.pk}"


class RequirementVersion(Evidence):
    """A requirement fully resolved for one vehicle and item, pinning every input (SPEC 5.1, 5.7).

    The id is derived from the pinned inputs (uuid5), so the same inputs always name the same
    row and a rerun never creates a second one. Obligations bind the version effective at their
    due instant, never the version a session was started under (CHK-4).
    """

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    vehicle = models.ForeignKey(
        "cya_checklists.Vehicle", on_delete=models.PROTECT, related_name="+"
    )
    item = models.ForeignKey("cya_checklists.Item", on_delete=models.PROTECT, related_name="+")
    item_def = models.ForeignKey(
        "cya_checklists.ChecklistItemDef", on_delete=models.PROTECT, related_name="+"
    )
    publication = models.ForeignKey(
        "cya_checklists.ChecklistPublication", on_delete=models.PROTECT, related_name="+"
    )
    override_version = models.ForeignKey(
        "cya_checklists.VehicleOverrideVersion",
        null=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    binding = models.ForeignKey(
        RequirementBinding, null=True, on_delete=models.PROTECT, related_name="+"
    )
    schedule_rule = models.ForeignKey(
        "cya_schedules.ScheduleRule", null=True, on_delete=models.PROTECT, related_name="+"
    )
    conditional_rule = models.ForeignKey(
        "cya_conditions.ConditionalRule", null=True, on_delete=models.PROTECT, related_name="+"
    )
    kit_instance = models.ForeignKey(
        "cya_checklists.KitInstance", null=True, on_delete=models.PROTECT, related_name="+"
    )
    kit_key = IdentityIDField(default=NO_KIT)
    # The first event of the kit instance's current uninterrupted run on this vehicle and slot.
    # Pinning it means a kit that leaves and returns gets a new requirement, so its retired
    # obligations are never revived (TIME-1a).
    kit_event = models.ForeignKey(
        "cya_kits.KitAssignmentEvent", null=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "requirement_version"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="reqversion_org_id"),
            # Target of the obligation's composite key: a row cannot name another vehicle/item.
            models.UniqueConstraint(
                fields=["id", "vehicle", "item", "kit_key"], name="reqversion_anchor"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(binding__isnull=False, schedule_rule__isnull=False)
                    | models.Q(
                        binding__isnull=True,
                        schedule_rule__isnull=True,
                        conditional_rule__isnull=True,
                    )
                ),
                name="reqversion_origin",
            ),
            kit_key_matches("reqversion_kit_key"),
            models.CheckConstraint(
                condition=(
                    models.Q(kit_instance__isnull=True, kit_event__isnull=True)
                    | models.Q(kit_instance__isnull=False, kit_event__isnull=False)
                ),
                name="reqversion_kit_event",
            ),
            actor_kind("reqversion_actor_kind"),
        ]

    def __str__(self):
        return f"RequirementVersion {self.pk}"


class SharedSatisfactionRule(Evidence):
    """Audited organization revision describing which requirement versions may share a check."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    revision = models.PositiveIntegerField()
    effective_at = models.DateTimeField()

    class Meta:
        db_table = "shared_satisfaction_rule"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="shared_rule_org_id"),
            models.UniqueConstraint(
                fields=["organization", "revision"], name="shared_rule_revision"
            ),
            models.CheckConstraint(
                condition=models.Q(revision__gte=1), name="shared_rule_revision_min"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    actor_user__isnull=False,
                    service_identity__isnull=True,
                    job_key__isnull=True,
                ),
                name="shared_rule_human_only",
            ),
        ]

    def __str__(self):
        return f"SharedSatisfactionRule r{self.revision} ({self.pk})"


class SharedSatisfactionRuleMember(Evidence):
    """One allowed source-to-additional requirement-version pair in a rule revision."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    rule = models.ForeignKey(
        SharedSatisfactionRule, on_delete=models.PROTECT, related_name="members"
    )
    source_requirement_version = models.ForeignKey(
        RequirementVersion, on_delete=models.PROTECT, related_name="+"
    )
    additional_requirement_version = models.ForeignKey(
        RequirementVersion, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "shared_satisfaction_rule_member"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="shared_member_org_id"),
            models.UniqueConstraint(
                fields=["rule", "source_requirement_version", "additional_requirement_version"],
                name="shared_member_pair",
            ),
            models.CheckConstraint(
                condition=~models.Q(
                    source_requirement_version=models.F("additional_requirement_version")
                ),
                name="shared_member_distinct_versions",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    actor_user__isnull=False,
                    service_identity__isnull=True,
                    job_key__isnull=True,
                ),
                name="shared_member_human_only",
            ),
        ]

    def __str__(self):
        return f"SharedSatisfactionRuleMember {self.rule_id}"


class Obligation(Evidence):
    """One concrete dated instance of a requirement: (vehicle, item, due instant, version)."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    vehicle = models.ForeignKey(
        "cya_checklists.Vehicle", on_delete=models.PROTECT, related_name="+"
    )
    item = models.ForeignKey("cya_checklists.Item", on_delete=models.PROTECT, related_name="+")
    kit_instance = models.ForeignKey(
        "cya_checklists.KitInstance", null=True, on_delete=models.PROTECT, related_name="+"
    )
    kit_key = IdentityIDField(default=NO_KIT)
    requirement_version = models.ForeignKey(
        RequirementVersion, on_delete=models.PROTECT, related_name="+"
    )
    due_at = models.DateTimeField()
    early_at = models.DateTimeField()
    grace_at = models.DateTimeField()
    routine = models.BooleanField()
    explanation = models.TextField(null=True)  # noqa: DJ001 — null distinguishes routine evidence.
    # Every record made before this instant was considered when the requirement was resolved,
    # and none made after. A new obligation resolves from all records (its own recorded_at); a
    # late repair resolves from the records before the cutoff it was judged against. Together
    # with early_at it fixes the instant after which changes can no longer reach this anchor.
    basis_at = models.DateTimeField()

    class Meta:
        db_table = "obligation"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="obligation_org_id"),
            models.UniqueConstraint(
                fields=[
                    "organization",
                    "vehicle",
                    "item",
                    "kit_key",
                    "due_at",
                    "requirement_version",
                ],
                name="obligation_natural_key",
            ),
            # Target of the selection's composite key: it can only name an obligation of its anchor.
            models.UniqueConstraint(
                fields=["id", "vehicle", "item", "kit_key", "due_at"], name="obligation_anchor"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(routine=True, explanation__isnull=True)
                    | (
                        models.Q(routine=False, explanation__isnull=False)
                        & non_blank("explanation")
                    )
                ),
                name="obligation_origin",
            ),
            kit_key_matches("obligation_kit_key"),
            models.CheckConstraint(
                condition=models.Q(early_at__lte=models.F("due_at"))
                & models.Q(due_at__lte=models.F("grace_at")),
                name="obligation_window_order",
            ),
            models.CheckConstraint(
                condition=models.Q(routine__in=[True, False]), name="obligation_bool"
            ),
            actor_kind("obligation_actor_kind"),
        ]

    def __str__(self):
        return f"Obligation {self.pk}"


class ObligationSelection(Evidence):
    """One event in the stream that decides which obligation is effective for an anchor.

    The anchor is (organization, vehicle, item, due_at). Sequence 1 is a SELECT; later events
    are REPLACE (names an obligation) or RETIRE (names none) and each references the event
    before it. The latest event wins; there is no mutable "effective" flag (contracts section 2).
    """

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    vehicle = models.ForeignKey(
        "cya_checklists.Vehicle", on_delete=models.PROTECT, related_name="+"
    )
    item = models.ForeignKey("cya_checklists.Item", on_delete=models.PROTECT, related_name="+")
    kit_instance = models.ForeignKey(
        "cya_checklists.KitInstance", null=True, on_delete=models.PROTECT, related_name="+"
    )
    kit_key = IdentityIDField(default=NO_KIT)
    due_at = models.DateTimeField()
    sequence = models.PositiveBigIntegerField()
    selection_type = models.CharField(max_length=7)
    selected_obligation = models.ForeignKey(
        Obligation, null=True, on_delete=models.PROTECT, related_name="+"
    )
    previous_selection = models.OneToOneField(
        "self", null=True, on_delete=models.PROTECT, related_name="+"
    )
    supersession_determination = models.OneToOneField(
        "cya_conditions.ObligationDetermination",
        null=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta:
        db_table = "obligation_selection"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="selection_org_id"),
            models.UniqueConstraint(
                fields=["organization", "vehicle", "item", "kit_key", "due_at", "sequence"],
                name="selection_anchor_sequence",
            ),
            # Target of the previous_selection key: the earlier event is of the same anchor.
            models.UniqueConstraint(
                fields=["id", "vehicle", "item", "kit_key", "due_at"], name="selection_anchor_id"
            ),
            kit_key_matches("selection_kit_key"),
            actor_kind("selection_actor_kind"),
            models.CheckConstraint(condition=models.Q(sequence__gte=1), name="selection_sequence"),
            models.CheckConstraint(
                condition=models.Q(selection_type__in=SELECTION_TYPES), name="selection_type"
            ),
            # SELECT is the first event only; RETIRE names no obligation; the others always do.
            models.CheckConstraint(
                condition=(
                    models.Q(
                        selection_type="SELECT",
                        sequence=1,
                        previous_selection__isnull=True,
                        selected_obligation__isnull=False,
                    )
                    | models.Q(
                        selection_type="REPLACE",
                        sequence__gt=1,
                        previous_selection__isnull=False,
                        selected_obligation__isnull=False,
                    )
                    | models.Q(
                        selection_type="RETIRE",
                        sequence__gt=1,
                        previous_selection__isnull=False,
                        selected_obligation__isnull=True,
                        supersession_determination__isnull=False,
                    )
                ),
                name="selection_shape",
            ),
        ]

    def __str__(self):
        return f"ObligationSelection {self.pk}"


class OdometerObligation(Evidence):
    """The vehicle-level odometer entry owed for one scheduled routine inspection (ODO-2).

    One row per inspection as one schedule revision defines it (slot, due instant and schedule
    row, all in the key). It has no selection stream of its own: it is effective exactly while
    an effective item obligation of the same vehicle and due instant pins the same schedule row
    (cya.obligations.queries), so it follows its inspection when a pre-window publication or
    schedule revision replaces or retires the inspection's obligations, and a revision that
    changes the window gets a new row while the old one stops being effective.
    """

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    vehicle = models.ForeignKey(
        "cya_checklists.Vehicle", on_delete=models.PROTECT, related_name="+"
    )
    schedule_rule = models.ForeignKey(
        "cya_schedules.ScheduleRule", on_delete=models.PROTECT, related_name="+"
    )
    scheduled_inspection_key = models.CharField(max_length=200, db_collation="utf8mb4_bin")
    due_at = models.DateTimeField()
    early_at = models.DateTimeField()
    grace_at = models.DateTimeField()

    class Meta:
        db_table = "odometer_obligation"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="odometer_ob_org_id"),
            models.UniqueConstraint(
                fields=["vehicle", "scheduled_inspection_key"], name="odometer_ob_inspection"
            ),
            models.CheckConstraint(
                condition=models.Q(early_at__lte=models.F("due_at"))
                & models.Q(due_at__lte=models.F("grace_at")),
                name="odometer_ob_window_order",
            ),
            models.CheckConstraint(
                condition=non_blank("scheduled_inspection_key"), name="odometer_ob_key"
            ),
            actor_kind("odometer_ob_actor_kind"),
        ]

    def __str__(self):
        return f"OdometerObligation {self.pk}"
