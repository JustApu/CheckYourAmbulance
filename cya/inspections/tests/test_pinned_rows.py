"""A session lists its pinned version's rows after a newer version is published (SPEC CHK-4, 5.7.1).

Issue #256 Part D, acceptance criterion "Part D, pinned version": with a session open on
checklist version v1, publish v2 of that checklist. The session screen still lists v1's rows,
including an item v2 removes and not an item v2 adds. A check recorded there is accepted and links
under SCH-6, and each row shows the vehicle's current state for that item. Before Part D the
screen filtered the current rows by the pinned version, so such a session listed no rows.
"""

from datetime import timedelta

from django.test import override_settings

from cya.attention.checklist import ChecklistQueryError, vehicle_checklist
from cya.attention.tests.support import AttentionCase
from cya.checklists import commands as checklist_commands
from cya.checklists.models import Item
from cya.checks.commands import record_check
from cya.checks.models import CheckObligationLink
from cya.inspections import commands
from cya.inspections.models import InspectionSession
from cya.obligations.tests.test_requirement_obligations import RequirementObligationCase
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings


class PinnedVersionMixin:
    def open_session(self, actor):
        session_id = commands.open_or_join_session(
            vehicle_id=str(self.vehicle.pk),
            template_id=str(self.template.pk),
            **self.context(actor),
        ).body["session_id"]
        return InspectionSession.objects.select_related("checklist_version").get(pk=session_id)

    def publish_v2(self):
        """v2 drops the limited item and adds one of its own, published after the session."""
        self.clock.set(self.clock.now + timedelta(minutes=1))
        self.added = Item.objects.create(organization=self.org, label="Synthetic added")
        items = [s for s in self.items if s["item_id"] != str(self.limited.pk)]
        self.v2 = self.published_version([*items, self.spec(self.added, 90)])
        self.assertEqual(self.generate().failures, [])
        self.clock.set(self.clock.now + timedelta(minutes=1))

    def ids(self, rows):
        return [r.item_id for r in rows]


