"""Informational deficiency links and the read-only match suggestions (SPEC DEF-1, DEF-2)."""

from uuid import uuid4

from cya.core.audit_commands import CommandDenied
from cya.core.models import AuditEvent
from cya.deficiencies import service
from cya.deficiencies.commands import add_link, remove_link
from cya.deficiencies.models import (
    DeficiencyEvent,
    DeficiencyLink,
    DeficiencyProjection,
)
from cya.deficiencies.service import DeficiencyError
from cya.deficiencies.tests.support import DeficiencyCase


class LinkTests(DeficiencyCase):
    def setUp(self):
        super().setUp()
        self.first = self.failed().body["deficiency_ids"][0]
        self.second = self.failed(self.siren, actor=self.supervisor).body["deficiency_ids"][0]

    def link(self, change=add_link, a=None, b=None, *, actor=None, **extra):
        return change(
            deficiency_id=a or self.first,
            related_deficiency_id=b or self.second,
            **self.context(actor or self.crew) | extra,
        )

    def rejected(self, code, change=add_link, status=422, **kwargs):
        before = self.counts()
        with self.assertRaises(DeficiencyError) as caught:
            self.link(change, **kwargs)
        self.assertEqual((caught.exception.code, caught.exception.status), (code, status))
        self.assertEqual(self.counts(), before)

    def states(self):
        return (
            list(DeficiencyProjection.objects.order_by("pk").values()),
            DeficiencyEvent.objects.count(),
        )

    def test_a_link_is_stored_low_id_first_whichever_side_names_it(self):
        result = self.link(a=self.second, b=self.first)
        row = DeficiencyLink.objects.get()
        low, high = sorted([self.first, self.second])
        self.assertEqual((row.lower_id, row.upper_id, row.link_type), (low, high, "ADD"))
        self.assertEqual(result.body["link_event_id"], row.pk)
        self.assertEqual((row.actor_user_id, row.actor_roles_at_time), (self.crew.pk, ["CREW"]))
        audit = AuditEvent.objects.get(action="DEFICIENCY_LINK_ADD")
        self.assertEqual(audit.outcome, "SUCCEEDED")

    def test_linking_never_merges_responsibility_or_closes_anything(self):
        before = self.states()
        self.link()
        self.link(remove_link)
        self.assertEqual(self.states(), before)

    def test_removal_keeps_the_history_and_a_pair_can_be_relinked(self):
        self.link()
        self.link(remove_link, a=self.second, b=self.first, actor=self.supervisor)
        self.link(actor=self.admin)
        rows = list(DeficiencyLink.objects.order_by("recorded_at", "pk"))
        self.assertEqual([r.link_type for r in rows], ["ADD", "REMOVE", "ADD"])
        self.assertEqual([r.prior_link_event_id for r in rows], [None, rows[0].pk, rows[1].pk])
        low, high = sorted([self.first, self.second])
        self.assertTrue(service.is_linked(str(self.org.pk), low, high))
        self.link(remove_link)
        self.assertFalse(service.is_linked(str(self.org.pk), low, high))

    def test_adding_a_linked_pair_or_removing_an_unlinked_one_is_a_conflict(self):
        self.rejected("NOT_LINKED", remove_link, status=409)
        self.link()
        self.rejected("ALREADY_LINKED", status=409)
        self.rejected("ALREADY_LINKED", a=self.second, b=self.first, status=409)

    def test_a_deficiency_cannot_be_linked_to_itself(self):
        self.rejected("SELF_LINK", b=self.first)
        self.rejected("SELF_LINK", remove_link, b=self.first)

    def test_unknown_and_malformed_ids_are_refused(self):
        self.rejected("NOT_FOUND", status=404, b=str(uuid4()))
        self.rejected("NOT_FOUND", status=404, a=str(uuid4()))
        self.rejected("MALFORMED_REQUEST", status=400, b="nope")

    def test_every_role_may_link_and_unlink(self):
        for actor in (self.crew, self.supervisor, self.supply, self.admin):
            with self.subTest(actor=actor.display_name):
                self.link(actor=actor)
                self.link(remove_link, actor=actor)
        self.assertEqual(DeficiencyLink.objects.count(), 8)

    def test_an_inactive_user_is_denied(self):
        outsider = self.make_user("outsider")
        outsider.active = False
        outsider.save()
        before = self.counts()
        with self.assertRaises(CommandDenied):
            self.link(actor=outsider)
        self.assertEqual(self.counts(), before)

    def test_a_replay_adds_nothing_and_a_changed_request_is_reported_as_reuse(self):
        operation_id = str(uuid4())
        first = self.link(operation_id=operation_id)
        counts = self.counts()
        again = self.link(operation_id=operation_id)
        self.assertEqual((again.body, again.replayed), (first.body, True))
        self.assertEqual(self.counts(), counts)


class SuggestionTests(DeficiencyCase):
    def suggest(self, vehicle=None, item=None):
        return service.suggest_related(
            str(self.org.pk),
            str((vehicle or self.vehicle).pk),
            None if item is None else str(item.pk),
        )

    def test_suggestions_are_read_only_active_matches_for_the_vehicle(self):
        match = self.failed().body["deficiency_ids"][0]
        self.failed(self.siren, actor=self.supervisor)
        self.failed(vehicle=self.vehicles[1], actor=self.supply)
        before = self.counts()
        self.assertEqual({row["deficiency_id"] for row in self.suggest(item=self.suction)}, {match})
        self.assertEqual(len(self.suggest()), 2)
        self.assertEqual(self.counts(), before)

    def test_a_closed_deficiency_is_not_suggested(self):
        self.failed()
        DeficiencyProjection.objects.update(status="CLOSED_ADMINISTRATOR")
        self.assertEqual(self.suggest(), [])

    def test_suggestions_are_bounded_and_newest_first(self):
        for _ in range(service.MAX_SUGGESTIONS + 3):
            self.failed()
        rows = self.suggest()
        self.assertEqual(len(rows), service.MAX_SUGGESTIONS)
        stamps = [row["reported_at"] for row in rows]
        self.assertEqual(stamps, sorted(stamps, reverse=True))

    def test_a_suggestion_never_selects_one_for_a_new_failed_check(self):
        self.failed()
        self.suggest(item=self.suction)
        self.failed()
        self.assertEqual(DeficiencyProjection.objects.count(), 2)
