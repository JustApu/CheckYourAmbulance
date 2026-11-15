"""Positions: add, revise, retire, and the writers that refuse a retired one (SPEC 4.8 CFG-2)."""

import threading
from datetime import timedelta
from unittest import mock

from django.db import close_old_connections

from cya.checklists.models import Compartment
from cya.checklists.validation import ChecklistValidationError
from cya.core.audit_commands import CommandDenied
from cya.kits import positions
from cya.kits.commands import KitError
from cya.kits.definitions import (
    create_kit_definition,
    publish_kit_definition_version,
    save_kit_definition_draft,
)
from cya.kits.kit_lists import KitListError, save_vehicle_kit_list_draft
from cya.kits.models import PositionKey
from cya.kits.positions import PositionError, add_position, revise_position
from cya.kits.sealable import designate_compartment_sealable
from cya.kits.tests.support import KitCase
from cya.obligations.commands import BindingError


class PositionCase(KitCase):
    def add(self, vehicle=None, key="drawer", label="Synthetic drawer", actor=None, **kw):
        return add_position(
            vehicle_id=str((vehicle or self.vehicle).pk),
            kind="COMPARTMENT",
            label=label,
            key=key,
            **kw,
            **self.context(actor or self.supervisor),
        )

    def revise(self, vehicle=None, slot=None, actor=None, **changes):
        return revise_position(
            vehicle_id=str((vehicle or self.vehicle).pk),
            kind="COMPARTMENT",
            stable_slot_id=slot or self.slot,
            **changes,
            **self.context(actor or self.supervisor),
        )

    def history(self, vehicle=None, slot=None):
        return list(
            Compartment.objects.filter(
                vehicle=vehicle or self.vehicle, stable_slot_id=slot or self.slot
            )
            .order_by("revision")
            .values_list("revision", "label", "display_order", "retired", "sealable")
        )

    def definition(self):
        definition = create_kit_definition(
            key="jump-kit", label="Synthetic jump kit", **self.context()
        ).body["definition_id"]
        version = save_kit_definition_draft(
            definition_id=definition,
            contents=[
                {
                    "item_id": str(self.kit_item.pk),
                    "display_order": 1,
                    "kind": "QUANTITY",
                    "expected_quantity": 2,
                    "allowed_outcomes": ["PASSED", "FAILED"],
                }
            ],
            **self.context(),
        ).body["version_id"]
        publish_kit_definition_version(version_id=version, **self.context())
        return definition


class AddAndReviseTests(PositionCase):
    def test_a_new_key_creates_the_position_and_another_vehicle_joins_it(self):
        first = self.add(display_order=20).body
        self.assertTrue(first["new_position"])
        key = PositionKey.objects.get(kind="COMPARTMENT", key="drawer")
        self.assertEqual(str(key.stable_slot_id), first["stable_slot_id"])
        second = self.add(vehicle=self.vehicle_b, label="Synthetic drawer B").body
        self.assertFalse(second["new_position"])
        self.assertEqual(second["stable_slot_id"], first["stable_slot_id"])
        self.assertEqual(PositionKey.objects.filter(key="drawer").count(), 1)
        self.assertEqual(
            self.history(slot=first["stable_slot_id"]),
            [(1, "Synthetic drawer", 20, False, False)],
        )
        with self.assertRaises(PositionError):
            self.add()  # the vehicle already has that position

    def test_only_supervisors_and_administrators_manage_positions(self):
        for actor in (self.crew, self.officer):
            with self.subTest(actor=actor.idp_subject), self.assertRaises(CommandDenied):
                self.add(actor=actor)
        self.add(actor=self.admin)

    def test_a_revision_keeps_the_slot_and_the_sealable_designation(self):
        designate_compartment_sealable(
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str(self.cabinet.pk),
            sealable=True,
            **self.context(),
        )
        self.revise(label="Renamed cabinet", display_order=5)
        self.assertEqual(self.history()[-1], (3, "Renamed cabinet", 5, False, True))
        with self.assertRaises(PositionError):
            self.revise(label="Renamed cabinet")  # no change
        with self.assertRaises(PositionError):
            self.revise()  # nothing named

    def test_a_scheduled_revision_refuses_another(self):
        designate_compartment_sealable(
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str(self.cabinet.pk),
            sealable=True,
            effective_at=self.clock.now + timedelta(days=1),
            **self.context(),
        )
        with self.assertRaises(PositionError):
            self.revise(display_order=3)

    def test_positions_sort_by_order_then_label_with_empty_order_last(self):
        self.add(key="aaa", label="A unordered")
        self.add(key="zzz", label="Z first", display_order=1)
        rows = positions.positions(str(self.org.pk), str(self.vehicle.pk), "COMPARTMENT")
        self.assertEqual([row.label for row, _key in rows][:2], ["Z first", "A unordered"])


