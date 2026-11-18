"""Application shell: navigation by role, server-side enforcement, accessibility markers."""

import pathlib
import re
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from django.http import HttpResponse
from django.template import Context, Template
from django.template.loader import render_to_string
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from cya.accounts.middleware import (
    SESSION_AUTH_TIME_KEY,
    SESSION_USER_KEY,
    OrganizationTimezoneMiddleware,
)
from cya.accounts.models import Organization, User
from cya.accounts.returns import sign_in_location
from cya.authorization.tests.stubs import configure_backend
from cya.shell import navigation

ROLES = {}  # user id -> role names, read by the injected lookup below
LOOKUP = "cya.tests.test_shell.lookup"


class StationTimeFilterTests(SimpleTestCase):
    def test_both_fall_back_instants_keep_distinct_zone_and_offset(self):
        first = datetime(2025, 11, 2, 5, 30, tzinfo=UTC)
        second = datetime(2025, 11, 2, 6, 30, tzinfo=UTC)
        template = Template("{% load station_time %}{{ instant|station_time }}")
        seconds_template = Template('{% load station_time %}{{ instant|station_time:"seconds" }}')
        with timezone.override(ZoneInfo("America/New_York")):
            self.assertEqual(
                template.render(Context({"instant": first})), "2025-11-02 01:30 EDT (-0400)"
            )
            self.assertEqual(
                template.render(Context({"instant": second})), "2025-11-02 01:30 EST (-0500)"
            )
            self.assertEqual(
                seconds_template.render(Context({"instant": first})),
                "2025-11-02 01:30:00 EDT (-0400)",
            )


