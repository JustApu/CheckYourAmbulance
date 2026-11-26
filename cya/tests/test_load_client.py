"""The shift-change load client (issue #10) against a fake loopback site; no network."""

import asyncio
import contextvars
import json
import pickle
import unittest

import h11
from wsproto import ConnectionType, WSConnection
from wsproto.events import AcceptConnection, CloseConnection, Request, TextMessage

from tools import load_client

VEHICLE = "11111111-1111-4111-8111-111111111111"
SESSION = "22222222-2222-4222-8222-222222222222"
SOCKET = f"/ws/v1/vehicles/{VEHICLE}/live/"
ITEMS = [f"3333333{n}-3333-4333-8333-333333333333" for n in range(3)]
TOKEN = "synthetic-csrf-token"


def script(element_id, config):
    # As Django's json_script: <, > and & are escaped inside the JSON.
    text = json.dumps(config).replace("<", "\\u003C").replace(">", "\\u003E")
    return f'<script id="{element_id}" type="application/json">{text}</script>'


class FakeSite:
    """Just enough of the application: pages, session start, checklist, saves, panel, sockets."""

    def __init__(self):
        self.latest = dict.fromkeys(ITEMS)
        self.saves, self.checklist_reads, self.panel_reads = [], 0, 0
        self.item_reads = []  # the item ids each partial checklist read named (issue #278 B)
        self.sockets = []
        self.fail_first_save_of = ITEMS[0]  # answer 503 once: the client must retry the same op
        self.conflict_first_save_of = ITEMS[1]  # answer 409 once: the client acknowledges
        self.refused = []
        self.stall = None  # a path whose requests are read but never answered
        self.push_rows = False  # SPEC 8.3.0: push each check's rows before its hint
        self.release = asyncio.Event()

    async def handle(self, reader, writer):
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = await reader.read(65536)
            if not chunk:
                writer.close()
                return
            head += chunk
        if b"upgrade: websocket" in head.lower():
            await self.websocket(head, reader, writer)
        else:
            await self.http(head, reader, writer)

    async def http(self, data, reader, writer):
        conn = h11.Connection(h11.SERVER)
        conn.receive_data(data)
        while True:
            request, body = None, b""
            while True:
                event = conn.next_event()
                if event is h11.NEED_DATA:
                    more = await reader.read(65536)
                    if not more:
                        writer.close()
                        return
                    conn.receive_data(more)
                elif isinstance(event, h11.Request):
                    request = event
                elif isinstance(event, h11.Data):
                    body += event.data
                elif isinstance(event, h11.EndOfMessage):
                    break
                elif isinstance(event, h11.ConnectionClosed):
                    writer.close()
                    return
            headers = {k.decode().lower(): v.decode() for k, v in request.headers}
            if request.target.decode().split("?")[0] == self.stall:
                await self.release.wait()
                writer.close()
                return
            status, content, extra = self.route(
                request.method.decode(), request.target.decode(), headers, body
            )
            out = conn.send(
                h11.Response(
                    status_code=status,
                    headers=[("Content-Length", str(len(content))), *extra],
                )
            )
            out += conn.send(h11.Data(data=content)) + conn.send(h11.EndOfMessage())
            writer.write(out)
            await writer.drain()
            conn.start_next_cycle()

    def route(self, method, target, headers, body):
        if "cya_session=synthetic-" not in headers.get("cookie", ""):
            self.refused.append(target)
            return 401, b"{}", []
        if method == "POST" and (
            headers.get("x-csrftoken") != TOKEN or headers.get("origin") != self.origin
        ):
            self.refused.append(target)
            return 403, b'{"status": "REJECTED", "code": "CSRF"}', []
        form = f'<input type="hidden" name="csrfmiddlewaretoken" value="{TOKEN}">'
        cookie = [("Set-Cookie", "csrftoken=synthetic; Path=/; Secure")]
        path = target.split("?")[0]
        if path == f"/inspection/vehicles/{VEHICLE}/":
            return 200, form.encode(), cookie
        if path == "/api/v1/inspection-sessions":
            result = {"status": "COMMITTED", "result": {"session_id": SESSION}}
            return 201, json.dumps(result).encode(), []
        if path == f"/inspection/sessions/{SESSION}/":
            config = {
                "session_id": SESSION,
                "vehicle_id": VEHICLE,
                "view": "COMPARTMENT",
                "urls": {
                    "checklist": f"/api/v1/vehicles/{VEHICLE}/checklist",
                    "checks": "/api/v1/checks",
                    "socket": SOCKET,
                },
            }
            return 200, (form + script("cya-config", config)).encode(), []
        if path == f"/api/v1/vehicles/{VEHICLE}/checklist":
            query = dict(p.split("=", 1) for p in target.partition("?")[2].split("&") if p)
            named = set(query["item_ids"].split(",")) if "item_ids" in query else None
            if named is None:
                self.checklist_reads += 1
            else:
                self.item_reads.append(named)
            items = [
                {"item_id": i, "checklist_version_id": "v1", "latest_check_id": c}
                for i, c in self.latest.items()
                if named is None or i in named
            ]
            return 200, json.dumps({"status": "OK", "items": items}).encode(), []
        if path == "/api/v1/checks":
            return self.save(json.loads(body))
        if path == "/fleet/readiness/":
            config = {
                "panel_url": "/fleet/readiness/panel/",
                "sockets": [SOCKET],
                "refresh_seconds": 0.05,
            }
            return 200, script("readiness-config", config).encode(), []
        if path == "/fleet/readiness/panel/":
            self.panel_reads += 1
            return 200, b"<section>panel</section>", []
        return 404, b"{}", []

    def save(self, payload):
        self.saves.append(payload)
        item = payload["item_id"]
        if item == self.fail_first_save_of:
            self.fail_first_save_of = None
            return 503, b"", []
        if item == self.conflict_first_save_of:
            self.conflict_first_save_of = None
            newest = {"check_id": "newer-check"}
            body = {"status": "CONFLICT", "code": "CHECK_CONFLICT", "newest_check": newest}
            return 409, json.dumps(body).encode(), []
        check_id = f"check-{len(self.saves)}"
        self.latest[item] = check_id
        # As the server does: the push goes out on commit, before the save's answer.
        for push in self.sockets:
            push(check_id, item)
        body = {"status": "COMMITTED", "result": {"check_id": check_id}}
        return 201, json.dumps(body).encode(), []

    async def websocket(self, data, reader, writer):
        if self.stall == SOCKET:
            await self.release.wait()
            writer.close()
            return
        ws = WSConnection(ConnectionType.SERVER)
        ws.receive_data(data)
        accepted = False
        for event in ws.events():
            # A session page's socket names its session (SPEC 8.3.0): ?session=<id>.
            if isinstance(event, Request) and event.target.split("?")[0] == SOCKET:
                writer.write(ws.send(AcceptConnection()))
                accepted = True
        if not accepted:
            writer.close()
            return

        writer.write(ws.send(TextMessage(data='{"type": "connection.ready"}')))

        def push(check_id, item_id):
            if self.push_rows:
                # SPEC 8.3.0: the item's rows for the session, before the hint.
                rows = {
                    "type": "checklist.rows",
                    "event_id": check_id,
                    "session_id": SESSION,
                    "item_id": item_id,
                    "as_of": "2026-06-01T00:00:00Z",
                    "items": [
                        {
                            "item_id": item_id,
                            "checklist_version_id": "v1",
                            "latest_check_id": check_id,
                        }
                    ],
                }
                writer.write(ws.send(TextMessage(data=json.dumps(rows))))
            hint = {
                "type": "vehicle.update",
                "event_id": check_id,
                "kind": "check.recorded",
                "item_id": item_id,
            }
            writer.write(ws.send(TextMessage(data=json.dumps(hint))))

        self.sockets.append(push)
        try:
            while data := await reader.read(65536):
                ws.receive_data(data)
                for event in ws.events():
                    if isinstance(event, CloseConnection):
                        return
        finally:
            self.sockets.remove(push)
            writer.close()


