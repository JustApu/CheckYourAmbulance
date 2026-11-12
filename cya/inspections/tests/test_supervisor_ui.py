"""Issue #16 supervisor and administrator screens over synthetic HTTP and the real commands.

The forms only decide what to offer; every test that saves goes through the audited endpoint, and
the refusals checked here come from the commands, not the page.
"""

import re
from datetime import timedelta
from unittest import mock
from uuid import uuid4

from cya.authorization.commands import change_role
from cya.checklists.tests.support import ids
from cya.core.models import AuditEvent
from cya.deficiencies import acknowledgement
from cya.deficiencies.commands import (
    assign_deficiency,
    report_deficiency,
    set_deadline,
    start_work,
)
from cya.deficiencies.extensions import decide_extension
from cya.deficiencies.models import DeficiencyProjection, DeficiencySeverity
from cya.inspections.tests import test_deficiency_actions_ui as crew_actions
from cya.inspections.tests.test_crew_ui import CrewUiCase

SUPERVISOR_FORMS = ("assign", "deadline", "start-work", "acknowledge", "regrade", "suspend")


class SupervisorUiCase(CrewUiCase):
    report = crew_actions.DeficiencyActionsUiTests.report
    latest = crew_actions.DeficiencyActionsUiTests.latest
    detail = crew_actions.DeficiencyActionsUiTests.detail
    pending = crew_actions.DeficiencyActionsUiTests.pending

    def signed_in(self, sub, role):
        client, user = self.harness.sign_in(sub)
        self.grant(user, role)
        return client, user, self.csrf(client)

    def act(self, who, pk, action, **fields):
        client, _user, token = who
        return self.post(
            f"/api/v1/deficiencies/{pk}/{action}",
            {"operation_id": str(uuid4()), "expected_event_id": self.latest(pk), **fields},
            client,
            token,
        )

    def page(self, who, pk):
        return who[0].get(f"/deficiencies/{pk}/")

    def assert_every_form_can_submit(self, page):
        # The script reads each form's submit button at load; one without would stop every form.
        forms = re.findall(r'<form class="[^"]*workflow-write.*?</form>', page, re.S)
        self.assertTrue(forms)
        for form in forms:
            self.assertIn('type="submit"', form)

    def assign_and_due(self, pk, due):
        for call, extra in (
            (assign_deficiency, {"assignee_id": str(self.user.pk)}),
            (set_deadline, {"deadline": due}),
        ):
            call(
                deficiency_id=pk,
                expected_event_id=self.latest(pk),
                **extra,
                **self.context(self.admin),
            )


class SupervisorFormTests(SupervisorUiCase):
    def kinds(self, page):
        return {
            kind for kind in SUPERVISOR_FORMS + ("admin-close",) if f'data-kind="{kind}"' in page
        }

    def test_forms_follow_roles_and_the_supply_context(self):
        pk = self.report()
        acknowledgement.set_acknowledgement_maximum(maximum_days=3, **self.context(self.admin))
        for roles, expected in [
            ({"CREW"}, set()),
            ({"SUPPLY_OFFICER"}, set()),
            ({"SUPERVISOR"}, set(SUPERVISOR_FORMS)),
            ({"ADMINISTRATOR"}, {*SUPERVISOR_FORMS, "admin-close"}),
            ({"CREW", "SUPERVISOR"}, set(SUPERVISOR_FORMS)),
        ]:
            with (
                self.subTest(roles=roles),
                mock.patch("cya.inspections.workflow_views.roles_for", return_value=roles),
            ):
                page = self.detail(pk)
                self.assertEqual(self.kinds(page.content.decode()), expected)
                self.assert_every_form_can_submit(page.content.decode())
                self.assertEqual(page.context["supervisor"]["shown"], bool(expected))
        supply = self.report(category=self.stock)
        with mock.patch(
            "cya.inspections.workflow_views.roles_for", return_value={"SUPPLY_OFFICER"}
        ):
            # A Supply Officer assigns and sets deadlines only on supply-related deficiencies.
            self.assertEqual(
                self.kinds(self.detail(supply).content.decode()),
                {"assign", "deadline", "start-work"},
            )

    def test_closed_deficiencies_offer_only_reopening_to_administrators(self):
        admin = self.signed_in("admin-ui", "ADMINISTRATOR")
        pk = self.report()
        closed = self.act(admin, pk, "administrator-closures", explanation="Synthetic close.")
        self.assertEqual(closed.status_code, 201, closed.content)
        page = self.page(admin, pk).content.decode()
        self.assertEqual(self.kinds(page), set())
        self.assertIn('data-kind="admin-reopen"', page)
        supervisor = self.signed_in("sup-ui", "SUPERVISOR")
        self.assertNotIn('data-kind="admin-reopen"', self.page(supervisor, pk).content.decode())
        forged = self.act(supervisor, pk, "administrator-reopenings", explanation="Synthetic.")
        self.assertEqual(forged.status_code, 403)
        reopened = self.act(admin, pk, "administrator-reopenings", explanation="Synthetic reopen.")
        self.assertEqual(reopened.status_code, 201, reopened.content)
        self.assertContains(self.page(admin, pk), "Reopened by administrator")


