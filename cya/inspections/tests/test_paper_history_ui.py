"""Paper marks, timing and invalidation forms in check history (SPEC 8.0.0 LATE-1, LATE-3,
LATE-5, LATE-6, INV-8, SCH-1, RPT-4; issue #9 B6b).

Over real HTTP sign-in. A sheet performed on 2026-06-02 at 06:15 New York time (10:15Z, inside
the 06:00 obligation's on-time window) is entered on 2026-06-03 (PaperCheckCase). The forms'
client behavior is exercised by workflow_script_harness.js.
"""

from datetime import time

from cya.paper.invalidation import invalidate_paper_entry_batch
from cya.paper.tests.support import JUNE_2

from .test_paper_entry_ui import PaperUiCase


class PaperHistoryCase(PaperUiCase):
    def history(self, client, check_id=None):
        url = f"/inspection/vehicles/{self.vehicle.pk}/check-history/"
        if check_id:
            url += f"?check={check_id}"
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def sheet_at(self, user, at, performers=None):
        return self.enter(
            actor=user,
            performers=performers or [user],
            performed_on=JUNE_2.date(),
            performed_time=at,
        ).body["batch_id"]


class PaperMarkTests(PaperHistoryCase):
    def test_a_paper_check_shows_its_source_performers_and_entry(self):
        client, user = self.sign_in("history-sup", role="SUPERVISOR")
        batch_id = self.sheet(user, performers=[self.crew, user])
        check_id = self.paper(batch_id, actor=user).body["check_id"]
        names = ", ".join(sorted([self.crew.display_name, user.display_name]))
        page = self.history(client)
        self.assertIn('<span class="pill pill-paper">Paper entry</span> performed', page)
        self.assertIn(f"by {names} · entered", page, "the performers are credited (RPT-4)")
        self.assertIn(f"by {user.display_name}", page)
        self.assertIn(f'href="#paper-{batch_id}"', page)
        self.assertIn(f'<li class="paper-batch" id="paper-{batch_id}">', page)
        self.assertIn('<h2 id="paper-entries-heading">Paper entries</h2>', page)
        self.assertIn("1 check entry", page)
        detail = self.history(client, check_id)
        self.assertIn(
            f'href="/inspection/vehicles/{self.vehicle.pk}/check-history/#paper-{batch_id}"',
            detail,
        )
        self.assertIn("<p>Source: Paper entry</p>", detail)
        self.assertIn("<p>Timing: On time</p>", detail)
        self.assertNotIn("Paper entries</h2>", detail, "the list section is on the list view")

    def test_a_paper_check_after_g_is_a_late_completion_shown_apart_from_the_source(self):
        client, user = self.sign_in("history-crew")
        batch_id = self.sheet_at(user, time(7, 30))  # 11:30Z: after G, by L
        check_id = self.paper(batch_id, actor=user).body["check_id"]
        detail = self.history(client, check_id)
        self.assertIn("<p>Source: Paper entry</p>\n<p>Timing: Late completion</p>", detail)

    def test_a_live_check_has_timing_but_no_paper_source(self):
        client, _user = self.sign_in("history-crew")
        check_id = self.live(at=JUNE_2.replace(hour=10, minute=30)).body["check_id"]
        detail = self.history(client, check_id)
        self.assertIn("<p>Timing: On time</p>", detail)
        self.assertNotIn("Source: Paper entry", detail)
        self.assertNotIn('class="pill pill-paper"', detail)

    def test_a_live_check_after_g_is_a_late_completion(self):
        client, _user = self.sign_in("history-crew")
        check_id = self.live(at=JUNE_2.replace(hour=11, minute=45)).body["check_id"]
        self.assertIn("<p>Timing: Late completion</p>", self.history(client, check_id))

    def test_names_and_explanations_are_escaped(self):
        client, user = self.sign_in("history-sup", role="SUPERVISOR")
        self.crew.display_name = "Synthetic <b>Performer</b>"
        self.crew.save(update_fields=["display_name"])
        batch_id = self.sheet(self.supervisor, performers=[self.crew])
        self.paper(batch_id, actor=self.supervisor)
        invalidate_paper_entry_batch(
            batch_id=batch_id,
            explanation="Synthetic <script>wrong</script> time.",
            **self.context(user),
        )
        page = self.history(client)
        self.assertNotIn("<b>Performer</b>", page)
        self.assertIn("Synthetic &lt;b&gt;Performer&lt;/b&gt;", page)
        self.assertNotIn("<script>wrong", page)
        self.assertIn("Synthetic &lt;script&gt;wrong&lt;/script&gt; time.", page)
        self.assertIn("paper entry invalidated", page)
        self.assertIn("<strong>Invalidated</strong>", page)


