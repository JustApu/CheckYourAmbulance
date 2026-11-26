"""Shift-change load client (issue #10): browser-like crews and supervisors against one site.

Reads a plan from `tools/load_prepare.py` (users with session cookies) and drives, at the same
time, what the pages do (`checklist.js`, `vehicle.js`, `readiness.js`):

* **Crew:**
  - open the vehicle page and start or join its inspection session, then open the session page;
  - keep the vehicle's live-update WebSocket open, and reload the checklist 250 ms after each push, one reload at a time. As the page does since issue #278, a push for one's own committed save, or one that arrives while one's save is unanswered, is covered by the reload after that save instead (`Hints`);
  - each of a vehicle's three crew saves a third of its items at a human pace. A save that gets no definite answer is retried with the same operation ID (REC-4).
* **Supervisors:** open the fleet readiness page and its live-update sockets, then reload the panel every refresh interval, and once 2 s after the first of a burst of pushes (issue #278 C).

Every request's latency is recorded, and the report gives percentiles per kind and per group.
Opening a connection and each request-response exchange are bounded by `--request-timeout`; a
request that times out counts as no answer, so a stalled site cannot hang the run.
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
RELOADS = ("checklist", "checklist_items", "panel")
MAX_ITEM_READ = 20  # items one partial checklist read may name (issue #278 B)
PANEL_PUSH_WINDOW = 2.0  # readiness.js gathers pushes this long, then reads once (#278 C)
PANEL_PUSH_FLOOR = 10.0  # and reads on a push no sooner after its last read began (SPEC 8.1.0)
GROUPS = {"pages_and_saves": PAGES + SAVES, "live_reloads": RELOADS}
PERCENTILES = (50, 90, 95, 98, 99)
MAX_CONNECTIONS = 6  # per user, as browsers do per host
RETRIES = 3
READ_SIZE = 65536
# Seconds per connection open or request-response exchange. Above a reverse proxy's usual 60 s
# read timeout, so the proxy's answer is what gets recorded when it gives one.
DEFAULT_TIMEOUT = 90.0


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
    def __init__(self, base_url, ca_file=None, timeout=DEFAULT_TIMEOUT):
        if not timeout > 0:
            raise ValueError("timeout must be positive")
        self.timeout = timeout
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
        return await asyncio.wait_for(
            asyncio.open_connection(
                self.host, self.port, ssl=self.ssl, server_hostname=self.host if self.tls else None
            ),
            self.timeout,
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

            async def exchange():
                nonlocal connection
                if connection is None:
                    connection = HttpConnection(*await self.site.open())
                return await connection.request(method, target, self._headers(extra), body)

            try:
                response, data = await asyncio.wait_for(exchange(), self.site.timeout)
            except (
                TimeoutError,
                OSError,
                ConnectionError,
                h11.ProtocolError,
                asyncio.IncompleteReadError,
            ) as error:
                if isinstance(error, asyncio.TimeoutError):
                    self.recorder.count("request_timeout")
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

    def __init__(self, browser, path, on_push, recorder, on_text=None):
        self.browser, self.path, self.on_push, self.recorder = browser, path, on_push, recorder
        self.on_text = on_text  # given: a push's text goes there instead of on_push(0.25)
        self.task = None

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def run(self):
        site = self.browser.site
        started = time.monotonic()
        try:
            reader, writer = await site.open()
        except (TimeoutError, OSError):
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
        # The upgrade handshake shares the request deadline; an open socket then waits for
        # pushes until it is stopped.
        deadline = started + site.timeout
        accepted = False
        try:
            while True:
                if accepted:
                    data = await reader.read(READ_SIZE)
                else:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise TimeoutError
                    data = await asyncio.wait_for(reader.read(READ_SIZE), left)
                ws.receive_data(data or None)
                for event in ws.events():
                    if isinstance(event, AcceptConnection):
                        accepted = True
                        self.recorder.add("ws_connect", time.monotonic() - started, 101)
                        self.on_push(0)
                    elif isinstance(event, RejectConnection):
                        self.recorder.add(
                            "ws_connect", time.monotonic() - started, event.status_code
                        )
                        return
                    elif isinstance(event, TextMessage):
                        self.recorder.count("ws_push")
                        if self.on_text:
                            self.on_text(event.data)
                        else:
                            self.on_push(0.25)
                    elif isinstance(event, Ping):
                        writer.write(ws.send(event.response()))
                    elif isinstance(event, CloseConnection):
                        self.recorder.count(f"ws_closed_{event.code}")
                        return
                if not data:
                    self.recorder.count("ws_dropped")
                    return
        except TimeoutError:
            self.recorder.count("request_timeout")
            self.recorder.add("ws_connect", time.monotonic() - started, 0)
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
        self.gathering = None  # a push window (gather); a read that starts covers it
        self.last_start = None  # loop time the last read began (the push floor)
        self.tasks = set()

    def schedule(self, delay):
        if self.timer:
            self.timer.cancel()
        self.timer = asyncio.get_running_loop().call_later(delay, self._fire)

    def gather(self, window, floor=0.0):
        """readiness.js (issue #278 C): the first push starts a window; later ones join it.

        With a floor (SPEC 8.1.0 §17.3, OQ-86), a push-driven read starts no sooner than `floor`
        after the previous read began: a push inside the floor waits for its end instead of
        opening a window. A push during a read that outlasts the floor runs once more afterwards.
        """
        if self.gathering is not None:
            return
        loop = asyncio.get_running_loop()
        delay = window
        if floor and self.last_start is not None:
            left = self.last_start + floor - loop.time()
            if left > 0:
                delay = left
        self.gathering = loop.call_later(delay, self._gathered)

    def _gathered(self):
        self.gathering = None
        self._fire()

    def _fire(self):
        task = asyncio.create_task(self._run())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _run(self):
        if self.running:
            self.again = True
            return
        self.running = True
        self.last_start = asyncio.get_running_loop().time()
        if self.gathering is not None:
            self.gathering.cancel()
            self.gathering = None
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
        if self.gathering is not None:
            self.gathering.cancel()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


class Hints:
    """What `checklist.js` does with a push (issue #278 A and B); a push is only a hint.

    Any checklist read that starts after a hint covers it. So a hint is skipped when it is the
    page's own committed save (whose reload is already scheduled), and waits while a save is
    unanswered, because that save's reload starts later. A save that is not committed, or an
    answer slower than `wait` seconds, leaves the hint its own read.

    A check hint that names its item, and one's own item save, owe only that item's rows
    (`item_ids`); anything else, and the first read, owes the whole list.
    """

    def __init__(self, reloader, wait=2.0):
        self.reloader, self.wait = reloader, wait
        self.own, self.in_flight, self.waiting, self.timer = set(), 0, False, None
        self.need_full, self.items = True, set()

    def read_started(self):
        """The items to read, or None for the whole list; the read covers what was owed."""
        self.waiting = False
        partial = not self.need_full and 0 < len(self.items) <= MAX_ITEM_READ
        items = sorted(self.items) if partial else None
        self.need_full, self.items = False, set()
        return items

    def read_failed(self, items):
        if items:
            self.items.update(items)
        else:
            self.need_full = True

    def full(self, delay):
        self.need_full = True
        self.reloader.schedule(delay)

    def save_started(self):
        self.in_flight += 1

    def save_finished(self, committed, check_id=None, item_id=None):
        self.in_flight -= 1
        if committed:
            if check_id:
                self.own.add(check_id)
            if item_id:
                self.items.add(item_id)
            else:
                self.need_full = True
            self.reloader.schedule(0)  # checklist.js: a read after a committed save
        elif self.waiting and not self.in_flight:
            self.reloader.schedule(0.25)

    def on_text(self, text):
        hint = json_body(text)
        if not isinstance(hint, dict):
            hint = {}
        if hint.get("type") == "connection.ready":
            return  # the socket's accept already scheduled a read
        if hint.get("type") == "vehicle.update" and hint.get("event_id") in self.own:
            self.own.discard(hint["event_id"])
            return
        if (
            hint.get("type") == "vehicle.update"
            and hint.get("kind") == "check.recorded"
            and isinstance(hint.get("item_id"), str)
            and hint["item_id"]
        ):
            self.items.add(hint["item_id"])
        else:
            self.need_full = True
        self.waiting = True
        if not self.in_flight:
            self.reloader.schedule(0.25)
        elif self.timer is None:
            self.timer = asyncio.get_running_loop().call_later(self.wait, self._late)

    def _late(self):
        self.timer = None
        if self.waiting:
            self.reloader.schedule(0)

    def stop(self):
        if self.timer:
            self.timer.cancel()


async def crew_member(site, user, member_index, plan, recorder, think, rng, ack_reload=True):
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
        # After the SPEC 5.0.0 cutover each vehicle has its own checklist (issue #278).
        "template_id": user.get("template_id") or plan["template_id"],
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
        items = hints.read_started() if hints else None
        url = f"{config['urls']['checklist']}?view={config['view']}&needs_attention_only=false"
        if items:
            url += "&item_ids=" + ",".join(items)
        status, data = await browser.get("checklist_items" if items else "checklist", url)
        body = json_body(data) or {}
        if status == 200 and body.get("status") == "OK":
            fresh = {(r["item_id"], r.get("kit_id")): r for r in body["items"]}
            if not items:
                rows.clear()
            rows.update(fresh)
        elif hints:
            hints.read_failed(items)

    reloader = Reloader(reload)
    # Without the post-save reload (--no-ack-reload) nothing covers a skipped hint, so every
    # push is read, as before issue #278.
    hints = Hints(reloader) if ack_reload else None
    live = Live(
        browser,
        config["urls"]["socket"],
        hints.full if hints else reloader.schedule,  # a (re)connect reconciles the whole list
        recorder,
        on_text=hints.on_text if hints else None,
    )
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
            if hints:
                hints.save_started()
            committed, check_id = await save(browser, config, session_page, row, recorder)
            if hints:
                # As the page: a kit presence save can change its kit's other rows.
                presence = row.get("kit_presence") or row.get("kit_requirement_id")
                hints.save_finished(committed, check_id, None if presence else row["item_id"])
    finally:
        if hints:
            hints.stop()
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
            return True, (body.get("result") or {}).get("check_id")
        if body.get("status") == "CONFLICT" and status == 409:
            # Proceed after the warning: a new decision acknowledging the newer check (CON-3).
            newer = (body.get("newest_check") or {}).get("check_id")
            recorder.count("conflict")
            payload = {**payload, "operation_id": str(uuid4()), "basis_check_id": newer}
            payload["acknowledges_check_id"] = newer
            continue
        if body.get("status") == "REJECTED" and 0 < status < 500:
            recorder.count(f"rejected_{body.get('code')}")
            return False, None
        recorder.count("unconfirmed_retry")
        await asyncio.sleep(1 + attempt)
    recorder.count("gave_up")
    return False, None


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
    sockets = [
        Live(
            browser,
            path,
            reloader.schedule,
            recorder,
            on_text=lambda _text: reloader.gather(PANEL_PUSH_WINDOW, PANEL_PUSH_FLOOR),
        )
        for path in config["sockets"]
    ]
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


async def run(plan, site, think, arrival, seed, limit=None, ack_reload=True):
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
            work = crew_member(site, user, index, plan, recorder, think, own, ack_reload)
            crews.append(asyncio.create_task(arrive(rng.uniform(0, arrival), work)))
    finished, pending = await asyncio.wait(crews, timeout=limit)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    if pending:
        recorder.events["crew_stopped_at_limit"] = len(pending)
    done.set()
    # Each supervisor request is bounded by the site timeout; this deadline is a backstop so a
    # supervisor can never keep the run from reporting.
    stuck = set()
    if supervisors:
        _, stuck = await asyncio.wait(supervisors, timeout=2 * site.timeout)
    for task in stuck:
        task.cancel()
    await asyncio.gather(*supervisors, return_exceptions=True)
    if stuck:
        recorder.events["supervisor_stopped_at_limit"] = len(stuck)
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
        "limit_seconds": limit,
        "request_timeout_seconds": site.timeout,
        "ack_reload": ack_reload,
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
    parser.add_argument(
        "--limit", type=float, help="stop crews still working after this many seconds"
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="seconds allowed to open a connection or complete one request (no answer after)",
    )
    parser.add_argument(
        "--no-ack-reload",
        dest="ack_reload",
        action="store_false",
        help="do not reload the checklist after a committed save (the shipped page does)",
    )
    args = parser.parse_args(argv)
    if not 0 <= args.think[0] <= args.think[1] or args.arrival < 0:
        parser.error("think times must satisfy 0 <= MIN <= MAX and arrival must be >= 0")
    if not args.request_timeout > 0:
        parser.error("--request-timeout must be positive")
    source = sys.stdin if args.plan == "-" else open(args.plan, encoding="utf-8")  # noqa: SIM115
    with source:
        plan = json.load(source)
    site = Site(args.base_url, args.ca_file, args.request_timeout)
    report = asyncio.run(
        run(plan, site, tuple(args.think), args.arrival, args.seed, args.limit, args.ack_reload)
    )
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