class SupervisorActionTests(SupervisorUiCase):
    def setUp(self):
        super().setUp()
        self.sup = self.signed_in("sup-ui", "SUPERVISOR")
        self.crew = (self.client, self.user, self.token)

    def test_assignment_deadline_and_extension_decision_round_trip(self):
        pk = self.report()
        page = self.page(self.sup, pk)
        self.assertIn(
            (str(self.user.pk), self.user.display_name), page.context["supervisor"]["assignees"]
        )
        assigned = self.act(self.sup, pk, "assignments", assignee_id=str(self.user.pk))
        self.assertEqual(assigned.status_code, 201, assigned.content)
        again = self.act(self.sup, pk, "assignments", assignee_id=str(self.user.pk))
        self.assertEqual(again.json()["code"], "ASSIGNEE_UNCHANGED")
        due = self.now + timedelta(days=2)
        self.assertEqual(
            self.act(self.sup, pk, "deadlines", deadline=due.isoformat()).status_code, 201
        )
        asked = self.act(
            self.crew,
            pk,
            "extension-requests",
            reason="Synthetic parts delay.",
            proposed_deadline=(due + timedelta(days=3)).isoformat(),
        )
        self.assertEqual(asked.status_code, 201, asked.content)
        request_id = asked.json()["result"]["event_id"]
        page = self.page(self.sup, pk)
        self.assertContains(page, 'data-kind="extension-decision"', count=1)
        self.assertContains(page, f'name="request_event_id" value="{request_id}"')
        self.assertNotContains(self.page(self.crew, pk), 'data-kind="extension-decision"')
        forged = self.act(
            self.crew,
            pk,
            "extension-decisions",
            request_event_id=request_id,
            decision="APPROVED",
            explanation="Synthetic forged.",
        )
        self.assertEqual(forged.status_code, 403)
        decided = self.act(
            self.sup,
            pk,
            "extension-decisions",
            request_event_id=request_id,
            decision="DIFFERENT_DATE",
            deadline=(due + timedelta(days=1)).isoformat(),
            explanation="Synthetic: one day is enough.",
        )
        self.assertEqual(decided.status_code, 201, decided.content)
        page = self.page(self.sup, pk)
        self.assertNotContains(page, 'data-kind="extension-decision"')
        self.assertContains(page, "Different date")
        self.assertContains(page, "Synthetic: one day is enough.")

    def test_assignee_list_is_bounded_searchable_and_follows_current_roles(self):
        pk = self.report()
        _client, revoked = self.harness.sign_in("crew-revoked")
        change_role(
            organization_id=str(self.org.pk),
            actor_id=str(self.admin.pk),
            target_id=str(revoked.pk),
            role="CREW",
            action="REVOKE",
            **ids(),
        )
        everyone = dict(self.page(self.sup, pk).context["supervisor"]["assignees"])
        self.assertIn(str(self.user.pk), everyone)
        self.assertIn(str(self.sup[1].pk), everyone)  # Supervisors can be assigned
        self.assertNotIn(str(revoked.pk), everyone)
        self.assertGreater(len(everyone), 2)
        # PR 31 review: more eligible people than the list holds must not take the page down.
        with (
            mock.patch("cya.inspections.workflow_views.MAX_CHOICES", 2),
            mock.patch("cya.notifications.enqueue.MAX_RECIPIENTS", 2),  # the old resolver's cap
        ):
            page = self.page(self.sup, pk)
            self.assertEqual(page.status_code, 200)
            self.assertEqual(len(page.context["supervisor"]["assignees"]), 2)
            self.assertTrue(page.context["supervisor"]["assignees_more"])
            self.assertContains(page, "More people can be assigned than this list shows")
            self.assert_every_form_can_submit(page.content.decode())
            name = self.user.display_name
            found = self.sup[0].get(f"/deficiencies/{pk}/", {"assignee_search": name})
            self.assertIn((str(self.user.pk), name), found.context["supervisor"]["assignees"])
            self.assertTrue(
                all(
                    name.lower() in n.lower() for _id, n in found.context["supervisor"]["assignees"]
                )
            )
            nobody = self.sup[0].get(f"/deficiencies/{pk}/", {"assignee_search": "zz-nobody"})
            self.assertContains(nobody, "No one matching that name can be assigned.")

    def test_acknowledgement_regrade_and_suspension_round_trip(self):
        pk = self.report()
        page = self.page(self.sup, pk)
        self.assertContains(page, "An administrator must set the longest")
        self.assertNotContains(page, 'data-kind="acknowledge"')
        self.assert_every_form_can_submit(page.content.decode())
        acknowledgement.set_acknowledgement_maximum(maximum_days=3, **self.context(self.admin))
        self.assertContains(self.page(self.sup, pk), 'max="3"')
        too_long = self.act(
            self.sup,
            pk,
            "escalation-acknowledgements",
            explanation="Synthetic review.",
            action_plan="Synthetic plan.",
            period_days=5,
        )
        self.assertEqual(too_long.json()["code"], "ACKNOWLEDGEMENT_PERIOD_TOO_LONG")
        acked = self.act(
            self.sup,
            pk,
            "escalation-acknowledgements",
            explanation="Synthetic review.",
            action_plan="Synthetic <b>plan</b>.",
            period_days=2,
        )
        self.assertEqual(acked.status_code, 201, acked.content)
        page = self.page(self.sup, pk)
        self.assertContains(page, "Escalation acknowledged")
        self.assertContains(page, "Synthetic &lt;b&gt;plan&lt;/b&gt;.")
        self.assertContains(page, "Acknowledged until")

        current = DeficiencyProjection.objects.get(deficiency_id=pk).severity_version.code
        other = next(
            level["code"]
            for level in page.context["supervisor"]["severities"]
            if level["code"] != current
        )
        regraded = self.act(
            self.sup, pk, "severity-changes", severity=other, explanation="Synthetic."
        )
        self.assertEqual(regraded.status_code, 201, regraded.content)
        self.assertContains(self.page(self.sup, pk), "Severity changed")

        choices = dict(self.page(self.sup, pk).context["supervisor"]["suspension_items"])
        self.assertIn(f"{self.suction.pk}:", choices)
        suspended = self.act(
            self.sup, pk, "check-suspensions", items=[{"item_id": str(self.suction.pk)}]
        )
        self.assertEqual(suspended.status_code, 201, suspended.content)
        page = self.page(self.sup, pk)
        self.assertContains(page, "Known Deficiency — Check Suspended")
        self.assertContains(page, "in force")
        repeat = self.act(
            self.sup, pk, "check-suspensions", items=[{"item_id": str(self.suction.pk)}]
        )
        self.assertEqual(repeat.json()["code"], "ALREADY_SUSPENDED")

    def test_an_in_force_suspension_offers_an_early_end_to_supervisors_only(self):
        # SUS-5 (issue 32): an End suspension form beside each suspension in force, for the
        # roles that may suspend; crew see the suspension without it, and an ended one has none.
        pk = self.report()
        suspended = self.act(
            self.sup, pk, "check-suspensions", items=[{"item_id": str(self.suction.pk)}]
        )
        self.assertEqual(suspended.status_code, 201, suspended.content)
        (suspension_id,) = suspended.json()["result"]["suspension_ids"]
        marker = f'data-suspension="{suspension_id}"'
        page = self.page(self.sup, pk).content.decode()
        self.assertIn(marker, page)
        self.assertIn('data-kind="end-suspension"', page)
        self.assert_every_form_can_submit(page)
        crew_page = self.page(self.crew, pk).content.decode()
        self.assertIn("Known Deficiency — Check Suspended", crew_page)
        self.assertNotIn('data-kind="end-suspension"', crew_page)
        denied = self.act(self.crew, pk, "check-suspension-ends", suspension_id=suspension_id)
        self.assertEqual(denied.status_code, 403, denied.content)

        ended = self.act(
            self.sup,
            pk,
            "check-suspension-ends",
            suspension_id=suspension_id,
            explanation="Synthetic <b>back</b>.",
        )
        self.assertEqual(ended.status_code, 201, ended.content)
        self.assertEqual(ended.json()["result"]["suspensions_ended"], [suspension_id])
        page = self.page(self.sup, pk)
        self.assertContains(page, "(Check suspension ended early)")  # the row's end label
        self.assertContains(page, "Check suspension ended early")  # the history entry
        self.assertContains(page, "Synthetic &lt;b&gt;back&lt;/b&gt;.")
        self.assertNotContains(page, marker)
        self.assertNotContains(page, "in force")
        again = self.act(self.sup, pk, "check-suspension-ends", suspension_id=suspension_id)
        self.assertEqual(
            (again.status_code, again.json()["code"]), (422, "SUSPENSION_ALREADY_ENDED")
        )
        missing = self.act(self.sup, pk, "check-suspension-ends", suspension_id=str(uuid4()))
        self.assertEqual(missing.json()["code"], "SUSPENSION_NOT_FOUND")
        malformed = self.act(self.sup, pk, "check-suspension-ends")
        self.assertEqual(malformed.status_code, 400, malformed.content)

    def test_administrator_close_and_reopen_render_labels_status_and_ended_suspension(self):
        # Issue 20: the administrator labels, the closed status and the suspension that the
        # closure ended all render, with the explanations escaped.
        admin = self.signed_in("admin-ui", "ADMINISTRATOR")
        pk = self.report()
        suspended = self.act(
            self.sup, pk, "check-suspensions", items=[{"item_id": str(self.suction.pk)}]
        )
        self.assertEqual(suspended.status_code, 201, suspended.content)
        closed = self.act(
            admin, pk, "administrator-closures", explanation="Synthetic <i>close</i>."
        )
        self.assertEqual(closed.status_code, 201, closed.content)
        page = self.page(admin, pk)
        self.assertEqual(page.context["status"], "Closed — Administrator")
        self.assertContains(page, "Closed — Administrator")
        self.assertContains(page, "Closed by administrator")
        self.assertContains(page, "Synthetic &lt;i&gt;close&lt;/i&gt;.")
        self.assertNotContains(page, "Synthetic <i>close</i>.")
        self.assertContains(page, "Known Deficiency — Check Suspended")
        self.assertContains(page, "(Closed by administrator)")  # the suspension's end label
        self.assertNotContains(page, "in force")

        reopened = self.act(
            admin, pk, "administrator-reopenings", explanation="Synthetic <b>reopen</b>."
        )
        self.assertEqual(reopened.status_code, 201, reopened.content)
        page = self.page(admin, pk)
        self.assertEqual(page.context["status"], "Open")
        self.assertContains(page, "Reopened by administrator")
        self.assertContains(page, "Synthetic &lt;b&gt;reopen&lt;/b&gt;.")
        self.assertContains(page, "(Closed by administrator)")  # history keeps the ended line

    def test_a_real_publication_empties_the_scope_end_to_end(self):
        # Issue 20: no mock of the scope resolver. Removing the item from the published
        # checklist empties the deficiency's scope, the repair is refused with EMPTY_SCOPE, a
        # supervisor closes it, and an administrator reopens it.
        admin = self.signed_in("admin-ui", "ADMINISTRATOR")
        pk = report_deficiency(
            vehicle_id=str(self.vehicle.pk),
            target={"item_id": str(self.suction.pk)},
            **self.new_fields(),
            **self.context(self.user),
        ).body["deficiency_id"]
        start_work(deficiency_id=pk, expected_event_id=self.latest(pk), **self.context(self.admin))
        self.published_version([s for s in self.items if s["item_id"] != str(self.suction.pk)])

        repair = self.post(
            f"/api/v1/deficiencies/{pk}/corrections",
            {"operation_id": str(uuid4()), "expected_event_id": self.latest(pk)},
        )
        self.assertEqual((repair.status_code, repair.json()["code"]), (422, "EMPTY_SCOPE"))
        closed = self.act(
            self.sup, pk, "empty-scope-closures", explanation="Synthetic: item retired."
        )
        self.assertEqual(closed.status_code, 201, closed.content)
        page = self.page(self.sup, pk)
        self.assertEqual(page.context["status"], "Closed — No current verification items")
        self.assertContains(page, "Closed — no current verification items")  # history label
        self.assertNotContains(page, 'data-kind="repair"')

        reopened = self.act(
            admin, pk, "administrator-reopenings", explanation="Synthetic: item restored."
        )
        self.assertEqual(reopened.status_code, 201, reopened.content)
        page = self.page(admin, pk)
        self.assertEqual(page.context["status"], "Open")
        self.assertContains(page, "Reopened by administrator")

    def test_expansion_offers_current_items_while_pending_verification(self):
        pk = self.pending()
        page = self.page(self.sup, pk)
        self.assertContains(page, 'data-kind="expand"')
        self.assertNotContains(page, 'data-kind="add-verification"')
        values = [value for value, _label in page.context["supervisor"]["verification_items"]]
        extra = next(v for v in values if not v.startswith(f"{self.suction.pk}:"))
        item, kit = extra.split(":")
        entry = {"item_id": item, **({"kit_instance_id": kit} if kit else {})}
        expanded = self.act(
            self.sup, pk, "scope-expansions", items=[entry], explanation="Synthetic expansion."
        )
        self.assertEqual(expanded.status_code, 201, expanded.content)
        self.assertContains(self.page(self.sup, pk), "Verification scope expanded")
        self.assertNotContains(self.page(self.crew, pk), 'data-kind="expand"')


