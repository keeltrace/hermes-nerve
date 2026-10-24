"""Native, read-only Hermes session metadata. Never reads messages or prompts.

A profile can serve many processes; session rows are PROFILE-scoped, never
asserted to be the activities of a given PID without explicit evidence.
"""
from __future__ import annotations
import hashlib
import math
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "hermes-session-index/v1"
PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
SAFE = re.compile(r"[a-zA-Z0-9._:/-]{1,80}")
SECRET = re.compile(r"(?i)(sk-[A-Za-z0-9_-]{8}|ghp_[A-Za-z0-9]{8}|bearer\s+\S+|"
                    r"password\s*[:=]|api[_-]?key\s*[:=]|token\s*[:=]|"
                    r"-----BEGIN |AKIA[0-9A-Z]{12}|[A-Za-z0-9+/]{48,}={0,2})")
SQL = """SELECT id,source,model,billing_provider,started_at,ended_at,
  message_count,tool_call_count,api_call_count,input_tokens,output_tokens,
  cache_read_tokens,cache_write_tokens,reasoning_tokens,estimated_cost_usd,
  actual_cost_usd,cost_status,cost_source,title,title_source,
  last_activity_at,last_activity_description,last_activity_provenance,
  profile_name,compression_fallback_streak
  FROM sessions ORDER BY COALESCE(last_activity_at,started_at) DESC LIMIT ?"""
MODEL_SQL = """SELECT session_id,model,billing_provider,api_call_count,
  input_tokens,output_tokens,estimated_cost_usd,actual_cost_usd,
  cost_status,last_seen FROM session_model_usage WHERE session_id IN ("""
LIMIT = 30

def _time(x):
    if type(x) not in (int, float) or not math.isfinite(x) or x < 946684800 or x > 4102444800:
        return None
    return datetime.fromtimestamp(x, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def _token(x):
    return x if isinstance(x, str) and SAFE.fullmatch(x) else None

def _number(x):
    return x if type(x) is int and 0 <= x <= 10**12 else None

def _money(x):
    return round(float(x), 8) if type(x) in (int, float) and math.isfinite(x) and 0 <= x <= 1e6 else None

def _text(x, length):
    if not isinstance(x, str) or not x: return None
    if SECRET.search(x): return "[REDACTED SENSITIVE METADATA]"
    normalized = " ".join(x.split())
    return normalized[:length] + ("..." if len(normalized) > length else "")

def _hash(x):
    return hashlib.sha256(str(x).encode("utf8", "replace")).hexdigest()[:16]

def load_profile(profile, *, home=None, limit=LIMIT, offset=0):
    if home is None:
        from hermes_nerve.paths import hermes_home
        home = str(hermes_home())
    if not isinstance(profile, str) or not PROFILE.fullmatch(profile):
        return {"schema": SCHEMA, "state": "unknown", "reason": "invalid_profile", "sessions": []}
    base = Path(home)
    path = (base if profile == "default" else base / "profiles" / profile) / "state.db"
    try:
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(base.resolve()):
            raise OSError()
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    except (OSError, ValueError, sqlite3.Error):
        return {"schema": SCHEMA, "state": "unknown", "reason": "profile_db_unavailable", "profile": profile, "sessions": []}
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        lim = max(1, min(LIMIT, int(limit)))
        off = max(0, min(10000, int(offset)))
        agg = db.execute("""SELECT COUNT(*),
            SUM(CASE WHEN ended_at IS NULL THEN 1 ELSE 0 END),
            SUM(CASE WHEN input_tokens >= 0 THEN input_tokens ELSE 0 END),
            SUM(CASE WHEN output_tokens >= 0 THEN output_tokens ELSE 0 END),
            SUM(CASE WHEN cache_read_tokens >= 0 THEN cache_read_tokens ELSE 0 END),
            SUM(CASE WHEN estimated_cost_usd >= 0 THEN estimated_cost_usd ELSE 0 END),
            SUM(CASE WHEN estimated_cost_usd IS NOT NULL THEN 1 ELSE 0 END)
            FROM sessions""").fetchone()
        total = agg[0]
        summary = {"profile_total_sessions": total, "open_session_records": agg[1],
                   "input_tokens": _number(agg[2]), "output_tokens": _number(agg[3]),
                   "cache_read_tokens": _number(agg[4]),
                   "stored_estimate_usd": _money(agg[5]),
                   "sessions_with_estimate": agg[6],
                   "cost_kind": "sum_of_native_session_estimates_NOT_an_invoice",
                   "attribution": "profile_history_not_PID"}
        rows = db.execute(SQL + " OFFSET ?", (lim, off)).fetchall()
        items = []
        for r in rows:
            id_ = _hash(r["id"])
            items.append({
                "id": id_,
                "title": _text(r["title"], 140),
                "title_source": _token(r["title_source"]),
                "activity": _text(r["last_activity_description"], 170),
                "activity_source": _token(r["last_activity_provenance"]),
                "source": _token(r["source"]),
                "profile": profile,
                "model": _token(r["model"]),
                "provider": _token(r["billing_provider"]),
                "started_at": _time(r["started_at"]),
                "last_at": _time(r["last_activity_at"]) or _time(r["started_at"]),
                "ended_at": _time(r["ended_at"]),
                "state": "closed_metadata" if r["ended_at"] is not None else "open_metadata",
                "messages": _number(r["message_count"]),
                "tool_calls": _number(r["tool_call_count"]),
                "api_calls": _number(r["api_call_count"]),
                "tokens": {k: _number(r[src]) for k, src in (
                    ("input", "input_tokens"), ("output", "output_tokens"),
                    ("cache_read", "cache_read_tokens"), ("cache_write", "cache_write_tokens"),
                    ("reasoning", "reasoning_tokens"))},
                "cost": {"estimated_usd": _money(r["estimated_cost_usd"]),
                         "actual_usd": _money(r["actual_cost_usd"]),
                         "status": _token(r["cost_status"]),
                         "source": _token(r["cost_source"]),
                         "is_provider_invoice": False},
                "compression_fallback_streak": _number(r["compression_fallback_streak"]),
                "models": [],
            })
        if rows:
            ids = [r["id"] for r in rows]
            placeholders = ",".join("?" for _ in ids)
            for m in db.execute(MODEL_SQL + placeholders + ")", ids).fetchmany(750):
                target = next((it for it in items if it["id"] == _hash(m["session_id"])), None)
                if target is None or len(target["models"]) >= 6: continue
                target["models"].append({
                    "model": _token(m["model"]), "provider": _token(m["billing_provider"]),
                    "api_calls": _number(m["api_call_count"]),
                    "input_tokens": _number(m["input_tokens"]),
                    "output_tokens": _number(m["output_tokens"]),
                    "estimated_cost_usd": _money(m["estimated_cost_usd"]),
                    "actual_cost_usd": _money(m["actual_cost_usd"]),
                    "cost_status": _token(m["cost_status"]),
                    "last_at": _time(m["last_seen"]),
                })
        return {"schema": SCHEMA, "state": "ok", "profile": profile,
                "total_sessions": total, "offset": off, "shown": len(items),
                "summary": summary, "sessions": items,
                "provenance": "Hermes state.db; profile scope; not proven PID-owned"}
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        return {"schema": SCHEMA, "state": "unknown", "reason": "schema_or_db_busy",
                "profile": profile, "sessions": []}
    finally:
        db.close()

__all__ = ["SCHEMA", "load_profile"]