class PinnedVersionRowTests(PinnedVersionMixin, AttentionCase):
    def setUp(self):
        super().setUp()
        self.v1 = self.version_id
        # Shortly before the first inspection (06:00), so the session is still in progress then.
        self.due = self.clock.now.replace(hour=6, minute=0, second=0, microsecond=0)
        self.clock.set(self.due - timedelta(minutes=10))
        self.session = self.open_session(self.crew)
        self.publish_v2()

    def session_rows(self, **kw):
        return vehicle_checklist(
            str(self.org.pk), str(self.vehicle.pk), session=self.session, **kw
        ).rows

    def test_the_session_lists_its_pinned_versions_rows(self):
        current = self.rows()
        self.assertIn(str(self.added.pk), self.ids(current))
        self.assertNotIn(str(self.limited.pk), self.ids(current))
        # Before Part D: filtering the current rows by the pinned version listed nothing.
        self.assertEqual(
            vehicle_checklist(
                str(self.org.pk), str(self.vehicle.pk), checklist_version_id=self.v1
            ).rows,
            (),
        )
        rows = self.session_rows()
        self.assertIn(str(self.limited.pk), self.ids(rows))  # v2 removes it
        self.assertNotIn(str(self.added.pk), self.ids(rows))  # v2 adds it
        self.assertEqual(sorted(set(self.ids(rows))), sorted({s["item_id"] for s in self.items}))
        self.assertTrue(all(r.checklist_version_id == self.v1 for r in rows))
        result = vehicle_checklist(str(self.org.pk), str(self.vehicle.pk), session=self.session)
        self.assertEqual(sum(g["total"] for g in result.group_progress), len(result.rows))

    def record_in_session(self, item):
        return record_check(
            vehicle_id=str(self.vehicle.pk),
            item_id=str(item.pk),
            checklist_version_id=self.v1,
            outcome="PASSED",
            purpose="ROUTINE",
            session_id=str(self.session.pk),
            **self.context(self.crew),
        ).body

    def links(self, check_id):
        return list(
            CheckObligationLink.objects.filter(check_record_id=check_id).select_related(
                "obligation"
            )
        )

    def test_a_check_recorded_there_is_accepted_and_linked(self):
        self.clock.set(self.due + timedelta(minutes=5))  # in the window of the 06:00 inspection
        # An item v2 keeps: the check completes the obligation its row shows (SCH-6).
        before = next(r for r in self.session_rows() if r.item_id == str(self.suction.pk))
        self.assertEqual((before.obligation_state, before.due_at), ("DUE", self.due))
        recorded = self.record_in_session(self.suction)
        links = self.links(recorded["check_id"])
        self.assertEqual(len(recorded["obligation_links"]), len(links))
        self.assertEqual(
            {(str(link.obligation.item_id), link.obligation.due_at) for link in links},
            {(str(self.suction.pk), self.due)},
        )
        after = next(r for r in self.session_rows() if r.item_id == str(self.suction.pk))
        self.assertEqual(
            (after.latest_check_id, after.obligation_state),
            (recorded["check_id"], "SATISFIED"),
        )
        # The item v2 removes: its 06:00 obligation, created under v1, is still owed, and a check
        # in the session pinned to v1 is accepted and completes it.
        removed = next(r for r in self.session_rows() if r.item_id == str(self.limited.pk))
        self.assertEqual((removed.obligation_state, removed.due_at), ("DUE", self.due))
        recorded = self.record_in_session(self.limited)
        self.assertEqual(recorded["code"], "CHECK_RECORDED")
        self.assertEqual(
            {
                (str(link.obligation.item_id), link.obligation.due_at)
                for link in self.links(recorded["check_id"])
            },
            {(str(self.limited.pk), self.due)},
        )
        after = next(r for r in self.session_rows() if r.item_id == str(self.limited.pk))
        self.assertEqual(
            (after.latest_check_id, after.obligation_state),
            (recorded["check_id"], "SATISFIED"),
        )

    def test_each_row_shows_the_vehicles_current_state(self):
        # A check outside the session, against the current version, shows on the session's row.
        recorded = record_check(
            vehicle_id=str(self.vehicle.pk),
            item_id=str(self.suction.pk),
            checklist_version_id=self.v2,
            outcome="PASSED",
            purpose="ROUTINE",
            **self.context(self.supervisor),
        ).body["check_id"]
        pinned = next(r for r in self.session_rows() if r.item_id == str(self.suction.pk))
        current = next(r for r in self.rows() if r.item_id == str(self.suction.pk))
        self.assertEqual(pinned.latest_check_id, recorded)
        self.assertEqual(
            (pinned.obligation_state, pinned.due_at, pinned.attention, pinned.deficiencies),
            (current.obligation_state, current.due_at, current.attention, current.deficiencies),
        )

    def test_a_session_on_another_vehicle_or_with_a_version_is_refused(self):
        with self.assertRaises(ChecklistQueryError) as caught:
            vehicle_checklist(str(self.org.pk), str(self.vehicles[1].pk), session=self.session)
        self.assertEqual((caught.exception.code, caught.exception.status), ("NOT_FOUND", 404))
        with self.assertRaises(ChecklistQueryError) as caught:
            vehicle_checklist(
                str(self.org.pk),
                str(self.vehicle.pk),
                session=self.session,
                checklist_version_id=self.v1,
            )
        self.assertEqual(caught.exception.status, 400)

    def test_a_count_target_is_the_one_a_check_is_judged_against_now(self):
        def masks():
            return next(r for r in self.session_rows() if r.item_id == str(self.masks.pk))

        self.assertEqual(masks().expected_quantity, 6)
        # A stocking deviation recorded after the session opened applies to a count recorded
        # in it now (cya.checks.commands._resolved_item), so the pinned row shows it too.
        checklist_commands.record_stocking_deviation(
            vehicle_id=str(self.vehicle.pk),
            item_id=str(self.masks.pk),
            template_id=str(self.template.pk),
            alternate_quantity=3,
            valid_from=None,
            valid_until=self.clock.now + timedelta(days=1),
            explanation="Synthetic restock delay.",
            **self.context(),
        )
        self.assertEqual(masks().expected_quantity, 3)
        recorded = record_check(
            vehicle_id=str(self.vehicle.pk),
            item_id=str(self.masks.pk),
            checklist_version_id=self.v1,
            outcome="PASSED",
            purpose="ROUTINE",
            quantity=3,
            session_id=str(self.session.pk),
            **self.context(self.crew),
        ).body
        self.assertEqual(recorded["code"], "CHECK_RECORDED")


@override_settings(**real_collaborator_settings())
class PinnedVersionApiTests(PinnedVersionMixin, AttentionCase):
    def make_org(self):
        self.harness = RoleHttpHarness()
        return self.harness.org

    def setUp(self):
        super().setUp()
        self.client, self.user = self.harness.sign_in("crew-http")
        self.session = self.open_session(self.user)
        self.publish_v2()

    def get(self, query, vehicle=None):
        vehicle = vehicle or self.vehicle
        return self.client.get(f"/api/v1/vehicles/{vehicle.pk}/checklist{query}")

    def test_a_participant_reads_the_pinned_rows(self):
        body = self.get(f"?needs_attention_only=false&session_id={self.session.pk}").json()
        ids = [i["item_id"] for i in body["items"]]
        self.assertIn(str(self.limited.pk), ids)
        self.assertNotIn(str(self.added.pk), ids)
        self.assertEqual({i["checklist_version_id"] for i in body["items"]}, {self.version_id})

    def test_others_cannot_tell_whether_a_session_exists(self):
        other = self.open_session(self.supervisor)  # joins: the supervisor is a participant
        self.assertEqual(other.pk, self.session.pk)
        unknown = "00000000-0000-4000-8000-000000000000"
        for query, vehicle in (
            (f"?session_id={unknown}", None),
            (f"?session_id={self.session.pk}", self.vehicles[1]),
        ):
            with self.subTest(query=query):
                response = self.get(query, vehicle)
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (404, "NOT_FOUND")
                )

    def test_a_non_participant_gets_not_found(self):
        _client, stranger = self.harness.sign_in("crew-http-2")
        response = _client.get(
            f"/api/v1/vehicles/{self.vehicle.pk}/checklist?session_id={self.session.pk}"
        )
        self.assertEqual((response.status_code, response.json()["code"]), (404, "NOT_FOUND"))

    def test_malformed_or_conflicting_queries_are_client_errors(self):
        response = self.get("?session_id=not-a-uuid")
        self.assertEqual(
            (response.status_code, response.json()["code"]), (400, "MALFORMED_REQUEST")
        )

    def test_a_session_and_a_version_together_are_malformed(self):
        unknown = "00000000-0000-4000-8000-000000000000"
        for session_id in (self.session.pk, unknown):
            with self.subTest(session_id=session_id):
                response = self.get(
                    f"?session_id={session_id}&checklist_version_id={self.version_id}"
                )
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (400, "MALFORMED_REQUEST")
                )


