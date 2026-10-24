"""Real-browser Control Panel test (config-repair Phase 7).

Drives the actual served panel (control_panel_server handler + real files)
in headless Google Chrome over the DevTools Protocol using a dependency-free
stdlib WebSocket client (suite convention: no external test packages). This
exercises the real app.js in a real DOM: server-driven profile views,
per-module reasons, the Configured/Effective/Running/Restart states strip,
and advanced full-registry access. Skips only if Chrome is unavailable.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from hermes_nerve import control_panel
from hermes_nerve.control_panel import ControlPanel
from hermes_nerve.control_panel_server import make_handler

CHROME = shutil.which("google-chrome") or shutil.which("chromium")
ARTIFACTS = Path(__file__).resolve().parent.parent / "browser-evidence"


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _urlopen_no_proxy(url, timeout=3):
    """Loopback only: never route through any configured HTTP proxy."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as resp:
        return resp.read().decode()


class _WebSocket:
    """Minimal RFC6455 text-frame client for the DevTools loopback socket."""

    def __init__(self, url):
        parsed = urllib.parse.urlsplit(url)
        self.sock = socket.create_connection((parsed.hostname, parsed.port), timeout=15)
        key = base64.b64encode(os.urandom(16)).decode()
        request = ("GET " + parsed.path + ("?" + parsed.query if parsed.query else "") +
                   " HTTP/1.1\r\nHost: " + parsed.hostname + ":" + str(parsed.port) +
                   "\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n" +
                   "Sec-WebSocket-Key: " + key + "\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket handshake failed: connection closed")
            response += chunk
        head, _, rest = response.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("WebSocket handshake rejected: " +
                               head.split(b"\r\n")[0].decode(errors="replace"))
        self.buffer = rest

    def _recv_exact(self, count):
        while len(self.buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("WebSocket closed while reading")
            self.buffer += chunk
        payload, self.buffer = self.buffer[:count], self.buffer[count:]
        return payload

    def recv_text(self):
        while True:
            header = self._recv_exact(2)
            opcode = header[0] & 0x0F
            length = header[1] & 0x7F
            if length == 126:
                length = int.from_bytes(self._recv_exact(2), "big")
            elif length == 127:
                length = int.from_bytes(self._recv_exact(8), "big")
            payload = self._recv_exact(length)
            if opcode == 8:
                raise RuntimeError("WebSocket closed by peer")
            if opcode == 9:  # ping -> pong
                self._send_frame(0x0A, payload)
                continue
            if opcode == 1:
                return payload.decode()
            # binary/pong/other control frames: ignore

    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        header = bytes([0x80 | opcode])
        count = len(payload)
        if count < 126:
            header += bytes([0x80 | count])
        elif count < 65536:
            header += bytes([0x80 | 126]) + count.to_bytes(2, "big")
        else:
            header += bytes([0x80 | 127]) + count.to_bytes(8, "big")
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send_text(self, text):
        self._send_frame(1, text.encode())

    def close(self):
        try:
            self._send_frame(8, b"")
            self.sock.close()
        except OSError:
            pass


class DevTools:
    """Tiny synchronous CDP driver over the loopback WebSocket."""

    def __init__(self, ws_url):
        self.ws = _WebSocket(ws_url)
        self.mid = 0

    def call(self, method, params=None, timeout=30):
        self.mid += 1
        message = json.dumps({"id": self.mid, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        self.ws.send_text(message)
        while time.time() < deadline:
            data = json.loads(self.ws.recv_text())
            if data.get("id") == self.mid:
                if "error" in data:
                    raise RuntimeError(f"{method}: {data['error']}")
                return data.get("result", {})
        raise RuntimeError(method + " timed out")

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True})
        return result.get("result", {}).get("value")

    def wait_for(self, expression, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.evaluate(expression):
                    return True
            except RuntimeError:
                pass
            time.sleep(0.25)
        return False

    def close(self):
        self.ws.close()


class BrowserHarness:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.panel = ControlPanel(home=Path(self.tmp.name),
                                  source=Path(control_panel.__file__).resolve().parents[1])
        self.token = secrets.token_urlsafe(16)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.panel, self.token))
        self.url = f"http://127.0.0.1:{self.server.server_port}/#token={self.token}"
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.debug_port = _free_port()
        self.profile_dir = Path(self.tmp.name) / "chrome-profile"
        self.chrome = None

    def start_chrome(self):
        self.chrome = subprocess.Popen(
            [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
             "--disable-dev-shm-usage", f"--remote-debugging-port={self.debug_port}",
             f"--user-data-dir={self.profile_dir}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                targets = json.loads(_urlopen_no_proxy(
                    f"http://127.0.0.1:{self.debug_port}/json/list", timeout=2))
                for target in targets:
                    if target.get("type") == "page":
                        return target["webSocketDebuggerUrl"]
            except Exception:
                time.sleep(0.3)
        raise RuntimeError("Chrome DevTools endpoint did not come up")

    def stop(self):
        if self.chrome:
            self.chrome.terminate()
            try:
                self.chrome.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.chrome.kill()
        self.server.shutdown()
        self.tmp.cleanup()