class LoadClientTests(unittest.IsolatedAsyncioTestCase):
    # Django's parallel runner pickles each test to send it to a worker. Since Python 3.11 an
    # IsolatedAsyncioTestCase holds a contextvars.Context, which cannot be pickled, so the whole
    # run aborted ("cannot pickle '_contextvars.Context' object"). The worker gets a fresh one.
    def __getstate__(self):
        state = self.__dict__.copy()
        state.pop("_asyncioTestContext", None)
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self._asyncioTestContext = contextvars.copy_context()

    async def asyncSetUp(self):
        self.fake = FakeSite()
        self.server = await asyncio.start_server(self.fake.handle, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.fake.origin = f"http://127.0.0.1:{port}"
        self.site = load_client.Site(self.fake.origin)

    async def asyncTearDown(self):
        self.fake.release.set()
        self.server.close()
        await self.server.wait_closed()

    def plan(self):
        crew = [
            {"role": "crew", "vehicle_id": VEHICLE, "user_id": f"u{n}", "cookie": f"synthetic-{n}"}
            for n in range(3)
        ]
        boss = {"role": "supervisor", "user_id": "s", "cookie": "synthetic-s"}
        return {"scale": 1, "template_id": "t1", "users": [*crew, boss]}

    async def test_a_shift_change_saves_every_item_and_reports_latencies(self):
        report = await load_client.run(self.plan(), self.site, (0.1, 0.15), 0.01, seed=3)
        self.assertEqual(self.fake.refused, [])
        committed = [s for s in self.fake.saves if s["item_id"] in ITEMS]
        self.assertEqual(report["events"]["saved"], 3)
        # The 503 is retried with the same operation ID; the conflict gets a new decision.
        first_item = [s for s in committed if s["item_id"] == ITEMS[0]]
        self.assertEqual(len(first_item), 2)
        self.assertEqual(first_item[0]["operation_id"], first_item[1]["operation_id"])
        second_item = [s for s in committed if s["item_id"] == ITEMS[1]]
        self.assertNotEqual(second_item[0]["operation_id"], second_item[1]["operation_id"])
        self.assertEqual(second_item[1]["acknowledges_check_id"], "newer-check")
        self.assertEqual(report["events"]["conflict"], 1)
        self.assertEqual(report["events"]["unconfirmed_retry"], 1)
        for save in committed:
            self.assertEqual(save["session_id"], SESSION)
            self.assertEqual(save["outcome"], "PASSED")
        # Pushes after each save made the pages re-read the saved items only (issue #278 B), and
        # the supervisor read the panel. Every crew member read the whole list at least once.
        self.assertGreater(report["events"]["ws_push"], 0)
        self.assertGreaterEqual(self.fake.checklist_reads, 3)
        self.assertTrue(self.fake.item_reads)
        self.assertTrue(all(named <= set(ITEMS) for named in self.fake.item_reads))
        self.assertGreater(self.fake.panel_reads, 0)
        saves = report["kinds"]["check_save"]
        self.assertEqual(saves["count"], 5)  # 3 commits, one 503, one 409
        self.assertEqual(saves["errors"], 2)
        self.assertEqual(report["kinds"]["ws_connect"]["errors"], 0)
        group = report["groups"]["pages_and_saves"]
        self.assertGreaterEqual(group["count"], 3 + 3 + 3 + 1 + 5)
        self.assertTrue(group["p50"] <= group["p95"] <= group["p99"] <= group["max"])
        self.assertEqual(report["shape"]["crew"], 3)
        self.assertEqual(report["shape"]["supervisors"], 1)

    async def test_hints_cost_one_read_per_own_save_and_keep_other_hints(self):
        # Issue #278 A, mirroring checklist.js: a read that starts after a hint covers it.
        reads = []

        class Reloader:
            def schedule(self, delay):
                reads.append(delay)

        def hint(event_id):
            return json.dumps({"type": "vehicle.update", "event_id": event_id, "kind": "x"})

        hints = load_client.Hints(Reloader(), wait=0.01)
        # Own push before the answer: it waits for the save, whose read covers it.
        hints.save_started()
        hints.on_text(hint("c1"))
        hints.save_finished(True, "c1")
        hints.read_started()
        await asyncio.sleep(0.03)
        self.assertEqual(reads, [0])
        # Own push after the answer is skipped; someone else's is read 250 ms later.
        hints.save_started()
        hints.save_finished(True, "c2")
        hints.read_started()
        hints.on_text(hint("c2"))
        self.assertEqual(reads, [0, 0])
        hints.on_text(hint("other"))
        self.assertEqual(reads, [0, 0, 0.25])
        hints.read_started()
        # A hint during a save that is refused gets its own read.
        hints.save_started()
        hints.on_text(hint("other-2"))
        hints.save_finished(False)
        self.assertEqual(reads, [0, 0, 0.25, 0.25])
        hints.read_started()
        # A hint during a save that never answers is read after `wait`.
        hints.save_started()
        hints.on_text(hint("other-3"))
        self.assertEqual(len(reads), 4)
        await asyncio.sleep(0.03)
        self.assertEqual(reads[-1], 0)
        hints.read_started()
        # connection.ready is no change; an unreadable frame is still a hint.
        hints.in_flight = 0
        hints.on_text('{"type": "connection.ready"}')
        self.assertEqual(len(reads), 5)
        hints.on_text("not json")
        self.assertEqual(reads[-1], 0.25)
        hints.stop()

    async def test_pushed_rows_replace_the_item_reads(self):
        # SPEC 8.3.0, mirroring checklist.js: a session's pushed rows are shown without a read,
        # the hint they cover needs none, and a save waits briefly for its own rows.
        reads, shown = [], []

        class Reloader:
            def schedule(self, delay):
                reads.append(delay)

        def rows(event_id, item_id):
            frame = {"type": "checklist.rows", "event_id": event_id, "item_id": item_id}
            frame["items"] = [{"item_id": item_id}]
            return json.dumps(frame)

        def hint(event_id, item_id):
            frame = {"type": "vehicle.update", "event_id": event_id, "kind": "check.recorded"}
            return json.dumps({**frame, "item_id": item_id})

        hints = load_client.Hints(Reloader(), apply_rows=shown.extend, own_rows_wait=0.02)
        hints.read_started()
        hints.on_text(rows("other", "a"))
        hints.on_text(hint("other", "a"))
        self.assertEqual((reads, shown), ([], [{"item_id": "a"}]), "another's save: no read")
        hints.save_started()
        hints.on_text(rows("mine", "b"))  # the rows come with the answer
        hints.save_finished(True, "mine", "b")
        hints.on_text(hint("mine", "b"))
        self.assertEqual(reads, [], "the saver shows its own rows and makes no read")
        hints.save_started()
        hints.save_finished(True, "mine-2", "c")
        self.assertEqual(reads, [], "the saver waits briefly for its rows")
        await asyncio.sleep(0.05)
        self.assertEqual(reads, [0], "without them it reads the item")
        self.assertEqual(hints.read_started(), ["c"])
        hints.stop()

    async def test_panel_pushes_are_gathered_into_one_read(self):
        # Issue #278 C, mirroring readiness.js: the first push opens a window; a read covers it.
        reads = []

        async def fetch():
            reads.append(1)

        reloader = load_client.Reloader(fetch)
        for _ in range(3):
            reloader.gather(0.02)
        await asyncio.sleep(0.06)
        self.assertEqual(len(reads), 1, "a burst costs one read")
        reloader.gather(0.02)
        reloader.schedule(0)  # the timer's read starts inside the window
        await asyncio.sleep(0.06)
        self.assertEqual(len(reads), 2, "that read covers the gathered push")
        await reloader.stop()

    async def test_panel_pushes_wait_for_the_floor(self):
        # SPEC 8.1.0 §17.3 (OQ-86), mirroring readiness.js: a push-driven read starts no sooner
        # than the floor after the previous read began, and one read covers the pushes meanwhile.
        loop = asyncio.get_running_loop()
        starts = []

        async def fetch():
            starts.append(loop.time())

        reloader = load_client.Reloader(fetch)
        reloader.gather(0.01, 0.2)
        await asyncio.sleep(0.05)
        self.assertEqual(len(starts), 1, "the first push opens the short window")
        for _ in range(3):
            reloader.gather(0.01, 0.2)
        await asyncio.sleep(0.05)
        self.assertEqual(len(starts), 1, "pushes inside the floor wait")
        await asyncio.sleep(0.25)
        self.assertEqual(len(starts), 2, "one read when the floor ends")
        self.assertGreaterEqual(starts[1] - starts[0], 0.19)  # the floor, less timer rounding
        await reloader.stop()

    async def test_hints_owe_item_reads_or_the_whole_list(self):
        # Issue #278 B, mirroring checklist.js.
        class Reloader:
            def schedule(self, delay):
                pass

        def hint(**fields):
            return json.dumps({"type": "vehicle.update", "event_id": "e", **fields})

        hints = load_client.Hints(Reloader())
        self.assertIsNone(hints.read_started(), "the first read is the whole list")
        hints.on_text(hint(kind="check.recorded", item_id="b"))
        hints.on_text(hint(kind="check.recorded", item_id="a"))
        self.assertEqual(hints.read_started(), ["a", "b"])
        hints.save_started()
        hints.save_finished(True, "c1", "c")
        self.assertEqual(hints.read_started(), ["c"], "an own item save reads only that item")
        hints.read_failed(["c"])
        self.assertEqual(hints.read_started(), ["c"], "a failed item read stays owed")
        for other in (
            hint(kind="check.recorded"),
            hint(kind="deficiency.changed", item_id="a"),
            "not json",
        ):
            hints.on_text(hint(kind="check.recorded", item_id="a"))
            hints.on_text(other)
            self.assertIsNone(hints.read_started(), other)
        hints.save_started()
        hints.save_finished(True, "c2", None)
        self.assertIsNone(hints.read_started(), "a kit presence save reads the whole list")
        for n in range(load_client.MAX_ITEM_READ + 1):
            hints.on_text(hint(kind="check.recorded", item_id=f"i{n}"))
        self.assertIsNone(hints.read_started(), "too many items for one read: the whole list")
        hints.full(0)
        self.assertIsNone(hints.read_started())
        hints.stop()

    async def test_a_signed_out_user_does_not_start_and_nothing_is_saved(self):
        plan = self.plan()
        for user in plan["users"]:
            user["cookie"] = "expired"
        report = await load_client.run(plan, self.site, (0, 0), 0, seed=1)
        self.assertEqual(self.fake.saves, [])
        self.assertEqual(report["events"]["crew_could_not_start"], 3)
        self.assertEqual(report["events"]["supervisor_could_not_start"], 1)
        self.assertEqual(report["kinds"]["page_vehicle"]["errors"], 3)

    async def test_an_unreachable_site_is_recorded_as_no_answer(self):
        self.server.close()
        await self.server.wait_closed()
        browser = load_client.Browser(self.site, "synthetic-x", load_client.Recorder())
        status, body = await browser.get("page_vehicle", "/")
        self.assertEqual((status, body), (0, b""))
        self.assertEqual(browser.recorder.samples[0][2], 0)

    async def test_a_stalled_request_times_out_as_no_answer(self):
        self.fake.stall = f"/inspection/vehicles/{VEHICLE}/"
        site = load_client.Site(self.fake.origin, timeout=0.2)
        browser = load_client.Browser(site, "synthetic-x", load_client.Recorder())
        started = asyncio.get_running_loop().time()
        status, body = await browser.get("page_vehicle", self.fake.stall)
        self.assertEqual((status, body), (0, b""))
        self.assertLess(asyncio.get_running_loop().time() - started, 2)
        self.assertEqual(browser.recorder.samples[0][2], 0)
        self.assertEqual(browser.recorder.events["request_timeout"], 1)

    async def test_an_unanswered_socket_upgrade_is_a_failed_connect(self):
        self.fake.stall = SOCKET
        site = load_client.Site(self.fake.origin, timeout=0.2)
        recorder = load_client.Recorder()
        browser = load_client.Browser(site, "synthetic-x", recorder)
        live = load_client.Live(browser, SOCKET, lambda delay: None, recorder)
        await asyncio.wait_for(live.run(), 5)
        self.assertEqual([s[0] for s in recorder.samples], ["ws_connect"])
        self.assertEqual(recorder.samples[0][2], 0)
        self.assertEqual(recorder.events["request_timeout"], 1)

    async def test_a_stalled_readiness_page_does_not_hang_the_run(self):
        # A supervisor waiting on an unanswered readiness request once kept run() from reporting.
        self.fake.stall = "/fleet/readiness/"
        site = load_client.Site(self.fake.origin, timeout=0.3)
        report = await asyncio.wait_for(
            load_client.run(self.plan(), site, (0, 0.01), 0.01, seed=3), 10
        )
        self.assertEqual(report["events"]["saved"], 3)
        self.assertEqual(report["events"]["supervisor_could_not_start"], 1)
        self.assertGreaterEqual(report["events"]["request_timeout"], 1)
        self.assertEqual(report["shape"]["request_timeout_seconds"], 0.3)


class ReportTests(unittest.TestCase):
    def test_the_async_tests_survive_the_parallel_runner(self):
        # The parallel runner pickles every test it sends to a worker.
        test = pickle.loads(
            pickle.dumps(LoadClientTests("test_an_unreachable_site_is_recorded_as_no_answer"))
        )
        self.assertIsInstance(test._asyncioTestContext, contextvars.Context)

    def test_nearest_rank_percentiles(self):
        values = [float(n) for n in range(1, 101)]
        self.assertEqual(load_client.percentile(values, 50), 50.0)
        self.assertEqual(load_client.percentile(values, 95), 95.0)
        self.assertEqual(load_client.percentile(values, 100), 100.0)
        self.assertEqual(load_client.percentile([7.0], 99), 7.0)
        self.assertEqual(load_client.percentile([1.0, 2.0], 0), 1.0)
        with self.assertRaises(ValueError):
            load_client.percentile([], 50)

    def test_share_under_the_targets_and_errors(self):
        recorder = load_client.Recorder()
        for seconds in (0.1, 0.5, 1.5, 2.5):
            recorder.add("check_save", seconds, 201)
        recorder.add("check_save", 0.2, 503)
        summary = recorder.report()["groups"]["pages_and_saves"]
        self.assertEqual(summary["count"], 5)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["under_1s"], 0.6)
        self.assertEqual(summary["under_2s"], 0.8)
        self.assertEqual(recorder.report()["groups"]["live_reloads"], {"count": 0, "errors": 0})

    def test_bad_arguments_are_refused(self):
        with self.assertRaises(ValueError):
            load_client.Site("http://127.0.0.1:1", timeout=0)
        with self.assertRaises(ValueError):
            load_client.Site("ftp://example.test")
        with self.assertRaises(ValueError):
            load_client.Site("https://example.test/path")
        with self.assertRaises(SystemExit):
            load_client.main(
                ["--base-url", "https://example.test", "--plan", "-", "--think", "5", "1"]
            )
