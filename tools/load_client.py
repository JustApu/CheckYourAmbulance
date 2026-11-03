"""Shift-change load client (issue #10): browser-like crews and supervisors against one site.

Reads a plan from `tools/load_prepare.py` (users with session cookies) and drives, at the same
time, what the pages do (`checklist.js`, `vehicle.js`, `readiness.js`):

* **Crew:**
  - open the vehicle page and start or join its inspection session, then open the session page;
  - keep the vehicle's live-update WebSocket open, and reload the checklist 250 ms after each push, one reload at a time;
  - each of a vehicle's three crew saves a third of its items at a human pace. A save that gets no definite answer is retried with the same operation ID (REC-4).
* **Supervisors:** open the fleet readiness page and its live-update sockets, then reload the panel every refresh interval and 250 ms after each push.

Every request's latency is recorded, and the report gives percentiles per kind and per group.
It uses only the standard library and the pinned `h11` and `wsproto`. It sends nothing outside
the given site, and needs the site's real (or a given) TLS trust.

    python tools/load_client.py --base-url https://<site> --plan <plan.json> --report <out.json>
"""

import argparse
import asyncio
import contextlib
import html
import json
import random
import re
import ssl
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

import h11
from wsproto import ConnectionType, WSConnection
from wsproto.events import (
    AcceptConnection,
    CloseConnection,
    Ping,
    RejectConnection,
    Request,
    TextMessage,
)

PAGES = ("page_vehicle", "page_session", "page_readiness")
SAVES = ("session_start", "check_save")
RELOADS = ("checklist", "panel")
GROUPS = {"pages_and_saves": PAGES + SAVES, "live_reloads": RELOADS}
PERCENTILES = (50, 90, 95, 98, 99)
MAX_CONNECTIONS = 6  # per user, as browsers do per host
RETRIES = 3
READ_SIZE = 65536


def percentile(values, p):
    """Nearest-rank percentile of a non-empty list (p in 0–100)."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    rank = max(1, -(-p * len(ordered) // 100))  # ceil(p/100 × n), at least 1
    return ordered[min(rank, len(ordered)) - 1]


def ok(status):
    """A 2xx answer, or 101 for an accepted WebSocket upgrade."""
    return 200 <= status < 300 or status == 101


class Recorder:
    def __init__(self):
        self.samples = []  # (kind, seconds, status) — status 0 means no answer
        self.events = {}

    def add(self, kind, seconds, status):
        self.samples.append((kind, seconds, status))

    def count(self, name):
        self.events[name] = self.events.get(name, 0) + 1

    def summary(self, kinds):
        times = [s for k, s, status in self.samples if k in kinds]
        errors = sum(1 for k, _s, status in self.samples if k in kinds and not ok(status))
        if not times:
            return {"count": 0, "errors": 0}
        out = {"count": len(times), "errors": errors, "max": round(max(times), 4)}
        for p in PERCENTILES:
            out[f"p{p}"] = round(percentile(times, p), 4)
        out["under_1s"] = round(sum(t < 1 for t in times) / len(times), 4)
        out["under_2s"] = round(sum(t < 2 for t in times) / len(times), 4)
        return out

    def report(self):
        kinds = sorted({k for k, _s, _st in self.samples})
        return {
            "kinds": {k: self.summary((k,)) for k in kinds},
            "groups": {name: self.summary(members) for name, members in GROUPS.items()},
            "events": dict(sorted(self.events.items())),
        }


class Site:
    def __init__(self, base_url, ca_file=None):
        parts = urlsplit(base_url)
        if (
            parts.scheme not in ("https", "http")
            or not parts.hostname
            or parts.path not in ("", "/")
        ):
            raise ValueError("base URL must be https://host[:port] (http only for tests)")
        self.host = parts.hostname
        self.tls = parts.scheme == "https"
        self.port = parts.port or (443 if self.tls else 80)
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.ssl = ssl.create_default_context(cafile=ca_file) if self.tls else None

    async def open(self):
        return await asyncio.open_connection(
            self.host, self.port, ssl=self.ssl, server_hostname=self.host if self.tls else None
        )


class HttpConnection:
    """One HTTP/1.1 keep-alive connection, one request at a time."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.h11 = h11.Connection(h11.CLIENT)
        self.reusable = True

    async def request(self, method, target, headers, body):
        if body:
            headers = [*headers, ("Content-Length", str(len(body)))]
        out = self.h11.send(h11.Request(method=method, target=target, headers=headers))
        if body:
            out += self.h11.send(h11.Data(data=body))
        out += self.h11.send(h11.EndOfMessage())
        self.writer.write(out)
        await self.writer.drain()
        response, chunks = None, []
        while True:
            event = self.h11.next_event()
            if event is h11.NEED_DATA:
                self.h11.receive_data(await self.reader.read(READ_SIZE))
            elif isinstance(event, h11.Response):
                response = event
            elif isinstance(event, h11.Data):
                chunks.append(event.data)
            elif isinstance(event, h11.EndOfMessage):
                break
            elif isinstance(event, h11.ConnectionClosed):
                raise ConnectionError("closed before the response ended")
        if self.h11.our_state is h11.DONE and self.h11.their_state is h11.DONE:
            self.h11.start_next_cycle()
        else:
            self.reusable = False
        return response, b"".join(chunks)

    def close(self):
        self.reusable = False
        with contextlib.suppress(Exception):
            self.writer.close()


