"""Step bindings for scenario AC-36 (tests/scenarios/AC-36.json; SPEC RPT-5a, issue #80).

The harness is the attention test fixture (cya.attention.tests.support.AttentionCase): vehicle A
follows a daily 06:00 schedule (early 05:00, grace G 07:00, pinned late limit L 19:00), nothing
is checked when it starts, and a controllable server clock drives the real commands. "Monday" is
day 0. Actions record what they read; the assert steps check those records. Every readiness read
goes through both the current (stored facts) path and the as-of (event replay) path, which must
agree. The scenario sets "database": "commit" because the commands commit for real.
"""

from datetime import timedelta

from django.core.management import call_command
from django.test import RequestFactory

from cya.attention import readiness, readiness_views
from cya.attention.readiness import fleet_readiness
from cya.testing.scenarios import STEPS, ScenarioError

US = timedelta(microseconds=1)


def _case(ctx):
    return ctx["ac36"]


def _counts(ctx, at, as_of=None):
    """{code: count} for vehicle A, also checking current == replay and the fleet totals."""
    case = _case(ctx)
    org = str(case.org.pk)
    picture = fleet_readiness(org, now=at, as_of=as_of)
    if as_of is None and picture != fleet_readiness(org, now=at, as_of=at):
        raise AssertionError(f"current picture differs from the event replay at {at}")
    for code, _label in readiness.CATEGORIES:
        if picture.totals[code] != sum(v.counts[code] for v in picture.vehicles):
            raise AssertionError(f"fleet total for {code} is not the sum of the vehicles")
    ctx.setdefault("reads", 0)
    ctx["reads"] += 1
    return dict(next(v for v in picture.vehicles if v.vehicle_id == str(case.vehicle.pk)).counts)


@STEPS.register("vehicle_with_daily_item")
def vehicle_with_daily_item(ctx):
    from cya.attention.tests.support import AttentionCase

    case = AttentionCase("run")
    case.setUp()  # the clock patch is undone by doCleanups; the scenario test flushes the data
    ctx["cleanups"].append(case.doCleanups)
    ctx["ac36"] = case


@STEPS.register("monday_obligation_missed_after_late_limit")
def monday_obligation_missed_after_late_limit(ctx):
    case = _case(ctx)
    ctx["limit"] = case.at(hours=19)  # day 0's L for every item (12 h pinned at creation)


@STEPS.register("read_current_readiness")
def read_current_readiness(ctx):
    ctx["base"] = _counts(ctx, _case(ctx).at(days=1, hours=5, minutes=29))


@STEPS.register("record_later_pass")
def record_later_pass(ctx):
    case = _case(ctx)
    case.clock.set(case.at(days=1, hours=5, minutes=30))
    ctx["pass_check"] = case.record(case.suction).body["check_id"]
    ctx["after_pass"] = _counts(ctx, case.at(days=1, hours=5, minutes=31))


@STEPS.register("record_later_fail")
def record_later_fail(ctx):
    case = _case(ctx)
    case.clock.set(case.at(days=1, hours=5, minutes=32))
    case.failed(case.siren)
    ctx["after_fail"] = _counts(ctx, case.at(days=1, hours=5, minutes=33))
    ctx["siren_row"] = case.row(case.siren)


@STEPS.register("read_at_late_limit_boundary")
def read_at_late_limit_boundary(ctx):
    case, limit = _case(ctx), ctx["limit"]
    ctx["at_limit"] = {d: _counts(ctx, limit + d * US)["MISSED"] for d in (-1, 0, 1)}
    # A check at exactly L may still be linked late (SCH-2): the linker's candidate query.
    from cya.checks.linking import _eligible

    found = _eligible(
        organization_id=str(case.org.pk),
        vehicle_id=str(case.vehicle.pk),
        item_id=str(case.masks.pk),
        kit_key="00000000-0000-0000-0000-000000000000",
        recorded_at=limit,
        late=True,
    )
    ctx["linkable_at_limit"] = [o.due_at for o in found]
    # Seven-day aging: generate a week ahead and read at day 0's due instant + 7 days.
    for day in range(2, 8):
        case.clock.set(case.at(days=day))
        if case.generate().failures:
            raise ScenarioError("obligation generation failed")
    edge = case.at(days=7, hours=6)
    ctx["per_day_missed"] = _counts(ctx, limit + US)["MISSED"]
    ctx["seven_days"] = (_counts(ctx, edge)["MISSED"], _counts(ctx, edge + US)["MISSED"])


