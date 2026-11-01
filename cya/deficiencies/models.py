"""Deficiencies, their evidence, links and rebuildable current state (SPEC 8, issue 80 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/80)).

Table kinds follow docs/data-model-contracts.md: deficiency_category is versioned (V);
deficiency is a mutable header (M) whose creation facts a trigger freezes; deficiency_lock and
deficiency_projection are mutable (M) and carry no history; deficiency_affected_item,
deficiency_event, verification_scope_row, deficiency_evidence and deficiency_link are append-only
(A). Migrations 0002 and 0012 install their organization-pair foreign keys, UUID/role checks,
freeze and append-only triggers, and insert triggers that tie each row to its command receipt.
Writers go through cya.deficiencies.service and cya.deficiencies.commands; see docs/deficiencies.md.
"""

from django.db import models

from cya.accounts.fields import IdentityIDField, new_id
from cya.checklists.models import non_blank, organization_fk
from cya.deficiencies.transitions import (
    ACTIVE_STATUSES,
    AWAITING_EXTERNAL_DEPENDENCY,
    CLOSED_ADMINISTRATOR,
    CLOSED_UNVERIFIABLE,
    CLOSED_VERIFIED,
    EMPTY_SCOPE_CLOSURE_STATUSES,
    IN_PROGRESS,
    OPEN,
    PENDING_VERIFICATION,
    REPORTED_IN_ERROR,
    STATUSES,
)
from cya.operations.models import MAX_TEXT, Evidence

# REPORTED opens a deficiency. A Failed to Passed Correct-entry appends REPORTED_IN_ERROR when the
# COR-4 conditions hold and otherwise CORRECTION_NOTICE (the supervisor-notification intent);
# invalidating the correcting chain appends REOPENED (INV-7). Each names its check chain in
# correction_ref. SEVERITY_CHANGED is a logged re-grade (DEF-8): it keeps the status and carries the
# previous and the new severity revision. CORRECTION_RECORDED is the repair event that starts a
# new materialized verification-scope revision. VERIFICATION_CLOSED is the automatic closure that
# commits with the last qualifying verification check, and VERIFICATION_FAILED is the Pending
# Verification to In Progress return a Failed verification check causes (VER-2, VER-4, VER-5); both
# name that check in verification_check. VERIFICATION_REOPENED (Closed Verified to Open, INV-4, issue
# issue 155 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/155)) is written by the invalidation or the author's non-Pass correction of a verification chain
# whose Pass the closure needed; it names that chain's root in verification_check. SCOPE_EXPANDED
# (Pending Verification, unchanged) appends a scope revision with supervisor-added items, and
# VERIFICATION_ADDED (Closed Verified to Open, with the new deadline) records items to verify after
# the next correction (VER-3, VER-4, SPEC 8.7, issue 156 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/156)); both explain themselves and list their
# items in deficiency_scope_expansion. CHECKS_SUSPENDED (status unchanged) starts a supervisor
# suspension of the routine checks it lists in check_suspension (SPEC 8.9, issue 13
# (https://github.com/JustApu/CheckYourAmbulance/issues/13)); a closing event lists in
# suspensions_ended every suspension still in force, which ends with it (SUS-3, SUS-4).
EVENT_TYPES = (
    "REPORTED",
    "REPORTED_IN_ERROR",
    "CORRECTION_NOTICE",
    "REOPENED",
    "SEVERITY_CHANGED",
    "ASSIGNED",
    "DEADLINE_SET",
    "WORK_STARTED",
    "DEPENDENCY_ENTERED",
    "DEPENDENCY_REVISED",
    "DEPENDENCY_ENDED",
    "PROGRESS_NOTE",
    "EXTENSION_REQUESTED",
    "EXTENSION_DECIDED",
    "ESCALATION_ACKNOWLEDGED",
    "ADMINISTRATOR_CLOSED",
    "ADMINISTRATOR_REOPENED",
    "EMPTY_SCOPE_CLOSED",
    "CORRECTION_RECORDED",
    "VERIFICATION_CLOSED",
    "VERIFICATION_FAILED",
    "VERIFICATION_REOPENED",
    "SCOPE_EXPANDED",
    "VERIFICATION_ADDED",
    "CHECKS_SUSPENDED",
)
# Types written by the assignment and work-start commands (issue 146 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/146)); each keeps the status
# except WORK_STARTED (Open to In Progress) and none carries a check chain or an explanation rule.
ASSIGNMENT_EVENT_TYPES = ("ASSIGNED", "DEADLINE_SET", "WORK_STARTED")
# Types written by the Awaiting External Dependency and progress-note commands (issue 149 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/149), SPEC
# 8.5, 8.1). The dependency record is the newest of the three dependency events since the entering
# one; a progress note keeps the status and carries its text in explanation.
DEPENDENCY_EVENT_TYPES = ("DEPENDENCY_ENTERED", "DEPENDENCY_REVISED", "DEPENDENCY_ENDED")
NOTE_EVENT_TYPES = ("PROGRESS_NOTE",)
# Types written by the deadline-extension commands (issue 150 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/150), SPEC DEF-5). A request carries the
# reason and the date asked for; a decision names its request, one outcome and, unless it denies,
# the date that governs. Both keep the status and are made only while the deficiency is active.
EXTENSION_EVENT_TYPES = ("EXTENSION_REQUESTED", "EXTENSION_DECIDED")
ACKNOWLEDGEMENT_EVENT_TYPES = ("ESCALATION_ACKNOWLEDGED",)
ADMINISTRATOR_CLOSURE_EVENT_TYPES = ("ADMINISTRATOR_CLOSED", "ADMINISTRATOR_REOPENED")
EMPTY_SCOPE_CLOSURE_EVENT_TYPES = ("EMPTY_SCOPE_CLOSED",)
VERIFICATION_EVENT_TYPES = ("VERIFICATION_CLOSED", "VERIFICATION_FAILED")
VERIFICATION_REOPEN_EVENT_TYPES = ("VERIFICATION_REOPENED",)
SCOPE_EVENT_TYPES = ("SCOPE_EXPANDED", "VERIFICATION_ADDED")
SUSPENSION_EVENT_TYPES = ("CHECKS_SUSPENDED",)
# The events that take an active deficiency to a closed or cancelled status. Each one ends every
# check suspension still in force and lists it in suspensions_ended (SUS-3, SUS-4): a suspension
# with no open deficiency is invalid. No other event may list one.
CLOSING_EVENT_TYPES = (
    "REPORTED_IN_ERROR",
    "ADMINISTRATOR_CLOSED",
    "EMPTY_SCOPE_CLOSED",
    "VERIFICATION_CLOSED",
)
SUSPENSION_ACTIONS = ("START", "END")
EXPLICIT_CLOSURE_EVENT_TYPES = (
    *ADMINISTRATOR_CLOSURE_EVENT_TYPES,
    *EMPTY_SCOPE_CLOSURE_EVENT_TYPES,
)
DECISIONS = ("APPROVED", "DENIED", "DIFFERENT_DATE")
LINK_TYPES = ("ADD", "REMOVE")
MAX_SUMMARY = 200
MAX_DESCRIPTION = 300
MAX_SEVERITY_CODE = 30
MAX_AGE_DAYS = 3650
MAX_AGE_THRESHOLDS = 5


