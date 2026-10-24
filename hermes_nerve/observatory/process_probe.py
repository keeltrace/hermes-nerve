"""Strictly allowlisted Linux Hermes process inventory.

The same stdlib module can run on another trusted Linux host via stdin over SSH.
It NEVER returns argv, environment, prompt contents, or full working directory.
An instance means an observed Hermes CLI OS process, NOT a logical session.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "hermes-process-probe/v1"
MAX_SCAN = 20000
MAX_ROWS = 512
MAX_ARGV = 8192
PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
SAFE_LABEL = re.compile(r"[^A-Za-z0-9_.-]")

def _iso(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def _candidate(argv: list[str]) -> tuple[str, str] | None:
    if not argv:
        return None
    program = os.path.basename(argv[0])
    # Do not confuse nerve-observatory, Hermes Factory, Laya, von, health
    # probes, death-supervisors, or unrelated paths containing "hermes".
    script_cli = program.startswith("python") and len(argv) > 1 and os.path.basename(argv[1]) == "hermes"
    is_cli = (program == "hermes" or script_cli or
              any(argv[i] == "-m" and argv[i + 1] == "hermes_cli.main"
                  for i in range(len(argv) - 1)))
    if not is_cli:
        return None
    profile = "default"
    for i, arg in enumerate(argv):
        if arg in ("-p", "--profile") and i + 1 < len(argv):
            if PROFILE_RE.fullmatch(argv[i + 1]):
                profile = argv[i + 1]
        elif arg.startswith("--profile=") and PROFILE_RE.fullmatch(arg[10:]):
            profile = arg[10:]
    mode = "unknown"
    for name in ("gateway", "chat", "proxy", "cron", "telegram", "discord", "whatsapp", "plugins"):
        if name in argv:
            mode = name
            break
    # e.g. python -m hermes_cli.main --profile freebrain gateway run
    return profile, mode

def _status(path: Path) -> tuple[float | None, int | None]:
    try:
        body = (path / "status").read_bytes()[:16384].decode("ascii", "ignore")
    except OSError:
        return None, None
    rss, threads = None, None
    for line in body.splitlines():
        if line.startswith("VmRSS:"):
            try: rss = round(int(line.split()[1]) / 1024, 1)
            except (IndexError, ValueError): pass
        elif line.startswith("Threads:"):
            try: threads = int(line.split()[1])
            except (IndexError, ValueError): pass
    return rss, threads

def _parse_stat(data: str) -> tuple[str, int, int, int] | None:
    end = data.rfind(")")
    if end < 0:
        return None
    fields = data[end + 2:].split()
    if len(fields) < 20:
        return None
    try:
        # After pid and (comm), field #3 = state (index 0).
        return fields[0], int(fields[11]) + int(fields[12]), int(fields[19]), int(fields[1])
    except (ValueError, IndexError):
        return None

def probe(root: str | Path = "/proc", *, now: float | None = None,
          host: str = "j2") -> dict:
    timestamp = time.time() if now is None else float(now)
    proc = Path(root)
    try:
        clock = os.sysconf("SC_CLK_TCK")
        uptime = float((proc / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        clock, uptime = 100, None
    boot = timestamp - uptime if uptime is not None else None
    records = []
    scanned = 0
    truncated = False
    try:
        entries = os.scandir(proc)
    except OSError:
        return {"schema": SCHEMA, "host": host, "state": "unknown",
                "reason": "proc_unavailable", "sampled_at": _iso(timestamp),
                "instances": [], "scanned": 0, "truncated": False}
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            scanned += 1
            if scanned > MAX_SCAN:
                truncated = True
                break
            pid = int(entry.name)
            if pid <= 0:
                continue
            path = proc / entry.name
            try:
                # Reject very large or unreadable process command lines.
                raw = (path / "cmdline").read_bytes()[:MAX_ARGV + 1]
                if len(raw) > MAX_ARGV:
                    continue
                argv = [x.decode("utf-8", "replace") for x in raw.split(b"\0") if x]
                identity = _candidate(argv)
                if identity is None:
                    continue
                stat_text = (path / "stat").read_text()[:4096]
                parsed = _parse_stat(stat_text)
                if parsed is None:
                    continue
                state, cpu_ticks, start_ticks, ppid = parsed
                rss, threads = _status(path)
                try:
                    cwd = os.readlink(path / "cwd")
                    cwd_label = SAFE_LABEL.sub("_", Path(cwd).name)[:72] or None
                except OSError:
                    cwd_label = None
            except (OSError, UnicodeError):
                continue
            profile, mode = identity
            # Session intents and command arguments are NOT included.
            records.append({
                "host": host, "pid": pid, "ppid": ppid,
                "start_ticks": start_ticks, "profile": profile,
                "mode": mode, "os_state": state[:1],
                "cpu_seconds": round(cpu_ticks / clock, 3),
                "rss_mib": rss, "threads": threads,
                "cwd_label": cwd_label,
                "started_at": _iso(boot + start_ticks / clock) if boot is not None else None,
                "source": "procfs",
            })
            if len(records) >= MAX_ROWS:
                truncated = True
                break
    records.sort(key=lambda r: (r["host"], r["pid"]))
    return {"schema": SCHEMA, "host": host, "state": "ok",
            "reason": "procfs_sample", "sampled_at": _iso(timestamp),
            "instances": records, "scanned": scanned, "truncated": truncated}

if __name__ == "__main__":
    print(json.dumps(probe(host="remote"), separators=(",", ":")))