class QueueTests(SupervisorUiCase):
    URL = "/deficiencies/queue/"

    def lists(self, response):
        return {
            group["heading"]: [str(row.deficiency_id) for row in group["page"]["rows"]]
            for group in response.context["lists"]
        }

    def test_queue_is_for_supervisors_and_administrators(self):
        before = AuditEvent.objects.count()
        self.assertEqual(self.client.get(self.URL).status_code, 403)
        self.assertGreater(AuditEvent.objects.count(), before)  # the refusal is audited
        self.assertNotContains(self.client.get("/deficiencies/"), self.URL)
        sup = self.signed_in("sup-ui", "SUPERVISOR")
        self.assertEqual(sup[0].get(self.URL).status_code, 200)
        self.assertContains(sup[0].get("/deficiencies/"), self.URL)

    def test_unassigned_overdue_and_waiting_extensions(self):
        unassigned = self.report(summary="Synthetic unassigned")
        self.clock.set(self.now + timedelta(seconds=1))
        unassigned_later = self.report(summary="Synthetic unassigned later")
        waiting = self.report(summary="Synthetic waiting first")
        self.assign_and_due(waiting, self.now + timedelta(days=2))
        self.post(
            f"/api/v1/deficiencies/{waiting}/extension-requests",
            {
                "operation_id": str(uuid4()),
                "expected_event_id": self.latest(waiting),
                "reason": "Synthetic delay.",
                "proposed_deadline": (self.now + timedelta(days=5)).isoformat(),
            },
        )
        self.clock.set(self.now + timedelta(seconds=2))
        waiting_later = self.report(summary="Synthetic waiting later")
        self.assign_and_due(waiting_later, self.now + timedelta(days=2))
        self.post(
            f"/api/v1/deficiencies/{waiting_later}/extension-requests",
            {
                "operation_id": str(uuid4()),
                "expected_event_id": self.latest(waiting_later),
                "reason": "Synthetic delay.",
                "proposed_deadline": (self.now + timedelta(days=5)).isoformat(),
            },
        )
        late = self.report(summary="Synthetic late first")
        self.assign_and_due(late, self.now + timedelta(hours=1))
        late_later = self.report(summary="Synthetic late later")
        self.assign_and_due(late_later, self.now + timedelta(hours=2))
        self.clock.set(self.now + timedelta(hours=3))
        sup = self.signed_in("sup-ui", "SUPERVISOR")
        found = self.lists(sup[0].get(self.URL))
        self.assertEqual(found["Unassigned"], [unassigned, unassigned_later])
        self.assertEqual(found["Overdue"], [late, late_later])
        self.assertEqual(
            found["Extension requests waiting for a decision"], [waiting, waiting_later]
        )

    def request_extension(self, pk):
        return self.post(
            f"/api/v1/deficiencies/{pk}/extension-requests",
            {
                "operation_id": str(uuid4()),
                "expected_event_id": self.latest(pk),
                "reason": "Synthetic delay.",
                "proposed_deadline": (self.now + timedelta(days=5)).isoformat(),
            },
        ).json()["result"]["event_id"]

    def test_decided_and_superseded_requests_never_hide_a_waiting_one(self):
        decided = self.report(summary="Synthetic decided")
        self.assign_and_due(decided, self.now + timedelta(days=2))
        decide_extension(
            deficiency_id=decided,
            request_event_id=self.request_extension(decided),
            decision="DENIED",
            explanation="Synthetic denial.",
            deadline=None,
            expected_event_id=self.latest(decided),
            **self.context(self.admin),
        )
        superseded = self.report(summary="Synthetic superseded")
        self.assign_and_due(superseded, self.now + timedelta(days=2))
        self.request_extension(superseded)
        set_deadline(
            deficiency_id=superseded,
            deadline=self.now + timedelta(days=3),
            expected_event_id=self.latest(superseded),
            **self.context(self.admin),
        )
        waiting = self.report(summary="Synthetic waiting")
        self.assign_and_due(waiting, self.now + timedelta(days=2))
        self.request_extension(waiting)
        sup = self.signed_in("sup-ui", "SUPERVISOR")
        with mock.patch("cya.inspections.workflow_views.DEFICIENCY_PAGE_SIZE", 1):
            found = self.lists(sup[0].get(self.URL))
        self.assertEqual(found["Extension requests waiting for a decision"], [waiting])

    def test_queue_adds_configured_high_severity_group_with_next_actions(self):
        urgent = self.report(summary="Synthetic configured urgent case", severity="HIGH")
        sup = self.signed_in("queue-priority-supervisor", "SUPERVISOR")
        response = sup[0].get(self.URL)
        self.assertEqual(response.status_code, 200)
        groups = {group["heading"]: group["page"] for group in response.context["lists"]}
        heading = next(name for name in groups if name.startswith("Top configured severity:"))
        self.assertIn("Critical / High", heading)
        self.assertEqual(groups[heading]["total"], 1)
        self.assertContains(response, "Next action: Review the active high-severity issue.")
        self.assertContains(response, 'href="/deficiencies/' + urgent + '/"')

    def test_queue_severity_priority_tracks_custom_active_levels(self):
        DeficiencySeverity.objects.create(
            organization=self.org,
            code="URGENT",
            revision=1,
            label="Urgent",
            description="Synthetic highest configured level.",
            rank=5,
            active=True,
            effective_at=self.now - timedelta(days=1),
        )
        self.report(summary="Synthetic urgent queue case", severity="URGENT")
        sup = self.signed_in("queue-urgent-supervisor", "SUPERVISOR")
        response = sup[0].get(self.URL)
        headings = [group["heading"] for group in response.context["lists"]]
        self.assertIn("Top configured severity: Urgent / Critical", headings)
        self.assertNotIn("Top configured severity: Critical / High", headings)

    def test_queue_prioritizes_a_grade_whose_level_was_retired(self):
        retired_grade = self.report(summary="Synthetic retired grade", severity="CRITICAL")
        DeficiencySeverity.objects.create(
            organization=self.org,
            code="CRITICAL",
            revision=2,
            label="Retired critical",
            description="Synthetic retired configured grade.",
            rank=4,
            active=False,
            effective_at=self.now - timedelta(days=1),
        )
        DeficiencySeverity.objects.create(
            organization=self.org,
            code="URGENT",
            revision=1,
            label="Urgent",
            description="Synthetic highest active level.",
            rank=5,
            active=True,
            effective_at=self.now - timedelta(days=1),
        )
        urgent_grade = self.report(summary="Synthetic urgent grade", severity="URGENT")
        sup = self.signed_in("queue-retired-supervisor", "SUPERVISOR")
        response = sup[0].get(self.URL)
        severity_group = next(
            group
            for group in response.context["lists"]
            if group["heading"] == "Top configured severity: Urgent / High"
        )
        self.assertEqual(
            [str(row.deficiency_id) for row in severity_group["page"]["rows"]],
            [str(urgent_grade), str(retired_grade)],
        )

    def test_queue_omits_severity_group_when_no_levels_are_active(self):
        self.report(summary="Synthetic case with retired severity", severity="HIGH")
        levels = list(DeficiencySeverity.objects.filter(organization=self.org))
        for level in levels:
            DeficiencySeverity.objects.create(
                organization=self.org,
                code=level.code,
                revision=level.revision + 1,
                label=f"Retired {level.label}",
                description="Synthetic retired severity level.",
                rank=level.rank,
                active=False,
                effective_at=self.now - timedelta(days=1),
            )
        sup = self.signed_in("queue-no-active-severity-supervisor", "SUPERVISOR")
        response = sup[0].get(self.URL)
        headings = [group["heading"] for group in response.context["lists"]]
        self.assertFalse(
            any(heading.startswith("Top configured severity:") for heading in headings)
        )

    def test_queue_pagination_preserves_filters_and_total_counts(self):
        first = self.report(summary="Synthetic queue marker first")
        self.report(summary="Synthetic queue marker second")
        sup = self.signed_in("queue-page-supervisor", "SUPERVISOR")
        with mock.patch("cya.inspections.workflow_views.DEFICIENCY_PAGE_SIZE", 1):
            response = sup[0].get(self.URL, {"q": "queue marker"})
            unassigned = next(
                group for group in response.context["lists"] if group["heading"] == "Unassigned"
            )["page"]
            self.assertEqual(unassigned["total"], 2)
            self.assertEqual(len(unassigned["rows"]), 1)
            self.assertIn("q=queue+marker", unassigned["next_url"])
            self.assertIn("unassigned_page=2", unassigned["next_url"])
            next_page = sup[0].get(unassigned["next_url"])
            next_unassigned = next(
                group for group in next_page.context["lists"] if group["heading"] == "Unassigned"
            )["page"]
            self.assertEqual(next_unassigned["total"], 2)
            self.assertEqual(len(next_unassigned["rows"]), 1)
            self.assertNotEqual(str(next_unassigned["rows"][0].deficiency_id), str(first))