class DeficiencyAcknowledgementRule(Evidence):
    """Append-only organization revisions that bound escalation acknowledgements (DEF-7)."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    operation_id = IdentityIDField()
    revision = models.PositiveIntegerField()
    maximum_days = models.PositiveIntegerField()

    class Meta:
        db_table = "deficiency_acknowledgement_rule"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="defackrule_org_id"),
            models.UniqueConstraint(
                fields=["organization", "operation_id"], name="defackrule_org_operation"
            ),
            models.UniqueConstraint(
                fields=["organization", "revision"], name="defackrule_revision"
            ),
            models.CheckConstraint(
                condition=models.Q(revision__gte=1), name="defackrule_revision_min"
            ),
            models.CheckConstraint(
                condition=models.Q(maximum_days__gte=1), name="defackrule_max_days"
            ),
        ]

    def __str__(self):
        return f"DeficiencyAcknowledgementRule r{self.revision}"


class DeficiencyCategory(models.Model):
    """One effective-dated revision of a category (V table); supply_related is per revision."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    # Names the category across revisions; clients send this, never a revision row id.
    stable_category_id = IdentityIDField()
    revision = models.PositiveIntegerField()
    label = models.CharField(max_length=200)
    supply_related = models.BooleanField()
    effective_at = models.DateTimeField()

    class Meta:
        db_table = "deficiency_category"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="defcat_org_id"),
            models.UniqueConstraint(
                fields=["organization", "stable_category_id", "revision"], name="defcat_revision"
            ),
            models.CheckConstraint(condition=models.Q(revision__gte=1), name="defcat_revision_min"),
            models.CheckConstraint(condition=non_blank("label"), name="defcat_label"),
            models.CheckConstraint(
                condition=models.Q(supply_related__in=[True, False]), name="defcat_bool"
            ),
        ]

    def __str__(self):
        return f"DeficiencyCategory {self.pk}"


class DeficiencySeverity(models.Model):
    """One effective-dated revision of a severity level (V table).

    Levels are administrator data, not a fixed list: a reporter names one by code, the server
    reads the revision in force, and the reporter is shown label and criteria when choosing
    (SPEC DEF-8). A level is retired by a newer revision with active false; reports already made
    keep the revision they were made under.
    """

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    # Names the level across revisions; clients send this.
    code = models.CharField(max_length=30, db_collation="ascii_bin")
    revision = models.PositiveIntegerField()
    label = models.CharField(max_length=40)
    # Concise criteria shown to the reporter beside the label.
    description = models.CharField(max_length=MAX_DESCRIPTION)
    rank = models.PositiveSmallIntegerField()
    active = models.BooleanField()
    effective_at = models.DateTimeField()

    class Meta:
        db_table = "deficiency_severity"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="defsev_org_id"),
            models.UniqueConstraint(
                fields=["organization", "code", "revision"], name="defsev_revision"
            ),
            models.CheckConstraint(condition=models.Q(revision__gte=1), name="defsev_revision_min"),
            models.CheckConstraint(condition=models.Q(rank__gte=1), name="defsev_rank_min"),
            models.CheckConstraint(
                condition=models.Q(code__regex=r"^[A-Z][A-Z0-9_]*$"), name="defsev_code"
            ),
            models.CheckConstraint(condition=non_blank("label"), name="defsev_label"),
            models.CheckConstraint(condition=non_blank("description"), name="defsev_description"),
            models.CheckConstraint(
                condition=models.Q(active__in=[True, False]), name="defsev_bool"
            ),
        ]

    def __str__(self):
        return f"DeficiencySeverity {self.code} r{self.revision}"


