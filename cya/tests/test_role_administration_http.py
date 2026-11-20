"""issue 25 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/25) exit test: one authorized, persisted, audited action through the web UI.

Sign-in uses the offline Google stub (cya.testing.stubs); roles, receipts, role
history and audit are the real MariaDB-backed collaborators. This proves logic,
not the Workspace integration, which issue 25 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/25) verifies separately outside CI.
"""

from unittest import mock
from urllib.parse import urlencode
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone

from cya.accounts.models import Organization, User
from cya.accounts.returns import sign_in_location
from cya.authorization.models import RoleGrant
from cya.authorization.persistence import current_role_codes
from cya.core.models import AuditEvent, CommandReceipt
from cya.shell.templatetags.station_time import station_time
from cya.testing.role_http import URL, RoleHttpHarness, real_collaborator_settings


@override_settings(**real_collaborator_settings())
class RoleAdministrationHttpTests(TransactionTestCase):
    def setUp(self):
        harness = RoleHttpHarness()
        self.org = harness.org
        self.sign_in = harness.sign_in
        self.make_administrator = harness.make_administrator
        self.form = harness.form
        self.data = harness.data
        self.change = harness.change
        self.save_data = harness.save_data
        self.save = harness.save
        self.counts = harness.counts

    def tearDown(self):
        call_command("flush", verbosity=0, interactive=False, reset_sequences=True)

    # Acceptance criteria -----------------------------------------------------

    def test_a_search_reload_names_and_focuses_its_result(self):
        # Issue #235 D5: the title and focus say what the search found.
        admin_client, _admin = self.make_administrator("admin-search")
        self.sign_in("crew-search")
        plain = admin_client.get(URL)
        self.assertEqual(plain.context["title"], "Role administration")
        self.assertNotContains(plain, 'tabindex="-1" data-focus-on-load')
        found = admin_client.get(URL, {"q": "crew-search"})
        self.assertEqual(found.context["title"], "Role administration (1 matching)")
        self.assertContains(
            found,
            '<p id="role-search-result" class="role-search-result" aria-live="polite" '
            'data-role-search-result tabindex="-1" data-focus-on-load>'
            "1 matching active user.</p>",
            html=True,
        )
        self.assertEqual(found.content.decode().count('tabindex="-1" data-focus-on-load'), 1)

    def test_role_history_names_who_made_the_change(self):
        # Issue #235 D8: "Changed by" shows the person's name, as the user list does.
        admin_client, admin = self.make_administrator("admin-named")
        admin.display_name = "Synthetic Admin Name"
        admin.save(update_fields=["display_name"])
        _, crew = self.sign_in("crew-named")
        self.change(admin_client, crew)
        history = admin_client.get(f"{URL}{crew.pk}/history/")
        self.assertContains(history, "<td>Synthetic Admin Name</td>", html=True)
        self.assertNotContains(history, f"<td>{admin.email}</td>", html=True)

    def test_authorized_change_is_persisted_in_history_and_audited(self):
        admin_client, admin = self.make_administrator("admin-1")
        _, crew = self.sign_in("crew-1")
        self.org.station_zone = "America/New_York"
        self.org.save(update_fields=["station_zone"])
        before = self.counts()

        response = self.change(admin_client, crew)

        self.assertEqual((response.status_code, response["Location"]), (302, URL + "?saved=1"))
        self.assertEqual(current_role_codes(crew), ("CREW", "SUPERVISOR"))
        grant = RoleGrant.objects.get(user=crew, role="SUPERVISOR")
        self.assertEqual((grant.action, grant.actor_user_id), ("GRANT", admin.pk))
        self.assertEqual(grant.actor_roles_at_time, ["ADMINISTRATOR", "CREW"])
        audit = AuditEvent.objects.get(action="ROLE_CHANGE", outcome="SUCCEEDED")
        self.assertEqual((audit.actor_user_id, audit.target_id), (admin.pk, str(crew.pk)))
        self.assertEqual(audit.command_id, grant.command_id)
        self.assertEqual((self.counts()[0] - before[0], self.counts()[1] - before[1]), (1, 1))
        users_page = admin_client.get(URL + "?saved=1")
        self.assertContains(users_page, "Saved")
        self.assertContains(users_page, "Crew")
        self.assertContains(users_page, "Supervisor")
        self.assertContains(users_page, "data-role-save-button")
        self.assertContains(users_page, "<legend>Roles</legend>", html=True)
        self.assertContains(users_page, 'name="roles" value="CREW"')
        self.assertContains(
            users_page,
            f'name="roles" value="CREW" data-role-choice data-role-label="Crew" aria-label="Crew role for {crew.email}" checked',
        )
        self.assertContains(users_page, 'name="roles" value="SUPERVISOR"')
        self.assertContains(
            users_page,
            f'name="roles" value="SUPERVISOR" data-role-choice data-role-label="Supervisor" aria-label="Supervisor role for {crew.email}" checked',
        )
        self.assertNotContains(users_page, "Review grant")
        self.assertNotContains(users_page, 'class="role-user-card"')
        self.assertContains(
            users_page,
            f'href="{URL}{crew.pk}/history/" aria-label="View role history for {crew.email}"',
        )
        self.assertNotContains(users_page, 'class="role-history-scroll"')

        history = admin_client.get(f"{URL}{crew.pk}/history/")
        # The page's own filter, so the expectation also holds in the repeated fall-back hour.
        with timezone.override(ZoneInfo("America/New_York")):
            expected_time = station_time(grant.recorded_at, "seconds")
        self.assertContains(
            history,
            '<th scope="col">When (station time)</th>',
            html=True,
        )
        self.assertContains(history, expected_time)
        self.assertContains(history, "Granted")
        self.assertContains(history, "Supervisor")
        self.assertNotContains(history, "<td>SUPERVISOR</td>")
        self.assertContains(
            history,
            f"<title>Role history for {crew.display_name} - Check Your Ambulance</title>",
        )
        self.assertContains(history, crew.email)
        self.assertContains(history, 'class="role-history-scroll"')
        self.assertContains(history, f'aria-label="Role history for {crew.email}"')

    def test_role_history_is_organization_scoped_and_denied_to_non_administrators(self):
        admin_client, admin = self.make_administrator("admin-1")
        crew_client, crew = self.sign_in("crew-1")
        other = Organization.objects.create(name="Other Agency", station_zone="UTC")
        stranger = User.objects.create(
            organization=other,
            idp_issuer="https://accounts.google.com",
            idp_subject="stranger",
            email="stranger@other.example",
        )

        users_page = admin_client.get(URL)
        self.assertNotContains(users_page, stranger.email)

        missing = admin_client.get(f"{URL}{stranger.pk}/history/")
        self.assertEqual(missing.status_code, 404)
        self.assertNotContains(missing, stranger.email, status_code=404)

        denied = crew_client.get(f"{URL}{crew.pk}/history/")
        self.assertEqual(denied.status_code, 403)
        self.assertNotContains(denied, admin.email, status_code=403)
        self.assertTrue(
            AuditEvent.objects.filter(
                actor_user=crew,
                action="VIEW_ADMINISTRATION",
                outcome="DENIED",
            ).exists()
        )

    def test_history_detail_shows_empty_state_for_a_user_without_role_events(self):
        admin_client, _ = self.make_administrator("admin-1")
        target = User.objects.create(
            organization=self.org,
            idp_issuer="https://accounts.google.com",
            idp_subject="no-role-history",
            email="no-history@agency.example",
            display_name="No History",
        )

        response = admin_client.get(f"{URL}{target.pk}/history/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No role changes have been recorded for this user.")
        self.assertContains(response, "Back to organization users")

    def test_role_history_paginates_newest_first(self):
        admin_client, _ = self.make_administrator("admin-1")
        _, crew = self.sign_in("crew-1")
        for index in range(51):
            action = "GRANT" if index % 2 == 0 else "REVOKE"
            response = self.change(admin_client, crew, action=action)
            self.assertEqual(response.status_code, 302)

        first_page = admin_client.get(f"{URL}{crew.pk}/history/")
        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(first_page.context["history_page"].number, 1)
        self.assertEqual(len(first_page.context["history"]), 50)
        self.assertGreater(
            first_page.context["history"][0].sequence,
            first_page.context["history"][-1].sequence,
        )
        self.assertContains(first_page, "Older changes")

        second_page = admin_client.get(f"{URL}{crew.pk}/history/", {"page": "2"})
        self.assertEqual(second_page.status_code, 200)
        self.assertEqual(second_page.context["history_page"].number, 2)
        self.assertEqual(len(second_page.context["history"]), 2)
        self.assertContains(second_page, "Newer changes")
        self.assertNotContains(second_page, "Older changes")

    def test_role_stream_query_reads_one_event_past_the_per_user_limit(self):
        from cya.authorization.views import ROLE_EVENT_LIMIT, _current_roles_by_user

        _, crew = self.sign_in("crew-1")
        rows = [{"event_number": ROLE_EVENT_LIMIT + 1, "user_id": crew.pk}]
        with mock.patch("cya.authorization.views.RoleGrant.objects.filter") as role_filter:
            query = role_filter.return_value
            query.annotate.return_value.filter.return_value.order_by.return_value.values.return_value = rows

            roles = _current_roles_by_user(self.org.pk, [crew])

            query.annotate.return_value.filter.assert_called_once_with(
                event_number__lte=ROLE_EVENT_LIMIT + 1
            )
        self.assertEqual(roles, {str(crew.pk): None})

    def test_role_list_keeps_other_users_available_when_one_history_is_unavailable(self):
        admin_client, admin = self.make_administrator("admin-1")
        _, crew = self.sign_in("crew-1")
        roles = {
            str(admin.pk): ("ADMINISTRATOR", "CREW"),
            str(crew.pk): None,
        }
        with mock.patch(
            "cya.authorization.views._current_roles_by_user",
            return_value=roles,
        ):
            response = admin_client.get(URL)

        self.assertEqual(response.status_code, 200)
        targets = {str(target.pk): target for target in response.context["targets"]}
        self.assertTrue(targets[str(crew.pk)].role_history_unavailable)
        self.assertIsNone(targets[str(crew.pk)].role_operation_id)
        self.assertFalse(targets[str(admin.pk)].role_history_unavailable)
        self.assertContains(response, "Current roles could not be verified.")
        self.assertNotContains(response, 'role="alert">Current roles could not be verified.')
        self.assertContains(response, "data-role-save-button")

    def test_unauthorized_user_is_refused_and_a_denial_is_recorded(self):
        _, admin = self.make_administrator("admin-1")
        client, crew = self.sign_in("crew-1")
        client.get("/")  # obtains a CSRF cookie; the Crew user is never shown the form
        grants = RoleGrant.objects.count()

        # A forged direct request: the Crew user tries to make themself an administrator.
        response = client.post(
            URL,
            {
                "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
                "stage": "confirm",
                "operation_id": str(uuid4()),
                "target": str(crew.pk),
                "role": "ADMINISTRATOR",
                "action": "GRANT",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(RoleGrant.objects.count(), grants)
        self.assertEqual(current_role_codes(crew), ("CREW",))
        self.assertEqual(current_role_codes(admin), ("ADMINISTRATOR", "CREW"))
        self.assertFalse(CommandReceipt.objects.filter(command_type="ROLE_CHANGE").exists())
        denial = AuditEvent.objects.get(action="ROLE_CHANGE", outcome="DENIED")
        self.assertEqual((denial.actor_user_id, denial.roles_at_time), (crew.pk, ["CREW"]))
        self.assertEqual(denial.reason_code, "FORBIDDEN")

    def test_replaying_the_same_operation_id_yields_one_record(self):
        admin_client, _ = self.make_administrator("admin-1")
        _, crew = self.sign_in("crew-1")
        token, operation_id = self.form(admin_client)
        data = {
            "csrfmiddlewaretoken": token,
            "stage": "confirm",
            "operation_id": operation_id,
            "target": str(crew.pk),
            "role": "SUPERVISOR",
            "action": "GRANT",
        }
        first = admin_client.post(URL, data)
        after_first = self.counts()
        second = admin_client.post(URL, data)

        self.assertEqual((first.status_code, second.status_code), (302, 302))
        self.assertEqual(self.counts(), after_first)
        self.assertEqual(RoleGrant.objects.filter(user=crew, role="SUPERVISOR").count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="ROLE_CHANGE").count(), 1)

    def test_reusing_an_operation_id_for_a_different_request_is_not_reported_saved(self):
        admin_client, _ = self.make_administrator("admin-1")
        _, crew = self.sign_in("crew-1")
        _, other = self.sign_in("crew-2")
        operation_id = str(uuid4())
        first = self.change(admin_client, crew, operation_id=operation_id)
        self.assertEqual(first.status_code, 302)
        after_first = self.counts()

        changes = {
            "action": {"action": "REVOKE"},
            "role": {"role": "SUPPLY_OFFICER"},
            "target": {"target_other": True},
        }
        for name, change in changes.items():
            with self.subTest(name):
                if change.pop("target_other", False):
                    response = self.change(admin_client, other, operation_id=operation_id)
                else:
                    response = self.change(admin_client, crew, operation_id=operation_id, **change)
                self.assertEqual(response.status_code, 409)
                self.assertContains(response, "was not applied", status_code=409)
                self.assertNotIn("Location", response)
        self.assertEqual(self.counts(), after_first)
        self.assertEqual(current_role_codes(crew), ("CREW", "SUPERVISOR"))
        self.assertEqual(current_role_codes(other), ("CREW",))

    def test_the_action_is_reachable_from_the_shell(self):
        client, _ = self.make_administrator("admin-1")
        home = client.get("/")
        self.assertContains(home, 'href="/administration/"')
        section = client.get("/administration/")
        self.assertContains(section, 'href="/administration/roles/"')
        page = client.get("/administration/roles/")
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        self.assertRegex(
            html,
            r'<a href="/administration/" aria-current="page"[^>]*>.*?<span>Administration</span>',
        )
        self.assertRegex(html, r'<button id="app-nav-more"[^>]*aria-current="true"')
        self.assertContains(page, "Organization users")

    def test_one_save_applies_several_role_changes_with_one_receipt(self):
        # Issue #236: any number of staged changes, one Save, no separate review page.
        admin_client, admin = self.make_administrator("admin-1")
        _, crew = self.sign_in("crew-1")

        page = admin_client.get(URL)
        self.assertContains(page, 'class="role-user-row"')
        self.assertContains(page, 'name="roles" value="CREW"')
        self.assertContains(page, 'name="current_roles" value="CREW"')
        self.assertContains(page, 'name="stage" value="save"')
        self.assertContains(page, ">Save</button>")
        self.assertNotContains(page, "Review changes")

        before = self.counts()
        data = self.save_data(admin_client, crew, ["SUPERVISOR", "SUPPLY_OFFICER"], q=crew.email)
        response = admin_client.post(URL, data)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            URL + "?" + urlencode({"saved": data["operation_id"], "q": crew.email}),
        )
        self.assertEqual(current_role_codes(crew), ("SUPERVISOR", "SUPPLY_OFFICER"))
        self.assertEqual(self.counts(), (before[0] + 3, before[1] + 1, before[2] + 4))
        receipt = CommandReceipt.objects.get(operation_id=data["operation_id"])
        self.assertEqual(receipt.command_type, "ROLE_CHANGES")
        self.assertEqual(
            receipt.response_body,
            {
                "code": "SAVED",
                "user_id": str(crew.pk),
                "roles": ["SUPERVISOR", "SUPPLY_OFFICER"],
                "added": ["SUPERVISOR", "SUPPLY_OFFICER"],
                "removed": ["CREW"],
            },
        )
        events = RoleGrant.objects.filter(command=receipt).order_by("sequence")
        self.assertEqual(
            [(event.sequence, event.role, event.action) for event in events],
            [(2, "SUPERVISOR", "GRANT"), (3, "SUPPLY_OFFICER", "GRANT"), (4, "CREW", "REVOKE")],
        )
        self.assertEqual({event.actor_user_id for event in events}, {admin.pk})
        audits = AuditEvent.objects.filter(command=receipt, action="ROLE_CHANGES")
        per_change = [audit for audit in audits if "role" in audit.new_snapshot["values"]]
        self.assertEqual(
            sorted(
                (audit.new_snapshot["values"]["role"], audit.new_snapshot["values"]["action"])
                for audit in per_change
            ),
            [("CREW", "REVOKE"), ("SUPERVISOR", "GRANT"), ("SUPPLY_OFFICER", "GRANT")],
        )
        self.assertEqual({audit.actor_user_id for audit in per_change}, {admin.pk})
        (summary,) = [audit for audit in audits if "roles" in audit.new_snapshot["values"]]
        self.assertEqual(summary.prior_snapshot["values"], {"roles": ["CREW"]})
        self.assertEqual(
            summary.new_snapshot["values"], {"roles": ["SUPERVISOR", "SUPPLY_OFFICER"]}
        )

        # D2: the row says what changed for whom, and that message takes focus.
        saved = admin_client.get(response["Location"])
        name = crew.display_name or crew.email
        self.assertContains(
            saved,
            f"Saved for {name}: added Supervisor, Supply Officer; removed Crew.",
        )
        html = saved.content.decode()
        self.assertEqual(html.count('tabindex="-1" data-focus-on-load'), 1)
        self.assertIn('role-row-message" role="status" tabindex="-1" data-focus-on-load', html)

    def test_saved_summary_is_only_read_from_this_administrators_own_save(self):
        admin_client, _ = self.make_administrator("admin-1")
        _, crew = self.sign_in("crew-1")
        data = self.save_data(admin_client, crew, ["CREW", "SUPERVISOR"])
        self.assertEqual(admin_client.post(URL, data).status_code, 302)
        other_client, other_admin = self.sign_in("admin-2")
        self.assertEqual(
            self.save(admin_client, other_admin, ["CREW", "ADMINISTRATOR"]).status_code, 302
        )
        other_client.get("/")  # current roles are read per request

        for value in (data["operation_id"], str(uuid4()), "not-a-uuid"):
            with self.subTest(saved=value):
                page = other_client.get(URL, {"saved": value})
                self.assertEqual(page.status_code, 200)
                self.assertNotContains(page, "Saved for")

    def test_a_stale_selection_is_refused_and_nothing_is_applied(self):
        admin_client, _ = self.make_administrator("admin-1")
        _, crew = self.sign_in("crew-1")
        # The page showed Crew only; meanwhile another change granted Supervisor.
        data = self.save_data(admin_client, crew, ["SUPPLY_OFFICER"], current=["CREW"])
        self.assertEqual(self.change(admin_client, crew).status_code, 302)
        before = self.counts()

        response = admin_client.post(URL, data)

        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "roles changed since the page loaded", status_code=409)
        self.assertContains(
            response,
            'role-row-message" role="alert" tabindex="-1" data-focus-on-load',
            status_code=409,
        )
        self.assertEqual(current_role_codes(crew), ("CREW", "SUPERVISOR"))
        self.assertEqual(self.counts()[:2], before[:2])
        self.assertTrue(
            AuditEvent.objects.filter(
                action="ROLE_CHANGES", outcome="FAILED", reason_code="COMMAND_FAILED"
            ).exists()
        )

    def test_a_refused_change_refuses_the_whole_save(self):
        admin_client, admin = self.make_administrator("admin-1")
        before = self.counts()
        # The only administrator adds Supervisor and removes their own Administrator role.
        response = self.save(admin_client, admin, ["CREW", "SUPERVISOR"])

        self.assertEqual(response.status_code, 409)
        self.assertContains(
            response, "removing the Administrator role was refused", status_code=409
        )
        self.assertContains(response, "No other change was applied", status_code=409)
        self.assertEqual(current_role_codes(admin), ("ADMINISTRATOR", "CREW"))
        self.assertEqual(self.counts()[:2], before[:2])

    def test_nothing_to_save_is_refused_without_a_change(self):
        admin_client, _ = self.make_administrator("admin-1")
        _, crew = self.sign_in("crew-1")
        before = self.counts()
        response = self.save(admin_client, crew, ["CREW"])
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Nothing to save", status_code=400)
        self.assertEqual(self.counts()[:2], before[:2])

    def test_removing_your_own_administrator_role_warns_and_ends_access_on_save(self):
        # D1: the warning is in the signed-in Administrator's own row only.
        admin_client, admin = self.make_administrator("admin-1")
        other_client, other = self.sign_in("admin-2")
        self.assertEqual(self.save(admin_client, other, ["CREW", "ADMINISTRATOR"]).status_code, 302)
        page = admin_client.get(URL)
        html = page.content.decode()
        self.assertEqual(html.count("data-role-self-warning hidden>"), 1)
        self.assertEqual(html.count("data-role-change-form data-role-self>"), 1)
        self.assertContains(page, "You will lose access to Administration as soon as you save.")

        response = self.save(admin_client, admin, ["CREW"])

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/")
        self.assertEqual(current_role_codes(admin), ("CREW",))
        self.assertEqual(admin_client.get(URL).status_code, 403)
        self.assertEqual(other_client.get(URL).status_code, 200)

    def test_replaying_a_save_yields_one_set_of_records(self):
        admin_client, _ = self.make_administrator("admin-1")
        _, crew = self.sign_in("crew-1")
        data = self.save_data(admin_client, crew, ["SUPERVISOR"])
        first = admin_client.post(URL, data)
        after_first = self.counts()
        second = admin_client.post(URL, data)

        self.assertEqual((first.status_code, second.status_code), (302, 302))
        self.assertEqual(first["Location"], second["Location"])
        self.assertEqual(self.counts(), after_first)
        self.assertEqual(current_role_codes(crew), ("SUPERVISOR",))
        self.assertEqual(CommandReceipt.objects.filter(command_type="ROLE_CHANGES").count(), 1)

    def test_reusing_a_save_operation_id_for_a_different_selection_is_not_saved(self):
        admin_client, _ = self.make_administrator("admin-1")
        _, crew = self.sign_in("crew-1")
        operation_id = str(uuid4())
        first = self.save(admin_client, crew, ["CREW", "SUPERVISOR"], operation_id=operation_id)
        self.assertEqual(first.status_code, 302)
        after_first = self.counts()

        response = self.save(
            admin_client,
            crew,
            ["CREW", "SUPERVISOR", "SUPPLY_OFFICER"],
            operation_id=operation_id,
        )

        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "was not applied", status_code=409)
        self.assertEqual(self.counts(), after_first)
        self.assertEqual(current_role_codes(crew), ("CREW", "SUPERVISOR"))

    def test_a_non_administrator_save_is_denied_and_audited(self):
        admin_client, admin = self.make_administrator("admin-1")
        client, crew = self.sign_in("crew-1")
        client.get("/")
        grants = RoleGrant.objects.count()
        response = client.post(
            URL,
            {
                "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
                "stage": "save",
                "role_selection_present": "1",
                "operation_id": str(uuid4()),
                "target": str(crew.pk),
                "current_roles": ["CREW"],
                "roles": ["CREW", "ADMINISTRATOR"],
            },
        )
        self.assertEqual(response.status_code, 403)
        for private in (admin.email, "Organization users"):
            self.assertNotContains(response, private, status_code=403)
        self.assertEqual(RoleGrant.objects.count(), grants)
        self.assertEqual(current_role_codes(crew), ("CREW",))
        denial = AuditEvent.objects.get(action="ROLE_CHANGES", outcome="DENIED")
        self.assertEqual((denial.actor_user_id, denial.reason_code), (crew.pk, "FORBIDDEN"))

    def test_user_search_finds_matches_beyond_the_default_first_page(self):
        admin_client, _ = self.make_administrator("admin-1")
        for index in range(55):
            User.objects.create(
                organization=self.org,
                idp_issuer="https://accounts.google.com",
                idp_subject=f"directory-{index:03}",
                email=f"directory-{index:03}@example.test",
                display_name=f"Directory User {index:03}",
            )
        target = User.objects.create(
            organization=self.org,
            idp_issuer="https://accounts.google.com",
            idp_subject="search-target",
            email="zoe.target@example.test",
            display_name="Zoe Searchtarget",
        )

        by_name = admin_client.get(URL, {"q": "Searchtarget"})
        self.assertEqual(by_name.status_code, 200)
        self.assertEqual(by_name.context["target_total"], 1)
        self.assertContains(by_name, target.display_name)
        self.assertNotContains(by_name, "Directory User 000")

        by_email = admin_client.get(URL, {"q": "zoe.target@example.test"})
        self.assertEqual(by_email.context["target_total"], 1)
        self.assertContains(by_email, target.email)

    def test_non_administrators_cannot_submit_retired_or_unknown_stages(self):
        admin_client, admin = self.make_administrator("admin-1")
        client, crew = self.sign_in("crew-1")
        client.get("/")
        token = client.cookies["csrftoken"].value
        role_rows = RoleGrant.objects.count()
        receipts = CommandReceipt.objects.filter(command_type="ROLE_CHANGE").count()

        for stage in ("preview", "edit", None, "unexpected"):
            with self.subTest(stage=stage):
                data = {
                    "csrfmiddlewaretoken": token,
                    "operation_id": str(uuid4()),
                    "target": str(crew.pk),
                    "role": "ADMINISTRATOR",
                    "action": "GRANT",
                }
                if stage is not None:
                    data["stage"] = stage
                response = client.post(URL, data)

                self.assertEqual(response.status_code, 403)
                for private in (admin.email, crew.email, "Role history", "Organization users"):
                    self.assertNotContains(response, private, status_code=403)

        self.assertEqual(RoleGrant.objects.count(), role_rows)
        self.assertEqual(
            CommandReceipt.objects.filter(command_type="ROLE_CHANGE").count(), receipts
        )
        denials = AuditEvent.objects.filter(
            action="VIEW_ADMINISTRATION", outcome="DENIED", actor_user=crew
        )
        self.assertEqual(denials.count(), 4)
        self.assertEqual(current_role_codes(crew), ("CREW",))

    def test_administrator_missing_or_unknown_stage_is_rejected_without_a_change(self):
        admin_client, _ = self.make_administrator("admin-1")
        _, crew = self.sign_in("crew-1")
        before = self.counts()

        # The review page's preview and edit stages are retired with issue #236.
        for stage in (None, "unexpected", "preview", "edit"):
            with self.subTest(stage=stage):
                data = self.data(admin_client, crew)
                if stage is None:
                    data.pop("stage")
                else:
                    data["stage"] = stage
                response = admin_client.post(URL, data)

                self.assertEqual(response.status_code, 400)
                self.assertContains(response, "request was not valid", status_code=400)

        self.assertEqual(self.counts(), before)
        self.assertEqual(current_role_codes(crew), ("CREW",))

    def test_malformed_save_is_rejected_without_a_change(self):
        admin_client, _ = self.make_administrator("admin-1")
        _, crew = self.sign_in("crew-1")
        before = self.counts()
        cases = {
            "unknown role": {"roles": ["CREW", "ROOT"]},
            "legacy EMT alias": {"roles": ["EMT", "SUPERVISOR"]},
            "duplicate role": {"roles": ["SUPERVISOR", "SUPERVISOR"]},
            "unknown current role": {"current_roles": ["ROOT"]},
            "selection marker missing": {"role_selection_present": ""},
            "target not a UUID": {"target": "not-a-uuid"},
            "target not canonical": {"target": str(crew.pk).upper()},
            "operation id not a UUID": {"operation_id": "not-a-uuid"},
            "operation id not UUID4": {"operation_id": "00000000-0000-1000-8000-000000000000"},
        }
        for name, change in cases.items():
            with self.subTest(name=name):
                data = self.save_data(admin_client, crew, ["CREW", "SUPERVISOR"])
                data.update(change)
                response = admin_client.post(URL, data)
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.counts()[:2], before[:2])
        self.assertEqual(current_role_codes(crew), ("CREW",))

    def test_save_rejects_inactive_and_cross_organization_targets(self):
        admin_client, _ = self.make_administrator("admin-1")
        other_org = Organization.objects.create(name="Other Agency", station_zone="UTC")
        outside = User.objects.create(
            organization=other_org,
            idp_issuer="https://accounts.google.com",
            idp_subject="outside-save",
            email="outside-save@example.test",
            display_name="Outside Save",
        )
        inactive = User.objects.create(
            organization=self.org,
            idp_issuer="https://accounts.google.com",
            idp_subject="inactive-save",
            email="inactive-save@example.test",
            display_name="Inactive Save",
            active=False,
        )
        before = self.counts()

        for target in (outside, inactive):
            with self.subTest(target=target.idp_subject):
                data = self.save_data(admin_client, target, ["SUPERVISOR"], current=[])
                response = admin_client.post(URL, data)
                self.assertEqual(response.status_code, 404)
                self.assertNotContains(response, target.email, status_code=404)

        self.assertEqual(self.counts()[:2], before[:2])
        self.assertEqual(RoleGrant.objects.filter(user__in=(outside, inactive)).count(), 0)

    def test_search_errors_hide_users(self):
        admin_client, _ = self.make_administrator("admin-1")
        _, crew = self.sign_in("crew-1")
        before = self.counts()
        cases = (
            ("duplicate", ["first", "second"], "Search was supplied more than once"),
            ("too long", "x" * 101, "Search is limited to 100 characters"),
        )

        for name, query, message in cases:
            with self.subTest(search=name, method="GET"):
                response = admin_client.get(URL, {"q": query})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, message)
                self.assertContains(response, "The user list is hidden")
                self.assertEqual(response.context["targets"], [])
                self.assertEqual(response.context["target_total"], 0)
                self.assertNotContains(response, 'id="role-target"')

        self.assertEqual(self.counts(), before)
        self.assertEqual(current_role_codes(crew), ("CREW",))

    def test_a_crew_user_is_not_shown_the_link(self):
        client, _ = self.sign_in("crew-1")
        self.assertNotContains(client.get("/"), "/administration/")
        self.assertNotContains(
            client.get("/administration/"), "/administration/roles/", status_code=403
        )

    # Boundaries --------------------------------------------------------------

    def test_signed_out_requests_are_redirected_and_change_nothing(self):
        before = self.counts()
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.get(URL)["Location"], sign_in_location(URL))
        # CSRF is checked first, so a token-less signed-out POST is refused outright.
        self.assertEqual(client.post(URL, {}).status_code, 403)
        self.assertEqual(self.counts(), before)

    def test_missing_csrf_token_is_rejected_before_any_change(self):
        admin_client, _ = self.make_administrator("admin-1")
        _, crew = self.sign_in("crew-1")
        before = self.counts()
        response = admin_client.post(
            URL,
            {
                "operation_id": str(uuid4()),
                "target": str(crew.pk),
                "role": "SUPERVISOR",
                "action": "GRANT",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.counts(), before)

    def test_malformed_input_is_a_400_and_changes_nothing(self):
        admin_client, _ = self.make_administrator("admin-1")
        _, crew = self.sign_in("crew-1")
        before = self.counts()
        cases = {
            "unknown role": {"role": "ROOT"},
            "legacy EMT alias": {"role": "EMT"},
            "unknown action": {"action": "DELETE"},
            "target not a UUID": {"target_id": "not-a-uuid"},
            "target not canonical": {"target_id": str(crew.pk).upper()},
            "operation id not a UUID": {"operation_id": "not-a-uuid"},
            "operation id not UUID4": {"operation_id": "00000000-0000-1000-8000-000000000000"},
        }
        for name, case in cases.items():
            with self.subTest(name):
                case = dict(case)
                target = case.pop("target_id", str(crew.pk))
                token, fresh_id = self.form(admin_client)
                data = {
                    "csrfmiddlewaretoken": token,
                    "stage": "confirm",
                    "operation_id": fresh_id,
                    "target": target,
                    "role": "SUPERVISOR",
                    "action": "GRANT",
                    **case,
                }
                self.assertEqual(admin_client.post(URL, data).status_code, 400)
        self.assertEqual(self.counts(), before)

    def test_a_non_administrator_learns_nothing_from_error_responses(self):
        admin_client, admin = self.make_administrator("admin-1")
        client, crew = self.sign_in("crew-1")
        client.get("/")
        base = {
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
            "stage": "confirm",
            "operation_id": str(uuid4()),
            "role": "SUPERVISOR",
            "action": "GRANT",
        }
        malformed = client.post(URL, {**base, "target": "not-a-uuid"})
        self.assertEqual(malformed.status_code, 400)
        for private in (admin.email, "Role history", "Organization users"):
            self.assertNotContains(malformed, private, status_code=400)
        # A missing target must look like a denial, not reveal which ids exist.
        missing = client.post(URL, {**base, "target": str(uuid4())})
        self.assertEqual(missing.status_code, 403)

    def test_unknown_or_cross_organization_target_is_not_found(self):
        admin_client, _ = self.make_administrator("admin-1")
        other = Organization.objects.create(name="Other Agency", station_zone="UTC")
        stranger = User.objects.create(
            organization=other,
            idp_issuer="https://accounts.google.com",
            idp_subject="stranger",
            email="stranger@other.example",
        )
        before = self.counts()
        response = self.change(admin_client, stranger)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(RoleGrant.objects.filter(user=stranger).count(), 0)
        self.assertEqual(self.counts()[:2], before[:2])
        self.assertTrue(AuditEvent.objects.filter(action="ROLE_CHANGE", outcome="FAILED").exists())

    def test_last_administrator_cannot_be_revoked(self):
        admin_client, admin = self.make_administrator("admin-1")
        response = self.change(admin_client, admin, role="ADMINISTRATOR", action="REVOKE")
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "last active administrator", status_code=409)
        self.assertEqual(current_role_codes(admin), ("ADMINISTRATOR", "CREW"))

    def test_audit_outage_returns_503_without_a_successful_change(self):
        admin_client, _ = self.make_administrator("admin-1")
        _, crew = self.sign_in("crew-1")
        before = self.counts()
        outage_detail = "synthetic-audit-outage-detail"
        with mock.patch(
            "cya.core.audit_commands.append_audit",
            side_effect=RuntimeError(outage_detail),
        ):
            response = self.change(admin_client, crew)
        self.assertEqual(response.status_code, 503)
        self.assertNotContains(response, outage_detail, status_code=503)
        self.assertEqual(self.counts()[:2], before[:2])
        self.assertEqual(current_role_codes(crew), ("CREW",))

    def test_history_is_administrator_only_and_denial_is_audited(self):
        client, crew = self.sign_in("crew-1")
        self.assertEqual(client.get(URL).status_code, 403)
        self.assertTrue(
            AuditEvent.objects.filter(
                actor_user=crew, action="VIEW_ADMINISTRATION", outcome="DENIED"
            ).exists()
        )
