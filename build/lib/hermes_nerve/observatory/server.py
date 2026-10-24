"""Stdlib, read-only HTTP server for the Nerve observatory.

Security posture:
- Binds 127.0.0.1 by default. A non-loopback bind refuses to start unless a
  token is supplied from the environment (HERMES_NERVE_OBSERVATORY_TOKEN).
- The static shell (/, /index.html, /app.js, /app.css) is deliberately
  data-free (no hostnames, runs, events or shadow values) and is served
  without credentials. Every operational /api/* route requires the bearer
  token, compared in constant time.
- Loopback-only servers reject Host headers that are not loopback names
  (DNS-rebinding guard).
- GET only. Every other method returns 405. No endpoint writes state.
- Responses carry a strict CSP, nosniff, frame denial and no-store caching.
- The token is never logged, echoed, or stored.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import collector, history_store, live_reader, source_probes, ui, fleet_monitor

TOKEN_ENV = "HERMES_NERVE_OBSERVATORY_TOKEN"
# Fixed, server-owned root for the live observer feed. Never derived from HTTP input.
from hermes_nerve.paths import hermes_home

LIVE_ROOT = hermes_home() / "local" / "share" / "hermes-nerve-live"
MIN_TOKEN_CHARS = 24
# The static shell (/, /index.html, /app.js, /app.css) is deliberately data-free:
# it contains no hostnames, runs, events, or shadow values. Everything under /api/
# requires the bearer token. The browser asks the operator for the token and keeps
# it in sessionStorage; it is never placed in a URL, cookie, or log line.
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
_LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _host_name(header_value: str) -> str:
    value = header_value.strip()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 0 else ""
    if value.count(":") == 1:
        return value.rsplit(":", 1)[0]
    return value


class ObservatoryServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        store_path: Path | None,
        shadow_path: Path | None,
        token: str,
        history_dir: Path | None = None,
    ) -> None:
        self.store_path = store_path
        self.shadow_path = shadow_path
        self.token = token
        self.history_dir = history_dir or (collector.report_home() / "observatory" / "history")
        # Fixed server-side root for /api/live. No HTTP request can change it.
        self.live_root = LIVE_ROOT
        # Set by the CLI only when periodic capture is explicitly enabled.
        self.history_recorder = None
        self.fleet_monitor = None  # opt-in background process census, never HTTP-controlled
        self.loopback_only = is_loopback(address[0])
        super().__init__(address, _Handler)


class _Handler(BaseHTTPRequestHandler):
    server_version = "NerveObservatory/1"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return  # No request logging: paths and query values stay out of logs.

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _deny(self, status: int, message: str, extra: dict[str, str] | None = None) -> None:
        self._send(status, message.encode("utf-8"), "text/plain; charset=utf-8", extra)

    def _host_allowed(self) -> bool:
        if not self.server.loopback_only:
            return True  # Explicit non-loopback bind: token auth is the control.
        return _host_name(self.headers.get("Host", "")).lower() in _LOOPBACK_NAMES

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False  # Absent, Basic, or malformed schemes are all refused.
        presented = header[len("Bearer ") :].strip()
        if not presented or len(presented) > 512:
            return False
        try:
            presented_bytes = presented.encode("utf-8")
        except UnicodeEncodeError:
            return False
        # Constant-time comparison; the expected token is never echoed.
        return hmac.compare_digest(presented_bytes, self.server.token.encode("utf-8"))

    def _dispatch(self) -> None:
        if not self._host_allowed():
            return self._deny(421, "misdirected request")
        parts = urlsplit(self.path)
        route = parts.path
        # The static shell carries no operational data, so it is served without
        # credentials. Every data endpoint under /api/ requires the token.
        if route in ("/", "/index.html"):
            return self._send(200, ui.INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        if route == "/app.js":
            return self._send(200, ui.APP_JS.encode("utf-8"), "application/javascript; charset=utf-8")
        if route == "/app.css":
            return self._send(200, ui.APP_CSS.encode("utf-8"), "text/css; charset=utf-8")
        if route == "/factory.js":
            return self._send(200, ui.FACTORY_JS.encode("utf-8"), "application/javascript; charset=utf-8")
        if route == "/factory.css":
            return self._send(200, ui.FACTORY_CSS.encode("utf-8"), "text/css; charset=utf-8")
        if not route.startswith("/api/"):
            return self._deny(404, "not found")
        if not self._authorized():
            return self._deny(401, "authentication required", {"WWW-Authenticate": 'Bearer realm="nerve-observatory"'})
        try:
            if route == "/api/health":
                return self._json(200, {"status": "ok", "schema": collector.SCHEMA, "auth": "bearer"})
            if route == "/api/snapshot":
                return self._json(
                    200,
                    collector.build_snapshot(store_path=self.server.store_path, shadow_path=self.server.shadow_path),
                )
            if route == "/api/run":
                query = parse_qs(parts.query, keep_blank_values=True)
                task_id = (query.get("task_id") or [""])[0]
                run_raw = (query.get("run_id") or [""])[0]
                if not task_id or len(task_id) > 200 or not run_raw.isdigit() or len(run_raw) > 12:
                    return self._deny(400, "invalid task_id or run_id")
                return self._json(
                    200,
                    collector.run_events(self.server.store_path, task_id, int(run_raw)),
                )
            if route == "/api/history/status":
                # Capture status is separate from the live snapshot. Never includes paths.
                recorder_ref = self.server.history_recorder
                if recorder_ref is None:
                    # Same shape as an active recorder; no path, only sanitized storage counts.
                    return self._json(200, {
                        "schema": collector.HISTORY_SCHEMA,
                        "capture": {
                            "enabled": False,
                            "state": "disabled",
                            "interval_seconds": None,
                            "rows_written": 0,
                            "last_error": None,
                            "running": False,
                            "storage": history_store.storage_state(self.server.history_dir),
                        },
                    })
                return self._json(200, {"schema": collector.HISTORY_SCHEMA, "capture": recorder_ref.status()})
            if route == "/api/history":
                query = parse_qs(parts.query, keep_blank_values=True)
                raw_limit = (query.get("limit") or [str(history_store.DEFAULT_PAGE_SIZE)])[0]
                cursor = (query.get("cursor") or [""])[0] or None
                if not raw_limit.isdigit() or len(raw_limit) > 4:
                    return self._deny(400, "invalid limit")
                try:
                    page = history_store.read_page(
                        self.server.history_dir, cursor=cursor, limit=int(raw_limit)
                    )
                except history_store.InvalidCursor:
                    return self._deny(400, "invalid cursor or limit")
                return self._json(200, {
                    "schema": collector.HISTORY_SCHEMA,
                    "rows": page["rows"],
                    "next_cursor": page["next_cursor"],
                    "skipped": page["skipped"],
                    "storage": history_store.storage_state(self.server.history_dir),
                })
            if route == "/api/fleet":
                watch = self.server.fleet_monitor
                if watch is None:
                    return self._json(200, {"schema": fleet_monitor.SCHEMA, "status": "disabled",
                                            "sources": {}, "counts": {}, "instances": [],
                                            "notes": fleet_monitor.NOTES})
                return self._json(200, watch.snapshot())
            if route == "/api/fleet/page":
                watch = self.server.fleet_monitor
                if watch is None:
                    return self._deny(503, "fleet monitor disabled")
                query = parse_qs(parts.query, keep_blank_values=True)
                offsets = query.get("offset") or ["0"]
                limits = query.get("limit") or ["100"]
                if (len(offsets) != 1 or len(limits) != 1 or
                    not offsets[0].isdigit() or not limits[0].isdigit() or
                    len(offsets[0]) > 5 or len(limits[0]) > 3 or
                    int(offsets[0]) > fleet_monitor.MAX_RETAINED or
                    int(limits[0]) > 512):
                    return self._deny(400, "invalid fleet page")
                return self._json(200, watch.page(offset=int(offsets[0]), limit=int(limits[0])))
            if route == "/api/fleet/profile":
                watch = self.server.fleet_monitor
                if watch is None:
                    return self._deny(503, "fleet monitor disabled")
                query = parse_qs(parts.query, keep_blank_values=True)
                names = query.get("name") or [""]
                offset_raw = query.get("offset") or ["0"]
                limit_raw = query.get("limit") or ["20"]
                if (len(names) != 1 or len(offset_raw) != 1 or len(limit_raw) != 1 or
                    not offset_raw[0].isdigit() or len(offset_raw[0]) > 5 or
                    not limit_raw[0].isdigit() or len(limit_raw[0]) > 3 or
                    int(limit_raw[0]) > 30 or int(offset_raw[0]) > 10000):
                    return self._deny(400, "invalid profile request")
                report = watch.profile_history(names[0], offset=int(offset_raw[0]),
                                               limit=int(limit_raw[0]))
                return self._json(200, report) if report is not None else self._deny(404, "profile not found")
            if route == "/api/fleet/instance":
                watch = self.server.fleet_monitor
                if watch is None:
                    return self._deny(503, "fleet monitor disabled")
                query = parse_qs(parts.query, keep_blank_values=True)
                keys = query.get("key") or [""]
                limits = query.get("limit") or ["60"]
                if len(keys) != 1 or len(limits) != 1 or not limits[0].isdigit() or len(limits[0]) > 3:
                    return self._deny(400, "invalid fleet query")
                key = keys[0]
                if len(key) > 100 or not fleet_monitor.re.fullmatch(
                        r"(j2|win4060):[0-9]{1,10}:[0-9]{1,20}", key):
                    return self._deny(400, "invalid instance key")
                detail = watch.detail(key, limit=int(limits[0]))
                return self._json(200, detail) if detail is not None else self._deny(404, "instance not found")
            if route == "/api/live":
                # Fixed root, no query parameters are read. Missing feed => unknown, not green.
                live = live_reader.read_live_snapshot(root=str(self.server.live_root))
                live["services"] = source_probes.collect()
                return self._json(200, live)
        except Exception:  # Never leak internals; the cause is not echoed.
            return self._deny(500, "observatory collector error")
        return self._deny(404, "not found")

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._dispatch()

    def _method_not_allowed(self) -> None:
        self._deny(405, "method not allowed", {"Allow": "GET"})

    do_HEAD = _method_not_allowed
    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_OPTIONS = _method_not_allowed


def make_server(
    host: str = "127.0.0.1",
    port: int = 8787,
    *,
    store_path: Path | str | None = None,
    shadow_path: Path | str | None = None,
    token: str | None = None,
    history_dir: Path | str | None = None,
) -> ObservatoryServer:
    """Create (but do not start) the observatory server.

    A bearer token is required for every bind, loopback included, so no
    operational API is ever reachable without credentials. The token comes from
    the environment (never a URL or a file this module writes). Refused before
    any socket is opened when missing or too short.
    """
    resolved_token = (token if token is not None else os.environ.get(TOKEN_ENV, "")).strip()
    if len(resolved_token) < MIN_TOKEN_CHARS:
        raise ValueError(
            f"refusing to start: {TOKEN_ENV} must be set to at least {MIN_TOKEN_CHARS} characters"
        )
    return ObservatoryServer(
        (host, int(port)),
        store_path=Path(store_path).expanduser() if store_path else None,
        shadow_path=Path(shadow_path).expanduser() if shadow_path else None,
        token=resolved_token,
        history_dir=Path(history_dir).expanduser() if history_dir else None,
    )