@STEPS.register("read_asof_before_later_invalidation")
def read_asof_before_later_invalidation(ctx):
    from cya.checks.commands import invalidate_chain

    case = _case(ctx)
    # Day 6's suction obligation is missed; a later Pass at day 7 05:30 clears it from Overdue,
    # and that Pass is invalidated at 05:50, after the as-of instant 05:40.
    case.clock.set(case.at(days=7, hours=5, minutes=30))
    check = case.record(case.suction).body["check_id"]
    known_at = case.at(days=7, hours=5, minutes=40)
    ctx["known"] = _counts(ctx, known_at)
    case.clock.set(case.at(days=7, hours=5, minutes=50))
    invalidate_chain(check_id=check, explanation="Synthetic.", **case.context(case.admin))
    now = case.at(days=7, hours=5, minutes=55)
    ctx["asof_after_invalidation"] = _counts(ctx, now, as_of=known_at)
    ctx["current_after_invalidation"] = _counts(ctx, now)


@STEPS.register("record_later_obligation_check_between_grace_and_late_limit")
def record_later_obligation_check_between_grace_and_late_limit(ctx):
    from cya.obligations.adhoc import request_inspection

    case = _case(ctx)
    kit = case.reference  # day 7's 06:00 reference-kit obligation: G 07:00, L 19:00
    case.clock.set(case.at(days=7, hours=8, minutes=2))
    request_inspection(
        vehicle_id=str(case.vehicle.pk),
        due_at=case.at(days=7, hours=10),
        items=[{"item_id": str(case.kit_item.pk), "kit_instance_id": kit}],
        explanation="Synthetic explicit order.",
        **case.context(case.supervisor),
    )
    before_at = case.at(days=7, hours=8, minutes=3)
    ctx["case9_before"] = _counts(ctx, before_at)
    ctx["case9_rows"] = _overdue_rows(case, case.kit_item, kit, before_at)
    case.clock.set(case.at(days=7, hours=8, minutes=5))
    body = case.record(case.kit_item, kit_id=kit).body
    ctx["case9_links"] = [
        (link["timing"], link["obligation_id"]) for link in body["obligation_links"]
    ]
    limit = case.at(days=7, hours=19)
    ctx["case9"] = {
        "after": _counts(ctx, case.at(days=7, hours=8, minutes=10)),
        "limit": {d: _counts(ctx, limit + d * US) for d in (-1, 0, 1)},
    }


def _overdue_rows(case, item, kit, at):
    """Overdue rows of one item and kit instance in the readiness window, from event replay."""
    from cya.obligations.projections import get_vehicle_obligations_as_of
    from cya.obligations.state import State

    states = get_vehicle_obligations_as_of(
        str(case.vehicle.pk),
        at,
        organization_id=str(case.org.pk),
        start=at - readiness.WINDOW_BACK,
        end=at + readiness.WINDOW_AHEAD,
    )
    return sum(
        1
        for s in states
        if s.state == State.OVERDUE
        and s.obligation.item_id == str(item.pk)
        and s.obligation.kit_instance_id == kit
    )


def _request(ctx, path):
    case = _case(ctx)
    request = RequestFactory().get(path)
    request.cya_user = case.supervisor
    request.session = {}
    return request


@STEPS.register("read_readiness_page")
def read_readiness_page(ctx):
    response = readiness_views.panel(_request(ctx, "/fleet/readiness/panel/"))
    ctx["page"] = (response.status_code, response.content.decode())


@STEPS.register("read_readiness_api")
def read_readiness_api(ctx):
    import json

    response = readiness_views.api(_request(ctx, "/api/v1/fleet-readiness"))
    ctx["api"] = (response.status_code, json.loads(response.content))


@STEPS.register("read_reports_containing_readiness_counts")
def read_reports_containing_readiness_counts(ctx):
    # OQ-61: no report output contains fleet-readiness counts yet; the list is explicit so a
    # future report that adds them must also be checked here.
    ctx["reports"] = list(readiness.REPORT_OUTPUTS)


def _check(condition, message):
    if not condition:
        raise AssertionError(message)


@STEPS.register("overdue_and_missed_counted")
def overdue_and_missed_counted(ctx):
    base = ctx["base"]
    # At day 1 05:29 every overdue obligation (day 0, nothing checked) is past its L.
    _check(base["OVERDUE"] > 0 and base["MISSED"] == base["OVERDUE"], f"base counts {base}")


