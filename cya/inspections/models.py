"""Session headers, participant and event history, and disposable state (SPEC §6)."""

from django.db import models

from cya.accounts.fields import IdentityIDField, new_id
from cya.checklists.models import organization_fk

STATES = ("OPEN", "INCOMPLETE", "CLOSED")
EVENT_TYPES = ("OPEN", "JOIN", "REOPEN", "END_INCOMPLETE", "END_CLOSED", "AUTO_INCOMPLETE")
# The six-hour rule (SPEC 6.3 SES-1) lives here so the command, the projection and the
# migration's SQL guard cannot drift apart.
INACTIVITY_HOURS = 6


class Evidence(models.Model):
    command = models.ForeignKey(
        "cya_core.CommandReceipt", on_delete=models.PROTECT, related_name="+"
    )
    recorded_at = models.DateTimeField()
    actor_user = models.ForeignKey("cya_accounts.User", on_delete=models.PROTECT, related_name="+")
    actor_roles_at_time = models.JSONField(default=list)

    class Meta:
        abstract = True


class InspectionSession(Evidence):
    """Immutable creation facts. Compliance never derives from this row or its projection."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    vehicle = models.ForeignKey(
        "cya_checklists.Vehicle", on_delete=models.PROTECT, related_name="+"
    )
    checklist_version = models.ForeignKey(
        "cya_checklists.ChecklistVersion", on_delete=models.PROTECT, related_name="+"
    )
    opened_at = models.DateTimeField()

    class Meta:
        db_table = "inspection_session"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="session_org_id")
        ]

    def __str__(self):
        return f"InspectionSession {self.pk}"


class SessionParticipant(Evidence):
    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    session = models.ForeignKey(InspectionSession, on_delete=models.PROTECT, related_name="+")
    user = models.ForeignKey("cya_accounts.User", on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "session_participant"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="participant_org_id"),
            models.UniqueConstraint(fields=["session", "user"], name="participant_session_user"),
        ]

    def __str__(self):
        return f"SessionParticipant {self.pk}"


class SessionEvent(Evidence):
    """Linear event stream. JOIN and END do not count as persisted activity.

    AUTO_INCOMPLETE is written by the auto-close job (a service receipt, so no actor user). Its
    effective instant is the last activity plus six hours, whatever instant the job ran at.
    """

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    session = models.ForeignKey(InspectionSession, on_delete=models.PROTECT, related_name="+")
    actor_user = models.ForeignKey(
        "cya_accounts.User", null=True, on_delete=models.PROTECT, related_name="+"
    )
    sequence = models.PositiveBigIntegerField()
    prior_event = models.OneToOneField(
        "self", null=True, on_delete=models.PROTECT, related_name="+"
    )
    event_type = models.CharField(max_length=20)
    effective_at = models.DateTimeField()
    persisted_activity_command = models.ForeignKey(
        "cya_core.CommandReceipt", null=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "session_event"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="session_event_org_id"),
            models.UniqueConstraint(fields=["id", "session"], name="session_event_anchor"),
            models.UniqueConstraint(fields=["session", "sequence"], name="session_event_sequence"),
            models.CheckConstraint(
                condition=models.Q(sequence__gte=1), name="session_event_seq_min"
            ),
            models.CheckConstraint(
                condition=models.Q(event_type__in=EVENT_TYPES), name="session_event_type"
            ),
            models.CheckConstraint(
                condition=models.Q(prior_event__isnull=False) | models.Q(sequence=1),
                name="session_event_first",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        event_type__in=["OPEN", "REOPEN"], persisted_activity_command__isnull=False
                    )
                    | models.Q(
                        event_type__in=["JOIN", "END_INCOMPLETE", "END_CLOSED", "AUTO_INCOMPLETE"],
                        persisted_activity_command__isnull=True,
                    )
                ),
                name="session_event_activity_shape",
            ),
            # Only the job closes automatically, and only a person does anything else. Each
            # side states its null test: a SQL CHECK passes on NULL.
            models.CheckConstraint(
                condition=(
                    models.Q(event_type="AUTO_INCOMPLETE", actor_user__isnull=True)
                    | (~models.Q(event_type="AUTO_INCOMPLETE") & models.Q(actor_user__isnull=False))
                ),
                name="session_event_actor_shape",
            ),
        ]

    def __str__(self):
        return f"SessionEvent {self.pk}"


class SessionProjection(models.Model):
    """Rebuildable cache; SessionEvent is the authority for state and activity."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    session = models.OneToOneField(InspectionSession, on_delete=models.PROTECT, related_name="+")
    state = models.CharField(max_length=10)
    last_activity_at = models.DateTimeField()
    last_activity_command = models.ForeignKey(
        "cya_core.CommandReceipt", on_delete=models.PROTECT, related_name="+"
    )
    last_applied_event = models.ForeignKey(SessionEvent, on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "session_projection"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="sess_projection_org_id"),
            models.CheckConstraint(
                condition=models.Q(state__in=STATES), name="sess_projection_state"
            ),
        ]

    def __str__(self):
        return f"SessionProjection {self.pk}"