class Browser:
    """One signed-in user: a cookie jar and a small connection pool to the site."""

    def __init__(self, site, cookie, recorder):
        self.site, self.recorder = site, recorder
        self.cookies = {"cya_session": cookie}
        self.idle, self.open_count = [], 0
        self.slots = asyncio.Semaphore(MAX_CONNECTIONS)
        self.csrf = None

    def _headers(self, extra=()):
        jar = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return [
            ("Host", self.site.host),
            ("User-Agent", "cya-load-test/1"),
            ("Accept", "*/*"),
            ("Cookie", jar),
            *extra,
        ]

    def _remember(self, response):
        for name, value in response.headers:
            if name.lower() == b"set-cookie":
                pair = value.decode("latin-1").split(";", 1)[0]
                key, _, val = pair.partition("=")
                if key.strip():
                    self.cookies[key.strip()] = val.strip()

    async def send(self, kind, method, target, body=b"", extra=()):
        """Return (status, body bytes); status 0 when no answer arrived. Records the latency."""
        async with self.slots:
            connection = self.idle.pop() if self.idle else None
            started = time.monotonic()
            try:
                if connection is None:
                    connection = HttpConnection(*await self.site.open())
                response, data = await connection.request(
                    method, target, self._headers(extra), body
                )
            except (OSError, ConnectionError, h11.ProtocolError, asyncio.IncompleteReadError):
                if connection is not None:
                    connection.close()
                self.recorder.add(kind, time.monotonic() - started, 0)
                return 0, b""
            self.recorder.add(kind, time.monotonic() - started, response.status_code)
            self._remember(response)
            if connection.reusable:
                self.idle.append(connection)
            else:
                connection.close()
            return response.status_code, data

    async def get(self, kind, path):
        return await self.send(kind, "GET", path)

    async def post_json(self, kind, path, payload, referer):
        extra = (
            ("Content-Type", "application/json"),
            ("X-CSRFToken", self.csrf or ""),
            ("Origin", self.site.origin),
            ("Referer", self.site.origin + referer),
        )
        return await self.send(kind, "POST", path, json.dumps(payload).encode(), extra)

    def close(self):
        for connection in self.idle:
            connection.close()
        self.idle = []


def page_csrf(page):
    found = re.search(rb'name="csrfmiddlewaretoken" value="([^"]+)"', page)
    return found.group(1).decode() if found else None


def page_config(page, element_id):
    found = re.search(
        rb'<script id="' + element_id.encode() + rb'" type="application/json">(.*?)</script>',
        page,
        re.S,
    )
    if not found:
        raise ValueError(f"page has no {element_id} configuration")
    return json.loads(html.unescape(found.group(1).decode()))


def json_body(data):
    try:
        return json.loads(data)
    except ValueError:
        return None


