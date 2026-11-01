"""Notification outbox, delivery attempts, read marks, preferences and mandatory policy (SPEC 13).

notification_outbox, notification_delivery_event and notification_read are append-only (A);
notification_pref and notification_policy are mutable (M) with audited writers. The outbox row is
written in the same transaction as the event that causes it, so the intent commits or rolls back
with that event (NOT-5). One row per source event, channel and recipient. Migration 0002 installs
the organization-pair foreign keys, UUID checks, append-only triggers and receipt attribution
guards. Writers go through cya.notifications.enqueue, .commands and .delivery.
"""

from django.db import models

from cya.accounts.fields import IdentityIDField, new_id
from cya.checklists.models import non_blank, organization_fk

CHANNELS = ("IN_APP", "EMAIL")
OUTCOMES = ("SENT", "RETRY", "GAVE_UP")
ERROR_CODES = ("TRANSIENT", "REJECTED", "INVALID_ADDRESS", "UNAVAILABLE", "UNKNOWN")
EVENT_TYPE_PATTERN = r"^[a-z][a-z0-9_.]{0,63}$"
TEMPLATE_PATTERN = r"^[a-z][a-z0-9_.]{0,63}$"


class NotificationOutbox(models.Model):
    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    command = models.ForeignKey(
        "cya_core.CommandReceipt", on_delete=models.PROTECT, related_name="+"
    )
    recorded_at = models.DateTimeField()
    # The source event is a typed reference by name and identifier, not a database foreign key,
    # so any later issue's event table can feed the outbox without a schema change here.
    source_event_type = models.CharField(max_length=64)
    source_event_id = IdentityIDField()
    event_type = models.CharField(max_length=64)
    channel = models.CharField(max_length=10)
    recipient_user = models.ForeignKey(
        "cya_accounts.User", on_delete=models.PROTECT, related_name="+"
    )
    template_key = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    deep_link = models.CharField(max_length=300)
    mandatory = models.BooleanField(default=False)

    class Meta:
        db_table = "notification_outbox"
        indexes = [
            models.Index(
                fields=["recipient_user", "channel", "recorded_at"], name="notif_outbox_inbox"
            ),
            models.Index(fields=["channel", "recorded_at"], name="notif_outbox_pending"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="notif_outbox_org_id"),
            models.UniqueConstraint(
                fields=["source_event_type", "source_event_id", "channel", "recipient_user"],
                name="notif_outbox_dedupe",
            ),
            models.CheckConstraint(
                condition=models.Q(channel__in=CHANNELS), name="notif_outbox_channel"
            ),
            models.CheckConstraint(
                condition=models.Q(event_type__regex=EVENT_TYPE_PATTERN), name="notif_outbox_event"
            ),
            models.CheckConstraint(
                condition=models.Q(template_key__regex=TEMPLATE_PATTERN),
                name="notif_outbox_template",
            ),
            models.CheckConstraint(
                condition=non_blank("source_event_type"), name="notif_outbox_source_type"
            ),
            models.CheckConstraint(
                condition=models.Q(deep_link__regex=r"^/[A-Za-z0-9._~/?=&%-]*$")
                & ~models.Q(deep_link__startswith="//"),
                name="notif_outbox_link",
            ),
        ]

    def __str__(self):
        return f"NotificationOutbox {self.pk}"