@STEPS.register("overdue_cleared_missed_retained_history_unchanged")
def overdue_cleared_missed_retained_history_unchanged(ctx):
    from cya.obligations.projections import get_vehicle_obligations_as_of

    base, after = ctx["base"], ctx["after_pass"]
    _check(after["OVERDUE"] == base["OVERDUE"] - 1, f"Overdue after a later Pass: {after}")
    _check(after["MISSED"] == base["MISSED"], f"Missed after a later Pass: {after}")
    case = _case(ctx)
    states = get_vehicle_obligations_as_of(
        str(case.vehicle.pk),
        case.at(days=1, hours=5, minutes=31),
        organization_id=str(case.org.pk),
        start=case.at(hours=6),
        end=case.at(hours=6),
    )
    monday = [s for s in states if s.obligation.item_id == str(case.suction.pk)]
    _check([s.state.value for s in monday] == ["OVERDUE"], "history keeps Monday Overdue")


@STEPS.register("fail_keeps_item_not_ready")
def fail_keeps_item_not_ready(ctx):
    after_pass, after_fail = ctx["after_pass"], ctx["after_fail"]
    _check(after_fail["OVERDUE"] == after_pass["OVERDUE"] - 1, f"Overdue after Fail {after_fail}")
    _check(after_fail["MISSED"] == after_pass["MISSED"], "Missed is unchanged by a later Fail")
    _check(ctx["siren_row"].attention.red, "the failed item is not ready (attention stays red)")


@STEPS.register("missed_aged_out_after_seven_days_and_late_limit_boundary")
def missed_aged_out_after_seven_days_and_late_limit_boundary(ctx):
    at_limit = ctx["at_limit"]
    _check(at_limit[-1] == 0 and at_limit[0] == 0 and at_limit[1] > 0, f"L boundary {at_limit}")
    _check(ctx["limit"] - timedelta(hours=13) in ctx["linkable_at_limit"], "linkable at L")
    on_edge, past_edge = ctx["seven_days"]
    _check(on_edge - past_edge == ctx["per_day_missed"], f"seven-day edge {ctx['seven_days']}")


@STEPS.register("asof_missed_as_known_then")
def asof_missed_as_known_then(ctx):
    _check(ctx["asof_after_invalidation"] == ctx["known"], "as-of ignores later invalidation")
    current = ctx["current_after_invalidation"]
    # The day-7 Pass cleared suction's misses of days 2 to 6 (day 1's own 06:00 was satisfied by
    # the earlier Pass, which also still clears day 0); invalidating it brings those five back.
    _check(current["OVERDUE"] == ctx["known"]["OVERDUE"] + 5, "current reflects the correction")
    _check(current["MISSED"] == ctx["known"]["MISSED"], "Missed is unchanged by it")


@STEPS.register("overdue_cleared_after_grace_missed_only_after_late_limit")
def overdue_cleared_after_grace_missed_only_after_late_limit(ctx):
    from cya.obligations.models import Obligation

    before, case9, rows = ctx["case9_before"], ctx["case9"], ctx["case9_rows"]
    ((timing, obligation_id),) = ctx["case9_links"]
    _check(timing == "ON_TIME", f"the check took the order's on-time window, not {timing}")
    _check(not Obligation.objects.get(pk=obligation_id).routine, "the check linked to the order")
    # Every earlier overdue row of that kit instance in the window clears, including day 7's
    # own 06:00 row, which is past G but not past L (case 9).
    _check(rows >= 1, "the kit instance had overdue rows")
    _check(case9["after"]["OVERDUE"] == before["OVERDUE"] - rows, f"case 9 Overdue {case9}")
    limit = case9["limit"]
    _check(limit[-1]["MISSED"] == limit[0]["MISSED"], "not missed at exactly L")
    _check(limit[1]["MISSED"] > limit[0]["MISSED"], "missed after L")
    _check(limit[1]["OVERDUE"] == limit[0]["OVERDUE"], "still cleared from Overdue after L")


@STEPS.register("stored_summary_equals_recomputation")
def stored_summary_equals_recomputation(ctx):
    case = _case(ctx)
    _check(ctx.get("reads", 0) > 0, "no readiness reads were compared")
    call_command("rebuild_obligation_projection", organization=str(case.org.pk), verify=True)


@STEPS.register("missed_count_on_page_per_vehicle_and_fleet_totals")
def missed_count_on_page_per_vehicle_and_fleet_totals(ctx):
    status, html = ctx["page"]
    _check(status == 200, f"page status {status}")
    _check("Fleet totals" in html and "Missed:" in html, "Missed shown on the page and totals")


@STEPS.register("missed_count_in_readiness_api")
def missed_count_in_readiness_api(ctx):
    status, body = ctx["api"]
    _check(status == 200 and "MISSED" in body["totals"], f"API totals {status}")
    _check(all("MISSED" in v["obligations"] for v in body["vehicles"]), "Missed per vehicle")


@STEPS.register("missed_count_summarized_in_reports")
def missed_count_summarized_in_reports(ctx):
    for name, render in ctx["reports"]:
        _check("MISSED" in render(), f"report {name} lacks the Missed count")