class EmptyScopeCloseUiTests(SupervisorUiCase):
    """Issue 21: the empty-scope close on the deficiency page."""

    FORM = 'data-kind="empty-scope-close"'

    def setUp(self):
        super().setUp()
        self.sup = self.signed_in("sup-esc", "SUPERVISOR")

    def shown(self, pk, roles):
        with mock.patch("cya.inspections.workflow_views.roles_for", return_value=roles):
            page = self.detail(pk).content.decode()
        return self.FORM in page, 'data-can-close-empty="1"' in page

    def test_only_supervisors_and_administrators_see_it(self):
        pk = self.report()
        for roles, expected in [
            ({"CREW"}, False),
            ({"SUPPLY_OFFICER"}, False),
            ({"CREW", "SUPPLY_OFFICER"}, False),
            ({"SUPERVISOR"}, True),
            ({"ADMINISTRATOR"}, True),
            ({"CREW", "SUPERVISOR"}, True),
        ]:
            with self.subTest(roles=roles):
                self.assertEqual(self.shown(pk, roles)[0], expected)

    def test_it_is_offered_only_in_open_in_progress_and_awaiting_dependency(self):
        sup = {"SUPERVISOR"}
        pk = self.report()
        self.assertEqual(self.shown(pk, sup), (True, False))  # Open: no repair form yet
        start_work(deficiency_id=pk, expected_event_id=self.latest(pk), **self.context(self.admin))
        # In Progress: the repair form says the close control is on the page.
        self.assertEqual(self.shown(pk, sup), (True, True))
        self.assertEqual(self.shown(pk, {"CREW"}), (False, False))
        entered = self.change(pk, "dependencies", explanation="Synthetic vendor")
        self.assertEqual(entered.status_code, 201, entered.content)
        self.assertEqual(self.shown(pk, sup)[0], True)  # Awaiting External Dependency
        self.assertEqual(self.shown(self.pending(), sup)[0], False)  # Pending Verification
        admin = self.signed_in("admin-esc", "ADMINISTRATOR")
        closed = self.act(admin, pk, "administrator-closures", explanation="Synthetic close.")
        self.assertEqual(closed.status_code, 201, closed.content)
        self.assertEqual(self.shown(pk, sup)[0], False)  # closed

    def test_the_form_states_the_outcome_and_requires_an_explanation(self):
        page = self.page(self.sup, self.report()).content.decode()
        form = re.search(rf"<form [^>]*{self.FORM}.*?</form>", page, re.S).group(0)
        self.assertIn("Close: no current verification items", form)
        self.assertIn("only an Administrator can reopen it", form)
        self.assertIn('name="explanation" required', form)

    def change(self, pk, action, **fields):
        return self.post(
            f"/api/v1/deficiencies/{pk}/{action}",
            {"operation_id": str(uuid4()), "expected_event_id": self.latest(pk), **fields},
        )

    def test_a_closure_posted_through_the_page_configuration(self):
        pk = report_deficiency(
            vehicle_id=str(self.vehicle.pk),
            target={"item_id": str(self.suction.pk)},
            **self.new_fields(),
            **self.context(self.user),
        ).body["deficiency_id"]
        client, _user, token = self.sup

        def from_page():
            page = client.get(f"/deficiencies/{pk}/").content.decode()
            form = re.search(rf"<form [^>]*{self.FORM}[^>]*>", page).group(0)
            url = re.search(r'data-url="([^"]+)"', form).group(1)
            expected = re.search(r'data-expected-event="([^"]+)"', form).group(1)
            return url, expected

        url, expected = from_page()
        self.assertEqual(url, f"/api/v1/deficiencies/{pk}/empty-scope-closures")
        body = {
            "operation_id": str(uuid4()),
            "expected_event_id": expected,
            "explanation": "Synthetic: still on the checklist.",
        }
        refused = self.post(url, body, client, token)
        self.assertEqual((refused.status_code, refused.json()["code"]), (422, "SCOPE_AVAILABLE"))

        self.published_version([s for s in self.items if s["item_id"] != str(self.suction.pk)])
        url, expected = from_page()
        body = {
            "operation_id": str(uuid4()),
            "expected_event_id": expected,
            "explanation": "Synthetic: the item was retired.",
        }
        closed = self.post(url, body, client, token)
        self.assertEqual(closed.status_code, 201, closed.content)
        page = self.page(self.sup, pk)
        self.assertEqual(page.context["status"], "Closed — No current verification items")
        self.assertNotContains(page, self.FORM)