class BatchFormTests(PaperHistoryCase):
    FORM = 'data-kind="paper-batch"'

    def test_another_supervisor_gets_the_batch_invalidation_form(self):
        client, _user = self.sign_in("history-sup", role="SUPERVISOR")
        batch_id = self.sheet(self.supervisor, performers=[self.crew])
        page = self.history(client)
        self.assertIn(f'<form class="workflow-write" {self.FORM} data-batch="{batch_id}">', page)
        self.assertIn(
            '<p class="workflow-errors banner banner-error" role="alert" tabindex="-1" hidden>',
            page,
        )
        self.assertIn('<textarea name="explanation" required maxlength="4000">', page)
        self.assertNotIn("Continue copying", page, "only the entry actor continues")
        self.assertNotIn("data-url", page.split('id="paper-entries-heading"')[1])

    def test_the_entry_actor_continues_copying_but_gets_no_form(self):
        client, user = self.sign_in("history-sup", role="SUPERVISOR")
        batch_id = self.sheet(user, performers=[self.crew])
        page = self.history(client)
        self.assertIn(f'href="/inspection/paper-entry/{batch_id}/">Continue copying', page)
        self.assertNotIn(self.FORM, page, "SELF_INVALIDATION_FORBIDDEN: no form offered")
        self.assertIn("another supervisor or administrator must invalidate it", page)

    def test_crew_get_no_form(self):
        client, user = self.sign_in("history-crew")
        own = self.sheet(user)
        other = self.sheet(self.supervisor, performers=[user])
        page = self.history(client)
        self.assertNotIn(self.FORM, page)
        self.assertIn(f'href="/inspection/paper-entry/{own}/">Continue copying', page)
        self.assertNotIn(f'href="/inspection/paper-entry/{other}/"', page)
        self.assertNotIn("another supervisor or administrator must invalidate it", page)

    def test_an_invalidated_batch_offers_nothing(self):
        client, user = self.sign_in("history-sup", role="SUPERVISOR")
        batch_id = self.sheet(user)
        invalidate_paper_entry_batch(
            batch_id=batch_id, explanation="Synthetic wrong time.", **self.context(self.supervisor)
        )
        page = self.history(client)
        self.assertIn(f'id="paper-{batch_id}"', page)
        self.assertIn("Synthetic wrong time.", page)
        self.assertNotIn("Continue copying", page)
        self.assertNotIn(self.FORM, page)


class MistakenNormalTests(PaperHistoryCase):
    FORM = 'data-kind="mistaken-normal"'
    OFFER = "<summary>Supervisor: invalidate a check mistakenly saved in Normal mode</summary>"

    def mistaken(self, actor=None):
        """A live check saved at 12:00Z and the same work copied from a 06:15 sheet."""
        check_id = self.live(actor=actor, at=JUNE_2.replace(hour=12)).body["check_id"]
        batch_id = self.sheet_at(self.supervisor, time(6, 15), performers=[self.crew])
        paper = self.paper(batch_id, actor=self.supervisor, acknowledges_check_id=check_id)
        self.paper_check_id = paper.body["check_id"]
        return check_id, batch_id

    def test_a_supervisor_chooses_the_replacement_paper_entry(self):
        check_id, batch_id = self.mistaken()
        other_work = self.sheet_at(self.supervisor, time(6, 15))
        self.paper(other_work, actor=self.supervisor, item=self.masks, quantity=6)
        client, _user = self.sign_in("history-sup", role="SUPERVISOR")
        detail = self.history(client, check_id)
        self.assertIn(f'<form class="workflow-write" {self.FORM} data-check="{check_id}">', detail)
        self.assertIn('<select name="replacement_paper_entry_batch_id" required>', detail)
        self.assertIn(f'<option value="{batch_id}">', detail)
        self.assertNotIn(f'<option value="{other_work}">', detail, "not the same work")
        self.assertIn(f"by {self.crew.display_name}; entered", detail)
        self.assertNotIn(self.FORM, self.history(client), "detail view only")

    def test_an_invalidated_replacement_is_not_offered(self):
        check_id, batch_id = self.mistaken()
        client, user = self.sign_in("history-sup", role="SUPERVISOR")
        invalidate_paper_entry_batch(
            batch_id=batch_id, explanation="Synthetic wrong time.", **self.context(user)
        )
        detail = self.history(client, check_id)
        self.assertIn("Supervisor: invalidate a check mistakenly saved in Normal mode", detail)
        self.assertNotIn(self.FORM, detail)
        self.assertIn("Copy the sheet through Paper entry first", detail)

    def test_crew_and_paper_checks_get_no_offer(self):
        check_id, _batch_id = self.mistaken()
        client, _user = self.sign_in("history-crew")
        self.assertNotIn(self.OFFER, self.history(client, check_id))
        sup_client, _sup = self.sign_in("history-sup", role="SUPERVISOR")
        self.assertIn(self.OFFER, self.history(sup_client, check_id))
        self.assertNotIn(
            self.OFFER,
            self.history(sup_client, self.paper_check_id),
            "PAPER_CHECK_NOT_NORMAL: a paper check is not offered",
        )

    def test_the_author_gets_no_offer(self):
        client, sup = self.sign_in("history-sup", role="SUPERVISOR")
        check_id, _batch_id = self.mistaken(actor=sup)
        self.assertNotIn(self.OFFER, self.history(client, check_id))
