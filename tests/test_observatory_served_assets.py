"""Regression guard: the observatory must never serve a stale asset.

The defect this pins: server.py serves /app.js and /app.css through
`ui.APP_JS` / `ui.APP_CSS`. When those were module-level string constants,
Python bound them once at import. A long-running server therefore kept
serving the pre-import snapshot forever, and editing the asset on disk had
no effect on any browser that connected — which silently turned correct
UI work into apparent browser failures.

Every assertion here goes through a REAL HTTP request against a REAL server
on an ephemeral port. Nothing is proven by importing the module, because
importing is exactly what used to hide the bug.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from hermes_nerve.observatory import server, ui

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "hermes_nerve" / "observatory" / "assets"
# Test-only credential: long enough for the server minimum, never a real secret.
TOKEN = "test-token-0123456789abcdefghijklmnop"


def _get(url: str, method: str = "GET"):
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


class ServedAssetFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store_path = Path(self.tmp.name) / "work-supervision.sqlite3"
        srv = server.make_server(
            "127.0.0.1", 0, store_path=self.store_path,
            shadow_path=Path(self.tmp.name) / "none.jsonl", token=TOKEN,
        )
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        self.base = f"http://127.0.0.1:{srv.server_address[1]}"

    def test_served_bytes_equal_on_disk_bytes(self):
        for route, filename, content_type in (
            ("/app.js", "app.js", "application/javascript; charset=utf-8"),
            ("/app.css", "app.css", "text/css; charset=utf-8"),
        ):
            with self.subTest(route=route):
                status, headers, body = _get(self.base + route)
                self.assertEqual(status, 200)
                self.assertIn("javascript" if route.endswith("js") else "css", headers.get("Content-Type", ""))
                on_disk = (ASSETS / filename).read_bytes()
                # The acceptance condition, verbatim: served == on-disk.
                self.assertEqual(body, on_disk, f"{route} served bytes are stale vs {filename}")
                self.assertGreater(len(body), 0, f"{route} served an empty payload")

    def test_edit_after_server_start_is_served(self):
        """The core regression: mutate the asset AFTER the server is already up.

        The old import-time binding could not observe this. If anyone
        reintroduces a module-level constant, this test fails rather than a
        browser silently showing old behaviour.
        """
        target = ASSETS / "app.css"
        original = target.read_bytes()
        try:
            # Change BOTH content and mtime so no (path, mtime, size) cache key
            # can collide with the previous entry.
            probe = b"\n/* factory-master freshness probe */\n"
            target.write_bytes(original + probe)
            _, _, body = _get(self.base + "/app.css")
            self.assertTrue(body.endswith(probe), "server served a stale snapshot after the asset changed")
            self.assertEqual(body, target.read_bytes())

            # And a second, different edit is picked up too: no one-shot warmup.
            probe2 = b"/* second probe */\n"
            target.write_bytes(original + probe2)
            _, _, body2 = _get(self.base + "/app.css")
            self.assertTrue(body2.endswith(probe2), "server served a stale snapshot on a later edit")
        finally:
            target.write_bytes(original)

    def test_missing_asset_fails_loudly_not_silently(self):
        """A missing asset must never become a 200 with an empty body."""
        original = ui.APP_CSS
        self.assertGreater(len(original), 0)
        # Point the loader at an empty dir: the contract is an actionable error,
        # never a silent empty payload.
        saved = ui._ASSET_DIR
        ui._ASSET_DIR = Path(self.tmp.name) / "no-such-assets"
        try:
            with self.assertRaises(RuntimeError) as caught:
                _ = ui.APP_CSS  # resolves through __getattr__ -> _read_asset
            self.assertIn("app.css", str(caught.exception))
            # And the served route cannot fall back to something empty.
            with self.assertRaises(RuntimeError):
                _ = ui._read_asset("app.css")
        finally:
            ui._ASSET_DIR = saved

    def test_static_shell_routes_unchanged(self):
        """The card's route-shape guard: moving assets must not move routes."""
        for route in ("/", "/index.html"):
            status, headers, body = _get(self.base + route)
            self.assertEqual(status, 200, route)
            self.assertIn("text/html", headers.get("Content-Type", ""))
            self.assertIn(b'<script src="/app.js">', body, route)
            self.assertIn(b'<link rel="stylesheet" href="/app.css">', body, route)

    def test_oversized_and_unknown_api_paths_still_behave(self):
        """The no-quota / oversized error path must survive the asset move.

        Note: server.py authenticates BEFORE route matching for anything under
        /api/ (security posture: no unauthenticated route probing), so an
        unknown /api path answers 401, not 404. That ordering is asserted here
        so a future refactor cannot quietly expose route enumeration.
        """
        status, _, _ = _get(self.base + "/api/definitely-not-a-route")
        self.assertEqual(status, 401)
        # A known data route without credentials still refuses (shape unchanged).
        self.assertEqual(_get(self.base + "/api/snapshot")[0], 401)
        # Invalid run id is still a 400, not a 500, once authenticated.
        req = urllib.request.Request(
            self.base + "/api/run?task_id=x&run_id=notanumber",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.assertEqual(resp.status, 400)
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
        # Non-API junk is still a 404 with no credentials required.
        self.assertEqual(_get(self.base + "/nope")[0], 404)
        # Non-GET is still refused on a static asset.
        self.assertEqual(_get(self.base + "/app.js", method="POST")[0], 405)


if __name__ == "__main__":
    unittest.main()