class RetireTests(PositionCase):
    def test_an_unused_position_retires_and_unretires(self):
        self.revise(retired=True)
        self.assertTrue(self.history()[-1][3])
        self.revise(retired=False)
        self.assertFalse(self.history()[-1][3])

    def blocked(self, fragment):
        with self.assertRaises(PositionError) as raised:
            self.revise(retired=True)
        self.assertIn(fragment, str(raised.exception))
        self.assertFalse(self.history()[-1][3])

    def test_a_bound_template_in_force_or_scheduled_blocks_retirement(self):
        self.publish_schedule("test-0600")
        self.published_version()
        self.bind()
        self.blocked("checklist template")

    def test_a_binding_that_starts_later_blocks_retirement(self):
        self.publish_schedule("test-0600")
        self.published_version()
        self.bind(effective_at=self.clock.now + timedelta(days=2))
        self.blocked("checklist template")

    def test_a_version_published_for_later_blocks_retirement(self):
        self.publish_schedule("test-0600")
        self.bind()
        self.published_version(effective_at=self.clock.now + timedelta(days=2))
        self.blocked("checklist template")

    def test_an_unbound_template_does_not_block(self):
        self.published_version()
        self.revise(retired=True)

    def test_a_kit_list_or_an_active_kit_blocks_retirement(self):
        definition = self.definition()
        save_vehicle_kit_list_draft(
            vehicle_id=str(self.vehicle.pk),
            lines=[
                {
                    "definition_id": definition,
                    "compartment_id": str(self.cabinet.pk),
                    "required_count": 1,
                    "display_order": 1,
                }
            ],
            **self.context(),
        )
        self.revise(retired=True)  # a draft kit list is not published
        self.revise(retired=False)
        self.add_kit()
        self.blocked("active kits")


class RetiredWritersTests(PositionCase):
    def setUp(self):
        super().setUp()
        self.revise(retired=True)  # vehicle A's cabinet

    def test_kits_kit_lists_and_drafts_refuse_a_retired_position(self):
        with self.assertRaises(KitError):
            self.add_kit()
        self.add_kit(vehicle=self.vehicle_b)  # B's cabinet is not retired
        with self.assertRaises(KitListError):
            save_vehicle_kit_list_draft(
                vehicle_id=str(self.vehicle.pk),
                lines=[
                    {
                        "definition_id": self.definition(),
                        "compartment_id": str(self.cabinet.pk),
                        "required_count": 1,
                        "display_order": 1,
                    }
                ],
                **self.context(),
            )
        with self.assertRaises(ChecklistValidationError):
            self.draft()  # base items reference vehicle A's cabinet row

    def test_publishing_and_binding_refuse_a_retired_position_on_a_bound_vehicle(self):
        self.publish_schedule("test-0600")
        items = [self.spec(self.suction, 10, compartment_id=str(self.cabinet_b.pk))]
        version = self.draft(items)  # drafted on B's row of the same slot
        self.bind(vehicle=self.vehicle_b)
        self.publish(version)  # B's cabinet is in use; A is not bound
        with self.assertRaises(BindingError):
            self.bind()  # binding A would make its retired cabinet required
        later = self.draft(items)
        self.revise(retired=False)
        self.bind()
        with self.assertRaises(PositionError):
            self.revise(retired=True)
        self.publish(later)


