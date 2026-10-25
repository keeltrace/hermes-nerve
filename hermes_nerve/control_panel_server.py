"""Local-only static web UI with origin checks and ephemeral session authorization."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
from urllib.parse import urlsplit, parse_qs
import webbrowser

from .control_panel import ControlPanel, PanelError

ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/guide": ("guide.html", "text/html; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/assets/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
MAX_BODY = 65536


def make_handler(panel: ControlPanel, session_token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NervePanel/1"

        def log_message(self, fmt, *args):
            # Never log a URL, token, profile contents or credentials.
            pass

        def _base(self):
            return "http://127.0.0.1:" + str(self.server.server_port)

        def _headers(self):
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self';"
                " connect-src 'self'; img-src 'self' data:; object-src 'none';"
                " base-uri 'none'; frame-ancestors 'none'",
            )

        def _reply(self, code, data):
            payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(code)
            self._headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _authorization(self, write=False):
            if self.headers.get("Host") != "127.0.0.1:" + str(self.server.server_port):
                raise PanelError("Invalid local Host header.", 403)
            submitted = self.headers.get("X-Nerve-Token", "")
            if not submitted or not secrets.compare_digest(session_token, submitted):
                raise PanelError("Unauthorized panel request.", 403)
            if write and self.headers.get("Origin") != self._base():
                raise PanelError("Untrusted browser origin.", 403)

        def _input(self):
            raw_size = self.headers.get("Content-Length")
            if not raw_size or not raw_size.isdecimal() or int(raw_size) > MAX_BODY:
                raise PanelError("Request is missing or too large.", 413)
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise PanelError("Expected application/json.", 415)
            try:
                return json.loads(self.rfile.read(int(raw_size)).decode("utf-8"))
            except (ValueError, UnicodeError):
                raise PanelError("Invalid JSON.")

        def do_GET(self):
            try:
                path = urlsplit(self.path).path
                if path in ASSETS:
                    if self.headers.get("Host") != "127.0.0.1:" + str(self.server.server_port):
                        raise PanelError("Invalid local Host header.", 403)
                    filename, content_type = ASSETS[path]
                    payload = (Path(__file__).parent / "web" / filename).read_bytes()
                    self.send_response(200)
                    self._headers()
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self._authorization()
                if path == "/api/status":
                    return self._reply(200, panel.status())
                if path == "/api/local-setup-status":
                    options = parse_qs(urlsplit(self.path).query)
                    backend = options.get("backend", [None])[0]
                    return self._reply(200, panel.local_setup_status(backend))
                if path == "/api/diagnostics":
                    status = panel.status()
                    return self._reply(200, {
                        "home": status["home"],
                        "version": status["version"],
                        "profile": status["current"]["profile"],
                        "backend": status["current"]["backend"],
                        "installation": status["installation"],
                        "warnings": status["warnings"],
                        "runtime": "Running Hermes processes are not inspected by this panel.",
                    })
                raise PanelError("Unknown route.", 404)
            except PanelError as exc:
                self._reply(exc.status, {"error": str(exc)})
            except Exception:
                self._reply(500, {"error": "Internal control panel error."})

        def do_POST(self):
            try:
                self._authorization(write=True)
                payload = self._input()
                route = urlsplit(self.path).path
                if route == "/api/preview":
                    return self._reply(200, panel.preview(payload))
                if route == "/api/apply":
                    return self._reply(200, panel.apply(payload))
                if route == "/api/rollback":
                    return self._reply(200, panel.rollback(payload))
                if route == "/api/test-backend":
                    return self._reply(200, panel.test_backend(payload))
                if route == "/api/local-setup-preview":
                    return self._reply(200, panel.local_setup_preview(payload))
                if route == "/api/local-setup-install":
                    return self._reply(200, panel.local_setup_install(payload))
                if route == "/api/local-setup-stop":
                    return self._reply(200, panel.local_setup_stop(payload))
                if route == "/api/install-preview":
                    return self._reply(200, panel.install_preview())
                if route == "/api/install":
                    return self._reply(200, panel.install(payload))
                raise PanelError("Unknown route.", 404)
            except PanelError as exc:
                self._reply(exc.status, {"error": str(exc)})
            except Exception:
                self._reply(500, {"error": "Internal control panel error."})

    return Handler


def serve(*, host="127.0.0.1", port=0, open_browser=True, panel=None):
    if host != "127.0.0.1" or type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Control Panel must bind to 127.0.0.1.")
    panel = panel if panel is not None else ControlPanel()
    token = secrets.token_urlsafe(32)
    with ThreadingHTTPServer(("127.0.0.1", port), make_handler(panel, token)) as server:
        url = "http://127.0.0.1:" + str(server.server_port) + "/#token=" + token
        print("Nerve Control Panel: " + url, flush=True)
        print("Local-only. Press Ctrl+C to stop. Changes require an explicit Apply click.", flush=True)
        if open_browser:
            webbrowser.open(url, new=2)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
    return url