@unittest.skipUnless(CHROME, "headless Chrome not available")
class ControlPanelBrowserTests(unittest.TestCase):
    """Real-browser run of the served panel; plain TestCase + synchronous CDP."""

    @classmethod
    def setUpClass(cls):
        cls.harness = BrowserHarness()
        cls.devtools_ws = cls.harness.start_chrome()
        ARTIFACTS.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls.harness.stop()

    def test_profile_views_states_and_reasons_in_real_browser(self):
        cdp = DevTools(self.devtools_ws)
        try:
            cdp.call("Page.enable")
            cdp.call("Page.navigate", {"url": self.harness.url})
            if not cdp.wait_for(
                    "document.readyState === 'complete' && !!document.querySelector('.profile-card')",
                    timeout=30):
                body = cdp.evaluate(
                    "document.body ? document.body.innerText.slice(0,400) : 'NO BODY'")
                self.fail("panel did not render; body: " + str(body))

            # Honest states strip, rendered in the real DOM.
            strip = cdp.evaluate(
                "(document.querySelector('.states-strip')||{innerText:''}).innerText")
            for marker in ("CONFIGURED", "EFFECTIVE", "RUNNING", "RESTART"):
                self.assertIn(marker, strip)
            # Standalone panel server: running-state must be declared
            # unavailable, never faked.
            self.assertIn("UNAVAILABLE", strip)

            # Fat Cat: server-driven simple view (primary + setup), reasons visible.
            cdp.evaluate(
                "document.querySelector('.profile-card[data-profile=\"fat_cat\"]').click()")
            cdp.evaluate("document.getElementById('next').click()")
            # Wait for the fat_cat preview to land: its signature reason is the
            # remote_workers pruning for an unset remote_hosts.
            self.assertTrue(cdp.wait_for(
                "document.body.innerText.includes('Features for Fat Cat') && "
                "document.body.innerText.includes('Why: no remote_hosts configured')",
                timeout=20))
            fat_rows = cdp.evaluate("document.querySelectorAll('.feature-row').length")
            self.assertIsNotNone(fat_rows)
            self.assertLess(fat_rows, 18, "simple view must not show the full registry")
            self.assertGreaterEqual(fat_rows, 15)
            body = cdp.evaluate("document.body.innerText")
            self.assertNotIn("Shadow testing", body,
                             "dev-calibration module stays out of the simple view")
            self.assertIn("Available after extra setup: shared_context", body)
            self.assertIn("Every supervisory subsystem available", body,
                          "server emphasis text must render")
            # Remote workers must show OFF with its resolver reason.
            remote_checked = cdp.evaluate(
                "document.querySelector('input[data-module=\"remote_workers\"]')"
                " ? document.querySelector('input[data-module=\"remote_workers\"]').checked"
                " : null")
            self.assertIs(remote_checked, False,
                          "fat_cat remote_workers resolves OFF without remote_hosts")

            # Toggling a feature surfaces the explicit-override reason.
            cdp.evaluate("document.querySelector('input[data-module=\"context_governor\"]').click()")
            self.assertTrue(cdp.wait_for(
                "document.body.innerText.includes('Why: explicit override')", timeout=15))

            # Advanced view: the complete registry.
            cdp.evaluate("document.getElementById('advanced-shortcut').click()")
            self.assertTrue(cdp.wait_for(
                "document.querySelectorAll('.feature-row').length === 18", timeout=10))
            self.assertEqual(
                cdp.evaluate("document.querySelectorAll('.feature-row').length"), 18)

            # Marie Kondo: distinct minimal view; interactive gating absent.
            cdp.evaluate("document.getElementById('advanced-shortcut').click()")
            cdp.evaluate("document.getElementById('back').click()")
            self.assertTrue(cdp.wait_for(
                "!!document.querySelector('.profile-card[data-profile=\"marie_kondo\"]')",
                timeout=10))
            cdp.evaluate(
                "document.querySelector('.profile-card[data-profile=\"marie_kondo\"]').click()")
            cdp.evaluate("document.getElementById('next').click()")
            self.assertTrue(cdp.wait_for(
                "document.body.innerText.includes('Features for Marie Kondo') && "
                "document.body.innerText.includes('Minimum Nerve')", timeout=20))
            marie_rows = cdp.evaluate("document.querySelectorAll('.feature-row').length")
            marie_body = cdp.evaluate("document.body.innerText")
            self.assertNotIn("Action gate", marie_body,
                             "minimal profile keeps interactive gating out of view")
            self.assertIn("Minimum Nerve", marie_body,
                          "server emphasis text must render")
            self.assertLess(marie_rows, fat_rows,
                            "Marie Kondo must show a strictly smaller feature set than Fat Cat")

            shot = cdp.call("Page.captureScreenshot", {"format": "png"})
            out = ARTIFACTS / "browser-panel-marie-kondo.png"
            out.write_bytes(base64.b64decode(shot["data"]))
            self.assertTrue(out.is_file())
        finally:
            cdp.close()


if __name__ == "__main__":
    unittest.main()
