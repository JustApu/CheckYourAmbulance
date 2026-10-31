"""Step bindings for scenario EX-role-change-end-to-end (issue 25 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/25)).

The walking skeleton's exit scenario: sign in through the offline Google stub,
change a role through the web interface, and show it persisted, in history and
audited; refuse the same action for a Crew user with a committed denial; and
replay one operation id into a single record. Runs with real commits on the
MariaDB test database (the scenario sets "database": "commit").
"""

from uuid import uuid4

from django.test import override_settings

from cya.authorization.models import RoleGrant
from cya.authorization.persistence import current_role_codes
from cya.core.models import AuditEvent, CommandReceipt
from cya.testing.role_http import URL, RoleHttpHarness, expect, real_collaborator_settings
from cya.testing.scenarios import STEPS


@STEPS.register("role_change_identities")
def role_change_identities(ctx):
    overrides = override_settings(**real_collaborator_settings())
    overrides.enable()
    ctx["cleanups"].append(overrides.disable)
    ctx["harness"] = harness = RoleHttpHarness()
    ctx["admin_client"], ctx["admin"] = harness.make_administrator("admin-1")
    ctx["crew_client"], ctx["crew"] = harness.sign_in("crew-1")


@STEPS.register("administrator_changes_role")
def administrator_changes_role(ctx):
    harness = ctx["harness"]
    ctx["before"] = harness.counts()
    ctx["operation_id"] = str(uuid4())
    ctx["data"] = harness.data(ctx["admin_client"], ctx["crew"], operation_id=ctx["operation_id"])
    ctx["first"] = ctx["admin_client"].post(URL, ctx["data"])


@STEPS.register("administrator_replays_operation")
def administrator_replays_operation(ctx):
    ctx["after_first"] = ctx["harness"].counts()
    ctx["replay"] = ctx["admin_client"].post(URL, ctx["data"])
    ctx["after_replay"] = ctx["harness"].counts()


@STEPS.register("crew_attempts_role_change")
def crew_attempts_role_change(ctx):
    crew_client, crew = ctx["crew_client"], ctx["crew"]
    crew_client.get("/")  # a CSRF cookie; the Crew user is never shown the form
    ctx["grants_before_denial"] = RoleGrant.objects.count()
    ctx["denied"] = crew_client.post(
        URL,
        {
            "csrfmiddlewaretoken": crew_client.cookies["csrftoken"].value,
            "operation_id": str(uuid4()),
            "target": str(crew.pk),
            "role": "ADMINISTRATOR",
            "action": "GRANT",
        },
    )


@STEPS.register("change_persisted_visible_audited")
def change_persisted_visible_audited(ctx):
    crew, admin = ctx["crew"], ctx["admin"]
    expect(ctx["first"].status_code, 302, "authorized change response")
    expect(current_role_codes(crew), ("CREW", "SUPERVISOR"), "persisted roles")
    grant = RoleGrant.objects.get(user=crew, role="SUPERVISOR")
    expect(grant.actor_user_id, admin.pk, "role history actor")
    audit = AuditEvent.objects.get(action="ROLE_CHANGE", outcome="SUCCEEDED")
    expect((audit.actor_user_id, audit.command_id), (admin.pk, grant.command_id), "audit link")
    history = ctx["admin_client"].get(URL).content.decode()
    expect(crew.email in history and "SUPERVISOR" in history, True, "change shown in history")


@STEPS.register("replay_yields_one_record")
def replay_yields_one_record(ctx):
    expect(ctx["replay"].status_code, 302, "replay response")
    expect(ctx["after_replay"], ctx["after_first"], "row counts after replay")
    expect(RoleGrant.objects.filter(user=ctx["crew"], role="SUPERVISOR").count(), 1, "grants")


@STEPS.register("denial_audited_nothing_changed")
def denial_audited_nothing_changed(ctx):
    crew = ctx["crew"]
    expect(ctx["denied"].status_code, 403, "unauthorized response")
    expect(RoleGrant.objects.count(), ctx["grants_before_denial"], "role rows after denial")
    expect(current_role_codes(crew), ("CREW", "SUPERVISOR"), "crew roles unchanged")
    denial = AuditEvent.objects.get(action="ROLE_CHANGE", outcome="DENIED")
    expect((denial.actor_user_id, denial.reason_code), (crew.pk, "FORBIDDEN"), "denial audit")
    expect(CommandReceipt.objects.filter(command_type="ROLE_CHANGE").count(), 1, "receipts")
