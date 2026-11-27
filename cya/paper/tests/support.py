"""Fixtures for paper-entry tests (issue #9 Part B)."""

from datetime import UTC, date, datetime, time, timedelta

from cya.checklists import commands as checklist_commands
from cya.checklists.models import Item
from cya.checks.commands import record_check
from cya.kits.definitions import (
    create_kit_definition,
    publish_kit_definition_version,
    save_kit_definition_draft,
)
from cya.obligations.projections import get_vehicle_obligations_as_of
from cya.obligations.tests.support import ObligationCase
from cya.paper.commands import PaperEntryError, record_paper_entry_batch
from cya.paper.models import PaperEntryBatch

ZONE = "America/New_York"


class PaperCase(ObligationCase):
    """Vehicle A has its own checklist (after the cutover) placing a synthetic jump kit.

    The station zone is New York, so on 2026-06-01 local time is UTC-4. The clock starts at
    08:00 local; the default paper time is 07:30 local, after every publication of setUp.
    """

    def make_org(self):
        org = super().make_org()
        org.station_zone = ZONE
        org.save(update_fields=["station_zone"])
        return org

    def setUp(self):
        super().setUp()
        self.officer = self.make_user("officer")
        self.grant(self.officer, "SUPPLY_OFFICER")
        self.record_cutover()
        self.kit_item = Item.objects.create(organization=self.org, label="Synthetic kit contents")
        self.definition = create_kit_definition(
            key="jump-kit", label="Synthetic jump kit", **self.context(self.supervisor)
        ).body["definition_id"]
        self.kit_version = self.kit_draft()
        publish_kit_definition_version(version_id=self.kit_version, **self.context(self.supervisor))
        self.checklist = self.vehicle_checklist()
        self.version = self.publish_own(placements=True)
        self.clock.set(self.at(hours=12))  # 2026-06-01 08:00 local

    def kit_draft(self):
        return save_kit_definition_draft(
            definition_id=self.definition,
            contents=[
                {
                    "item_id": str(self.kit_item.pk),
                    "display_order": 1,
                    "kind": "PRESENCE_CONDITION",
                    "allowed_outcomes": ["PASSED", "FAILED"],
                }
            ],
            **self.context(self.supervisor),
        ).body["version_id"]

    def publish_own(self, placements=False, checklist=None, vehicle=None, effective_at=None):
        version_id = checklist_commands.save_template_draft(
            template_id=str((checklist or self.checklist).pk),
            items=self.own_items(vehicle),
            placements=(
                [{"definition_id": self.definition, "display_order": 90}] if placements else []
            ),
            **self.context(),
        ).body["version_id"]
        self.publish(version_id, effective_at)
        return version_id

    def enter(self, actor=None, performers=None, **overrides):
        actor = actor or self.crew
        request = {
            "vehicle_id": str(self.vehicle.pk),
            "checklist_version_id": self.version,
            "performed_on": date(2026, 6, 1),
            "performed_time": time(7, 30),  # 11:30 UTC, after every publication above
            "performer_ids": [str(u.pk) for u in (performers or [actor])],
            "kit_definition_version_ids": [],
        }
        request.update(overrides)
        return record_paper_entry_batch(**request, **self.context(actor))

    def refused(self, code, **kwargs):
        before = PaperEntryBatch.objects.count()
        with self.assertRaises(PaperEntryError) as caught:
            self.enter(**kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(PaperEntryBatch.objects.count(), before)
        return caught.exception


JUNE_2 = datetime(2026, 6, 2, tzinfo=UTC)
ENTRY = datetime(2026, 6, 3, 12, tzinfo=UTC)


class PaperCheckCase(PaperCase):
    def setUp(self):
        super().setUp()
        self.publish_schedule("test-0600")
        self.bind(template=self.checklist)
        self.generate()
        self.clock.set(ENTRY)

    def batch(self, actor=None, performers=None, at=time(6, 15), on=date(2026, 6, 2)):
        return self.enter(
            actor=actor, performers=performers, performed_on=on, performed_time=at
        ).body["batch_id"]

    def paper(self, batch_id, actor=None, item=None, **fields):
        args = {
            "vehicle_id": str(self.vehicle.pk),
            "item_id": str((item or self.suction).pk),
            "checklist_version_id": self.version,
            "outcome": "PASSED",
            "paper_entry_batch_id": batch_id,
            **fields,
        }
        return record_check(**args, **self.context(actor or self.crew))

    def live(self, actor=None, at=None, **fields):
        if at is not None:
            self.clock.set(at)
        args = {
            "vehicle_id": str(self.vehicle.pk),
            "item_id": str(self.suction.pk),
            "checklist_version_id": self.version,
            "outcome": "PASSED",
            **fields,
        }
        result = record_check(**args, **self.context(actor or self.crew))
        self.clock.set(ENTRY)
        return result

    def june_2_obligation(self):
        found = [
            o
            for o in self.obligations(vehicle=self.vehicle)
            if str(o.item_id) == str(self.suction.pk) and o.due_at == JUNE_2.replace(hour=10)
        ]
        self.assertEqual(len(found), 1)
        return found[0]

    def state_at(self, instant):
        obligation = self.june_2_obligation()
        states = get_vehicle_obligations_as_of(
            str(self.vehicle.pk),
            instant,
            organization_id=str(self.org.pk),
            start=JUNE_2,
            end=JUNE_2 + timedelta(days=1),
        )
        return next(s for s in states if s.obligation.id == str(obligation.pk))