class DeficiencyAgeRule(Evidence):
    """One revision of the organization's age highlighting (SPEC DEF-6, AUD-4); append-only.

    The newest revision recorded by an instant applies at that instant; with no row nothing is
    highlighted (the owner's decision of 2026-10-27: no defaults are hard-coded). age_thresholds_days
    are up to five ascending ages in days that a dashboard marks; a case is long-running when its
    age reaches long_running_age_days or its extension requests reach long_running_extensions
    (either null leaves that test off). A revision with no thresholds and both nulls switches
    highlighting off again. There is no age-based notification (DEF-6).
    """

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    operation_id = IdentityIDField()
    revision = models.PositiveIntegerField()
    age_thresholds_days = models.JSONField(default=list)
    long_running_age_days = models.PositiveIntegerField(null=True)
    long_running_extensions = models.PositiveIntegerField(null=True)

    class Meta:
        db_table = "deficiency_age_rule"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="defage_org_id"),
            models.UniqueConstraint(
                fields=["organization", "operation_id"], name="defage_org_operation"
            ),
            models.UniqueConstraint(fields=["organization", "revision"], name="defage_revision"),
            models.CheckConstraint(condition=models.Q(revision__gte=1), name="defage_revision_min"),
            models.CheckConstraint(
                condition=models.Q(long_running_age_days__isnull=True)
                | models.Q(long_running_age_days__gte=1, long_running_age_days__lte=MAX_AGE_DAYS),
                name="defage_long_age",
            ),
            models.CheckConstraint(
                condition=models.Q(long_running_extensions__isnull=True)
                | models.Q(long_running_extensions__gte=1),
                name="defage_long_extensions",
            ),
        ]

    def __str__(self):
        return f"DeficiencyAgeRule r{self.revision}"


class Deficiency(Evidence):
    """Immutable creation header; current status, assignee and deadline are in the projection."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    operation_id = IdentityIDField()
    reported_at = models.DateTimeField()
    reporter = models.ForeignKey("cya_accounts.User", on_delete=models.PROTECT, related_name="+")
    category_version = models.ForeignKey(
        DeficiencyCategory, on_delete=models.PROTECT, related_name="+"
    )
    severity_version = models.ForeignKey(
        DeficiencySeverity, on_delete=models.PROTECT, related_name="+"
    )
    summary = models.CharField(max_length=MAX_SUMMARY)
    description = models.CharField(max_length=MAX_TEXT)
    vehicle = models.ForeignKey(
        "cya_checklists.Vehicle", on_delete=models.PROTECT, related_name="+"
    )
    item = models.ForeignKey(
        "cya_checklists.Item", null=True, on_delete=models.PROTECT, related_name="+"
    )
    kit = models.ForeignKey(
        "cya_checklists.KitInstance", null=True, on_delete=models.PROTECT, related_name="+"
    )
    compartment = models.ForeignKey(
        "cya_checklists.Compartment", null=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "deficiency"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="deficiency_org_id"),
            models.UniqueConstraint(
                fields=["organization", "operation_id"], name="deficiency_org_operation"
            ),
            models.CheckConstraint(condition=non_blank("summary"), name="deficiency_summary"),
            models.CheckConstraint(
                condition=non_blank("description"), name="deficiency_description"
            ),
            # An item (inside a kit or not), a kit or a compartment; none means the whole vehicle.
            models.CheckConstraint(
                condition=models.Q(compartment__isnull=True)
                | (models.Q(item__isnull=True) & models.Q(kit__isnull=True)),
                name="deficiency_one_target",
            ),
        ]

    def __str__(self):
        return f"Deficiency {self.pk}"


class DeficiencyLock(models.Model):
    """An empty row held to serialize every writer of one deficiency."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    deficiency = models.OneToOneField(Deficiency, on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "deficiency_lock"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="deficiency_lock_org_id"),
        ]

    def __str__(self):
        return f"DeficiencyLock {self.deficiency_id}"


class DeficiencyAffectedItem(Evidence):
    """The scope snapshot taken when the deficiency was created; never changed afterwards."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    deficiency = models.ForeignKey(Deficiency, on_delete=models.PROTECT, related_name="+")
    item = models.ForeignKey("cya_checklists.Item", on_delete=models.PROTECT, related_name="+")
    definition = models.ForeignKey(
        "cya_checklists.ChecklistItemDef", on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "deficiency_affected_item"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="defaffect_org_id"),
            models.UniqueConstraint(
                fields=["deficiency", "item", "definition"], name="defaffect_scope"
            ),
        ]

    def __str__(self):
        return f"DeficiencyAffectedItem {self.deficiency_id}/{self.item_id}"


class VerificationScopeRow(Evidence):
    """Append-only, instance-qualified items that must be checked after a repair."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    deficiency = models.ForeignKey(Deficiency, on_delete=models.PROTECT, related_name="+")
    correction_event = models.ForeignKey(
        "DeficiencyEvent", on_delete=models.PROTECT, related_name="+"
    )
    item = models.ForeignKey("cya_checklists.Item", on_delete=models.PROTECT, related_name="+")
    definition = models.ForeignKey(
        "cya_checklists.ChecklistItemDef", on_delete=models.PROTECT, related_name="+"
    )
    kit_instance = models.ForeignKey(
        "cya_checklists.KitInstance", null=True, on_delete=models.PROTECT, related_name="+"
    )
    # MariaDB allows duplicate NULLs in unique keys; this explicit key distinguishes
    # non-kit items (all-zero UUID) from every physical kit instance.
    instance_key = models.CharField(max_length=36, db_collation="ascii_bin")
    required_after = models.DateTimeField()
    scope_revision = models.PositiveIntegerField()

    class Meta:
        db_table = "verification_scope_row"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="verscope_org_id"),
            models.UniqueConstraint(
                fields=["deficiency", "scope_revision", "item", "instance_key"],
                name="verscope_revision_item_instance",
            ),
            models.CheckConstraint(
                condition=models.Q(scope_revision__gte=1), name="verscope_rev_min"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    instance_key__regex=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
                ),
                name="verscope_instance_key",
            ),
        ]

    def __str__(self):
        return f"VerificationScopeRow {self.deficiency_id}#{self.scope_revision}/{self.item_id}"


