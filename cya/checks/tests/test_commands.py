"""record_check: validation, authorization, audit, replay and a same-operation race."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest import mock
from uuid import uuid1, uuid4

from django.db import connection

from cya.checks.commands import CheckError, chosen_kit_id, correct_check
from cya.checks.models import CheckEvent, CheckRecord
from cya.checks.tests.support import CheckCase
from cya.core.audit_commands import CommandDenied, OperationConflict
from cya.core.models import AuditEvent, CommandReceipt


class RecordTests(CheckCase):
    def counts(self):
        return CheckRecord.objects.count(), CommandReceipt.objects.count()

    def correct(self, check, tip, *, actor=None, operation_id=None, **fields):
        args = self.context(actor or self.crew)
        if operation_id is not None:
            args["operation_id"] = operation_id
        return correct_check(
            check_id=str(check),
            expected_tip_check_id=str(tip),
            outcome="PASSED",
            **args,
            **fields,
        )

    def rejected(self, code, **kwargs):
        """The call is refused with code; nothing is stored and the operation id stays free."""
        before = self.counts()
        with self.assertRaises(CheckError) as caught:
            self.record(**kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        self.assertEqual(self.counts(), before)
        return caught.exception

    def test_a_passed_check_is_stored_attributed_and_audited(self):
        result = self.record(client_observed_at=datetime(2026, 6, 1, 12, tzinfo=UTC))
        row = CheckRecord.objects.get()
        self.assertEqual((result.status, result.body["code"]), (201, "CHECK_RECORDED"))
        self.assertEqual(result.body["check_id"], row.pk)
        self.assertEqual(
            (row.root_check_id, row.actor_user_id, row.actor_roles_at_time),
            (row.pk, self.crew.pk, ["CREW"]),
        )
        self.assertEqual(
            (row.vehicle_id, row.item_id, row.checklist_version_id, row.outcome, row.purpose),
            (self.vehicle.pk, self.suction.pk, self.version_id, "PASSED", "ROUTINE"),
        )
        self.assertEqual(row.item_def_id, self.resolve().items[0].requirement.definition_id)
        self.assertEqual(row.client_observed_at, datetime(2026, 6, 1, 12, tzinfo=UTC))
        self.assertEqual((row.command.recorded_at, row.recorded_at), (row.recorded_at,) * 2)
        self.assertIsNone(row.kit_id)
        self.assertEqual(result.body["obligation_links"], [])
        audit = AuditEvent.objects.get(action="CHECK_RECORD")
        self.assertEqual((audit.outcome, audit.actor_user_id), ("SUCCEEDED", self.crew.pk))
        self.assertEqual(audit.new_snapshot["values"]["check_id"], row.pk)

    def test_author_can_correct_and_then_correct_the_replacement(self):
        original = self.record().body["check_id"]
        first = self.correct(original, original)
        replacement = CheckRecord.objects.get(pk=first.body["check_id"])
        self.assertEqual(
            (replacement.root_check_id, replacement.predecessor_check_id), (original, original)
        )
        second = self.correct(original, replacement.pk, explanation="Synthetic correction note.")
        final = CheckRecord.objects.get(pk=second.body["check_id"])
        self.assertEqual(
            (final.root_check_id, final.predecessor_check_id), (original, replacement.pk)
        )
        self.assertEqual(CheckEvent.objects.filter(root_check_id=original).count(), 2)

    def test_correction_has_no_time_limit_by_default(self):
        original = self.record().body["check_id"]
        check = CheckRecord.objects.get(pk=original)
        later = check.recorded_at + timedelta(days=365)
        with mock.patch("cya.checks.commands.timezone.now", return_value=later):
            result = self.correct(original, original)
        self.assertEqual(result.status, 201)
        self.assertGreater(
            CheckRecord.objects.get(pk=result.body["check_id"]).recorded_at,
            check.recorded_at,
        )

    def test_correction_validates_quantity_against_deviation_at_original_check_time(self):
        from cya.checklists import commands as checklist_commands

        checklist_commands.record_stocking_deviation(
            vehicle_id=str(self.vehicle.pk),
            item_id=str(self.masks.pk),
            template_id=str(self.template.pk),
            alternate_quantity=3,
            valid_from=None,
            valid_until=self.now + timedelta(days=1),
            explanation="Synthetic restock delay.",
            **self.context(),
        )
        original = self.record(item=self.masks, quantity=3).body["check_id"]
        corrected = self.correct(original, original, quantity=3)
        self.assertEqual(corrected.status, 201)
        with self.assertRaises(CheckError) as caught:
            self.correct(original, corrected.body["check_id"], quantity=6)
        self.assertEqual(caught.exception.code, "INVALID_OBSERVATION")

    def test_correction_cannot_revive_an_idle_session_before_auto_close(self):
        from cya.inspections import commands as session_commands
        from cya.inspections.projections import current_state, is_expired

        opened = session_commands.open_or_join_session(
            vehicle_id=str(self.vehicle.pk),
            template_id=str(self.template.pk),
            **self.context(self.crew),
        )
        session_id = opened.body["session_id"]
        original = self.record(session_id=session_id).body["check_id"]
        root = CheckRecord.objects.get(pk=original)
        before = self.counts()

        with mock.patch(
            "cya.checks.commands.timezone.now",
            return_value=root.recorded_at + timedelta(hours=6, minutes=1),
        ):
            with self.assertRaises(CheckError) as caught:
                self.correct(original, original)

        self.assertEqual(caught.exception.code, "SESSION_NOT_OPEN")
        self.assertEqual(self.counts(), before)
        state = current_state(root.session)
        self.assertTrue(is_expired(state, root.recorded_at + timedelta(hours=6, minutes=1)))
        self.assertEqual(CheckRecord.objects.filter(root_check_id=original).count(), 1)
        self.assertFalse(CheckEvent.objects.filter(root_check_id=original).exists())

    def test_non_author_and_stale_tip_cannot_extend_a_chain(self):
        original = self.record().body["check_id"]
        with self.assertRaises(CheckError) as caught:
            self.correct(original, original, actor=self.supervisor)
        self.assertEqual(caught.exception.code, "AUTHOR_REQUIRED")
        independent_result = self.record(actor=self.supervisor, acknowledges_check_id=original)
        independent = independent_result.body["check_id"]
        self.assertNotEqual(CheckRecord.objects.get(pk=independent).root_check_id, original)
        self.correct(original, original)
        with self.assertRaises(CheckError) as caught:
            self.correct(original, original)
        self.assertEqual(caught.exception.code, "CHAIN_CHANGED")
        self.assertEqual(CheckRecord.objects.filter(root_check_id=original).count(), 2)

    def test_correction_replay_is_idempotent(self):
        original = self.record().body["check_id"]
        operation_id = str(uuid4())
        first = self.correct(original, original, operation_id=operation_id)
        replay = self.correct(original, original, operation_id=operation_id)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.body, first.body)
        self.assertEqual(CheckEvent.objects.count(), 1)

    def test_concurrent_corrections_from_one_tip_do_not_fork(self):
        original = self.record().body["check_id"]

        def submit():
            try:
                return self.correct(original, original)
            except CheckError as error:
                return error.code
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                future.result(timeout=30) for future in (pool.submit(submit), pool.submit(submit))
            ]
        self.assertEqual(sum(not isinstance(result, str) for result in results), 1)
        self.assertEqual(
            [result for result in results if isinstance(result, str)], ["CHAIN_CHANGED"]
        )
        self.assertEqual(CheckRecord.objects.filter(root_check_id=original).count(), 2)
        self.assertEqual(CheckEvent.objects.filter(root_check_id=original).count(), 1)

    def test_the_audit_snapshot_never_carries_free_text(self):
        self.record(item=self.limited, explanation="Synthetic private note.")
        audit = AuditEvent.objects.get(action="CHECK_RECORD")
        self.assertNotIn("Synthetic private note", str(audit.new_snapshot))
        self.assertEqual(CheckRecord.objects.get().explanation, "Synthetic private note.")

    def test_every_role_may_record_and_the_check_is_attributed_to_the_caller(self):
        self.grant(self.crew, "SUPPLY_OFFICER")
        # One item each: the same item by different users would (correctly) warn, CON-3.
        for actor, item in zip(
            (self.crew, self.supervisor, self.admin),
            (self.special, self.limited, self.suction),
            strict=True,
        ):
            with self.subTest(actor.idp_subject):
                self.record(actor=actor, item=item)
        rows = CheckRecord.objects.order_by("recorded_at")
        self.assertEqual(
            [r.actor_user_id for r in rows],
            [u.pk for u in (self.crew, self.supervisor, self.admin)],
        )
        self.assertEqual(rows[0].actor_roles_at_time, ["CREW", "SUPPLY_OFFICER"])

    def test_a_disabled_user_is_denied_and_nothing_is_stored(self):
        self.crew.active = False
        self.crew.save(update_fields=["active"])
        before = self.counts()
        with self.assertRaises(CommandDenied):
            self.record()
        self.assertEqual(self.counts(), before)
        self.assertTrue(AuditEvent.objects.filter(action="CHECK_RECORD", outcome="DENIED").exists())

    # Idempotency ------------------------------------------------------------------------

    def test_a_replay_returns_the_original_result_and_stores_nothing_more(self):
        operation_id = str(uuid4())
        first = self.record(operation_id=operation_id)
        again = self.record(operation_id=operation_id)
        self.assertTrue(again.replayed and not again.operation_reused)
        self.assertEqual((again.status, again.body), (first.status, first.body))
        self.assertEqual(again.correlation_id, first.correlation_id)
        self.assertEqual(CheckRecord.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="CHECK_RECORD").count(), 1)

    def test_the_same_id_with_a_different_request_applies_nothing(self):
        operation_id = str(uuid4())
        first = self.record(operation_id=operation_id)
        other = self.record(operation_id=operation_id, item=self.special)
        self.assertTrue(other.replayed and other.operation_reused)
        self.assertEqual(other.body, first.body)
        self.assertEqual(CheckRecord.objects.count(), 1)

    def test_another_users_operation_id_is_refused_without_a_new_check(self):
        operation_id = str(uuid4())
        self.record(operation_id=operation_id)
        with self.assertRaises(OperationConflict):
            self.record(operation_id=operation_id, actor=self.supervisor)
        self.assertEqual(CheckRecord.objects.count(), 1)

    def test_a_replay_does_not_bypass_a_revoked_user(self):
        operation_id = str(uuid4())
        self.record(operation_id=operation_id)
        self.crew.active = False
        self.crew.save(update_fields=["active"])
        with self.assertRaises(CommandDenied):
            self.record(operation_id=operation_id)

    def test_a_rejected_write_does_not_consume_its_operation_id(self):
        operation_id = str(uuid4())
        self.rejected("INVALID_OBSERVATION", item=self.masks, operation_id=operation_id)
        ok = self.record(item=self.masks, operation_id=operation_id, quantity=6)
        self.assertFalse(ok.replayed)
        self.assertEqual(CheckRecord.objects.get().quantity, 6)

    def test_concurrent_submissions_of_one_operation_id_store_one_check(self):
        operation_id = str(uuid4())

        def submit():
            try:
                return self.record(operation_id=operation_id)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = [f.result(timeout=60) for f in [pool.submit(submit) for _ in range(6)]]
        self.assertEqual(CheckRecord.objects.count(), 1)
        self.assertEqual({r.body["check_id"] for r in results}, {CheckRecord.objects.get().pk})
        self.assertEqual(sum(not r.replayed for r in results), 1)

    # Server-side validation ---------------------------------------------------------------

    def test_unknown_foreign_and_inactive_targets_are_refused(self):
        self.rejected("NOT_FOUND", vehicle_id=str(uuid4()))
        self.rejected("NOT_FOUND", checklist_version_id=str(uuid4()))
        self.rejected("NOT_FOUND", checklist_version_id=self.draft(self.items))  # never published
        self.rejected("ITEM_NOT_IN_CHECKLIST", item=self.stray)
        self.vehicle.active = False
        self.vehicle.save(update_fields=["active"])
        self.rejected("VEHICLE_INACTIVE")

    def test_another_organizations_vehicle_and_version_are_not_found(self):
        from cya.accounts.models import Organization
        from cya.checklists.models import Vehicle

        other = Organization.objects.create(name="Other Sample Agency", station_zone="UTC")
        foreign = Vehicle.objects.create(organization=other, display_name="Foreign")
        self.rejected("NOT_FOUND", vehicle=foreign)

    def test_a_superseded_checklist_version_is_never_silently_substituted(self):
        newer = self.published_version(self.items)
        self.rejected("CHECKLIST_VERSION_NOT_CURRENT")
        self.record(checklist_version_id=newer)
        self.assertEqual(CheckRecord.objects.get().checklist_version_id, newer)

    def test_outcome_rules(self):
        self.rejected("INVALID_OUTCOME", outcome="NOT_APPLICABLE")  # no NA rule exists yet
        self.rejected("INVALID_OUTCOME", outcome="MAYBE")
        self.rejected("INVALID_OUTCOME", outcome=None)
        self.rejected(
            "INVALID_OUTCOME",
            item=self.limited,
            outcome="UNABLE_TO_INSPECT",
            explanation="Synthetic.",
        )
        self.rejected("EXPLANATION_REQUIRED", outcome="UNABLE_TO_INSPECT")

    def test_quantity_items(self):
        self.record(item=self.masks, quantity=6)
        for label, fields in {
            "missing": {},
            "short of the exact target": {"quantity": 5},
            "above the target": {"quantity": 7},
            "boolean": {"quantity": True},
            "float": {"quantity": 6.0},
            "string": {"quantity": "6"},
            "negative": {"quantity": -1},
            "absurd": {"quantity": 10**7},
            "with a measurement": {"quantity": 6, "measurement": {"value": "1", "unit": "x"}},
        }.items():
            with self.subTest(label):
                self.rejected("INVALID_OBSERVATION", item=self.masks, **fields)
        # A Failed count may differ from the target (only Passed must equal it) but needs its
        # deficiency.
        self.rejected("DEFICIENCY_REQUIRED", item=self.masks, outcome="FAILED", quantity=2)
        self.rejected("INVALID_OBSERVATION", item=self.masks, outcome="FAILED")
        self.rejected(
            "INVALID_OBSERVATION",
            item=self.masks,
            outcome="UNABLE_TO_INSPECT",
            explanation="Synthetic.",
            quantity=1,
        )

    def test_a_quantity_item_derives_its_outcome_from_the_count(self):
        result = self.record(item=self.masks, outcome=None, quantity=6)
        row = CheckRecord.objects.get()
        self.assertEqual(
            (result.body["outcome"], row.outcome, row.quantity), ("PASSED",) * 2 + (6,)
        )
        audit = AuditEvent.objects.get(action="CHECK_RECORD")
        self.assertEqual(audit.new_snapshot["values"]["outcome_source"], "DERIVED")
        # A stated outcome is recorded as stated.
        self.record(item=self.masks, outcome="PASSED", quantity=6)
        stated = AuditEvent.objects.filter(action="CHECK_RECORD").order_by("-recorded_at").first()
        self.assertEqual(stated.new_snapshot["values"]["outcome_source"], "STATED")
        # Any count but the target derives Failed (shortage and surplus alike), which must name its
        # deficiency, so without one it is refused rather than lost.
        for count in (0, 5, 7):
            with self.subTest(count=count):
                error = self.rejected(
                    "DEFICIENCY_REQUIRED", item=self.masks, outcome=None, quantity=count
                )
                self.assertEqual(error.path, "deficiency")

    def test_the_crew_can_only_make_a_derived_quantity_outcome_worse(self):
        # A matching count may still be failed for condition...
        self.rejected("DEFICIENCY_REQUIRED", item=self.masks, outcome="FAILED", quantity=6)
        # ...but a mismatch can never be stated as Passed.
        self.rejected("INVALID_OBSERVATION", item=self.masks, outcome="PASSED", quantity=5)

    def test_the_outcome_may_be_omitted_only_when_a_count_can_decide_it(self):
        self.rejected("INVALID_OBSERVATION", item=self.masks, outcome=None)  # no count
        self.rejected("INVALID_OUTCOME", outcome=None)  # presence item
        self.rejected(
            "INVALID_OUTCOME",
            item=self.gauge,
            outcome=None,
            measurement={"value": "2200", "unit": "psi"},
        )

    def test_measurement_items(self):
        self.record(item=self.gauge, measurement={"value": "2200.5", "unit": "psi"})
        row = CheckRecord.objects.get()
        self.assertEqual((row.measurement_value, row.measurement_unit), (Decimal("2200.5"), "psi"))
        bad = {
            "missing": None,
            "wrong unit": {"value": "1", "unit": "kPa"},
            "blank unit": {"value": "1", "unit": " "},
            "not text": {"value": 1, "unit": "psi"},
            "NaN": {"value": "NaN", "unit": "psi"},
            "infinite": {"value": "Infinity", "unit": "psi"},
            "too large": {"value": "1e30", "unit": "psi"},
            "too many places": {"value": "0.12345678901", "unit": "psi"},
            "extra key": {"value": "1", "unit": "psi", "x": 1},
            "not an object": "12 psi",
        }
        for label, measurement in bad.items():
            with self.subTest(label):
                self.rejected("INVALID_OBSERVATION", item=self.gauge, measurement=measurement)
        self.rejected("INVALID_OBSERVATION", item=self.gauge, quantity=3)

    def test_other_kinds_take_no_observation_and_readings_are_not_checks(self):
        self.rejected("INVALID_OBSERVATION", quantity=1)
        self.rejected(
            "INVALID_OBSERVATION", item=self.special, measurement={"value": "1", "unit": "psi"}
        )
        self.rejected("NOT_YET_SUPPORTED", item=self.odometer)

    def test_free_text_and_instants_are_bounded(self):
        for label, fields in {
            "blank explanation": {"explanation": "   "},
            "oversize explanation": {"explanation": "x" * 4001},
            "non-text explanation": {"explanation": 5},
            "naive client time": {"client_observed_at": datetime(2026, 6, 1, 12)},
            "client time as text": {"client_observed_at": "2026-06-01T12:00:00Z"},
            "unknown purpose": {"purpose": "BONUS"},
        }.items():
            with self.subTest(label):
                self.rejected("MALFORMED_REQUEST", **fields)
        self.record(explanation="x" * 4000, item=self.limited)

    def test_ids_must_be_canonical_lowercase_and_the_operation_id_version_4(self):
        for label, fields in {
            "vehicle": {"vehicle_id": "not-a-uuid"},
            "item": {"item_id": str(uuid4()).upper()},
            "version": {"checklist_version_id": None},
            "operation id v1": {"operation_id": str(uuid1())},
            "operation id junk": {"operation_id": "x"},
        }.items():
            with self.subTest(label), self.assertRaises(CheckError) as caught:
                self.record(**fields)
            self.assertEqual(caught.exception.code, "MALFORMED_REQUEST")
        self.assertEqual(CheckRecord.objects.count(), 0)

    def test_the_purpose_and_the_verification_target_must_agree(self):
        for label, (code, fields) in {
            "verification without a target": (
                "VERIFICATION_TARGET_REQUIRED",
                {"purpose": "VERIFICATION"},
            ),
            "routine with a target": (
                "VERIFICATION_TARGET_NOT_ALLOWED",
                {"verification_for_deficiency_id": str(uuid4())},
            ),
        }.items():
            with self.subTest(label):
                error = self.rejected(code, **fields)
                self.assertEqual(error.path, "verification_for_deficiency_id")

    def test_a_verification_target_that_does_not_exist_is_refused(self):
        error = self.rejected(
            "NOT_FOUND", purpose="VERIFICATION", verification_for_deficiency_id=str(uuid4())
        )
        self.assertEqual(error.status, 404)

    def test_the_kit_rule_always_names_an_instance_for_kit_items_and_none_otherwise(self):
        one, two = (("k1", "A"),), (("k1", "A"), ("k2", "B"))
        self.assertIsNone(chosen_kit_id(False, (), None))
        self.assertEqual(chosen_kit_id(True, one, "k1"), "k1")
        self.assertEqual(chosen_kit_id(True, two, "k2"), "k2")
        for in_kit, kits, requested, code in (
            (True, one, None, "KIT_REQUIRED"),  # even a single kit must be named
            (True, two, None, "KIT_REQUIRED"),
            (True, one, "k2", "KIT_NOT_ON_VEHICLE"),
            (True, (), None, "KIT_NOT_ON_VEHICLE"),  # no instance: nothing to check
            (True, (), "k1", "KIT_NOT_ON_VEHICLE"),
            (False, (), "k1", "KIT_NOT_ON_VEHICLE"),
        ):
            with self.subTest(in_kit=in_kit, kits=kits, requested=requested):
                with self.assertRaises(CheckError) as caught:
                    chosen_kit_id(in_kit, kits, requested)
                self.assertEqual((caught.exception.code, caught.exception.path), (code, "kit_id"))


class VehicleSpecificTests(CheckCase):
    """The check follows the vehicle's own effective checklist, not just the template."""

    def test_an_override_changes_the_definition_target_and_membership_for_its_vehicle_only(self):
        from cya.checklists import commands as checklist_commands
        from cya.checklists.models import ChecklistItemDef

        checklist_commands.publish_override_version(
            override_version_id=checklist_commands.save_override_draft(
                vehicle_id=str(self.vehicle.pk),
                template_version_id=self.version_id,
                entries=[
                    {"item_id": str(self.suction.pk), "action": "REMOVE"},
                    {
                        "item_id": str(self.masks.pk),
                        "action": "REPLACE",
                        "definition": self.spec(
                            self.masks, 20, kind="QUANTITY", expected_quantity=4
                        ),
                    },
                ],
                **self.context(),
            ).body["override_version_id"],
            **self.context(),
        )
        other = self.vehicles[1]
        with self.assertRaises(CheckError) as removed:
            self.record()
        self.assertEqual(removed.exception.code, "ITEM_NOT_IN_CHECKLIST")
        with self.assertRaises(CheckError) as stale_target:
            self.record(item=self.masks, quantity=6)
        self.assertEqual(stale_target.exception.code, "INVALID_OBSERVATION")
        self.record(item=self.masks, quantity=4)
        override_def = ChecklistItemDef.objects.get(
            override_version__isnull=False, item=self.masks
        ).pk
        self.assertEqual(CheckRecord.objects.get(item=self.masks).item_def_id, override_def)
        # The other vehicle still follows the template: suction exists and masks target 6.
        self.record(vehicle=other)
        self.record(vehicle=other, item=self.masks, quantity=6)
        template_def = ChecklistItemDef.objects.get(
            checklist_version_id=self.version_id, item=self.masks
        ).pk
        self.assertEqual(
            CheckRecord.objects.get(item=self.masks, vehicle=other).item_def_id, template_def
        )

    def test_an_active_stocking_deviation_sets_the_exact_target_for_its_vehicle(self):
        from datetime import timedelta

        from django.utils import timezone

        from cya.checklists import commands as checklist_commands

        checklist_commands.record_stocking_deviation(
            vehicle_id=str(self.vehicle.pk),
            item_id=str(self.masks.pk),
            template_id=str(self.template.pk),
            alternate_quantity=3,
            valid_from=None,
            valid_until=timezone.now() + timedelta(days=1),
            explanation="Synthetic restock delay.",
            **self.context(),
        )
        with self.assertRaises(CheckError) as old_target:
            self.record(item=self.masks, quantity=6)
        self.assertEqual(old_target.exception.code, "INVALID_OBSERVATION")
        self.record(item=self.masks, quantity=3)
        derived = self.record(item=self.masks, outcome=None, quantity=3)
        self.assertEqual(derived.body["outcome"], "PASSED")
        with self.assertRaises(CheckError) as shortfall:  # the normal 6 now derives Failed
            self.record(item=self.masks, outcome=None, quantity=6)
        self.assertEqual(shortfall.exception.code, "DEFICIENCY_REQUIRED")
        # The deviation belongs to one vehicle; the other still needs the normal target.
        self.record(item=self.masks, quantity=6, vehicle=self.vehicles[1])

    def test_each_kit_instance_is_checked_on_its_own(self):
        from cya.checklists.models import Compartment, Item, KitInstance

        kit_item = Item.objects.create(organization=self.org, label="Synthetic kit contents")
        kit_a = KitInstance.objects.create(
            organization=self.org, label="Kit A", vehicle=self.vehicle, compartment=self.cabinet
        )
        self.version_id = self.published_version(
            [*self.items, self.spec(kit_item, 80, kit_id=str(kit_a.pk))]
        )
        other = self.vehicles[1]
        other_cabinet = Compartment.objects.get(
            vehicle=other, stable_slot_id=self.cabinet.stable_slot_id
        )

        def refused(code, **fields):
            with self.assertRaises(CheckError) as caught:
                self.record(item=kit_item, **fields)
            self.assertEqual((caught.exception.code, caught.exception.path), (code, "kit_id"))

        refused("KIT_NOT_ON_VEHICLE", vehicle=other)  # zero instances: nothing to check
        refused("KIT_REQUIRED")  # one instance still has to be named
        self.record(item=kit_item, kit_id=str(kit_a.pk))
        kit_b = KitInstance.objects.create(
            organization=self.org, label="Kit B", vehicle=self.vehicle, compartment=self.cabinet
        )
        refused("KIT_REQUIRED")
        self.record(item=kit_item, kit_id=str(kit_b.pk))
        foreign = KitInstance.objects.create(
            organization=self.org, label="Kit C", vehicle=other, compartment=other_cabinet
        )
        refused("KIT_NOT_ON_VEHICLE", kit_id=str(foreign.pk))
        refused("KIT_NOT_ON_VEHICLE", kit_id=str(uuid4()))
        with self.assertRaises(CheckError) as outside:  # an item that is not in a kit
            self.record(kit_id=str(kit_a.pk))
        self.assertEqual(outside.exception.code, "KIT_NOT_ON_VEHICLE")
        rows = CheckRecord.objects.filter(item=kit_item).order_by("recorded_at")
        # Checking Kit A recorded nothing for Kit B: two checks, one per instance.
        self.assertEqual(
            [(r.vehicle_id, r.kit_id) for r in rows],
            [(self.vehicle.pk, kit_a.pk), (self.vehicle.pk, kit_b.pk)],
        )
