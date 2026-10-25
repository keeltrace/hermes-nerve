"""Release-scope contract: deferred extras absent, core Nerve surfaces retained."""
import contextlib
import io
import json
from pathlib import Path
import secrets
import tempfile
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener
from http.server import ThreadingHTTPServer

from hermes_nerve import cli
from hermes_nerve.control_panel import ControlPanel
from hermes_nerve.control_panel_server import ASSETS, make_handler
from hermes_nerve.runtime_manifest import collect_runtime_files, RUNTIME_ASSET_PATTERNS

ROOT = Path(__file__).resolve().parents[1]
RETIRED = ("observatory/", "university/", "web/learn.")


class ReleaseScopeTests(unittest.TestCase):
    def test_payload_keeps_core_and_excludes_deferred_features(self):
        package = ROOT / "hermes_nerve"
        payload = {p.relative_to(package).as_posix() for p in collect_runtime_files(package)}
        self.assertTrue(payload)
        self.assertFalse([p for p in payload if p.startswith(RETIRED)])
        for required in ("integrations/shared_context.py", "selective_context.py",
                         "experience.py", "gate.py", "provenance.py", "policy_tables.py",
                         "web/index.html", "web/app.js", "web/styles.css"):
            self.assertIn(required, payload)
        self.assertFalse(any(p.startswith(RETIRED) for p in RUNTIME_ASSET_PATTERNS))
        self.assertFalse((ROOT / "extras/hermes-nerve-live-observer").exists())
        self.assertFalse((ROOT / "scripts/add_uni_rubrics.py").exists())

    def test_cli_keeps_core_without_deferred_commands(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as result:
            cli.main(["--help"])
        self.assertEqual(result.exception.code, 0)
        help_text = out.getvalue()
        for command in ("ui", "setup", "experience"):
            self.assertIn(command, help_text)
        for command in ("university", "observatory", "live-observer"):
            self.assertNotIn(command, help_text)

    def test_removed_web_routes_are_404_and_diagnostics_still_work(self):
        with tempfile.TemporaryDirectory() as home:
            panel = ControlPanel(home=Path(home), source=ROOT)
            token = secrets.token_urlsafe(16)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(panel, token))
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = "http://127.0.0.1:" + str(server.server_port)
            opener = build_opener(ProxyHandler({}))
            def request(path, *, post=False, authorized=True):
                headers = {"Origin": base, "Content-Type": "application/json"}
                if authorized:
                    headers["X-Nerve-Token"] = token
                req = Request(base + path, data=b"{}" if post else None, headers=headers)
                return opener.open(req, timeout=5)
            try:
                for path in ("/learn", "/assets/learn.js", "/assets/learn.css",
                             "/api/university/manifest", "/api/university/module",
                             "/api/university/status", "/api/university/export"):
                    with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                        request(path)
                    self.assertEqual(error.exception.code, 404)
                    error.exception.close()
                for endpoint in ("lesson", "check", "lab", "capstone/fixture", "capstone/verify", "reset"):
                    with self.subTest(endpoint=endpoint), self.assertRaises(HTTPError) as error:
                        request("/api/university/" + endpoint, post=True)
                    self.assertEqual(error.exception.code, 404)
                    error.exception.close()
                for path, (filename, _) in ASSETS.items():
                    self.assertTrue((ROOT / "hermes_nerve/web" / filename).is_file())
                    with request(path) as response:
                        self.assertEqual(response.status, 200)
                with request("/api/diagnostics") as response:
                    self.assertIn("version", json.load(response))
                with self.assertRaises(HTTPError) as error:
                    request("/api/diagnostics", authorized=False)
                self.assertEqual(error.exception.code, 403)
                error.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