class DeficiencyScopeExpansion(Evidence):
    """Append-only items a supervisor added to a deficiency's verification scope (issue 156 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/156)).

    Written with a SCOPE_EXPANDED or VERIFICATION_ADDED event by the same command. Every later
    correction's scope also includes these items, with the physical kit instance named here while
    it is still active on the vehicle (VER-3, amendment 1.22.0).
    """

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    deficiency = models.ForeignKey(Deficiency, on_delete=models.PROTECT, related_name="+")
    event = models.ForeignKey("DeficiencyEvent", on_delete=models.PROTECT, related_name="+")
    item = models.ForeignKey("cya_checklists.Item", on_delete=models.PROTECT, related_name="+")
    definition = models.ForeignKey(
        "cya_checklists.ChecklistItemDef", on_delete=models.PROTECT, related_name="+"
    )
    kit_instance = models.ForeignKey(
        "cya_checklists.KitInstance", null=True, on_delete=models.PROTECT, related_name="+"
    )
    instance_key = models.CharField(max_length=36, db_collation="ascii_bin")

    class Meta:
        db_table = "deficiency_scope_expansion"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="defexpansion_org_id"),
            models.UniqueConstraint(
                fields=["event", "item", "instance_key"], name="defexpansion_event_item_instance"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    instance_key__regex=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
                ),
                name="defexpansion_instance_key",
            ),
        ]

    def __str__(self):
        return f"DeficiencyScopeExpansion {self.deficiency_id}/{self.item_id}"


class CheckSuspension(Evidence):
    """Append-only START and END of a supervisor suspension of one routine check (SPEC 8.9).

    A START names one item (and physical kit instance for a kit-defined item) of the deficiency's
    vehicle and is written with the CHECKS_SUSPENDED event of the same command. Its END is written
    by the event that closes or cancels the deficiency, which lists the START in
    suspensions_ended; prior_suspension then names the START, once. Nothing resumes a suspension:
    a reopened deficiency needs a new START (issue 13
    (https://github.com/JustApu/CheckYourAmbulance/issues/13)).
    """

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    deficiency = models.ForeignKey(Deficiency, on_delete=models.PROTECT, related_name="+")
    vehicle = models.ForeignKey(
        "cya_checklists.Vehicle", on_delete=models.PROTECT, related_name="+"
    )
    item = models.ForeignKey("cya_checklists.Item", on_delete=models.PROTECT, related_name="+")
    definition = models.ForeignKey(
        "cya_checklists.ChecklistItemDef", on_delete=models.PROTECT, related_name="+"
    )
    kit_instance = models.ForeignKey(
        "cya_checklists.KitInstance", null=True, on_delete=models.PROTECT, related_name="+"
    )
    instance_key = models.CharField(max_length=36, db_collation="ascii_bin")
    action = models.CharField(max_length=5)
    # START: the CHECKS_SUSPENDED event of this command. END: the closing event that lists it.
    deficiency_event = models.ForeignKey(
        "DeficiencyEvent", on_delete=models.PROTECT, related_name="+"
    )
    prior_suspension = models.OneToOneField(
        "self", null=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "check_suspension"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="checksusp_org_id"),
            models.UniqueConstraint(
                fields=["deficiency_event", "item", "instance_key"],
                name="checksusp_event_item_instance",
            ),
            models.CheckConstraint(
                condition=models.Q(action="START", prior_suspension__isnull=True)
                | models.Q(action="END", prior_suspension__isnull=False),
                name="checksusp_action",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    instance_key__regex=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
                ),
                name="checksusp_instance_key",
            ),
        ]

    def __str__(self):
        return f"CheckSuspension {self.action} {self.deficiency_id}/{self.item_id}"


