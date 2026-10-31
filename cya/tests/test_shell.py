"""Application shell: navigation by role, server-side enforcement, accessibility markers."""

import re
import time
from unittest import mock

from django.template.loader import render_to_string
from django.test import Client, TestCase, override_settings

from cya.accounts.middleware import SESSION_AUTH_TIME_KEY, SESSION_USER_KEY
from cya.accounts.models import Organization, User
from cya.authorization.tests.stubs import configure_backend
from cya.shell import navigation

ROLES = {}  # user id -> role names, read by the injected lookup below
LOOKUP = "cya.tests.test_shell.lookup"


def lookup(user):
    return ROLES.get(str(user.id), ())


def failing_lookup(user):
    raise RuntimeError("secret detail that must not be logged")


@override_settings(CYA_AUTHORIZATION_BACKEND="cya.authorization.tests.stubs.recording_backend")
class ShellTestCase(TestCase):
    def setUp(self):
        ROLES.clear()
        self.addCleanup(ROLES.clear)
        self.backend = configure_backend()
        self.organization = Organization.objects.create(
            name="Sample Agency", station_zone="America/New_York"
        )

    def make_user(self, subject="1", roles=(), name="Pat Example"):
        user = User.objects.create(
            organization=self.organization,
            idp_issuer="https://accounts.google.com",
            idp_subject=subject,
            email=f"user{subject}@example.test",
            display_name=name,
        )
        ROLES[str(user.id)] = tuple(roles)
        return user

    def client_for(self, user):
        client = Client()
        session = client.session
        session[SESSION_USER_KEY] = str(user.id)
        session[SESSION_AUTH_TIME_KEY] = int(time.time())
        session.save()
        client.cookies["cya_session"] = session.session_key
        return client


@override_settings(CYA_ROLE_LOOKUP=LOOKUP)
class NavigationTests(ShellTestCase):
    def labels(self, response):
        html = response.content.decode()
        nav = re.search(r'<nav class="site-nav".*?</nav>', html, re.S)
        return re.findall(r">([^<]+)</a>", nav.group(0)) if nav else []

    def test_each_role_sees_its_sections(self):
        cases = {
            "CREW": ["Fleet", "Inspection", "Vehicle status", "Deficiencies"],
            "SUPERVISOR": ["Fleet", "Inspection", "Vehicle status", "Deficiencies"],
            "SUPPLY_OFFICER": ["Fleet", "Inspection", "Vehicle status", "Deficiencies"],
            "ADMINISTRATOR": [
                "Fleet",
                "Inspection",
                "Vehicle status",
                "Deficiencies",
                "Administration",
            ],
        }
        for index, (role, expected) in enumerate(cases.items()):
            with self.subTest(role=role):
                client = self.client_for(self.make_user(str(index), [role]))
                self.assertEqual(self.labels(client.get("/fleet/")), expected)

    def test_legacy_emt_constant_aliases_crew(self):
        self.assertEqual(navigation.EMT, navigation.CREW)

    def test_roles_are_a_union(self):
        client = self.client_for(self.make_user(roles=["CREW", "ADMINISTRATOR"]))
        self.assertIn("Administration", self.labels(client.get("/")))

    def test_current_section_is_marked_not_by_color_alone(self):
        client = self.client_for(self.make_user(roles=["CREW"]))
        html = client.get("/inspection/").content.decode()
        self.assertEqual(html.count('aria-current="page">'), 1)
        self.assertRegex(html, r'<a href="/inspection/" aria-current="page">Inspection</a>')

    def test_hidden_sections_are_still_denied_by_the_server(self):
        client = self.client_for(self.make_user(roles=["CREW"]))
        response = client.get("/administration/")
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("not available yet", response.content.decode())

    def test_administration_links_to_role_administration(self):
        client = self.client_for(self.make_user(roles=["ADMINISTRATOR"]))
        response = client.get("/administration/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'href="/administration/roles/"')

    def test_signed_out_visitors_go_to_sign_in_and_see_no_navigation(self):
        client = Client()
        for path in ("/", "/fleet/", "/inspection/", "/deficiencies/", "/administration/"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], "/auth/login/")

    def test_responses_are_not_cached(self):
        client = self.client_for(self.make_user(roles=["CREW"]))
        self.assertIn("no-store", client.get("/fleet/")["Cache-Control"])

    def test_only_get_is_allowed(self):
        client = self.client_for(self.make_user(roles=["CREW"]))
        self.assertEqual(client.post("/fleet/").status_code, 405)


