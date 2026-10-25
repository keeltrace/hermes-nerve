"""Harness failure regressions; synthetic processes are NOT browser acceptance."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import tests.test_control_panel_browser as browser


@unittest.skipUnless(os.name == "posix", "executable fixture uses a POSIX shebang")
class BrowserHarnessLifecycleTests(unittest.TestCase):
    def fake_executable(self, harness, source):
        executable = Path(harness.tmp.name) / "synthetic-browser"
        executable.write_text("#!" + sys.executable + "\n" + source)
        executable.chmod(0o700)
        return str(executable)

    def test_noisy_child_cannot_deadlock_before_devtools_ready(self):
        harness = browser.BrowserHarness()
        self.addCleanup(harness.stop)
        executable = self.fake_executable(harness, r'''import sys,json
from pathlib import Path
from http.server import HTTPServer,BaseHTTPRequestHandler
args=dict(arg[2:].split("=",1) for arg in sys.argv[1:] if arg.startswith("--") and "=" in arg)
sys.stderr.write("synthetic-noise-" * 20000); sys.stderr.flush()
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body=json.dumps([{"type":"page","webSocketDebuggerUrl":f"ws://127.0.0.1:{self.server.server_port}/devtools/page/fixture"}]).encode()
        self.send_response(200); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self,*args): pass
server=HTTPServer(("127.0.0.1",int(args["remote-debugging-port"])),Handler)
profile=Path(args["user-data-dir"]); profile.mkdir(parents=True,exist_ok=True)
(profile/"DevToolsActivePort").write_text(str(server.server_port)+"\n/devtools/browser/fixture\n")
server.serve_forever()
''')
        with patch.object(browser, "CHROME", executable):
            endpoint = harness.start_chrome(timeout=2)
        self.assertEqual(endpoint, f"ws://127.0.0.1:{harness.debug_port}/devtools/page/fixture")

    def test_invalid_desktop_bus_does_not_reach_browser(self):
        harness = browser.BrowserHarness()
        self.addCleanup(harness.stop)
        executable = self.fake_executable(harness, """import os,sys
from pathlib import Path
profile = Path(next(arg.split('=',1)[1] for arg in sys.argv if arg.startswith('--user-data-dir=')))
expected = 'unix:path=' + str(profile.parent / 'unavailable-dbus')
isolated = all(os.environ.get(key) == expected for key in ('DBUS_SESSION_BUS_ADDRESS','DBUS_SYSTEM_BUS_ADDRESS'))
print('fixture bus isolated' if isolated else 'desktop bus leaked', file=sys.stderr, flush=True)
sys.exit(7)
""")
        inherited = {"DBUS_SESSION_BUS_ADDRESS": "invalid-fixture:",
                     "DBUS_SYSTEM_BUS_ADDRESS": "invalid-fixture:"}
        with patch.dict(os.environ, inherited), patch.object(browser, "CHROME", executable):
            with self.assertRaisesRegex(RuntimeError, "fixture bus isolated"):
                harness.start_chrome(timeout=1)

    def test_crashed_child_never_becomes_skip_or_success(self):
        harness = browser.BrowserHarness()
        self.addCleanup(harness.stop)
        executable = self.fake_executable(harness,
            "import sys\nsys.stderr.write('synthetic startup failure\\n'); sys.stderr.flush(); sys.exit(7)\n")
        with patch.object(browser, "CHROME", executable):
            with self.assertRaisesRegex(RuntimeError, "synthetic startup failure"):
                harness.start_chrome(timeout=1)


if __name__ == "__main__":
    unittest.main()
