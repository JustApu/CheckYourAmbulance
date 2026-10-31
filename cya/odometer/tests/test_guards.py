"""Database guards: append-only, receipt attribution, previous reading, anomaly, link and revision."""

from decimal import Decimal
from uuid import uuid4

from django.db import DatabaseError, transaction

from cya.core.audit_repository import append_receipt
from cya.odometer.models import (
    EngineHoursEntry,
    OdometerEntry,
    OdometerObligationLink,
    OdometerThreshold,
)
from cya.odometer.tests.support import OdometerCase


class GuardTests(OdometerCase):
    def setUp(self):
        super().setUp()
        self.at_window(-30)
        self.result = self.record(1000, engine_hours=10)
        self.row = OdometerEntry.objects.get()
        self.hours = EngineHoursEntry.objects.get()
        self.link = OdometerObligationLink.objects.get()

    def refused(self, operation, message=None):
        with self.assertRaises(DatabaseError) as caught, transaction.atomic():
            operation()
        if message is not None:
            self.assertIn(message, str(caught.exception))

    def receipt(self, *, command_type="ODOMETER_RECORD", operation_id=None, actor=None):
        """A committed receipt no row uses yet (the command always makes both)."""
        with transaction.atomic():
            return append_receipt(
                organization=self.org,
                operation_id=operation_id or str(uuid4()),
                actor_user=actor or self.crew,
                command_type=command_type,
                request_digest="0" * 64,
                response_status=201,
                response_body={"code": "SYNTHETIC"},
                correlation_id=str(uuid4()),
            )

    def common(self, receipt, **overrides):
        entry_id = overrides.pop("id", str(uuid4()))
        return {
            "id": entry_id,
            "root_entry_id": entry_id,
            "organization_id": self.org.pk,
            "command": receipt,
            "recorded_at": receipt.recorded_at,
            "actor_user": self.crew,
            "actor_roles_at_time": ["CREW"],
            "operation_id": receipt.operation_id,
            "vehicle": self.vehicle,
            **overrides,
        }

    def insert(self, value, receipt=None, **overrides):
        """An odometer entry that would be valid after the stored one; overrides break a rule."""
        receipt = receipt or self.receipt()
        fields = self.common(receipt, **overrides)
        fields.setdefault("prior_entry", self.row)
        return OdometerEntry.objects.create(value=value, unit=fields.pop("unit", "mi"), **fields)

    def test_a_valid_follow_up_row_is_accepted_so_the_refusals_below_are_meaningful(self):
        self.insert(1500)
        self.assertEqual(OdometerEntry.objects.count(), 2)

    def test_every_table_is_append_only(self):
        threshold = self.set_threshold("mi", 500)
        rows = (
            self.row,
            self.hours,
            self.link,
            OdometerThreshold.objects.get(pk=threshold.body["threshold_id"]),
        )
        for row in rows:
            with self.subTest(table=row._meta.db_table):
                self.refused(
                    lambda row=row: (
                        type(row).objects.filter(pk=row.pk).update(recorded_at=row.recorded_at)
                    )
                )
                self.refused(lambda row=row: type(row).objects.filter(pk=row.pk).delete())

    def test_a_row_must_match_its_command_receipt(self):
        self.refused(
            lambda: self.insert(1500, receipt=self.receipt(command_type="CHECK_RECORD")),
            "receipt attribution",
        )
        receipt = self.receipt()
        self.refused(lambda: self.insert(1500, receipt=receipt, operation_id=str(uuid4())))
        self.refused(lambda: self.insert(1500, receipt=receipt, actor_user=self.supervisor))
        self.refused(lambda: self.insert(1500, receipt=receipt, recorded_at=self.row.recorded_at))

    def test_the_previous_reading_must_be_the_vehicles_latest(self):
        self.refused(lambda: self.insert(1500, prior_entry=None), "latest")
        newest = self.insert(1500)
        self.refused(lambda: self.insert(1600, prior_entry=self.row), "latest")
        self.insert(1600, prior_entry=newest)

    def test_an_anomalous_reading_needs_its_confirmation_and_only_then(self):
        self.refused(lambda: self.insert(999), "needs confirmation")  # lower, not confirmed
        self.refused(lambda: self.insert(2001), "needs confirmation")  # over the 1000 mi default
        self.refused(lambda: self.insert(1500, anomaly_confirmed=True), "without an anomaly")
        self.insert(999, anomaly_confirmed=True, confirmation_explanation="Cluster replaced.")

    def test_the_threshold_in_force_is_the_one_the_trigger_applies(self):
        self.set_threshold("mi", 100)
        self.refused(lambda: self.insert(1101), "needs confirmation")
        self.insert(1100)

    def test_the_unit_must_match_the_previous_reading(self):
        self.refused(lambda: self.insert(1100, unit="km"), "unit differs")

    def test_an_explanation_needs_a_confirmed_anomaly_and_text(self):
        self.refused(lambda: self.insert(1100, confirmation_explanation="Why."))
        self.refused(
            lambda: self.insert(999, anomaly_confirmed=True, confirmation_explanation="   ")
        )

    def test_engine_hours_follow_the_same_previous_and_lower_rules(self):
        def hours(value, **overrides):
            receipt = self.receipt()
            fields = self.common(receipt, **overrides)
            fields.setdefault("prior_entry", self.hours)
            return EngineHoursEntry.objects.create(value=value, unit="h", **fields)

        self.refused(lambda: hours(9), "needs confirmation")
        self.refused(lambda: hours(11, prior_entry=None))
        hours(9, anomaly_confirmed=True)
        self.assertEqual(EngineHoursEntry.objects.count(), 2)

    def test_values_are_bounded(self):
        self.refused(lambda: self.insert(-1, anomaly_confirmed=True))
        self.refused(lambda: self.insert(Decimal(10**7), anomaly_confirmed=True))
        self.refused(lambda: self.insert(1100, unit="m"))

    def test_a_correction_exceeding_a_downstream_reading_is_anomalous_in_the_trigger(self):
        self.insert(1500)
        receipt = self.receipt(command_type="READING_CORRECTION")
        fields = self.common(
            receipt,
            root_entry_id=self.row.pk,
            predecessor_entry=self.row,
            prior_entry=None,
        )
        # Correcting root 1000 to 2000 exceeds later reading (1500) and needs confirmation
        self.refused(
            lambda: OdometerEntry.objects.create(value=2000, unit="mi", **fields),
            "needs confirmation",
        )
        # Correcting to 1200 does not exceed 1500 and is not lower than prior (None),
        # so confirmation without an anomaly is refused
        self.refused(
            lambda: OdometerEntry.objects.create(
                value=1200, unit="mi", anomaly_confirmed=True, **fields
            ),
            "without an anomaly",
        )
        # With anomaly_confirmed=True, the downstream anomaly is accepted
        OdometerEntry.objects.create(
            value=2000,
            unit="mi",
            anomaly_confirmed=True,
            confirmation_explanation="Recalibrated.",
            **fields,
        )

    def test_a_link_must_match_its_entry_obligation_and_receipt(self):
        def link(**overrides):
            receipt = overrides.pop("receipt", None) or self.receipt()
            entry = self.insert(1100, receipt=receipt)
            fields = {
                "id": str(uuid4()),
                "organization_id": self.org.pk,
                "command": receipt,
                "recorded_at": receipt.recorded_at,
                "actor_user": self.crew,
                "actor_roles_at_time": ["CREW"],
                "obligation": self.second,
                "entry": entry,
                **overrides,
            }
            return OdometerObligationLink.objects.create(**fields)

        # The first obligation already has an effective link; the second's window has not opened.
        self.refused(lambda: link(obligation=self.first), "already has valid evidence")
        self.refused(lambda: link(), "does not match")
        self.refused(lambda: link(entry=self.row))  # an entry recorded under another command
        self.refused(lambda: link(actor_user=self.supervisor))

    def test_a_link_to_another_vehicles_entry_is_refused(self):
        other = self.vehicles[1]
        self.bind(vehicle=other, reconcile=False)
        self.generate()
        theirs = next(
            o
            for o in self.odometers()
            if str(o.vehicle_id) == str(other.pk) and o.early_at <= self.row.recorded_at
        )
        receipt = self.receipt()
        entry = OdometerEntry.objects.create(
            value=5, unit="mi", **self.common(receipt, prior_entry=None, vehicle=other)
        )
        self.refused(
            lambda: OdometerObligationLink.objects.create(
                id=str(uuid4()),
                organization_id=self.org.pk,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=self.crew,
                actor_roles_at_time=["CREW"],
                obligation=self.first,
                entry=entry,
            )
        )
        OdometerObligationLink.objects.create(
            id=str(uuid4()),
            organization_id=self.org.pk,
            command=receipt,
            recorded_at=receipt.recorded_at,
            actor_user=self.crew,
            actor_roles_at_time=["CREW"],
            obligation=theirs,
            entry=entry,
        )

    def test_threshold_revisions_count_up_without_gaps(self):
        def revision(number, unit="mi", **overrides):
            receipt = self.receipt(command_type="ODOMETER_THRESHOLD_SET", actor=self.admin)
            return OdometerThreshold.objects.create(
                id=str(uuid4()),
                organization_id=self.org.pk,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=self.admin,
                actor_roles_at_time=["ADMINISTRATOR"],
                operation_id=receipt.operation_id,
                unit=unit,
                revision=number,
                max_increase=Decimal(100),
                **overrides,
            )

        self.refused(lambda: revision(2), "next one")
        revision(1)
        self.refused(lambda: revision(1))
        self.refused(lambda: revision(3), "next one")
        revision(2)
        revision(1, unit="km")