class Live:
    """A page's live-update socket: a push schedules a reload, as the pages do."""

    def __init__(self, browser, path, on_push, recorder):
        self.browser, self.path, self.on_push, self.recorder = browser, path, on_push, recorder
        self.task = None

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def run(self):
        site = self.browser.site
        started = time.monotonic()
        try:
            reader, writer = await site.open()
        except OSError:
            self.recorder.add("ws_connect", time.monotonic() - started, 0)
            return
        ws = WSConnection(ConnectionType.CLIENT)
        jar = "; ".join(f"{k}={v}" for k, v in self.browser.cookies.items())
        writer.write(
            ws.send(
                Request(
                    host=site.host,
                    target=self.path,
                    extra_headers=[(b"cookie", jar.encode()), (b"origin", site.origin.encode())],
                )
            )
        )
        try:
            while True:
                data = await reader.read(READ_SIZE)
                ws.receive_data(data or None)
                for event in ws.events():
                    if isinstance(event, AcceptConnection):
                        self.recorder.add("ws_connect", time.monotonic() - started, 101)
                        self.on_push(0)
                    elif isinstance(event, RejectConnection):
                        self.recorder.add(
                            "ws_connect", time.monotonic() - started, event.status_code
                        )
                        return
                    elif isinstance(event, TextMessage):
                        self.recorder.count("ws_push")
                        self.on_push(0.25)
                    elif isinstance(event, Ping):
                        writer.write(ws.send(event.response()))
                    elif isinstance(event, CloseConnection):
                        self.recorder.count(f"ws_closed_{event.code}")
                        return
                if not data:
                    self.recorder.count("ws_dropped")
                    return
        except (OSError, ConnectionError):
            self.recorder.count("ws_dropped")
        finally:
            writer.close()

    async def stop(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task


class Reloader:
    """At most one reload in flight; a request during one runs once more afterwards."""

    def __init__(self, fetch):
        self.fetch, self.timer, self.running, self.again = fetch, None, False, False
        self.tasks = set()

    def schedule(self, delay):
        if self.timer:
            self.timer.cancel()
        self.timer = asyncio.get_running_loop().call_later(delay, self._fire)

    def _fire(self):
        task = asyncio.create_task(self._run())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _run(self):
        if self.running:
            self.again = True
            return
        self.running = True
        try:
            await self.fetch()
        finally:
            self.running = False
            if self.again:
                self.again = False
                self.schedule(0)

    async def stop(self):
        if self.timer:
            self.timer.cancel()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


async def crew_member(site, user, member_index, plan, recorder, think, rng):
    browser = Browser(site, user["cookie"], recorder)
    vehicle_page = f"/inspection/vehicles/{user['vehicle_id']}/"
    status, page = await browser.get("page_vehicle", vehicle_page)
    browser.csrf = page_csrf(page)
    if status != 200 or not browser.csrf:
        recorder.count("crew_could_not_start")
        return
    start = {
        "operation_id": str(uuid4()),
        "vehicle_id": user["vehicle_id"],
        "template_id": plan["template_id"],
        "separate": False,
    }
    session_id = None
    for _attempt in range(RETRIES):
        status, data = await browser.post_json(
            "session_start", "/api/v1/inspection-sessions", start, vehicle_page
        )
        body = json_body(data) or {}
        if body.get("status") == "COMMITTED":
            session_id = body["result"]["session_id"]
            break
        if body.get("status") == "REJECTED" and 0 < status < 500:
            break
        await asyncio.sleep(1)
    if session_id is None:
        recorder.count("crew_could_not_start")
        return
    session_page = f"/inspection/sessions/{session_id}/"
    status, page = await browser.get("page_session", session_page)
    if status != 200:
        recorder.count("crew_could_not_start")
        return
    browser.csrf = page_csrf(page) or browser.csrf
    config = page_config(page, "cya-config")
    rows = {}

    async def reload():
        status, data = await browser.get(
            "checklist",
            f"{config['urls']['checklist']}?view={config['view']}&needs_attention_only=false",
        )
        body = json_body(data) or {}
        if status == 200 and body.get("status") == "OK":
            rows.clear()
            rows.update({(r["item_id"], r.get("kit_id")): r for r in body["items"]})

    reloader = Reloader(reload)
    live = Live(browser, config["urls"]["socket"], reloader.schedule, recorder)
    live.start()
    await reload()
    mine = sorted(rows)[member_index::3]
    try:
        for key in mine:
            await asyncio.sleep(rng.uniform(*think))
            row = rows.get(key)
            if row is None:
                recorder.count("item_vanished")
                continue
            await save(browser, config, session_page, row, recorder)
    finally:
        await live.stop()
        await reloader.stop()
        browser.close()


async def save(browser, config, referer, row, recorder):
    payload = {
        "operation_id": str(uuid4()),
        "vehicle_id": config["vehicle_id"],
        "item_id": row["item_id"],
        "checklist_version_id": row["checklist_version_id"],
        "purpose": "ROUTINE",
        "basis_check_id": row.get("latest_check_id"),
        "session_id": config["session_id"],
        "outcome": "PASSED",
    }
    if row.get("kit_id"):
        payload["kit_id"] = row["kit_id"]
    for attempt in range(RETRIES):
        status, data = await browser.post_json(
            "check_save", config["urls"]["checks"], payload, referer
        )
        body = json_body(data) or {}
        if body.get("status") == "COMMITTED":
            recorder.count("saved")
            return
        if body.get("status") == "CONFLICT" and status == 409:
            # Proceed after the warning: a new decision acknowledging the newer check (CON-3).
            newer = (body.get("newest_check") or {}).get("check_id")
            recorder.count("conflict")
            payload = {**payload, "operation_id": str(uuid4()), "basis_check_id": newer}
            payload["acknowledges_check_id"] = newer
            continue
        if body.get("status") == "REJECTED" and 0 < status < 500:
            recorder.count(f"rejected_{body.get('code')}")
            return
        recorder.count("unconfirmed_retry")
        await asyncio.sleep(1 + attempt)
    recorder.count("gave_up")


async def supervisor(site, user, recorder, done):
    browser = Browser(site, user["cookie"], recorder)
    status, page = await browser.get("page_readiness", "/fleet/readiness/")
    if status != 200:
        recorder.count("supervisor_could_not_start")
        return
    config = page_config(page, "readiness-config")

    async def refresh():
        await browser.get("panel", config["panel_url"])

    reloader = Reloader(refresh)
    sockets = [Live(browser, path, reloader.schedule, recorder) for path in config["sockets"]]
    for socket in sockets:
        socket.start()
    reloader.schedule(0)
    try:
        while not done.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(done.wait(), config["refresh_seconds"])
            if not done.is_set():
                reloader.schedule(0)
    finally:
        for socket in sockets:
            await socket.stop()
        await reloader.stop()
        browser.close()


async def run(plan, site, think, arrival, seed):
    recorder = Recorder()
    rng = random.Random(seed)
    done = asyncio.Event()
    by_vehicle = {}
    for user in plan["users"]:
        if user["role"] == "crew":
            by_vehicle.setdefault(user["vehicle_id"], []).append(user)

    async def arrive(delay, coroutine):
        await asyncio.sleep(delay)
        await coroutine

    started = time.monotonic()
    supervisors = []
    for user in plan["users"]:
        if user["role"] == "supervisor":
            work = supervisor(site, user, recorder, done)
            supervisors.append(asyncio.create_task(arrive(rng.uniform(0, arrival), work)))
    crews = []
    for members in by_vehicle.values():
        for index, user in enumerate(members):
            own = random.Random(rng.random())
            work = crew_member(site, user, index, plan, recorder, think, own)
            crews.append(asyncio.create_task(arrive(rng.uniform(0, arrival), work)))
    await asyncio.gather(*crews)
    done.set()
    await asyncio.gather(*supervisors)
    report = recorder.report()
    report["seconds"] = round(time.monotonic() - started, 1)
    report["shape"] = {
        "scale": plan.get("scale"),
        "vehicles": len(by_vehicle),
        "crew": sum(len(m) for m in by_vehicle.values()),
        "supervisors": len(supervisors),
        "think_seconds": list(think),
        "arrival_seconds": arrival,
        "seed": seed,
    }
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--plan", required=True, help="plan JSON from load_prepare ('-' for stdin)")
    parser.add_argument("--report", help="write the JSON report here (default: stdout)")
    parser.add_argument("--think", type=float, nargs=2, default=(3.0, 8.0), metavar=("MIN", "MAX"))
    parser.add_argument("--arrival", type=float, default=30.0, help="arrivals spread over seconds")
    parser.add_argument("--seed", type=int, default=10)
    parser.add_argument("--ca-file")
    args = parser.parse_args(argv)
    if not 0 <= args.think[0] <= args.think[1] or args.arrival < 0:
        parser.error("think times must satisfy 0 <= MIN <= MAX and arrival must be >= 0")
    source = sys.stdin if args.plan == "-" else open(args.plan, encoding="utf-8")  # noqa: SIM115
    with source:
        plan = json.load(source)
    site = Site(args.base_url, args.ca_file)
    report = asyncio.run(run(plan, site, tuple(args.think), args.arrival, args.seed))
    text = json.dumps(report, indent=2)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as out:
            out.write(text + "\n")
    else:
        print(text)
    groups = report["groups"]
    print(
        "pages_and_saves p95={p95}s p98={p98}s p90={p90}s; live_reloads p95={r}s".format(
            p95=groups["pages_and_saves"].get("p95"),
            p98=groups["pages_and_saves"].get("p98"),
            p90=groups["pages_and_saves"].get("p90"),
            r=groups["live_reloads"].get("p95"),
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