class RetireRaceTests(PositionCase):
    """The vehicle row serializes retirement with publication and binding (design part 3)."""

    def race(self, first, first_pause, second):
        """Run `first`, paused after `first_pause` returns; `second` starts meanwhile.

        Returns (order, errors): order lists the commands that committed, errors their
        exceptions, in completion order.
        """
        module, name = first_pause
        reached, release = threading.Event(), threading.Event()
        order, errors = [], []
        original = getattr(module, name)

        def paused(*args, **kwargs):
            result = original(*args, **kwargs)
            reached.set()
            release.wait(timeout=30)
            return result

        def run(label, call, patch):
            try:
                if patch:
                    with mock.patch.object(module, name, paused):
                        call()
                else:
                    reached.wait(timeout=30)
                    call()
                order.append(label)
            except Exception as error:  # noqa: BLE001 - asserted by callers
                errors.append((label, type(error).__name__))
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=run, args=("first", first, True)),
            threading.Thread(target=run, args=("second", second, False)),
        ]
        for thread in threads:
            thread.start()
        self.assertTrue(reached.wait(timeout=30))
        threads[1].join(timeout=3)  # a second command that is not blocked finishes here
        release.set()
        for thread in threads:
            thread.join(timeout=60)
        return order, errors

    def setUp(self):
        super().setUp()
        self.publish_schedule("test-0600")
        self.bind()  # A bound; the template has no published version yet

    # The two commands in a race must have different actors: a command locks its actor's row,
    # so one actor would serialize them whatever the vehicle lock does (lesson of PR #145).
    def retire_as_admin(self):
        return self.revise(retired=True, actor=self.admin)

    def retire_as_supervisor(self):
        return self.revise(retired=True, actor=self.supervisor)

    def test_a_retirement_first_makes_a_publication_wait_and_refuse(self):
        version = self.draft()
        order, errors = self.race(
            self.retire_as_admin,
            (positions, "retired_blockers"),
            lambda: self.publish(version, actor=self.supervisor),
        )
        self.assertEqual((order, errors), (["first"], [("second", "ChecklistValidationError")]))

    def test_a_publication_first_makes_a_retirement_wait_and_refuse(self):
        from cya.checklists import commands

        version = self.draft()
        order, errors = self.race(
            lambda: self.publish(version, actor=self.supervisor),
            (commands, "placements_of"),
            self.retire_as_admin,
        )
        self.assertEqual((order, errors), (["first"], [("second", "PositionError")]))

    def test_a_retirement_first_makes_a_binding_wait_and_refuse(self):
        self.publish(self.draft())
        self.bind(action="UNBIND")
        # A is unbound now, so the cabinet can retire; binding it again must then refuse.
        order, errors = self.race(
            self.retire_as_supervisor,
            (positions, "retired_blockers"),
            lambda: self.bind(actor=self.admin),
        )
        self.assertEqual((order, errors), (["first"], [("second", "BindingError")]))

    def test_a_binding_first_makes_a_retirement_wait_and_refuse(self):
        self.publish(self.draft())
        self.bind(action="UNBIND")
        order, errors = self.race(
            lambda: self.bind(actor=self.admin),
            (positions, "template_slot_rows"),
            self.retire_as_supervisor,
        )
        self.assertEqual((order, errors), (["first"], [("second", "PositionError")]))


class RetiredSealTests(PositionCase):
    """Designations and the seal workflow respect order and retirement (CFG-2, CAB-1)."""

    def designate(self, sealable=True):
        return designate_compartment_sealable(
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str(self.cabinet.pk),
            sealable=sealable,
            **self.context(),
        )

    def test_a_designation_keeps_the_order_and_the_retirement(self):
        self.revise(display_order=7)
        self.designate()
        self.assertEqual(self.history()[-1][2:], (7, False, True))
        self.revise(retired=True)
        self.designate(sealable=False)  # making it not sealable is still allowed
        self.assertEqual(self.history()[-1][2:], (7, True, False))

    def test_a_retired_compartment_cannot_be_designated_sealable(self):
        from cya.kits.sealable import SealableError

        self.revise(retired=True)
        with self.assertRaises(SealableError):
            self.designate()

    def test_a_retired_cabinet_is_no_seal_subject(self):
        from cya.seals.commands import SealError, start_inspection
        from cya.seals.readers import subjects

        self.designate()
        slot = self.slot
        listed = subjects(str(self.org.pk), str(self.vehicle.pk))
        self.assertTrue(any(slot in str(subject) for subject in listed))
        self.revise(retired=True)
        listed = subjects(str(self.org.pk), str(self.vehicle.pk))
        self.assertFalse(any(slot in str(subject) for subject in listed))
        with self.assertRaises(SealError):
            start_inspection(
                vehicle_id=str(self.vehicle.pk), slot_id=slot, **self.context(self.crew)
            )


class ScheduledUnbindTests(PositionCase):
    """PR #157 review: an UNBIND scheduled for later leaves the binding in force until then."""

    def setUp(self):
        super().setUp()
        self.publish_schedule("test-0600")
        self.tomorrow = self.clock.now + timedelta(days=1)

    def test_retirement_stays_blocked_while_a_scheduled_unbind_is_pending(self):
        self.published_version()
        self.bind()
        self.bind(action="UNBIND", effective_at=self.tomorrow)
        with self.assertRaises(PositionError) as raised:
            self.revise(retired=True)
        self.assertIn("checklist template", str(raised.exception))

    def test_publication_checks_a_vehicle_bound_until_a_scheduled_unbind(self):
        self.revise(retired=True)  # A's cabinet, while A is unbound
        self.bind()  # the template has no version yet, so nothing uses the cabinet
        self.bind(action="UNBIND", effective_at=self.tomorrow)
        with self.assertRaises(ChecklistValidationError):
            self.publish(
                self.draft([self.spec(self.suction, 10, compartment_id=str(self.cabinet_b.pk))])
            )
