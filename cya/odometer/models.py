"""Append-only odometer and engine-hours readings (SPEC 6.4, ODO-1..ODO-5; contracts section 2).

odometer_entry, engine_hours_entry, odometer_obligation_link and odometer_threshold are A
tables: application code never updates or deletes a row, and the database rejects it.
Migration 0002 installs the organization-pair keys, UUID/role checks, the append-only triggers
and the insert triggers that tie a row to its receipt, its previous reading and the anomaly
rule. Writers go through cya.odometer.commands.

Corrections append a new entry in the same root chain. ReadingEvent records each correction or
invalidation decision; neither an earlier value nor a voided obligation link is revived.
"""

from django.db import models

from cya.accounts.fields import IdentityIDField, new_id
from cya.checklists.models import Vehicle, non_blank, organization_fk
from cya.operations.models import MAX_TEXT, Evidence

UNITS = ("mi", "km")
# Smallest safe plausibility defaults (SPEC 6.4 ODO-3, OQ-55) used until an administrator sets one.
DEFAULT_MAX_INCREASE = {"mi": 1000, "km": 1600}
READING_DIGITS = 10  # 9 whole digits and one decimal place
READING_LIMIT = 10_000_000  # exclusive; a reading is below ten million


def reading_field():
    return models.DecimalField(max_digits=READING_DIGITS, decimal_places=1)


class _Reading(Evidence):
    """Columns shared by an odometer and an engine-hours entry (contracts section 2)."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    operation_id = IdentityIDField()
    vehicle = models.ForeignKey(Vehicle, on_delete=models.PROTECT, related_name="+")
    value = reading_field()
    # The previous valid reading this one was compared with; null only for a vehicle's first.
    prior_entry = models.ForeignKey("self", null=True, on_delete=models.PROTECT, related_name="+")
    root_entry = models.ForeignKey("self", on_delete=models.PROTECT, related_name="+")
    predecessor_entry = models.OneToOneField(
        "self", null=True, on_delete=models.PROTECT, related_name="replacement_entry"
    )
    # True only when the reading was anomalous and the author explicitly confirmed it (ODO-3);
    # the confirmer is the entry's own actor_user.
    anomaly_confirmed = models.BooleanField(default=False)
    confirmation_explanation = models.CharField(max_length=MAX_TEXT, null=True)  # noqa: DJ001

    class Meta:
        abstract = True


def _reading_constraints(prefix, units):
    return [
        models.UniqueConstraint(fields=["organization", "id"], name=f"{prefix}_org_id"),
        models.UniqueConstraint(
            fields=["organization", "operation_id"], name=f"{prefix}_org_operation"
        ),
        models.CheckConstraint(
            condition=models.Q(value__gte=0, value__lt=READING_LIMIT), name=f"{prefix}_value"
        ),
        models.CheckConstraint(condition=models.Q(unit__in=units), name=f"{prefix}_unit"),
        models.CheckConstraint(
            condition=(
                models.Q(predecessor_entry__isnull=True, root_entry=models.F("id"))
                | models.Q(predecessor_entry__isnull=False)
            ),
            name=f"{prefix}_root_shape",
        ),
        models.CheckConstraint(
            condition=models.Q(confirmation_explanation__isnull=True)
            | (non_blank("confirmation_explanation") & models.Q(anomaly_confirmed=True)),
            name=f"{prefix}_explanation",
        ),
    ]


class OdometerEntry(_Reading):
    unit = models.CharField(max_length=2)

    class Meta:
        db_table = "odometer_entry"
        constraints = _reading_constraints("odometer_entry", UNITS)

    def __str__(self):
        return f"OdometerEntry {self.pk}"


class EngineHoursEntry(_Reading):
    unit = models.CharField(max_length=2, default="h")

    class Meta:
        db_table = "engine_hours_entry"
        constraints = _reading_constraints("engine_hours", ("h",))

    def __str__(self):
        return f"EngineHoursEntry {self.pk}"


class OdometerObligationLink(Evidence):
    """Append-only evidence; at most one non-invalidated link satisfies an obligation."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    obligation = models.ForeignKey(
        "cya_obligations.OdometerObligation", on_delete=models.PROTECT, related_name="+"
    )
    entry = models.ForeignKey(OdometerEntry, on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "odometer_obligation_link"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="odometer_link_org_id"),
            models.UniqueConstraint(
                fields=["obligation", "entry"], name="odometer_link_obligation_entry"
            ),
        ]

    def __str__(self):
        return f"OdometerObligationLink {self.pk}"