class PinnedTwoPlaceTests(RequirementObligationCase):
    """After the item-requirement cutover (SPEC 6.0.0 CHK-10, CHK-11), one item kept in two places
    is two rows with two obligation streams. Sessions pinned to each place's checklist keep that
    keying: each row has its own obligation and latest check, and a check at one place leaves the
    other as it was (issue #256 Part D after C2, #263)."""

    def setUp(self):
        super().setUp()
        self.record_item_cutover()
        # Checklist A keeps suction in the cabinet; checklist B keeps it on the system.
        self.a_v1 = self.publish_checklist(self.a, [self.suction_spec()])
        self.b_v1 = self.publish_checklist(
            self.b, [self.suction_spec(compartment_id=None, system_id=self.system_row())]
        )
        self.assertEqual(self.generate().failures, [])
        self.due = self.at(days=2, hours=6)
        self.clock.set(self.due - timedelta(minutes=10))
        self.session_a = self.open_session(self.a)
        self.session_b = self.open_session(self.b)
        # A newer A, published after the sessions opened, so session A's rows come from its pin.
        self.clock.set(self.clock.now + timedelta(minutes=1))
        self.publish_checklist(self.a, [self.suction_spec(), self.masks_spec()])
        self.assertEqual(self.generate().failures, [])
        self.requirement_a = str(self.version_of(self.a_v1, self.suction).requirement_id)
        self.requirement_b = str(self.version_of(self.b_v1, self.suction).requirement_id)
        self.assertNotEqual(self.requirement_a, self.requirement_b)

    def open_session(self, checklist):
        session_id = commands.open_or_join_session(
            vehicle_id=str(self.vehicle.pk),
            template_id=str(checklist.pk),
            **self.context(self.crew),
        ).body["session_id"]
        return InspectionSession.objects.select_related("checklist_version").get(pk=session_id)

    def suction_row(self, session):
        rows = vehicle_checklist(str(self.org.pk), str(self.vehicle.pk), session=session).rows
        (row,) = [r for r in rows if r.item_id == str(self.suction.pk)]
        return row

    def test_each_pinned_place_keeps_its_own_state(self):
        a, b = self.suction_row(self.session_a), self.suction_row(self.session_b)
        self.assertEqual(
            (a.item_requirement_id, b.item_requirement_id),
            (self.requirement_a, self.requirement_b),
        )
        self.assertTrue(a.kit_id is None and b.kit_id is None)
        self.assertNotIn(
            str(self.masks.pk),
            [
                r.item_id
                for r in vehicle_checklist(
                    str(self.org.pk), str(self.vehicle.pk), session=self.session_a
                ).rows
            ],
        )
        self.clock.set(self.due + timedelta(minutes=5))  # in the window of the 06:00 inspection
        a, b = self.suction_row(self.session_a), self.suction_row(self.session_b)
        self.assertEqual(
            [(r.obligation_state, r.due_at, r.latest_check_id) for r in (a, b)],
            [("DUE", self.due, None)] * 2,
        )

        recorded = record_check(
            vehicle_id=str(self.vehicle.pk),
            item_id=str(self.suction.pk),
            checklist_version_id=self.a_v1,
            outcome="PASSED",
            purpose="ROUTINE",
            session_id=str(self.session_a.pk),
            **self.context(self.crew),
        ).body
        self.assertEqual(recorded["code"], "CHECK_RECORDED")
        links = CheckObligationLink.objects.filter(
            check_record_id=recorded["check_id"]
        ).select_related("obligation")
        self.assertEqual(
            {(str(link.obligation.kit_key), link.obligation.due_at) for link in links},
            {(self.requirement_a, self.due)},
        )

        a, b = self.suction_row(self.session_a), self.suction_row(self.session_b)
        self.assertEqual(
            (a.item_requirement_id, a.latest_check_id, a.obligation_state),
            (self.requirement_a, recorded["check_id"], "SATISFIED"),
        )
        self.assertEqual(
            (b.item_requirement_id, b.latest_check_id, b.obligation_state, b.due_at),
            (self.requirement_b, None, "DUE", self.due),
        )
