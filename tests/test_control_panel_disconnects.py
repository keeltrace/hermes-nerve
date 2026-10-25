"""Closed-browser regressions over real sockets and the actual HTTP handler."""
import io
import json
from pathlib import Path
import socket
import tempfile
from socketserver import BaseServer
import unittest

from hermes_nerve.control_panel import ControlPanel
from hermes_nerve.control_panel_server import make_handler


class SocketPairServer(BaseServer):
    server_port = 4321


class BrowserDisconnectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.panel = ControlPanel(home=Path(self.temp.name))
        responses = self.responses = []
        class RecordingHandler(make_handler(self.panel, "fixture-panel-token")):
            def send_response(self, code, message=None):
                responses.append(code)
                return super().send_response(code, message)
        self.handler = RecordingHandler
        # Socketpair transports real HTTP bytes without binding a guessed port.
        self.server = SocketPairServer(("127.0.0.1", 4321), self.handler)

    def exchange(self, path, payload=None, *, token="fixture-panel-token", close_peer=True):
        method = "POST" if payload is not None else "GET"
        body = json.dumps(payload).encode() if payload is not None else b""
        request = (f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:4321\r\n"
                   f"Origin: http://127.0.0.1:4321\r\nX-Nerve-Token: {token}\r\n"
                   "Connection: close\r\nContent-Type: application/json\r\n"
                   f"Content-Length: {len(body)}\r\n\r\n").encode() + body
        server_side, browser_side = socket.socketpair()
        try:
            browser_side.sendall(request)
            if close_peer:
                browser_side.close()
            try:
                handler = self.handler(server_side, ("127.0.0.1", 4321), self.server)
            except (BrokenPipeError, ConnectionResetError) as exc:
                self.fail("closed browser escaped HTTP response handling: " + type(exc).__name__)
            return handler, b"" if close_peer else browser_side.recv(65536)
        finally:
            server_side.close()
            browser_side.close()

    def test_asset_disconnect_does_not_attempt_second_error_response(self):
        self.exchange("/assets/app.js")
        self.assertEqual(self.responses, [200])

    def test_denied_request_still_denied_when_browser_closes(self):
        self.exchange("/api/status", token="invalid")
        self.assertEqual(self.responses, [403])
        self.assertFalse((self.panel.home / "nerve/profile.json").exists())

    def test_save_finishes_once_when_browser_disconnects(self):
        request = {"profile": "lean", "overrides": {}, "advanced": {}}
        preview = self.panel.preview(request)
        self.exchange("/api/apply", {**request, "preview_id": preview["preview_id"]})
        self.assertEqual(self.responses, [200])
        self.assertEqual(json.loads((self.panel.home / "nerve/profile.json").read_text())["nerve_profile"], "lean")
        self.assertEqual(len(self.panel.snapshots()), 1)

    def test_application_error_still_returns_sanitized_500(self):
        from unittest.mock import patch
        with patch.object(self.panel, "status", side_effect=ValueError("private-fixture-detail")):
            _, response = self.exchange("/api/status", close_peer=False)
        self.assertEqual(self.responses, [500])
        self.assertIn(b"Internal control panel error.", response)
        self.assertNotIn(b"private-fixture-detail", response)

    def response_handler(self, writer):
        handler = object.__new__(self.handler)
        handler.request_version = "HTTP/1.1"
        handler.requestline = "GET /api/status HTTP/1.1"
        handler.wfile = writer
        return handler

    def test_header_and_body_peer_errors_close_without_second_response(self):
        for error in (BrokenPipeError, ConnectionResetError):
            for fail_on in (1, 2):
                with self.subTest(error=error.__name__, write=fail_on):
                    class PeerWriter(io.BytesIO):
                        calls = 0
                        def write(self, data):
                            self.calls += 1
                            if self.calls == fail_on:
                                raise error("fixture peer closed")
                            return super().write(data)
                    self.responses.clear()
                    with PeerWriter() as writer:
                        handler = self.response_handler(writer)
                        handler._reply(200, {"ok": True})
                        self.assertTrue(handler.close_connection)
                        self.assertEqual(writer.calls, fail_on)
                        self.assertEqual(self.responses, [200])

    def test_unrelated_io_failure_is_not_swallowed(self):
        class FaultWriter(io.BytesIO):
            def write(self, data):
                raise OSError("fixture unexpected I/O failure")
        with FaultWriter() as writer:
            with self.assertRaisesRegex(OSError, "unexpected I/O"):
                self.response_handler(writer)._reply(200, {"ok": True})

    def test_status_disconnect_is_not_an_internal_server_error(self):
        handler, _ = self.exchange("/api/status")
        self.assertTrue(handler.close_connection)
        self.assertFalse((self.panel.home / "nerve/profile.json").exists())


if __name__ == "__main__":
    unittest.main()