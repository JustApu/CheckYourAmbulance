"""Step bindings for scenario AC-22 (tests/scenarios/AC-22.json).

AC-22: a token with a valid signature but a personal-account identity or a
non-organization account is refused; a disabled user cannot sign in, and
historical attribution remains intact. Steps run inside the scenario test's
database transaction against the offline Google stub (cya.testing.stubs).
"""

import json

from django.test import Client, RequestFactory, override_settings

from cya.accounts.models import Organization, User
from cya.accounts.views import SIGNIN_COOKIE, SIGNIN_COOKIE_SALT
from cya.testing.scenarios import STEPS
from cya.testing.stubs import AUDIT, ROLE_GRANTER, activate_stub_google, stub_google_settings

DOMAIN_EMAIL = "someone@agency.example"


@STEPS.register("workspace_stub_identities")
def workspace_stub_identities(ctx):
    overrides = override_settings(**stub_google_settings())
    overrides.enable()
    ctx["cleanups"].append(overrides.disable)
    AUDIT.events.clear()
    ROLE_GRANTER.grants.clear()
    ctx["google"] = activate_stub_google()
    organization = Organization.objects.create(
        name="Sample Agency", station_zone="America/New_York"
    )
    ctx["disabled_user"] = User.objects.create(
        organization=organization,
        idp_issuer="https://accounts.google.com",
        idp_subject="disabled-1",
        email="disabled-1@agency.example",
        active=False,
    )
    ctx["attempts"] = {
        "managed": {"sub": "managed-1"},
        "personal": {"sub": "personal-1", "omit": ("hd",), "email": "person@gmail.example"},
        "domain_only": {"sub": "domain-1", "omit": ("hd",), "email": DOMAIN_EMAIL},
        "disabled": {"sub": "disabled-1"},
    }


def _sign_in(google, claims):
    client = Client()
    response = client.get("/auth/login/")
    request = RequestFactory().get("/")
    request.COOKIES[SIGNIN_COOKIE] = response.cookies[SIGNIN_COOKIE].value
    transaction = json.loads(request.get_signed_cookie(SIGNIN_COOKIE, salt=SIGNIN_COOKIE_SALT))
    code = google.issue_code(google.id_token(nonce=transaction["nonce"], **claims))
    result = client.get("/auth/callback/", {"state": transaction["state"], "code": code})
    return result.status_code


@STEPS.register("attempt_personal_domain_only_disabled")
def attempt_personal_domain_only_disabled(ctx):
    ctx["results"] = {
        name: _sign_in(ctx["google"], claims) for name, claims in ctx["attempts"].items()
    }


@STEPS.register("rejected")
def rejected(ctx):
    expected = {"managed": 200, "personal": 403, "domain_only": 403, "disabled": 403}
    if ctx["results"] != expected:
        raise AssertionError(f"sign-in results {ctx['results']} != {expected}")
    reasons = [e.reason_code for e in AUDIT.events if e.outcome == "REFUSED"]
    if reasons != ["not_workspace_account", "not_workspace_account", "user_inactive"]:
        raise AssertionError(f"unexpected refusal reasons {reasons}")
    subjects = set(User.objects.values_list("idp_subject", flat=True))
    if subjects != {"managed-1", "disabled-1"}:
        raise AssertionError(f"refused identities were provisioned: {subjects}")


@STEPS.register("historical_attribution_kept")
def historical_attribution_kept(ctx):
    user = User.objects.get(pk=ctx["disabled_user"].pk)
    if user.active or user.idp_subject != "disabled-1":
        raise AssertionError("disabled user's row changed")
