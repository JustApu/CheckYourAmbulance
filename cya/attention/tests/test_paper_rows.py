"""Checklist rows of a paper-entry batch's sheet (SPEC 8.0.0 LATE-4 to LATE-6; issue #9 B6a).

The vehicle's own checklist places a synthetic jump kit. A batch says the sheet was performed on
2026-06-02 at 06:15 New York time (10:15Z); it is entered on 2026-06-03 (PaperCheckCase).
"""

from datetime import date, time

from cya.attention.api import checklist_row
from cya.attention.checklist import ChecklistQueryError, vehicle_checklist
from cya.checklists import commands as checklist_commands
from cya.checks.commands import CheckError, correct_check, record_check
from cya.kits.commands import add_kit_instance, record_kit_assignment
from cya.paper.invalidation import invalidate_paper_entry_batch
from cya.paper.models import PaperEntryBatch
from cya.paper.readers import batch_effective_checks, check_paper_sources
from cya.paper.tests.support import ENTRY, JUNE_2, PaperCase, PaperCheckCase


class PaperRowsCase(PaperCheckCase):
    def sheet(self, actor=None, performers=None, kits=None):
        result = self.enter(
            actor=actor,
            performers=performers,
            performed_on=date(2026, 6, 2),
            performed_time=time(6, 15),
            kit_definition_version_ids=kits or [],
        )
        return PaperEntryBatch.objects.get(pk=result.body["batch_id"])

    def rows(self, batch, **kwargs):
        return vehicle_checklist(
            str(self.org.pk), str(self.vehicle.pk), paper_batch=batch, **kwargs
        )

    def row_of(self, rows, item, kit_id=None):
        found = [
            r for r in rows if r.item_id == str(item.pk) and (kit_id is None or r.kit_id == kit_id)
        ]
        self.assertEqual(len(found), 1, [r.item_label for r in rows])
        return found[0]


class PrintedVersionRowsTests(PaperRowsCase):
    def test_the_rows_are_the_printed_versions_even_after_a_replacement(self):
        batch = self.sheet()
        # A newer version, without the siren, is in force from now.
        newer = checklist_commands.save_template_draft(
            template_id=str(self.checklist.pk),
            items=self.own_items()[:2],
            placements=[{"definition_id": self.definition, "display_order": 90}],
            **self.context(),
        ).body["version_id"]
        self.publish(newer)
        paper = self.rows(batch).rows
        self.assertEqual({r.checklist_version_id for r in paper}, {self.version})
        self.row_of(paper, self.siren)  # still on the printed sheet
        live = vehicle_checklist(str(self.org.pk), str(self.vehicle.pk)).rows
        self.assertNotIn(str(self.siren.pk), [r.item_id for r in live])

    def test_each_row_names_the_batch_check_through_correction_and_invalidation(self):
        batch = self.sheet()
        self.assertIsNone(self.row_of(self.rows(batch).rows, self.masks).paper_entry_check_id)
        first = self.paper(str(batch.pk), item=self.masks, quantity=6).body["check_id"]
        row = self.row_of(self.rows(batch).rows, self.masks)
        self.assertEqual(
            (row.paper_entry_check_id, row.paper_entry_check_outcome), (first, "PASSED")
        )
        self.assertIsNone(self.row_of(self.rows(batch).rows, self.suction).paper_entry_check_id)
        corrected = correct_check(
            check_id=first,
            expected_tip_check_id=first,
            outcome="PASSED",
            quantity=6,
            **self.context(self.crew),
        ).body["check_id"]
        row = self.row_of(self.rows(batch).rows, self.masks)
        self.assertEqual(
            (row.paper_entry_check_id, row.paper_entry_check_outcome), (corrected, "PASSED")
        )
        invalidate_paper_entry_batch(
            batch_id=str(batch.pk),
            explanation="Synthetic wrong time.",
            **self.context(self.supervisor),
        )
        self.assertIsNone(self.row_of(self.rows(batch).rows, self.masks).paper_entry_check_id)
        self.assertEqual(batch_effective_checks(str(self.org.pk), str(batch.pk), ENTRY), {})

    def test_a_live_read_names_no_batch_check(self):
        batch = self.sheet()
        self.paper(str(batch.pk))
        live = vehicle_checklist(str(self.org.pk), str(self.vehicle.pk)).rows
        self.assertTrue(all(r.paper_entry_check_id is None for r in live))
        wire = checklist_row(self.row_of(live, self.suction), None)
        self.assertIsNone(wire["paper_entry_check"])
        self.assertIsNone(wire["latest_check_paper"], "the paper check is not the newest here")

    def test_an_item_read_matches_the_whole_list(self):
        batch = self.sheet()
        self.paper(str(batch.pk))
        whole = self.row_of(self.rows(batch).rows, self.suction)
        partial = self.rows(batch, item_ids=[str(self.suction.pk)])
        self.assertEqual(list(partial.rows), [whole])
        self.assertIsNone(partial.group_progress)

    def test_a_batch_is_given_alone_and_only_for_its_own_vehicle(self):
        batch = self.sheet()
        with self.assertRaises(ChecklistQueryError) as caught:
            self.rows(batch, checklist_version_id=self.version)
        self.assertEqual(
            (caught.exception.code, caught.exception.status), ("INVALID_CHECKLIST_VERSION", 400)
        )
        with self.assertRaises(ChecklistQueryError) as caught:
            vehicle_checklist(str(self.org.pk), str(self.vehicles[1].pk), paper_batch=batch)
        self.assertEqual((caught.exception.code, caught.exception.status), ("NOT_FOUND", 404))


