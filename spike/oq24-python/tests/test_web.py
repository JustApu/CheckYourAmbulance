"""THROWAWAY spike (Python candidate) criterion 6b: CSRF, session-cookie flags, WebSocket push (Django Channels),
and SSE framing over loopback with a real client."""
import asyncio
import os
import subprocess
import sys
import time
import unittest

from spikepy import dj


class Web(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dj.configure("unused")      # no database access in these tests (signed-cookie sessions)

    def test_cookie_flags_and_csrf(self):
        from django.test import Client
        c = Client(enforce_csrf_checks=True)
        r = c.get("/login", secure=True)
        cookie = r.cookies["sessionid"]
        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Strict")
        # state-changing POST: refused without or with a wrong CSRF token; accepted with the issued token.
        self.assertEqual(c.post("/checks", secure=True).status_code, 403)
        self.assertEqual(c.post("/checks", {"csrfmiddlewaretoken": "wrong"}, secure=True).status_code, 403)
        token = c.get("/csrf", secure=True).content.decode()
        # Django also checks Origin/Referer on HTTPS POSTs (observed: the valid token alone is refused with 403).
        self.assertEqual(c.post("/checks", {"csrfmiddlewaretoken": token}, secure=True).status_code, 403)
        o = {"HTTP_ORIGIN": "https://testserver"}
        self.assertEqual(c.post("/checks", {"csrfmiddlewaretoken": token}, secure=True, **o).status_code, 200)
        self.assertEqual(c.post("/checks", {"csrfmiddlewaretoken": token}, secure=True, HTTP_ORIGIN="https://evil.invalid").status_code, 403)
        self.assertEqual(c.post("/checks", secure=True, HTTP_X_CSRFTOKEN=token, **o).status_code, 200)   # header form
        # unauthenticated request is refused even with a valid token
        anon = Client(enforce_csrf_checks=True)
        atoken = anon.get("/csrf", secure=True).content.decode()
        self.assertEqual(anon.post("/checks", {"csrfmiddlewaretoken": atoken}, secure=True, **o).status_code, 401)

    def test_websocket_push_to_group(self):
        from channels.layers import get_channel_layer
        from channels.testing import WebsocketCommunicator
        from spikepy.asgi import application

        async def run():
            a = WebsocketCommunicator(application, "/vehicles/7/live")
            b = WebsocketCommunicator(application, "/vehicles/7/live")
            other = WebsocketCommunicator(application, "/vehicles/8/live")
            for comm in (a, b, other):
                ok, _ = await comm.connect()
                self.assertTrue(ok)
            await get_channel_layer().group_send("vehicle-7", {"type": "check.recorded", "text": "check:1"})
            self.assertEqual(await a.receive_from(), "check:1")
            self.assertEqual(await b.receive_from(), "check:1")
            self.assertTrue(await other.receive_nothing())
            for comm in (a, b, other):
                await comm.disconnect()
        asyncio.run(run())

    def test_sse_framing_over_loopback(self):
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        env = dict(os.environ, PYTHONPATH=os.getcwd())
        server = subprocess.Popen([sys.executable, "-m", "spikepy.serve", str(port)], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(2.0)
            out = subprocess.run(["curl", "-sS", "-N", "-m", "5", f"http://127.0.0.1:{port}/sse-once"],
                                 capture_output=True, text=True).stdout
        finally:
            server.terminate()
            server.wait(timeout=10)
        self.assertRegex(out, r"(?m)^: connected\r?$")
        self.assertRegex(out, r"(?m)^event: check\r?$")
        self.assertRegex(out, r"(?m)^data: check:42\r?$")


if __name__ == "__main__":
    unittest.main()
