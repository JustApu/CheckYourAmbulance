"""The pinned ASGI server runs the runbook's launch command in one process (issue 24).

The command is read from docs/deployment-runbook.md and parsed by uvicorn's own CLI, so the
documented flags are the ones tested. The server then runs in this process on a Unix socket in a
private temporary directory (no network): plain HTTP is served, and a status request posted over
HTTP reaches a WebSocket on the live-update route through the in-memory channel layer (DEP-2).
"""

import importlib
import json
import re
import shlex
import socket
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock
from uuid import uuid4

import uvicorn
import wsproto
from django.conf import settings
from wsproto.events import AcceptConnection, CloseConnection, Request, TextMessage

from cya.operations.tests.test_api import StatusHttpCase

RUNBOOK = Path(settings.BASE_DIR) / "docs" / "deployment-runbook.md"
TIMEOUT = 10
# The module, not the "main" command that the uvicorn package re-exports under that name.
CLI = importlib.import_module("uvicorn.main")


def documented_launch_args():
    """The uvicorn arguments of the runbook's launch command (from the application argument on)."""
    blocks = re.findall(r"```sh\n(.*?)```", RUNBOOK.read_text(), flags=re.S)
    commands = [b for b in blocks if "cya.asgi:application" in b and "--uds" in b]
    if len(commands) != 1:
        raise AssertionError(f"expected one launch command in the runbook, found {len(commands)}")
    words = shlex.split(commands[0].replace("\\\n", " "))
    if not words[0].endswith("/uvicorn"):
        raise AssertionError(f"the launch command must run uvicorn, not {words[0]}")
    return words[1:]


def parsed_launch(args):
    """Parse args with uvicorn's own CLI and return the keyword arguments it would run with."""
    with mock.patch.object(CLI, "run") as run:
        CLI.main.main(args=args, standalone_mode=False)
    run.assert_called_once()
    return run.call_args


def unix_connection(path):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(TIMEOUT)
    connection.connect(path)
    return connection


def http(path, request):
    """Send one raw HTTP/1.1 request and return (status, headers, body)."""
    with unix_connection(path) as connection:
        connection.sendall(request)
        response = b""
        while chunk := connection.recv(65536):
            response += chunk
    head, _, body = response.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers.setdefault(name.strip().lower(), value.strip())
    return int(lines[0].split()[1]), headers, body


class WebSocketClient:
    """A minimal wsproto client over a Unix socket."""

    def __init__(self, path, target, headers):
        self.connection = unix_connection(path)
        self.ws = wsproto.WSConnection(wsproto.ConnectionType.CLIENT)
        self.connection.sendall(
            self.ws.send(Request(host="testserver", target=target, extra_headers=headers))
        )
        self.pending = []

    def next_event(self):
        while not self.pending:
            data = self.connection.recv(65536)
            self.ws.receive_data(data or None)
            self.pending.extend(self.ws.events())
        return self.pending.pop(0)

    def close(self):
        try:
            self.connection.sendall(self.ws.send(CloseConnection(code=1000)))
        except Exception:  # The server may already have gone; the socket closes either way.
            pass
        self.connection.close()


def unit_launch_args():
    """The uvicorn arguments in deploy/rocky9/cya.service's ExecStart (issue #6 host profile)."""
    unit = (Path(settings.BASE_DIR) / "deploy" / "rocky9" / "cya.service").read_text()
    unit = unit.replace("\\\n", " ")
    lines = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
    if len(lines) != 1:
        raise AssertionError(f"expected one ExecStart in cya.service, found {len(lines)}")
    words = shlex.split(lines[0].removeprefix("ExecStart="))
    if not words[0].endswith("/local/venv/bin/uvicorn"):
        raise AssertionError(f"cya.service must run the release's uvicorn, not {words[0]}")
    return words[1:]