class PaperSourceTests(PaperRowsCase):
    def test_a_paper_checks_performers_and_entry_actor_are_named(self):
        live = self.live(actor=self.crew, at=JUNE_2.replace(hour=9, minute=30)).body["check_id"]
        batch = self.sheet(actor=self.supervisor, performers=[self.crew, self.admin])
        paper = self.paper(str(batch.pk), actor=self.supervisor).body["check_id"]
        sources = check_paper_sources(str(self.org.pk), [paper, live])
        self.assertEqual(list(sources), [paper])
        self.assertEqual(
            sources[paper]["performers"],
            sorted([self.crew.display_name, self.admin.display_name]),
        )
        self.assertEqual(sources[paper]["entered_by"], self.supervisor.display_name)
        row = self.row_of(self.rows(batch).rows, self.suction)
        self.assertEqual(row.latest_check_id, paper)
        wire = checklist_row(row, self.supervisor.display_name, sources)
        self.assertEqual(wire["latest_check_paper"]["performers"], sources[paper]["performers"])
        self.assertIsInstance(wire["latest_check_paper"]["entered_at"], str)
        self.assertEqual(wire["paper_entry_check"], {"check_id": paper, "outcome": "PASSED"})


class PrintedKitRowsTests(PaperRowsCase):
    def setUp(self):
        PaperCase.setUp(self)  # 2026-06-01 08:00 local, before the sheet's time
        self.kit_id = add_kit_instance(
            label="Synthetic jump kit 1",
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str(self.own_row(self.cabinet, self.vehicle).pk),
            definition_id=self.definition,
            **self.context(self.supervisor),
        ).body["kit_id"]
        self.publish_schedule("test-0600")
        self.bind(template=self.checklist)
        self.generate()
        self.clock.set(ENTRY)

    def kit_rows(self, rows):
        return [r for r in rows if r.item_id == str(self.kit_item.pk) and r.kit_id == self.kit_id]

    def test_kit_content_is_listed_only_through_the_printed_kit_version(self):
        self.assertEqual(self.kit_rows(self.rows(self.sheet()).rows), [])
        printed = self.sheet(kits=[self.kit_version])
        self.assertEqual(len(self.kit_rows(self.rows(printed).rows)), 1)

    def test_a_kit_moved_after_the_sheets_time_is_still_on_its_sheet(self):
        batch = self.sheet(kits=[self.kit_version])
        copied = record_check(
            vehicle_id=str(self.vehicle.pk),
            item_id=str(self.kit_item.pk),
            kit_id=self.kit_id,
            checklist_version_id=self.version,
            outcome="PASSED",
            paper_entry_batch_id=str(batch.pk),
            **self.context(self.crew),
        ).body["check_id"]
        record_kit_assignment(
            kit_id=self.kit_id,
            active=True,
            vehicle_id=str(self.vehicles[1].pk),
            compartment_id=str(self.own_row(self.cabinet, self.vehicles[1]).pk),
            **self.context(self.supervisor),
        )
        rows = self.kit_rows(self.rows(batch).rows)
        self.assertEqual([r.paper_entry_check_id for r in rows], [copied])
        live = vehicle_checklist(str(self.org.pk), str(self.vehicle.pk)).rows
        self.assertEqual(self.kit_rows(live), [])
        with self.assertRaises(CheckError) as caught:
            record_check(
                vehicle_id=str(self.vehicle.pk),
                item_id=str(self.kit_item.pk),
                kit_id=self.kit_id,
                checklist_version_id=self.version,
                outcome="PASSED",
                paper_entry_batch_id=str(batch.pk),
                acknowledges_check_id=copied,
                **self.context(self.crew),
            )
        self.assertEqual(caught.exception.code, "KIT_MOVED_SINCE_PAPER")