class RoleLookupFailClosedTests(ShellTestCase):
    def test_unset_lookup_means_no_sections(self):
        with override_settings(CYA_ROLE_LOOKUP=None):
            client = self.client_for(self.make_user(roles=["ADMINISTRATOR"]))
            for path in ("/fleet/", "/administration/"):
                self.assertEqual(client.get(path).status_code, 403)
            home = client.get("/")
            self.assertNotContains(home, 'class="site-nav"')
            self.assertContains(home, "No sections are available")

    def test_failing_lookup_means_no_sections_and_logs_only_the_class(self):
        user = self.make_user(roles=["ADMINISTRATOR"])
        with override_settings(CYA_ROLE_LOOKUP="cya.tests.test_shell.failing_lookup"):
            with self.assertLogs("cya.shell.navigation", "ERROR") as logs:
                response = self.client_for(user).get("/fleet/")
        self.assertEqual(response.status_code, 403)
        output = "\n".join(logs.output)
        self.assertIn("RuntimeError", output)
        self.assertNotIn("secret detail", output)

    def test_unknown_role_names_grant_nothing(self):
        with override_settings(CYA_ROLE_LOOKUP=LOOKUP):
            client = self.client_for(self.make_user(roles=["ROOT", "administrator"]))
            self.assertEqual(client.get("/administration/").status_code, 403)


@override_settings(CYA_ROLE_LOOKUP=LOOKUP)
class ShellMarkupTests(ShellTestCase):
    def page(self):
        return self.client_for(self.make_user(roles=["CREW"])).get("/fleet/").content.decode()

    def test_page_has_responsive_accessible_structure(self):
        html = self.page()
        self.assertIn('<html lang="en">', html)
        self.assertIn('name="viewport" content="width=device-width, initial-scale=1"', html)
        self.assertIn('<a class="skip-link" href="#main">', html)
        self.assertIn('<nav class="site-nav" aria-label="Main">', html)
        self.assertEqual(html.count("<h1>"), 1)
        self.assertIn('<main id="main"', html)

    def test_connection_banner_is_an_alert_with_text(self):
        html = self.page()
        banner = re.search(r'<div id="connection-banner".*?</div>', html, re.S).group(0)
        self.assertIn('role="alert"', banner)
        self.assertIn("hidden", banner)
        self.assertIn("Connection lost.", banner)
        self.assertIn("Unconfirmed", banner)
        self.assertNotIn("not been recorded", banner)

    def test_touch_targets_and_responsive_css_are_present(self):
        css = render_to_string("shell/shell.css")
        self.assertGreaterEqual(css.count("min-height: 48px"), 4)
        self.assertIn("@media (min-width: 48rem)", css)
        self.assertIn("prefers-color-scheme: dark", css)
        self.assertIn(":focus-visible", css)

    def test_signed_in_header_has_csrf_protected_sign_out(self):
        html = self.page()
        self.assertRegex(html, r'<form method="post" action="/auth/logout/">')
        self.assertIn("csrfmiddlewaretoken", html)

    def test_user_text_is_escaped(self):
        user = self.make_user("9", ["CREW"], name="<script>alert(1)</script>")
        html = self.client_for(user).get("/fleet/").content.decode()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)


class SaveStateIndicatorTests(TestCase):
    STATES = {
        "pending": ("Saving", "save-state-pending"),
        "saved": ("Saved", "save-state-saved"),
        "failed": ("Not saved. Try again.", "save-state-failed"),
    }

    def test_every_state_has_text_and_an_icon_not_just_color(self):
        for state, (text, css_class) in self.STATES.items():
            with self.subTest(state=state):
                html = render_to_string("shell/save_state.html", {"state": state})
                self.assertIn(text, html)
                self.assertIn(css_class, html)
                self.assertIn('role="status"', html)
                self.assertRegex(
                    html, r'<span class="indicator-icon" aria-hidden="true">\S+</span>'
                )

    def test_states_use_distinct_icons(self):
        icons = {
            re.search(
                r'indicator-icon" aria-hidden="true">(\S+)<',
                render_to_string("shell/save_state.html", {"state": state}),
            ).group(1)
            for state in self.STATES
        }
        self.assertEqual(len(icons), 3)

    def test_script_treats_unknown_state_as_failed(self):
        js = render_to_string("shell/shell.js")
        self.assertIn('if (!labels[state]) { state = "failed"; }', js)


class SignInPagesUseTheShellTests(ShellTestCase):
    def test_sign_in_status_page_uses_the_shell(self):
        user = self.make_user()
        with override_settings(CYA_ROLE_LOOKUP=LOOKUP):
            response = self.client_for(user).get("/auth/status/")
        self.assertContains(response, 'class="skip-link"')
        self.assertContains(response, "Pat Example")

    def test_signed_out_page_does_not_look_signed_in(self):
        user = self.make_user()
        with mock.patch("cya.accounts.signin.record"):
            client = self.client_for(user)
            token = client.get("/auth/status/").cookies["csrftoken"].value
            response = client.post("/auth/logout/", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "/auth/logout/")
        self.assertNotContains(response, "Pat Example")


class SectionTableTests(TestCase):
    def test_every_section_has_a_distinct_route_and_known_roles(self):
        keys = [s.key for s in navigation.SECTIONS]
        self.assertEqual(len(keys), len(set(keys)))
        for section in navigation.SECTIONS:
            self.assertTrue(section.roles and section.roles <= navigation.ALL_ROLES)
        self.assertEqual(
            navigation.section_by_key("administration").roles,
            frozenset({navigation.ADMINISTRATOR}),
        )