class LaunchCommandTests(StatusHttpCase):
    def test_the_host_profile_service_runs_the_documented_command(self):
        self.assertEqual(unit_launch_args(), documented_launch_args())

    def test_runbook_command_is_one_process_without_reload_or_proxy_headers(self):
        call = parsed_launch(documented_launch_args())
        self.assertEqual(call.args, ("cya.asgi:application",))
        options = call.kwargs
        self.assertEqual(options["workers"], 1)  # explicit, so WEB_CONCURRENCY cannot raise it
        self.assertFalse(options["reload"])
        self.assertIsNotNone(options["uds"])  # DEP-10: a permission-restricted Unix socket
        self.assertIsNone(options["fd"])
        self.assertEqual((options["loop"], options["http"]), ("asyncio", "h11"))
        self.assertEqual((options["ws"], options["lifespan"]), ("wsproto", "off"))
        # Proxy trust stays with CYA_TRUST_PROXY_SSL_HEADER and X-Real-IP (issues 25 and 26).
        self.assertFalse(options["proxy_headers"])
        self.assertFalse(options["access_log"])  # Its lines would carry OAuth query strings.
        self.assertFalse(options["server_header"])
        self.assertEqual(options["timeout_graceful_shutdown"], 10)

    def test_pinned_server_serves_http_and_live_updates_in_one_process(self):
        args = documented_launch_args()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "app.sock")
            args[args.index("--uds") + 1] = path
            options = parsed_launch(args).kwargs
            options.pop("app_dir")
            options["log_config"] = None  # leave the test process's logging alone
            server = uvicorn.Server(uvicorn.Config("cya.asgi:application", **options))
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + TIMEOUT
                while not server.started:
                    self.assertTrue(thread.is_alive(), "the server stopped while starting")
                    self.assertLess(time.monotonic(), deadline, "the server did not start")
                    time.sleep(0.05)
                self.exercise(path)
            finally:
                server.should_exit = True
                thread.join(TIMEOUT)
            self.assertFalse(thread.is_alive(), "the server did not stop")

    def exercise(self, path):
        # Plain HTTP: a signed-out page navigation still redirects to sign-in.
        status, headers, _ = http(
            path, b"GET /fleet/readiness/ HTTP/1.1\r\nHost: testserver\r\nConnection: close\r\n\r\n"
        )
        self.assertEqual((status, headers["location"]), (302, "/auth/login/"))

        cookies = "; ".join(
            f"{name}={morsel.value}" for name, morsel in self.client.cookies.items()
        )
        socket_client = WebSocketClient(
            path,
            f"/ws/v1/vehicles/{self.vehicle.pk}/live/",
            [(b"origin", b"http://testserver"), (b"cookie", cookies.encode())],
        )
        try:
            self.assertIsInstance(socket_client.next_event(), AcceptConnection)
            ready = socket_client.next_event()
            self.assertIn('"type":"connection.ready"', ready.data)

            body = json.dumps(
                {
                    "operation_id": str(uuid4()),
                    "requested_status": "OUT_OF_SERVICE",
                    "reason": "Synthetic request.",
                }
            ).encode()
            status, _, response = http(
                path,
                b"POST /api/v1/vehicles/%s/status-requests HTTP/1.1\r\n"
                b"Host: testserver\r\nConnection: close\r\n"
                b"Content-Type: application/json\r\nContent-Length: %d\r\n"
                b"X-CSRFToken: %s\r\nCookie: %s\r\n\r\n%s"
                % (
                    str(self.vehicle.pk).encode(),
                    len(body),
                    self.token.encode(),
                    cookies.encode(),
                    body,
                ),
            )
            self.assertEqual(status, 200, response)
            request_id = json.loads(response)["result"]["request_id"]

            hint = socket_client.next_event()
            self.assertIsInstance(hint, TextMessage)
            self.assertEqual(
                json.loads(hint.data),
                {"type": "vehicle.update", "event_id": request_id, "kind": "status.request"},
            )
        finally:
            socket_client.close()