class DeficiencyEvent(Evidence):
    """Append-only lifecycle event; status is the latest event's to_status (SPEC 8.2)."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    deficiency = models.ForeignKey(Deficiency, on_delete=models.PROTECT, related_name="+")
    sequence = models.PositiveBigIntegerField()
    predecessor_event = models.OneToOneField(
        "self", null=True, on_delete=models.PROTECT, related_name="+"
    )
    event_type = models.CharField(max_length=30)
    from_status = models.CharField(max_length=30, null=True)  # noqa: DJ001
    to_status = models.CharField(max_length=30, null=True)  # noqa: DJ001
    explanation = models.CharField(max_length=MAX_TEXT, null=True)  # noqa: DJ001
    assignee = models.ForeignKey(
        "cya_accounts.User", null=True, on_delete=models.PROTECT, related_name="+"
    )
    deadline = models.DateTimeField(null=True)
    proposed_deadline = models.DateTimeField(null=True)
    follow_up_at = models.DateTimeField(null=True)
    # An extension decision answers exactly one request; the outcome says what it decided.
    decision = models.CharField(max_length=14, null=True)  # noqa: DJ001
    request_event = models.OneToOneField(
        "self", null=True, on_delete=models.PROTECT, related_name="+"
    )
    acknowledgement_until = models.DateTimeField(null=True)
    action_plan = models.CharField(max_length=MAX_TEXT, null=True)  # noqa: DJ001
    # The check_suspension START ids a closing event ended (SUS-3, SUS-4), sorted; empty otherwise.
    suspensions_ended = models.JSONField(default=list)
    correction_ref = models.CharField(max_length=MAX_TEXT, null=True)  # noqa: DJ001
    # A re-grade names the level revision it leaves and the one it sets (DEF-8); no other type does.
    previous_severity_version = models.ForeignKey(
        DeficiencySeverity, null=True, on_delete=models.PROTECT, related_name="+"
    )
    severity_version = models.ForeignKey(
        DeficiencySeverity, null=True, on_delete=models.PROTECT, related_name="+"
    )
    # The verification check that caused a VERIFICATION_CLOSED or VERIFICATION_FAILED event, written
    # by the same command, or the root of the verification chain whose lost Pass caused a
    # VERIFICATION_REOPENED event; no other type names one.
    verification_check = models.ForeignKey(
        "cya_checks.CheckRecord", null=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "deficiency_event"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="defevent_org_id"),
            models.UniqueConstraint(fields=["deficiency", "sequence"], name="defevent_sequence"),
            # Target of the predecessor's same-deficiency foreign key.
            models.UniqueConstraint(fields=["id", "deficiency"], name="defevent_id_deficiency"),
            models.CheckConstraint(condition=models.Q(sequence__gte=1), name="defevent_seq_min"),
            models.CheckConstraint(
                condition=models.Q(event_type__in=EVENT_TYPES), name="defevent_type"
            ),
            models.CheckConstraint(
                condition=models.Q(from_status__isnull=True) | models.Q(from_status__in=STATUSES),
                name="defevent_from_status",
            ),
            models.CheckConstraint(
                condition=models.Q(to_status__isnull=True) | models.Q(to_status__in=STATUSES),
                name="defevent_to_status",
            ),
            models.CheckConstraint(
                condition=models.Q(explanation__isnull=True) | non_blank("explanation"),
                name="defevent_explanation",
            ),
            models.CheckConstraint(
                condition=models.Q(predecessor_event__isnull=False) | models.Q(sequence=1),
                name="defevent_first",
            ),
            # REPORTED is the creation event: first, Open, and it carries only an optional
            # assignee and deadline (assignment at report time is a SPEC 3.3 privilege).
            # The correction effects name the check chain and explain themselves; they carry no
            # assignment, deadline or plan fields.
            models.CheckConstraint(
                condition=models.Q(
                    event_type__in=(
                        "REPORTED",
                        "REOPENED",
                        "CORRECTION_NOTICE",
                        "SEVERITY_CHANGED",
                        *ASSIGNMENT_EVENT_TYPES,
                        *DEPENDENCY_EVENT_TYPES,
                        *NOTE_EVENT_TYPES,
                        *EXTENSION_EVENT_TYPES,
                        *ACKNOWLEDGEMENT_EVENT_TYPES,
                        *ADMINISTRATOR_CLOSURE_EVENT_TYPES,
                        *EMPTY_SCOPE_CLOSURE_EVENT_TYPES,
                        "CORRECTION_RECORDED",
                        *VERIFICATION_EVENT_TYPES,
                        *VERIFICATION_REOPEN_EVENT_TYPES,
                        *SCOPE_EVENT_TYPES,
                        *SUSPENSION_EVENT_TYPES,
                    )
                )
                | models.Q(
                    event_type="REPORTED_IN_ERROR",
                    to_status=REPORTED_IN_ERROR,
                    from_status__in=(OPEN, IN_PROGRESS, PENDING_VERIFICATION),
                ),
                name="defevent_error_shape",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    event_type__in=(
                        "REPORTED",
                        "SEVERITY_CHANGED",
                        *ASSIGNMENT_EVENT_TYPES,
                        *DEPENDENCY_EVENT_TYPES,
                        *NOTE_EVENT_TYPES,
                        *EXTENSION_EVENT_TYPES,
                        *ACKNOWLEDGEMENT_EVENT_TYPES,
                        *ADMINISTRATOR_CLOSURE_EVENT_TYPES,
                        *EMPTY_SCOPE_CLOSURE_EVENT_TYPES,
                        "CORRECTION_RECORDED",
                        *VERIFICATION_EVENT_TYPES,
                        *VERIFICATION_REOPEN_EVENT_TYPES,
                        *SCOPE_EVENT_TYPES,
                        *SUSPENSION_EVENT_TYPES,
                    )
                )
                | (
                    models.Q(
                        explanation__isnull=False,
                        correction_ref__isnull=False,
                        assignee__isnull=True,
                        deadline__isnull=True,
                        proposed_deadline__isnull=True,
                        follow_up_at__isnull=True,
                        acknowledgement_until__isnull=True,
                        action_plan__isnull=True,
                        from_status__isnull=False,
                        to_status__isnull=False,
                    )
                    & (
                        models.Q(event_type="REPORTED_IN_ERROR")
                        | models.Q(
                            event_type="CORRECTION_NOTICE", to_status=models.F("from_status")
                        )
                        | models.Q(
                            event_type="REOPENED",
                            from_status=REPORTED_IN_ERROR,
                            to_status=OPEN,
                        )
                    )
                ),
                name="defevent_effect_shape",
            ),
            # A re-grade keeps the status, explains itself, names two different level revisions and
            # is made only while the deficiency is still being worked; it carries nothing else.
            models.CheckConstraint(
                condition=~models.Q(event_type="SEVERITY_CHANGED")
                | (
                    models.Q(
                        explanation__isnull=False,
                        correction_ref__isnull=True,
                        assignee__isnull=True,
                        deadline__isnull=True,
                        proposed_deadline__isnull=True,
                        follow_up_at__isnull=True,
                        acknowledgement_until__isnull=True,
                        action_plan__isnull=True,
                        previous_severity_version__isnull=False,
                        severity_version__isnull=False,
                        from_status__in=ACTIVE_STATUSES,
                        to_status__isnull=False,
                        to_status=models.F("from_status"),
                    )
                    & ~models.Q(previous_severity_version=models.F("severity_version"))
                ),
                name="defevent_severity_shape",
            ),
            models.CheckConstraint(
                condition=models.Q(event_type="SEVERITY_CHANGED")
                | models.Q(previous_severity_version__isnull=True, severity_version__isnull=True),
                name="defevent_severity_only_regrade",
            ),
            # Assignment, a deadline set or revised, and the start of work (DEF-4) are made only
            # while the deficiency is still being worked. Assignment carries the assignee only,
            # a deadline carries the deadline only, and starting work carries neither and is the
            # one Open to In Progress arrow; none names a check chain, a plan or a re-grade level.
            models.CheckConstraint(
                condition=~models.Q(event_type__in=ASSIGNMENT_EVENT_TYPES)
                | (
                    models.Q(
                        correction_ref__isnull=True,
                        proposed_deadline__isnull=True,
                        follow_up_at__isnull=True,
                        acknowledgement_until__isnull=True,
                        action_plan__isnull=True,
                        from_status__in=ACTIVE_STATUSES,
                        to_status__isnull=False,
                    )
                    & (
                        models.Q(
                            event_type="ASSIGNED",
                            assignee__isnull=False,
                            deadline__isnull=True,
                            to_status=models.F("from_status"),
                        )
                        | models.Q(
                            event_type="DEADLINE_SET",
                            assignee__isnull=True,
                            deadline__isnull=False,
                            to_status=models.F("from_status"),
                        )
                        | models.Q(
                            event_type="WORK_STARTED",
                            assignee__isnull=True,
                            deadline__isnull=True,
                            from_status=OPEN,
                            to_status=IN_PROGRESS,
                        )
                    )
                ),
                name="defevent_assignment_shape",
            ),
            # Awaiting External Dependency (SPEC 8.5) and progress notes (SPEC 8.1). Entering and
            # revising explain themselves and may carry a follow-up instant; ending carries
            # nothing and returns to Open or In Progress; a note keeps the status and carries its
            # text in explanation. None names an assignee, a deadline, a plan or a check chain.
            models.CheckConstraint(
                condition=~models.Q(event_type__in=(*DEPENDENCY_EVENT_TYPES, *NOTE_EVENT_TYPES))
                | (
                    models.Q(
                        correction_ref__isnull=True,
                        assignee__isnull=True,
                        deadline__isnull=True,
                        proposed_deadline__isnull=True,
                        acknowledgement_until__isnull=True,
                        action_plan__isnull=True,
                        from_status__isnull=False,
                        to_status__isnull=False,
                    )
                    & (
                        models.Q(
                            event_type="DEPENDENCY_ENTERED",
                            explanation__isnull=False,
                            from_status__in=(OPEN, IN_PROGRESS),
                            to_status=AWAITING_EXTERNAL_DEPENDENCY,
                        )
                        | models.Q(
                            event_type="DEPENDENCY_REVISED",
                            explanation__isnull=False,
                            from_status=AWAITING_EXTERNAL_DEPENDENCY,
                            to_status=AWAITING_EXTERNAL_DEPENDENCY,
                        )
                        | models.Q(
                            event_type="DEPENDENCY_ENDED",
                            explanation__isnull=True,
                            follow_up_at__isnull=True,
                            from_status=AWAITING_EXTERNAL_DEPENDENCY,
                            to_status__in=(OPEN, IN_PROGRESS),
                        )
                        | models.Q(
                            event_type="PROGRESS_NOTE",
                            explanation__isnull=False,
                            follow_up_at__isnull=True,
                            from_status__in=ACTIVE_STATUSES,
                            to_status=models.F("from_status"),
                        )
                    )
                ),
                name="defevent_dependency_shape",
            ),
            # A deadline extension (SPEC DEF-5): a request explains itself and carries the date
            # asked for; a decision explains itself, names its one request and an outcome, and
            # carries the date that governs unless it denies. Both keep the status of a deficiency
            # that is still being worked and name no assignee, plan, follow-up or check chain.
            models.CheckConstraint(
                condition=~models.Q(event_type__in=EXTENSION_EVENT_TYPES)
                | (
                    models.Q(
                        correction_ref__isnull=True,
                        assignee__isnull=True,
                        follow_up_at__isnull=True,
                        acknowledgement_until__isnull=True,
                        action_plan__isnull=True,
                        explanation__isnull=False,
                        from_status__in=ACTIVE_STATUSES,
                        to_status=models.F("from_status"),
                    )
                    & (
                        models.Q(
                            event_type="EXTENSION_REQUESTED",
                            proposed_deadline__isnull=False,
                            deadline__isnull=True,
                            decision__isnull=True,
                            request_event__isnull=True,
                        )
                        | (
                            models.Q(
                                event_type="EXTENSION_DECIDED",
                                proposed_deadline__isnull=True,
                                request_event__isnull=False,
                                decision__in=DECISIONS,
                            )
                            & (
                                models.Q(decision="DENIED", deadline__isnull=True)
                                | models.Q(
                                    decision__in=("APPROVED", "DIFFERENT_DATE"),
                                    deadline__isnull=False,
                                )
                            )
                        )
                    )
                ),
                name="defevent_extension_shape",
            ),
            models.CheckConstraint(
                condition=models.Q(event_type="EXTENSION_DECIDED")
                | models.Q(decision__isnull=True, request_event__isnull=True),
                name="defevent_extension_only_decision",
            ),
            models.CheckConstraint(
                condition=~models.Q(event_type__in=ACKNOWLEDGEMENT_EVENT_TYPES)
                | models.Q(
                    explanation__isnull=False,
                    action_plan__isnull=False,
                    acknowledgement_until__isnull=False,
                    assignee__isnull=True,
                    deadline__isnull=True,
                    proposed_deadline__isnull=True,
                    follow_up_at__isnull=True,
                    decision__isnull=True,
                    request_event__isnull=True,
                    correction_ref__isnull=True,
                    previous_severity_version__isnull=True,
                    severity_version__isnull=True,
                    from_status__in=ACTIVE_STATUSES,
                    to_status=models.F("from_status"),
                )
                & non_blank("action_plan"),
                name="defevent_acknowledgement_shape",
            ),
            models.CheckConstraint(
                condition=~models.Q(event_type__in=EXPLICIT_CLOSURE_EVENT_TYPES)
                | (
                    models.Q(
                        explanation__isnull=False,
                        assignee__isnull=True,
                        deadline__isnull=True,
                        proposed_deadline__isnull=True,
                        follow_up_at__isnull=True,
                        decision__isnull=True,
                        request_event__isnull=True,
                        acknowledgement_until__isnull=True,
                        action_plan__isnull=True,
                        correction_ref__isnull=True,
                        previous_severity_version__isnull=True,
                        severity_version__isnull=True,
                    )
                    & non_blank("explanation")
                    & (
                        models.Q(
                            event_type="ADMINISTRATOR_CLOSED",
                            from_status__in=ACTIVE_STATUSES,
                            to_status=CLOSED_ADMINISTRATOR,
                        )
                        | models.Q(
                            event_type="EMPTY_SCOPE_CLOSED",
                            from_status__in=EMPTY_SCOPE_CLOSURE_STATUSES,
                            to_status=CLOSED_UNVERIFIABLE,
                        )
                        | models.Q(
                            event_type="ADMINISTRATOR_REOPENED",
                            from_status__in=(CLOSED_ADMINISTRATOR, CLOSED_UNVERIFIABLE),
                            to_status=OPEN,
                        )
                    )
                ),
                name="defevent_admin_close_shape",
            ),
            # Only a closing event ends suspensions (SUS-3, SUS-4); the insert trigger requires its
            # list to be exactly the deficiency's suspensions still in force.
            models.CheckConstraint(
                condition=models.Q(suspensions_ended=[])
                | models.Q(event_type__in=CLOSING_EVENT_TYPES),
                name="defevent_suspensions_closing",
            ),
            models.CheckConstraint(
                condition=~models.Q(event_type="REPORTED")
                | (
                    models.Q(
                        sequence=1,
                        from_status__isnull=True,
                        to_status=OPEN,
                        explanation__isnull=True,
                        proposed_deadline__isnull=True,
                        follow_up_at__isnull=True,
                        acknowledgement_until__isnull=True,
                        action_plan__isnull=True,
                        correction_ref__isnull=True,
                    )
                ),
                name="defevent_reported_shape",
            ),
            models.CheckConstraint(
                condition=~models.Q(event_type="CORRECTION_RECORDED")
                | models.Q(
                    from_status=IN_PROGRESS,
                    to_status=PENDING_VERIFICATION,
                    explanation__isnull=True,
                    assignee__isnull=True,
                    deadline__isnull=True,
                    proposed_deadline__isnull=True,
                    follow_up_at__isnull=True,
                    decision__isnull=True,
                    request_event__isnull=True,
                    acknowledgement_until__isnull=True,
                    action_plan__isnull=True,
                    correction_ref__isnull=True,
                    previous_severity_version__isnull=True,
                    severity_version__isnull=True,
                    suspensions_ended=[],
                ),
                name="defevent_correction_shape",
            ),
            # A verification event (VER-2, VER-4, VER-5) leaves Pending Verification for Closed
            # Verified or back to In Progress, names the check that caused it and carries nothing
            # else; the check, its Pass or Fail and its scope are verified by the insert trigger.
            models.CheckConstraint(
                condition=~models.Q(event_type__in=VERIFICATION_EVENT_TYPES)
                | (
                    models.Q(
                        verification_check__isnull=False,
                        from_status=PENDING_VERIFICATION,
                        explanation__isnull=True,
                        assignee__isnull=True,
                        deadline__isnull=True,
                        proposed_deadline__isnull=True,
                        follow_up_at__isnull=True,
                        decision__isnull=True,
                        request_event__isnull=True,
                        acknowledgement_until__isnull=True,
                        action_plan__isnull=True,
                        correction_ref__isnull=True,
                        previous_severity_version__isnull=True,
                        severity_version__isnull=True,
                    )
                    & (
                        models.Q(event_type="VERIFICATION_CLOSED", to_status=CLOSED_VERIFIED)
                        | models.Q(event_type="VERIFICATION_FAILED", to_status=IN_PROGRESS)
                    )
                ),
                name="defevent_verification_shape",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    event_type__in=(*VERIFICATION_EVENT_TYPES, *VERIFICATION_REOPEN_EVENT_TYPES)
                )
                | models.Q(verification_check__isnull=True),
                name="defevent_verification_only",
            ),
            # A verification reopening (INV-4) takes a Closed Verified deficiency back to Open,
            # explains itself and names the verification chain that lost its Pass; it carries
            # nothing else. The trigger checks that chain, its command and the lost support.
            models.CheckConstraint(
                condition=~models.Q(event_type__in=VERIFICATION_REOPEN_EVENT_TYPES)
                | models.Q(
                    verification_check__isnull=False,
                    from_status=CLOSED_VERIFIED,
                    to_status=OPEN,
                    explanation__isnull=False,
                    assignee__isnull=True,
                    deadline__isnull=True,
                    proposed_deadline__isnull=True,
                    follow_up_at__isnull=True,
                    decision__isnull=True,
                    request_event__isnull=True,
                    acknowledgement_until__isnull=True,
                    action_plan__isnull=True,
                    correction_ref__isnull=True,
                    previous_severity_version__isnull=True,
                    severity_version__isnull=True,
                    suspensions_ended=[],
                ),
                name="defevent_verification_reopen_shape",
            ),
            # Supervisor scope expansion (VER-3, VER-4) keeps a Pending Verification deficiency
            # pending; added verification after closure (SPEC 8.7) reopens a Closed Verified one
            # with the new resolution deadline. Both explain themselves and carry nothing else;
            # their items are deficiency_scope_expansion rows written by the same command.
            models.CheckConstraint(
                condition=~models.Q(event_type__in=SCOPE_EVENT_TYPES)
                | (
                    models.Q(
                        explanation__isnull=False,
                        assignee__isnull=True,
                        proposed_deadline__isnull=True,
                        follow_up_at__isnull=True,
                        decision__isnull=True,
                        request_event__isnull=True,
                        acknowledgement_until__isnull=True,
                        action_plan__isnull=True,
                        correction_ref__isnull=True,
                        previous_severity_version__isnull=True,
                        severity_version__isnull=True,
                        verification_check__isnull=True,
                        suspensions_ended=[],
                    )
                    & (
                        models.Q(
                            event_type="SCOPE_EXPANDED",
                            from_status=PENDING_VERIFICATION,
                            to_status=PENDING_VERIFICATION,
                            deadline__isnull=True,
                        )
                        | models.Q(
                            event_type="VERIFICATION_ADDED",
                            from_status=CLOSED_VERIFIED,
                            to_status=OPEN,
                            deadline__isnull=False,
                        )
                    )
                ),
                name="defevent_scope_shape",
            ),
            # A supervisor check suspension (SPEC 8.9) keeps the status of a deficiency still being
            # worked and may explain itself; it carries nothing else. Its items are check_suspension
            # START rows written by the same command.
            models.CheckConstraint(
                condition=~models.Q(event_type__in=SUSPENSION_EVENT_TYPES)
                | models.Q(
                    assignee__isnull=True,
                    deadline__isnull=True,
                    proposed_deadline__isnull=True,
                    follow_up_at__isnull=True,
                    decision__isnull=True,
                    request_event__isnull=True,
                    acknowledgement_until__isnull=True,
                    action_plan__isnull=True,
                    correction_ref__isnull=True,
                    previous_severity_version__isnull=True,
                    severity_version__isnull=True,
                    verification_check__isnull=True,
                    suspensions_ended=[],
                    from_status__in=ACTIVE_STATUSES,
                    to_status=models.F("from_status"),
                ),
                name="defevent_suspension_shape",
            ),
        ]

    def __str__(self):
        return f"DeficiencyEvent {self.deficiency_id}#{self.sequence}"


class DeficiencyEvidence(Evidence):
    """One thing supporting a deficiency: a check now, a kit inspection item or note later."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    deficiency = models.ForeignKey(Deficiency, on_delete=models.PROTECT, related_name="+")
    check_record = models.ForeignKey(
        "cya_checks.CheckRecord",
        null=True,
        db_column="check_id",
        on_delete=models.PROTECT,
        related_name="+",
    )
    note = models.CharField(max_length=MAX_TEXT, null=True)  # noqa: DJ001

    class Meta:
        db_table = "deficiency_evidence"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="defevidence_org_id"),
            models.UniqueConstraint(
                fields=["deficiency", "check_record"], name="defevidence_check"
            ),
            # Exactly one source. kit_inspection_item_id joins this CHECK with the sealed-kit work.
            models.CheckConstraint(
                condition=(
                    models.Q(check_record__isnull=False, note__isnull=True)
                    | (
                        models.Q(check_record__isnull=True, note__isnull=False)
                        & models.Q(note__regex=r"\S")
                    )
                ),
                name="defevidence_one_source",
            ),
        ]

    def __str__(self):
        return f"DeficiencyEvidence {self.deficiency_id}"


