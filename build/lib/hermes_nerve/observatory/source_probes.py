"""Read-only, bounded health probes for the known local control-room services.

No caller-supplied URL, shell command, authentication secret or cached success.
Unavailable/invalid data are always unknown rather than inferred healthy.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from urllib import request
from urllib.error import HTTPError, URLError

SCHEMA = "nerve-source-health/v1"
MAX_BODY = 16384
_TIMEOUT = 1.5
_SOURCES = (
    ("commandcenter", "Commandcenter", "http://127.0.0.1:4173/api/v1/health", ("ready",)),
    ("inference-gateway", "Hermes inference gateway", "http://127.0.0.1:20128/health", ("ok",)),
    ("paperclip", "Paperclip", "http://127.0.0.1:3100/api/health", ("ok",)),
)


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = request.build_opener(_NoRedirect())


def _fetch(url: str) -> dict:
    req = request.Request(url, headers={"Accept": "application/json"}, method="GET")
    with _OPENER.open(req, timeout=_TIMEOUT) as response:
        if response.status != 200:
            raise ValueError("health probe not 200")
        raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise ValueError("health probe too large")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("health probe not object")
        return data


def _mega_mcp_state() -> str:
    result = subprocess.run(
        ["/usr/bin/systemctl", "is-active", "mega-mcp.target"],
        text=True, capture_output=True, timeout=_TIMEOUT, check=False,
    )
    return "ok" if result.returncode == 0 and result.stdout.strip() == "active" else "unknown"


def collect() -> dict:
    now = datetime.now(timezone.utc).isoformat()
    result = []
    for identity, label, url, accepted in _SOURCES:
        state = "unknown"
        try:
            report = _fetch(url)
            if report.get("status") in accepted:
                state = "ok"
        except (OSError, ValueError, URLError, HTTPError, TimeoutError):
            pass
        result.append({"id": identity, "label": label, "state": state,
                       "observed_at": now, "source": "fixed-loopback-health"})
    try:
        mega = _mega_mcp_state()
    except (OSError, subprocess.TimeoutExpired, ValueError):
        mega = "unknown"
    result.append({"id": "mega-mcp", "label": "MegaMCP", "state": mega,
                   "observed_at": now, "source": "systemd-target"})
    return {"schema": SCHEMA, "sources": result, "generated_at": now}


__all__ = ["SCHEMA", "collect"]