class OrganizationTimezoneMiddlewareTests(TestCase):
    def test_station_zone_is_activated_and_anonymous_requests_reset_to_utc(self):
        middleware = OrganizationTimezoneMiddleware(
            lambda _request: HttpResponse(timezone.get_current_timezone_name())
        )
        signed_in = SimpleNamespace(
            cya_user=SimpleNamespace(organization=SimpleNamespace(station_zone="America/New_York"))
        )
        self.assertEqual(middleware(signed_in).content, b"America/New_York")
        self.assertEqual(middleware(SimpleNamespace(cya_user=None)).content, b"UTC")


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
    def nav(self, response):
        html = response.content.decode()
        found = re.search(r'<nav class="app-nav" aria-label="Main">.*?</nav>', html, re.S)
        return found.group(0) if found else ""

    def labels(self, response):
        """(tab labels including More, More's place links) as shown in the navigation."""
        nav = self.nav(response)
        return (
            re.findall(r'<span class="app-nav-label">([^<]+)</span>', nav),
            re.findall(r"</svg><span>([^<]+)</span>(?:<kbd [^>]*>[^<]*</kbd>)?</a>", nav),
        )

    def test_each_role_sees_the_same_tabs_and_its_more_items(self):
        tabs = ["Home", "Inspect", "Problems", "Vehicles", "More"]
        cases = {
            "CREW": [],
            "SUPERVISOR": ["Kits", "Administration"],
            "SUPPLY_OFFICER": ["Kits", "Administration"],
            "ADMINISTRATOR": ["Kits", "Administration"],
        }
        for index, (role, more) in enumerate(cases.items()):
            with self.subTest(role=role):
                client = self.client_for(self.make_user(str(index), [role]))
                self.assertEqual(self.labels(client.get("/")), (tabs, more))

    def test_kits_links_the_first_kit_page_the_role_may_open(self):
        cases = {
            "SUPERVISOR": "/administration/kits/",
            "SUPPLY_OFFICER": "/administration/kit-instances/",
        }
        for index, (role, url) in enumerate(cases.items()):
            with self.subTest(role=role):
                client = self.client_for(self.make_user(str(index), [role]))
                self.assertRegex(
                    self.nav(client.get("/")), rf'<a href="{url}"[^>]*>.*?<span>Kits</span>'
                )

    def test_legacy_emt_constant_aliases_crew(self):
        self.assertEqual(navigation.EMT, navigation.CREW)

    def test_roles_are_a_union(self):
        client = self.client_for(self.make_user(roles=["CREW", "ADMINISTRATOR"]))
        self.assertIn("Administration", self.labels(client.get("/"))[1])

    def test_current_place_is_marked_not_by_color_alone(self):
        client = self.client_for(self.make_user(roles=["CREW"]))
        cases = {
            "/": "Home",
            "/inspection/": "Inspect",
            "/problems/": "Problems",
            "/status/": "Vehicles",
            "/fleet/": "Vehicles",
        }
        for path, label in cases.items():
            with self.subTest(path=path):
                nav = self.nav(client.get(path))
                self.assertEqual(nav.count('aria-current="page"'), 1)
                self.assertRegex(
                    nav,
                    r'aria-current="page"[^>]*><svg [^>]*aria-hidden="true".*?</svg>'
                    rf'<span class="app-nav-label">{label}</span>',
                )

    def test_more_is_marked_when_one_of_its_places_is_current(self):
        client = self.client_for(self.make_user(roles=["ADMINISTRATOR"]))
        nav = self.nav(client.get("/administration/"))
        self.assertIn("app-nav-more is-current", nav)
        # The visible More button tells assistive technology the current page is in its group.
        more = re.search(r'<button id="app-nav-more".*?>', nav).group(0)
        self.assertIn('aria-current="true"', more)
        self.assertRegex(nav, r'<a href="/administration/" aria-current="page"[^>]*>')
        nav = self.nav(client.get("/administration/kits/"))
        self.assertRegex(nav, r'<a href="/administration/kits/" aria-current="page"[^>]*>')
        self.assertNotRegex(nav, r'<a href="/administration/" aria-current')
        nav = self.nav(client.get("/inspection/"))
        self.assertNotIn("is-current", nav)
        self.assertNotIn('aria-current="true"', nav)

    def test_administration_groups_its_pages_in_task_order(self):
        # Issue #194: People, Vehicles, Kits, System, each a heading over its own cards.
        html = (
            self.client_for(self.make_user(roles=["ADMINISTRATOR"]))
            .get("/administration/")
            .content.decode()
        )
        groups = re.findall(
            r'<section class="admin-group" aria-labelledby="admin-group-(\w+)">'
            r'\s*<h2 id="admin-group-\1">([^<]+)</h2>(.*?)</section>',
            html,
            re.S,
        )
        self.assertEqual(
            [(key, label) for key, label, _ in groups],
            [
                ("people", "People"),
                ("vehicles", "Vehicles"),
                ("kits", "Kits"),
                ("system", "System"),
            ],
        )
        titles = {
            key: re.findall(r'<span class="admin-card-title">([^<]+)</span>', body)
            for key, _, body in groups
        }
        self.assertEqual(titles["people"], ["Role administration"])
        self.assertEqual(
            titles["vehicles"],
            ["Vehicles", "Compartments and systems", "Checklist templates", "Vehicle files"],
        )
        self.assertEqual(
            titles["kits"], ["Kit definitions", "Kit lists", "Kit instances", "Kit library"]
        )
        self.assertEqual(titles["system"], ["Outgoing mail"])

    def test_administration_hides_groups_with_no_page_for_the_role(self):
        html = (
            self.client_for(self.make_user(roles=["SUPPLY_OFFICER"]))
            .get("/administration/")
            .content.decode()
        )
        self.assertEqual(re.findall(r'<h2 id="admin-group-\w+">([^<]+)</h2>', html), ["Kits"])
        self.assertIn('href="/administration/kit-instances/"', html)

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
        for path in ("/", "/fleet/", "/inspection/", "/problems/", "/administration/"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], sign_in_location(path))

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
            self.assertNotContains(home, 'class="app-nav"')
            self.assertContains(home, 'action="/auth/logout/"')
            self.assertContains(home, 'id="theme-select"')
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
        self.assertIn(
            'name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"',
            html,
        )
        self.assertIn('<a class="skip-link" href="#main">', html)
        self.assertIn('<body class="has-app-nav">', html)
        self.assertIn('<nav class="app-nav" aria-label="Main">', html)
        self.assertLess(html.index('class="site-header"'), html.index('<nav class="app-nav"'))
        self.assertLess(html.index('<nav class="app-nav"'), html.index('<main id="main"'))
        # More: a hamburger icon with a visible name, a real button that reports its state.
        more = re.search(r'<button id="app-nav-more".*?</button>', html, re.S).group(0)
        self.assertIn(
            'type="button" aria-label="More menu" aria-controls="app-nav-more-panel" '
            'aria-expanded="false"',
            more,
        )
        # #194 keeps "Menu" in the name; it starts with the visible label (WCAG 2.5.3).
        self.assertIn('<span class="app-nav-label">More</span>', more)
        self.assertIn('aria-hidden="true"', more)
        self.assertIn('id="app-nav-more-panel" class="app-nav-more-panel"', html)
        # Every icon is decorative; each place has a visible text label.
        self.assertEqual(html.count("<svg "), html.count('aria-hidden="true" focusable="false"'))
        self.assertEqual(html.count("<h1>"), 1)
        self.assertIn('<main id="main"', html)

    def test_theme_control_offers_auto_light_and_dark(self):
        html = self.page()
        self.assertIn('<label class="theme-control" for="theme-select">Theme', html)
        for theme in ("auto", "light", "dark"):
            with self.subTest(theme=theme):
                self.assertIn(f'<option value="{theme}">{theme.title()}</option>', html)
        self.assertLess(
            html.index('localStorage.getItem("cya-theme")'),
            html.index("<style>"),
            "saved theme must be applied before the page stylesheet can paint",
        )

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
        self.assertIn("@media (min-width: 64rem)", css)
        self.assertIn("env(safe-area-inset-bottom)", css)
        self.assertIn("prefers-color-scheme: dark", css)
        self.assertIn(":focus-visible", css)

    def test_signed_in_header_has_csrf_protected_sign_out(self):
        html = self.page()
        self.assertEqual(html.count('action="/auth/logout/"'), 1, "Sign out is in More, once")
        panel = html[html.index('id="app-nav-more-panel"') :]
        self.assertRegex(panel, r'<form method="post" action="/auth/logout/">')
        self.assertIn('id="theme-select"', panel)
        self.assertIn("csrfmiddlewaretoken", html)

    def test_user_text_is_escaped(self):
        user = self.make_user("9", ["CREW"], name="<script>alert(1)</script>")
        html = self.client_for(user).get("/fleet/").content.decode()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)


