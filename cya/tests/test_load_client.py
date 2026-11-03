"""The shift-change load client (issue #10) against a fake loopback site; no network."""

import asyncio
import json
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
        self.sockets = []
        self.fail_first_save_of = ITEMS[0]  # answer 503 once: the client must retry the same op
        self.conflict_first_save_of = ITEMS[1]  # answer 409 once: the client acknowledges
        self.refused = []

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
            self.checklist_reads += 1
            items = [
                {"item_id": i, "checklist_version_id": "v1", "latest_check_id": c}
                for i, c in self.latest.items()
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
        self.latest[item] = f"check-{len(self.saves)}"
        for push in self.sockets:
            push()
        return 201, json.dumps({"status": "COMMITTED"}).encode(), []

    async def websocket(self, data, reader, writer):
        ws = WSConnection(ConnectionType.SERVER)
        ws.receive_data(data)
        accepted = False
        for event in ws.events():
            if isinstance(event, Request) and event.target == SOCKET:
                writer.write(ws.send(AcceptConnection()))
                accepted = True
        if not accepted:
            writer.close()
            return

        def push():
            writer.write(ws.send(TextMessage(data='{"type": "vehicle.changed"}')))

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
    async def asyncSetUp(self):
        self.fake = FakeSite()
        self.server = await asyncio.start_server(self.fake.handle, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.fake.origin = f"http://127.0.0.1:{port}"
        self.site = load_client.Site(self.fake.origin)

    async def asyncTearDown(self):
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
        report = await load_client.run(self.plan(), self.site, (0, 0.01), 0.01, seed=3)
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
        # Pushes after each save made the pages reload, and the supervisor read the panel.
        self.assertGreater(report["events"]["ws_push"], 0)
        self.assertGreater(self.fake.checklist_reads, 3)
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


class ReportTests(unittest.TestCase):
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
            load_client.Site("ftp://example.test")
        with self.assertRaises(ValueError):
            load_client.Site("https://example.test/path")
        with self.assertRaises(SystemExit):
            load_client.main(
                ["--base-url", "https://example.test", "--plan", "-", "--think", "5", "1"]
            )