class DeficiencyLink(Evidence):
    """An informational link between two deficiencies, stored low id first (SPEC DEF-2)."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    lower = models.ForeignKey(Deficiency, on_delete=models.PROTECT, related_name="+")
    upper = models.ForeignKey(Deficiency, on_delete=models.PROTECT, related_name="+")
    link_type = models.CharField(max_length=10)
    # The pair's previous link row: null for its first ADD, else the row this one follows.
    prior_link_event = models.OneToOneField(
        "self", null=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "deficiency_link"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="deflink_org_id"),
            models.CheckConstraint(
                condition=models.Q(lower__lt=models.F("upper")), name="deflink_canonical_order"
            ),
            models.CheckConstraint(
                condition=models.Q(link_type__in=LINK_TYPES), name="deflink_type"
            ),
            models.CheckConstraint(
                condition=~models.Q(link_type="REMOVE") | models.Q(prior_link_event__isnull=False),
                name="deflink_remove_has_prior",
            ),
        ]

    def __str__(self):
        return f"DeficiencyLink {self.lower_id}/{self.upper_id} {self.link_type}"


class DeficiencyProjection(models.Model):
    """Disposable cache of the latest event; rebuilt from the events, never authoritative."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    deficiency = models.OneToOneField(Deficiency, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=30)
    assignee = models.ForeignKey(
        "cya_accounts.User", null=True, on_delete=models.PROTECT, related_name="+"
    )
    deadline = models.DateTimeField(null=True)
    # The level revision now in force: the header's, or the newest re-grade's (DEF-8).
    severity_version = models.ForeignKey(
        DeficiencySeverity, on_delete=models.PROTECT, related_name="+"
    )
    last_applied_event = models.ForeignKey(
        DeficiencyEvent, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "deficiency_projection"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="defproj_org_id"),
            models.CheckConstraint(condition=models.Q(status__in=STATUSES), name="defproj_status"),
        ]

    def __str__(self):
        return f"DeficiencyProjection {self.deficiency_id}"