class SortableTableTemplateTests(TestCase):
    """Issue #194: these admin lists are marked for the shell's sort and filter enhancement."""

    TABLES = {
        "operations/admin_vehicles.html": "Filter this list",
        "operations/admin_templates.html": "Filter templates",
        "kits/admin_definitions.html": "Filter kits",
        "kits/kit_instances.html": "Filter kits on this vehicle",
    }

    def test_admin_lists_are_sortable_and_filterable(self):
        from django.template.loader import get_template

        for name, label in self.TABLES.items():
            with self.subTest(template=name):
                source = pathlib.Path(get_template(name).origin.name).read_text(encoding="utf-8")
                self.assertIn(f'<table data-sortable data-filter-label="{label}">', source)

    def test_action_columns_are_not_sortable(self):
        from django.template.loader import get_template

        source = pathlib.Path(get_template("kits/kit_instances.html").origin.name).read_text(
            encoding="utf-8"
        )
        self.assertIn('<th scope="col" data-nosort>Duplicate</th>', source)

    def test_script_reports_sort_state_and_filter_counts(self):
        js = render_to_string("shell/shell.js")
        for marker in (
            'setAttribute("aria-sort", direction)',
            'other.removeAttribute("aria-sort")',
            "Showing ",
            'th.hasAttribute("data-nosort")',
        ):
            self.assertIn(marker, js)


class BrandMarkTests(TestCase):
    """Owner phone check (#194): the app icon sits beside the name in the header."""

    def test_header_brand_shows_the_decorative_icon_beside_the_name(self):
        from django.template.loader import get_template

        html = render_to_string("shell/brand_mark.html")
        self.assertIn('aria-hidden="true"', html)
        self.assertIn('focusable="false"', html)
        base = pathlib.Path(get_template("shell/base.html").origin.name).read_text(encoding="utf-8")
        self.assertIn(
            '{% include "shell/brand_mark.html" %}<span class="brand-name">Check Your Ambulance</span>',
            base,
        )


class SafeAreaTests(TestCase):
    """Owner iPhone check (#194): without viewport-fit=cover iOS reports 0 safe-area insets and
    the tab bar ran into the rounded corners and home indicator."""

    def test_viewport_reports_safe_areas_and_the_shell_pads_by_them(self):
        from django.template.loader import get_template

        base = pathlib.Path(get_template("shell/base.html").origin.name).read_text(encoding="utf-8")
        self.assertIn(
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">',
            base,
        )
        css = render_to_string("shell/shell.css")
        for inset in ("top", "bottom", "left", "right"):
            self.assertIn(f"env(safe-area-inset-{inset})", css)
        self.assertIn("calc(6rem + env(safe-area-inset-left))", css)


class DateTimeFocusTests(TestCase):
    """Issue #194 evidence: date and time fields keep a focus ring while Tab moves through their parts."""

    def test_date_and_time_fields_show_a_ring_on_focus(self):
        css = render_to_string("shell/shell.css")
        self.assertIn('input[type="datetime-local"]:focus', css)
        self.assertIn('input[type="date"]:focus', css)


class TapFeedbackTests(TestCase):
    """Issue #194 item 8: the shell's Saved toast and pressed state."""

    def test_toast_is_one_status_region_that_never_takes_focus(self):
        js = render_to_string("shell/shell.js")
        for marker in (
            'toastNode.setAttribute("role", "status")',
            "toast: showToast",
            "options.announce === false",
        ):
            self.assertIn(marker, js)
        self.assertNotIn("toastNode.focus", js)
        css = render_to_string("shell/shell.css")
        self.assertIn("pointer-events: none", css)
        self.assertIn("animation: none !important", css)
        self.assertIn(".button:active:not(:disabled)", css)


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


class AdministrationGroupTableTests(TestCase):
    def test_every_administration_page_is_in_exactly_one_group(self):
        from cya.shell.views import ADMINISTRATION_GROUPS, ADMINISTRATION_LINKS

        grouped = [name for _, _, _, names in ADMINISTRATION_GROUPS for name in names]
        self.assertEqual(len(grouped), len(set(grouped)))
        self.assertEqual(set(grouped), {name for name, *_ in ADMINISTRATION_LINKS})


class SectionTableTests(TestCase):
    def test_every_section_has_a_distinct_route_and_known_roles(self):
        keys = [s.key for s in navigation.SECTIONS]
        self.assertEqual(len(keys), len(set(keys)))
        for section in navigation.SECTIONS:
            self.assertTrue(section.roles and section.roles <= navigation.ALL_ROLES)
        self.assertEqual(
            navigation.section_by_key("administration").roles,
            frozenset({navigation.SUPERVISOR, navigation.SUPPLY_OFFICER, navigation.ADMINISTRATOR}),
        )
