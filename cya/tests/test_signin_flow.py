"""End-to-end sign-in tests: offline Google stub, real MariaDB test database."""

import json
import logging
import time
from unittest import mock

from django.conf import settings
from django.db import IntegrityError
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import Resolver404, resolve

from cya.accounts import signin
from cya.accounts.models import Organization, User
from cya.accounts.views import SIGNIN_COOKIE, SIGNIN_COOKIE_SALT
from cya.testing.stubs import (
    AUDIT,
    ROLE_GRANTER,
    STUB_CLIENT_ID,
    STUB_HOSTED_DOMAIN,
    STUB_REDIRECT_URI,
    activate_stub_google,
    stub_google_settings,
)


@override_settings(**stub_google_settings())
class SignInFlowTests(TestCase):
    def setUp(self):
        self.google = activate_stub_google()
        AUDIT.events.clear()
        AUDIT.fail = False
        AUDIT.fail_with = None
        ROLE_GRANTER.grants.clear()
        ROLE_GRANTER.fail = False
        ROLE_GRANTER.fail_with = None
        self.organization = Organization.objects.create(
            name="Sample Agency", station_zone="America/New_York"
        )

    # Helpers -----------------------------------------------------------------

    def start(self, client=None):
        client = client or self.client
        response = client.get("/auth/login/")
        self.assertEqual(response.status_code, 302)
        # Read the signed cookie the way the callback does (public Django API).
        request = RequestFactory().get("/")
        request.COOKIES[SIGNIN_COOKIE] = response.cookies[SIGNIN_COOKIE].value
        return response, json.loads(
            request.get_signed_cookie(SIGNIN_COOKIE, salt=SIGNIN_COOKIE_SALT)
        )

    def sign_in(self, sub="1001", client=None, state=None, **claims):
        client = client or self.client
        _, transaction = self.start(client)
        code = self.google.issue_code(
            self.google.id_token(sub=sub, nonce=transaction["nonce"], **claims)
        )
        return client.get("/auth/callback/", {"state": state or transaction["state"], "code": code})

    def refusals(self):
        return [e.reason_code for e in AUDIT.events if e.outcome == "REFUSED"]

    def failures(self):
        return [e.reason_code for e in AUDIT.events if e.outcome == "FAILED"]

    def assert_unavailable_and_cookie_cleared(self, response):
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.cookies[SIGNIN_COOKIE].value, "")

    def status_text(self, client=None):
        return (client or self.client).get("/auth/status/").content.decode()

    # Starting sign-in ------------------------------------------------------------

    def test_login_redirects_to_google_with_state_nonce_pkce_and_domain_hint(self):
        response, transaction = self.start()
        location = response["Location"]
        self.assertTrue(location.startswith("https://accounts.google.com/o/oauth2/v2/auth?"))
        for part in (
            f"client_id={STUB_CLIENT_ID}",
            "response_type=code",
            "scope=openid+email+profile",
            f"state={transaction['state']}",
            f"nonce={transaction['nonce']}",
            "code_challenge_method=S256",
            f"hd={STUB_HOSTED_DOMAIN}",
        ):
            self.assertIn(part, location)
        self.assertNotIn(transaction["code_verifier"], location)  # only its hash is sent
        cookie = response.cookies[SIGNIN_COOKIE]
        self.assertEqual(
            (cookie["samesite"], cookie["path"], cookie["secure"], cookie["httponly"]),
            ("Lax", "/auth/", True, True),
        )

    def test_each_sign_in_attempt_gets_fresh_random_values(self):
        _, first = self.start()
        _, second = self.start()
        for name in ("state", "nonce", "code_verifier"):
            self.assertNotEqual(first[name], second[name])

    @override_settings(CYA_GOOGLE_CLIENT_ID="")
    def test_unconfigured_google_makes_sign_in_unavailable(self):
        self.assertEqual(self.client.get("/auth/login/").status_code, 503)

    @override_settings(CYA_AUTH_AUDIT_SINK=None)
    def test_sign_in_is_unavailable_without_an_audit_sink(self):
        self.assertEqual(self.client.get("/auth/login/").status_code, 503)

    # Successful sign-in and provisioning --------------------------------------

    def test_first_sign_in_provisions_an_emt_and_starts_a_strict_session(self):
        response = self.sign_in(sub="1001")
        self.assertEqual(response.status_code, 200)
        user = User.objects.get()
        self.assertEqual(
            (user.idp_issuer, user.idp_subject, user.organization_id, user.active),
            ("https://accounts.google.com", "1001", self.organization.id, True),
        )
        self.assertEqual([(g[0], g[1]) for g in ROLE_GRANTER.grants], [(str(user.id), "CREW")])
        self.assertEqual(
            [(e.action, e.outcome) for e in AUDIT.events],
            [("USER_PROVISIONED", "SUCCEEDED"), ("SIGN_IN", "SUCCEEDED")],
        )
        session_cookie = response.cookies[settings.SESSION_COOKIE_NAME]
        self.assertEqual(
            (session_cookie["samesite"], session_cookie["secure"], session_cookie["httponly"]),
            ("Strict", True, True),
        )
        self.assertEqual(response.cookies[SIGNIN_COOKIE].value, "")  # transaction cookie removed
        self.assertIn(f"1001@{STUB_HOSTED_DOMAIN}", self.status_text())

    def test_code_exchange_uses_configured_redirect_and_pkce_verifier(self):
        _, transaction = self.start()
        code = self.google.issue_code(self.google.id_token(sub="1001", nonce=transaction["nonce"]))
        self.client.get("/auth/callback/", {"state": transaction["state"], "code": code})
        self.assertEqual(
            self.google.exchanges,
            [{"redirect_uri": STUB_REDIRECT_URI, "code_verifier": transaction["code_verifier"]}],
        )

    def test_signing_in_as_someone_else_replaces_the_whole_session(self):
        self.sign_in(sub="1001")
        session = self.client.session
        session["leftover"] = "from the previous person"
        session.save()
        self.client.cookies[settings.SESSION_COOKIE_NAME] = session.session_key
        self.sign_in(sub="2002")
        self.assertNotIn("leftover", self.client.session)
        self.assertIn("2002@", self.status_text())

    def test_identity_is_the_subject_not_the_email(self):
        self.sign_in(sub="1001", email=f"old@{STUB_HOSTED_DOMAIN}")
        self.sign_in(sub="1001", email=f"new@{STUB_HOSTED_DOMAIN}", name="Renamed")
        user = User.objects.get()
        self.assertEqual((user.email, user.display_name), (f"new@{STUB_HOSTED_DOMAIN}", "Renamed"))
        update = [e for e in AUDIT.events if e.action == "USER_PROFILE_UPDATED"]
        self.assertEqual(len(update), 1)
        self.assertEqual(
            update[0].changes,
            (
                ("email", f"old@{STUB_HOSTED_DOMAIN}", f"new@{STUB_HOSTED_DOMAIN}"),
                ("display_name", "Synthetic 1001", "Renamed"),
            ),
        )
        self.assertEqual(len(ROLE_GRANTER.grants), 1)  # provisioned once
        self.sign_in(sub="2002", email=f"new@{STUB_HOSTED_DOMAIN}")
        self.assertEqual(User.objects.count(), 2)  # same email, different person

    # Refusals (AC-22, AUTH-1, AUTH-2) ------------------------------------------

    def test_personal_account_is_refused_and_nothing_is_written(self):
        response = self.sign_in(sub="3003", omit=("hd",), email="person@gmail.example")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.exists())
        self.assertEqual(ROLE_GRANTER.grants, [])
        self.assertEqual(self.refusals(), ["not_workspace_account"])
        self.assertIn("Not signed in", self.status_text())

    def test_disabled_user_cannot_sign_in_and_history_is_kept(self):
        self.sign_in(sub="1001")
        user = User.objects.get()
        User.objects.filter(pk=user.pk).update(active=False)
        self.client = Client()
        response = self.sign_in(sub="1001")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.refusals(), ["user_inactive"])
        self.assertTrue(User.objects.filter(pk=user.pk, active=False).exists())

    def test_state_mismatch_is_refused_before_any_code_exchange(self):
        response = self.sign_in(state="forged-state")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.refusals(), ["state_mismatch"])
        self.assertEqual(self.google.exchanges, [])

    def test_missing_or_tampered_transaction_cookie_is_refused(self):
        response = Client().get("/auth/callback/", {"state": "s", "code": "c"})
        self.assertEqual(response.status_code, 403)
        self.client.cookies[SIGNIN_COOKIE] = "tampered"
        self.assertEqual(
            self.client.get("/auth/callback/", {"state": "s", "code": "c"}).status_code, 403
        )
        self.assertEqual(self.refusals(), ["signin_expired", "signin_expired"])

    def test_provider_error_and_missing_code_are_refused(self):
        _, transaction = self.start()
        self.client.get(
            "/auth/callback/", {"state": transaction["state"], "error": "access_denied"}
        )
        _, transaction = self.start()
        self.client.get("/auth/callback/", {"state": transaction["state"]})
        self.assertEqual(self.refusals(), ["provider_error", "missing_code"])

    def test_authorization_code_cannot_be_replayed(self):
        response, transaction = self.start()
        raw_cookie = response.cookies[SIGNIN_COOKIE].value
        code = self.google.issue_code(self.google.id_token(sub="1001", nonce=transaction["nonce"]))
        params = {"state": transaction["state"], "code": code}
        self.assertEqual(self.client.get("/auth/callback/", params).status_code, 200)
        attacker = Client()
        attacker.cookies[SIGNIN_COOKIE] = raw_cookie
        self.assertEqual(attacker.get("/auth/callback/", params).status_code, 403)
        self.assertEqual(self.refusals(), ["code_rejected"])

    def test_refusal_page_and_audit_never_contain_the_token_or_code(self):
        _, transaction = self.start()
        token = self.google.id_token(sub="1001", nonce=transaction["nonce"], hd="other.example")
        code = self.google.issue_code(token)
        response = self.client.get("/auth/callback/", {"state": transaction["state"], "code": code})
        page = response.content.decode()
        self.assertNotIn(code, page)
        self.assertNotIn(token, page)
        self.assertNotIn(code, repr(AUDIT.events))
        self.assertIn("wrong_workspace_domain", page)

    def test_sign_in_requires_exactly_one_active_organization(self):
        Organization.objects.create(name="Second", station_zone="America/New_York")
        self.assertEqual(self.sign_in().status_code, 403)
        Organization.objects.all().delete()
        self.assertEqual(self.sign_in().status_code, 403)
        self.assertEqual(self.refusals(), ["organization_not_configured"] * 2)

    def test_unreachable_google_is_unavailable_not_refused(self):
        self.google.unavailable = True
        self.assert_unavailable_and_cookie_cleared(self.sign_in())
        self.assertEqual(self.refusals(), [])
        self.assertEqual(self.failures(), ["provider_unavailable"])
        self.assertFalse(User.objects.exists())

    def test_role_grant_failure_rolls_back_and_is_audited_as_a_failure(self):
        ROLE_GRANTER.fail = True
        with self.assertLogs("cya.accounts.views", "ERROR"):
            self.assert_unavailable_and_cookie_cleared(self.sign_in(sub="1001"))
        self.assertFalse(User.objects.exists())
        # Only the separate failure record survives; the provisioning events rolled back.
        self.assertEqual(
            [(e.action, e.outcome, e.reason_code) for e in AUDIT.events],
            [("SIGN_IN", "FAILED", "internal_error")],
        )
        self.assertIn("Not signed in", self.status_text())

    def test_audit_outage_during_provisioning_rolls_back_and_returns_503(self):
        AUDIT.fail = True
        with self.assertLogs("cya.accounts.views", "ERROR"):
            self.assert_unavailable_and_cookie_cleared(self.sign_in(sub="1001"))
        self.assertFalse(User.objects.exists())
        self.assertIn("Not signed in", self.status_text())

    def test_audit_outage_during_profile_refresh_keeps_the_old_profile(self):
        self.sign_in(sub="1001", email=f"old@{STUB_HOSTED_DOMAIN}")
        AUDIT.fail = True
        self.client = Client()
        with self.assertLogs("cya.accounts.views", "ERROR"):
            response = self.sign_in(sub="1001", email=f"new@{STUB_HOSTED_DOMAIN}")
        self.assert_unavailable_and_cookie_cleared(response)
        self.assertEqual(User.objects.get().email, f"old@{STUB_HOSTED_DOMAIN}")

    def assert_logs_hide(self, logs, *markers):
        """No marker may reach any log output, including formatted tracebacks."""
        formatter = logging.Formatter("%(levelname)s %(name)s %(message)s")
        for record in logs.records:
            self.assertIsNone(record.exc_info)
            text = formatter.format(record)
            for marker in markers:
                self.assertNotIn(marker, text)

    @staticmethod
    def chained_error(marker):
        try:
            raise ValueError(f"inner {marker}-inner")
        except ValueError as inner:
            try:
                raise RuntimeError(f"outer {marker}-outer") from inner
            except RuntimeError as outer:
                return outer

    def test_collaborator_failure_logs_never_contain_exception_text(self):
        ROLE_GRANTER.fail_with = self.chained_error("SECRET-GRANT")
        with self.assertLogs("cya.accounts.views", "ERROR") as logs:
            response = self.sign_in(sub="1001")
        self.assert_unavailable_and_cookie_cleared(response)
        self.assertFalse(User.objects.exists())
        self.assert_logs_hide(logs, "SECRET-GRANT")
        self.assertIn("RuntimeError <- ValueError", logs.output[0])
        failure = AUDIT.events[-1]
        self.assertEqual((failure.outcome, failure.reason_code), ("FAILED", "internal_error"))
        self.assertIn(failure.correlation_id, logs.output[0])
        self.assertIn(failure.correlation_id, response.content.decode())  # same reference shown
        self.assertNotIn("SECRET-GRANT", response.content.decode())

    def test_audit_failure_logs_never_contain_exception_text(self):
        _, transaction = self.start()
        AUDIT.fail_with = self.chained_error("SECRET-AUDIT")
        with self.assertLogs("cya.accounts.views", "ERROR") as logs:
            response = self.client.get("/auth/callback/", {"state": "forged", "code": "x"})
        self.assert_unavailable_and_cookie_cleared(response)
        self.assert_logs_hide(logs, "SECRET-AUDIT")

    def test_sign_out_audit_failure_logs_never_contain_exception_text(self):
        client = Client(enforce_csrf_checks=True)
        self.sign_in(sub="1001", client=client)
        self.status_text(client)
        token = client.cookies[settings.CSRF_COOKIE_NAME].value
        AUDIT.fail_with = self.chained_error("SECRET-SIGNOUT")
        with self.assertLogs("cya.accounts.views", "ERROR") as logs:
            response = client.post("/auth/logout/", {"csrfmiddlewaretoken": token})
        self.assertEqual(response.status_code, 503)
        self.assert_logs_hide(logs, "SECRET-SIGNOUT")
        self.assertIn("Not signed in", self.status_text(client))

    def test_refusal_that_cannot_be_audited_returns_503(self):
        _, transaction = self.start()
        AUDIT.fail = True
        with self.assertLogs("cya.accounts.views", "ERROR"):
            response = self.client.get("/auth/callback/", {"state": "forged", "code": "unused"})
        self.assert_unavailable_and_cookie_cleared(response)

    @override_settings(CYA_INITIAL_ROLE_GRANTER=None)
    def test_new_user_cannot_be_provisioned_without_the_role_granter(self):
        self.assert_unavailable_and_cookie_cleared(self.sign_in(sub="1001"))
        self.assertFalse(User.objects.exists())
        self.assertEqual(self.failures(), ["not_configured"])

    def test_concurrent_first_sign_in_retries_once_after_unique_conflict(self):
        # Simulates losing the insert race: the first attempt hits the unique
        # identity key (and rolls back), the single retry finds or creates the row.
        real = signin._find_or_provision
        with mock.patch.object(
            signin, "_find_or_provision", side_effect=_first_raises_then(real)
        ) as patched:
            self.assertEqual(self.sign_in(sub="1001").status_code, 200)
        self.assertEqual(patched.call_count, 2)
        self.assertEqual(User.objects.count(), 1)

    # Sessions, revocation and sign-out (AUTH-4, 16.2) ------------------------

    def test_deactivated_user_loses_an_active_session_on_the_next_request(self):
        self.sign_in(sub="1001")
        self.assertIn("Signed in", self.status_text())
        User.objects.update(active=False)
        self.assertIn("Not signed in", self.status_text())

    @override_settings(CYA_SESSION_MAX_AGE=600)
    def test_session_older_than_the_bound_requires_signing_in_again(self):
        self.sign_in(sub="1001")
        later = time.time() + 601
        with mock.patch("cya.accounts.middleware.time.time", return_value=later):
            self.assertIn("Not signed in", self.status_text())

    def test_sign_out_requires_post_with_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        self.sign_in(sub="1001", client=client)
        self.assertIn("Signed in", self.status_text(client))
        self.assertEqual(client.get("/auth/logout/").status_code, 405)
        self.assertEqual(client.post("/auth/logout/").status_code, 403)  # no CSRF token
        token = client.cookies[settings.CSRF_COOKIE_NAME].value
        response = client.post("/auth/logout/", {"csrfmiddlewaretoken": token})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Not signed in", self.status_text(client))
        self.assertEqual(AUDIT.events[-1].action, "SIGN_OUT")

    def test_sign_out_ends_the_session_even_if_auditing_fails(self):
        client = Client(enforce_csrf_checks=True)
        self.sign_in(sub="1001", client=client)
        self.status_text(client)  # issues the CSRF cookie
        token = client.cookies[settings.CSRF_COOKIE_NAME].value
        AUDIT.fail = True
        with self.assertLogs("cya.accounts.views", "ERROR"):
            response = client.post("/auth/logout/", {"csrfmiddlewaretoken": token})
        self.assertEqual(response.status_code, 503)
        self.assertIn("Not signed in", self.status_text(client))

    def test_there_is_no_password_or_alternate_login(self):
        # AUTH-5 / OQ-13: Google is the only way in.
        self.assertNotIn("django.contrib.auth", settings.INSTALLED_APPS)
        for path in ("/auth/password/", "/admin/", "/login/", "/accounts/login/"):
            with self.subTest(path=path), self.assertRaises(Resolver404):
                resolve(path)


def _first_raises_then(real):
    calls = {"n": 0}

    def side_effect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IntegrityError("duplicate identity")
        return real(*args, **kwargs)

    return side_effect