class OdometerThreshold(Evidence):
    """One revision of the largest plausible increase between readings (ODO-3, AUD-4).

    The latest revision for an organization and unit applies; without one the defaults above do.
    """

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    operation_id = IdentityIDField()
    unit = models.CharField(max_length=2)
    revision = models.PositiveIntegerField()
    max_increase = reading_field()

    class Meta:
        db_table = "odometer_threshold"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="odometer_thr_org_id"),
            models.UniqueConstraint(
                fields=["organization", "operation_id"], name="odometer_thr_org_operation"
            ),
            models.UniqueConstraint(
                fields=["organization", "unit", "revision"], name="odometer_thr_revision"
            ),
            models.CheckConstraint(condition=models.Q(unit__in=UNITS), name="odometer_thr_unit"),
            models.CheckConstraint(condition=models.Q(revision__gte=1), name="odometer_thr_rev"),
            models.CheckConstraint(
                condition=models.Q(max_increase__gt=0, max_increase__lt=READING_LIMIT),
                name="odometer_thr_value",
            ),
        ]

    def __str__(self):
        return f"OdometerThreshold {self.pk}"


class ReadingEvent(Evidence):
    """One attributed decision about exactly one odometer or engine-hours chain."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    event_type = models.CharField(max_length=16)
    root_odometer = models.ForeignKey(
        OdometerEntry, null=True, on_delete=models.PROTECT, related_name="+"
    )
    selected_odometer = models.ForeignKey(
        OdometerEntry, null=True, on_delete=models.PROTECT, related_name="+"
    )
    replacement_odometer = models.OneToOneField(
        OdometerEntry, null=True, on_delete=models.PROTECT, related_name="+"
    )
    root_hours = models.ForeignKey(
        EngineHoursEntry, null=True, on_delete=models.PROTECT, related_name="+"
    )
    selected_hours = models.ForeignKey(
        EngineHoursEntry, null=True, on_delete=models.PROTECT, related_name="+"
    )
    replacement_hours = models.OneToOneField(
        EngineHoursEntry, null=True, on_delete=models.PROTECT, related_name="+"
    )
    explanation = models.CharField(max_length=MAX_TEXT, null=True)  # noqa: DJ001

    class Meta:
        db_table = "reading_event"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="reading_event_org_id"),
            models.CheckConstraint(
                condition=models.Q(event_type__in=("CORRECTION", "INVALIDATION")),
                name="reading_event_type",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        root_odometer__isnull=False,
                        selected_odometer__isnull=False,
                        root_hours__isnull=True,
                        selected_hours__isnull=True,
                        replacement_hours__isnull=True,
                    )
                    | models.Q(
                        root_hours__isnull=False,
                        selected_hours__isnull=False,
                        root_odometer__isnull=True,
                        selected_odometer__isnull=True,
                        replacement_odometer__isnull=True,
                    )
                ),
                name="reading_event_kind",
            ),
            models.CheckConstraint(
                condition=(
                    (
                        models.Q(event_type="CORRECTION")
                        & (
                            models.Q(replacement_odometer__isnull=False)
                            | models.Q(replacement_hours__isnull=False)
                        )
                    )
                    | models.Q(
                        event_type="INVALIDATION",
                        replacement_odometer__isnull=True,
                        replacement_hours__isnull=True,
                    )
                ),
                name="reading_event_replacement",
            ),
            models.CheckConstraint(
                condition=models.Q(explanation__isnull=True) | non_blank("explanation"),
                name="reading_event_explanation",
            ),
        ]
