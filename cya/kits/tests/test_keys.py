"""Stable keys: VINs, template keys and position keys (SPEC 2.1.0 IMP-6, issue #129)."""

from uuid import uuid4

from django.db import DatabaseError, transaction

from cya.checklists.models import ChecklistTemplate, Compartment, Vehicle
from cya.checklists.tests.support import ChecklistCase
from cya.core.audit_commands import CommandDenied
from cya.kits.keys import (
    KeyRuleError,
    position_keys,
    set_position_key,
    set_template_key,
    set_vehicle_vin,
)
from cya.kits.models import PositionKey

VIN = "1FDXE4FS8KDC00001"  # synthetic
OTHER_VIN = "1FDXE4FS8KDC00002"  # synthetic


class VinTests(ChecklistCase):
    def vin(self, vin, vehicle=None, actor=None):
        return set_vehicle_vin(
            vehicle_id=str((vehicle or self.vehicles[0]).pk), vin=vin, **self.context(actor)
        )

    def test_a_vin_is_normalized_and_set_once(self):
        self.vin(" 1fdxe4fs8kdc00001 ")
        self.assertEqual(Vehicle.objects.get(pk=self.vehicles[0].pk).key, VIN)
        with self.assertRaises(KeyRuleError):
            self.vin(OTHER_VIN)  # a VIN never changes; a remount is a new vehicle
        with self.assertRaises(KeyRuleError):
            self.vin(VIN, vehicle=self.vehicles[1])  # unique in the organization

    def test_malformed_vins_are_refused(self):
        for bad in ("", "SHORT", "1FDXE4FS8KDC0000I", "1FDXE4FS8KDC0000O", "1FDXE4FS8KDC0000Q", 7):
            with self.subTest(bad=bad), self.assertRaises(KeyRuleError):
                self.vin(bad)
        self.assertIsNone(Vehicle.objects.get(pk=self.vehicles[0].pk).key)

    def test_the_database_keeps_a_set_vin(self):
        self.vin(VIN)
        with self.assertRaises(DatabaseError), transaction.atomic():
            Vehicle.objects.filter(pk=self.vehicles[0].pk).update(key=OTHER_VIN)
        with self.assertRaises(DatabaseError), transaction.atomic():
            Vehicle.objects.filter(pk=self.vehicles[1].pk).update(key="NOT-A-VIN")
        Vehicle.objects.filter(pk=self.vehicles[1].pk).update(display_name="Renamed")  # fine

    def test_only_administrators_set_keys(self):
        with self.assertRaises(CommandDenied):
            self.vin(VIN, actor=self.supervisor)


class TemplateKeyTests(ChecklistCase):
    def test_a_template_key_is_set_once_and_unique(self):
        set_template_key(template_id=str(self.template.pk), key="daily-check", **self.context())
        self.assertEqual(ChecklistTemplate.objects.get(pk=self.template.pk).key, "daily-check")
        with self.assertRaises(KeyRuleError):
            set_template_key(template_id=str(self.template.pk), key="other", **self.context())
        second = ChecklistTemplate.objects.create(organization=self.org, label="Synthetic other")
        with self.assertRaises(KeyRuleError):
            set_template_key(template_id=str(second.pk), key="daily-check", **self.context())
        for bad in ("Daily", "-x", "", "a" * 101):
            with self.subTest(bad=bad), self.assertRaises(KeyRuleError):
                set_template_key(template_id=str(second.pk), key=bad, **self.context())
        with self.assertRaises(DatabaseError), transaction.atomic():
            ChecklistTemplate.objects.filter(pk=self.template.pk).update(key="changed")


class PositionKeyTests(ChecklistCase):
    def set(self, key, slot=None, kind="COMPARTMENT"):
        return set_position_key(
            kind=kind,
            stable_slot_id=str(slot or self.cabinet.stable_slot_id),
            key=key,
            **self.context(),
        )

    def test_a_position_key_names_the_slot_on_every_vehicle(self):
        self.set("cabinet")
        self.assertEqual(
            position_keys(str(self.org.pk), "COMPARTMENT"),
            {str(self.cabinet.stable_slot_id): "cabinet"},
        )
        other = Compartment.objects.get(
            vehicle=self.vehicles[1], stable_slot_id=self.cabinet.stable_slot_id
        )
        self.assertEqual(
            position_keys(str(self.org.pk), "COMPARTMENT")[str(other.stable_slot_id)], "cabinet"
        )

    def test_position_key_rules(self):
        self.set("cabinet")
        drawer = self.make_slot(Compartment, "drawer")
        with self.assertRaises(KeyRuleError):
            self.set("other")  # set once
        with self.assertRaises(KeyRuleError):
            self.set("cabinet", slot=drawer.stable_slot_id)  # unique per kind
        with self.assertRaises(KeyRuleError):
            self.set("ghost", slot=str(uuid4()))  # no such position
        with self.assertRaises(KeyRuleError):
            self.set("lights", kind="SYSTEM")  # the cabinet is not a system position
        self.set("lights", slot=self.system.stable_slot_id, kind="SYSTEM")

    def test_the_database_keeps_position_keys(self):
        self.set("cabinet")
        row = PositionKey.objects.get()
        for operation in (
            lambda: PositionKey.objects.filter(pk=row.pk).update(key="changed"),
            lambda: PositionKey.objects.filter(pk=row.pk).delete(),
        ):
            with (
                self.subTest(operation=operation),
                self.assertRaises(DatabaseError),
                transaction.atomic(),
            ):
                operation()
