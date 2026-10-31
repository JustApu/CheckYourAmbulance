"""Shared driver for the role-administration HTTP tests and scenario (issue 25 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/25)).

Signs synthetic users in through the offline Google stub and drives
/administration/roles/ with real CSRF checks. Roles, receipts and audit stay the
real MariaDB-backed collaborators; only the identity provider is stubbed. The
caller must be able to commit (TransactionTestCase or a "database": "commit"
scenario) because audited commands refuse to run inside a wrapping transaction.
"""

import io
import json
import re

from django.core.management import call_command
from django.test import Client, RequestFactory

from cya.accounts.models import Organization, User
from cya.accounts.views import SIGNIN_COOKIE, SIGNIN_COOKIE_SALT
from cya.authorization.models import RoleGrant
from cya.core.models import AuditEvent, CommandReceipt
from cya.testing.stubs import activate_stub_google, stub_google_settings

URL = "/administration/roles/"


def real_collaborator_settings():
    """stub_google_settings() without the recording audit and role-grant stubs."""
    overrides = stub_google_settings()
    del overrides["CYA_AUTH_AUDIT_SINK"], overrides["CYA_INITIAL_ROLE_GRANTER"]
    return overrides


def expect(actual, expected, what):
    """Assertion usable outside unittest (scenario steps have no TestCase)."""
    if actual != expected:
        raise AssertionError(f"{what}: expected {expected!r}, got {actual!r}")


class RoleHttpHarness:
    def __init__(self, organization=None):
        self.google = activate_stub_google()
        self.org = (
            organization
            if organization is not None
            else Organization.objects.create(name="Sample Agency", station_zone="UTC")
        )

    def sign_in(self, sub):
        client = Client(enforce_csrf_checks=True)
        response = client.get("/auth/login/")
        request = RequestFactory().get("/")
        request.COOKIES[SIGNIN_COOKIE] = response.cookies[SIGNIN_COOKIE].value
        txn = json.loads(request.get_signed_cookie(SIGNIN_COOKIE, salt=SIGNIN_COOKIE_SALT))
        code = self.google.issue_code(self.google.id_token(sub=sub, nonce=txn["nonce"]))
        result = client.get("/auth/callback/", {"state": txn["state"], "code": code})
        expect(result.status_code, 200, "sign-in callback")
        expect(client.get("/").status_code, 200, "signed-in home page")
        return client, User.objects.get(idp_subject=sub)

    def make_administrator(self, sub):
        client, user = self.sign_in(sub)
        call_command(
            "bootstrap_administrator",
            organization=self.org.pk,
            user=user.pk,
            stdout=io.StringIO(),
        )
        return client, user

    def form(self, client):
        """(csrf token, operation id) read from the rendered role-administration form."""
        page = client.get(URL)
        expect(page.status_code, 200, "role administration page")
        html = page.content.decode()
        return (
            re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html).group(1),
            re.search(r'name="operation_id" value="([^"]+)"', html).group(1),
        )

    def data(self, client, target, role="SUPERVISOR", action="GRANT", operation_id=None, **extra):
        token, fresh_id = self.form(client)
        return {
            "csrfmiddlewaretoken": token,
            "operation_id": operation_id or fresh_id,
            "target": str(target.pk),
            "role": role,
            "action": action,
            **extra,
        }

    def change(self, client, target, **kwargs):
        return client.post(URL, self.data(client, target, **kwargs))

    @staticmethod
    def counts():
        return (
            RoleGrant.objects.count(),
            CommandReceipt.objects.count(),
            AuditEvent.objects.count(),
        )