class NotificationDeliveryEvent(models.Model):
    """One delivery attempt, written by a worker command (job identity in its receipt)."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    command = models.ForeignKey(
        "cya_core.CommandReceipt", on_delete=models.PROTECT, related_name="+"
    )
    outbox = models.ForeignKey(NotificationOutbox, on_delete=models.PROTECT, related_name="+")
    attempt_number = models.PositiveSmallIntegerField()
    outcome = models.CharField(max_length=10)
    error_code = models.CharField(max_length=20, null=True)  # noqa: DJ001
    recorded_at = models.DateTimeField()

    class Meta:
        db_table = "notification_delivery_event"
        constraints = [
            models.UniqueConstraint(
                fields=["outbox", "attempt_number"], name="notif_delivery_attempt"
            ),
            models.CheckConstraint(
                condition=models.Q(attempt_number__gte=1), name="notif_delivery_number"
            ),
            models.CheckConstraint(
                condition=models.Q(outcome__in=OUTCOMES), name="notif_delivery_outcome"
            ),
            # SENT carries no error; every other outcome carries a fixed safe code.
            models.CheckConstraint(
                condition=(
                    models.Q(outcome="SENT", error_code__isnull=True)
                    | (
                        ~models.Q(outcome="SENT")
                        & models.Q(error_code__isnull=False, error_code__in=ERROR_CODES)
                    )
                ),
                name="notif_delivery_error",
            ),
        ]

    def __str__(self):
        return f"NotificationDeliveryEvent {self.pk}"


class NotificationRead(models.Model):
    """The recipient marked one inbox item read; at most one mark per item."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    command = models.ForeignKey(
        "cya_core.CommandReceipt", on_delete=models.PROTECT, related_name="+"
    )
    outbox = models.OneToOneField(NotificationOutbox, on_delete=models.PROTECT, related_name="+")
    # The recipient who marked it read; named like every other receipt-attributed actor column.
    actor_user = models.ForeignKey("cya_accounts.User", on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField()

    class Meta:
        db_table = "notification_read"

    def __str__(self):
        return f"NotificationRead {self.pk}"


class NotificationPref(models.Model):
    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    user = models.ForeignKey("cya_accounts.User", on_delete=models.PROTECT, related_name="+")
    event_type = models.CharField(max_length=64)
    channel = models.CharField(max_length=10)
    enabled = models.BooleanField()

    class Meta:
        db_table = "notification_pref"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "event_type", "channel"], name="notif_pref_unique"
            ),
            models.CheckConstraint(
                condition=models.Q(channel__in=CHANNELS), name="notif_pref_channel"
            ),
            models.CheckConstraint(
                condition=models.Q(event_type__regex=EVENT_TYPE_PATTERN), name="notif_pref_event"
            ),
            models.CheckConstraint(
                condition=models.Q(enabled__in=[True, False]), name="notif_pref_bool"
            ),
        ]

    def __str__(self):
        return f"NotificationPref {self.pk}"


class NotificationPolicy(models.Model):
    """Administrator policy: which event/channel pairs are mandatory (NOT-1). No default rows."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    event_type = models.CharField(max_length=64)
    channel = models.CharField(max_length=10)
    mandatory = models.BooleanField()

    class Meta:
        db_table = "notification_policy"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "event_type", "channel"], name="notif_policy_unique"
            ),
            models.CheckConstraint(
                condition=models.Q(channel__in=CHANNELS), name="notif_policy_channel"
            ),
            models.CheckConstraint(
                condition=models.Q(event_type__regex=EVENT_TYPE_PATTERN), name="notif_policy_event"
            ),
            models.CheckConstraint(
                condition=models.Q(mandatory__in=[True, False]), name="notif_policy_bool"
            ),
        ]

    def __str__(self):
        return f"NotificationPolicy {self.pk}"


RECIPIENT_KINDS = (
    "ASSIGNEE",
    "REPORTER",
    "REQUESTER",
    "PARTICIPANTS",
    "SUPERVISORS",
    "ADMINISTRATORS",
)


class NotificationRecipientRule(models.Model):
    """Administrator override of one default recipient row (SPEC 13 NOT-1, NOT-3; issue 14).

    The defaults are documented data in default_recipients.json. A row here turns one recipient
    kind on or off for one event type in this organization; with no row the default applies.
    """

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    event_type = models.CharField(max_length=64)
    recipient = models.CharField(max_length=20)
    enabled = models.BooleanField()

    class Meta:
        db_table = "notification_recipient_rule"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "event_type", "recipient"], name="notif_recipient_unique"
            ),
            models.CheckConstraint(
                condition=models.Q(recipient__in=RECIPIENT_KINDS), name="notif_recipient_kind"
            ),
            models.CheckConstraint(
                condition=models.Q(event_type__regex=EVENT_TYPE_PATTERN),
                name="notif_recipient_event",
            ),
            models.CheckConstraint(
                condition=models.Q(enabled__in=[True, False]), name="notif_recipient_bool"
            ),
        ]

    def __str__(self):
        return f"NotificationRecipientRule {self.pk}"
