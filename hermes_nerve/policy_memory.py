"""Conservative persisted memory for runtime-wide tool policy blocks.

Only unmistakable execution-mode policy failures belong here. Ordinary path,
schema, permission-to-one-resource, and transient failures stay in the turn-local
nervous-system control loop.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .jsonl import append_jsonl, read_jsonl
from .paths import hermes_home
from .privacy import canonical_hash

_UNATTENDED_MARKERS = (
    "single-query mode",
    "single query mode",
    "unattended session",
    "without a user present",
)
_POLICY_MARKERS = (
    "blocked:",
    "not allowed",
    "requires approval",
    "approval required",
    "approve only if",
)

# Persist a whole-tool ban only when we have direct evidence that the entire
# capability is unavailable in unattended mode. A dangerous *terminal command*
# being approval-gated does not make the terminal tool itself unavailable.
_TOOL_WIDE_UNATTENDED_BLOCK_TOOLS = frozenset({"execute_code"})


def tool_wide_policy_reason(tool_name: str, status: str, result: str, error_message: str) -> str:
    """Return a stable reason only for proven whole-tool runtime policy blocks."""
    if str(tool_name or "").strip().lower() not in _TOOL_WIDE_UNATTENDED_BLOCK_TOOLS:
        return ""
    text = " ".join((str(result or ""), str(error_message or ""))).lower()
    normalized_status = str(status or "").strip().lower()
    if not any(marker in text for marker in _UNATTENDED_MARKERS):
        return ""
    if normalized_status == "blocked" or any(marker in text for marker in _POLICY_MARKERS):
        return "unattended-policy-block"
    return ""


def _invocation_mode(argv: list[str] | None = None) -> str:
    values = [str(value) for value in (sys.argv if argv is None else argv)]
    if "-q" in values or "--query" in values or any(value.startswith("--query=") for value in values):
        return "single-query"
    return "interactive"


def _config_digest(home: Path) -> str:
    path = home / "config.yaml"
    if not path.is_file():
        return "missing"
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "unreadable"


def runtime_signature() -> str:
    """Fingerprint the profile + execution mode + policy-relevant config state."""
    home = hermes_home().expanduser().resolve(strict=False)
    return canonical_hash(
        {
            "hermes_home": str(home),
            "invocation_mode": _invocation_mode(),
            "config_sha256": _config_digest(home),
        }
    )


def default_policy_memory_path() -> Path:
    explicit = str(os.getenv("HERMES_NERVE_POLICY_MEMORY") or "").strip()
    return Path(explicit).expanduser() if explicit else hermes_home() / "nerve" / "policy-memory.jsonl"


@dataclass(frozen=True)
class PolicyMemoryEntry:
    tool_name: str
    reason: str
    runtime_signature: str
    created_at_epoch: float
    expires_at_epoch: float
    evidence_fingerprint: str

    def as_record(self) -> dict[str, Any]:
        return {
            "record_type": "tool_policy_memory",
            "tool_name": self.tool_name,
            "reason": self.reason,
            "runtime_signature": self.runtime_signature,
            "created_at_epoch": self.created_at_epoch,
            "expires_at_epoch": self.expires_at_epoch,
            "evidence_fingerprint": self.evidence_fingerprint,
        }


class PolicyMemory:
    """Small append-only cache keyed by the current runtime signature."""

    def __init__(self, *, path: Path | None = None, ttl_seconds: int = 3600) -> None:
        self.path = path or default_policy_memory_path()
        self.ttl_seconds = max(0, int(ttl_seconds))
        self._signature = ""
        self._mtime_ns = -1
        self._entries: dict[str, PolicyMemoryEntry] = {}

    def _refresh(self, *, now: float | None = None, force: bool = False) -> None:
        current_signature = runtime_signature()
        try:
            current_mtime = self.path.stat().st_mtime_ns
        except OSError:
            current_mtime = 0
        if not force and current_signature == self._signature and current_mtime == self._mtime_ns:
            return
        current_time = time.time() if now is None else float(now)
        entries: dict[str, PolicyMemoryEntry] = {}
        for row in read_jsonl(self.path)[-512:]:
            if row.get("record_type") != "tool_policy_memory":
                continue
            if str(row.get("runtime_signature") or "") != current_signature:
                continue
            try:
                expires = float(row.get("expires_at_epoch") or 0.0)
                created = float(row.get("created_at_epoch") or 0.0)
            except (TypeError, ValueError):
                continue
            if expires <= current_time:
                continue
            tool_name = str(row.get("tool_name") or "").strip().lower()
            reason = str(row.get("reason") or "").strip()
            evidence = str(row.get("evidence_fingerprint") or "").strip()
            if not tool_name or not reason:
                continue
            candidate = PolicyMemoryEntry(
                tool_name=tool_name,
                reason=reason,
                runtime_signature=current_signature,
                created_at_epoch=created,
                expires_at_epoch=expires,
                evidence_fingerprint=evidence,
            )
            previous = entries.get(tool_name)
            if previous is None or candidate.created_at_epoch >= previous.created_at_epoch:
                entries[tool_name] = candidate
        self._signature = current_signature
        self._mtime_ns = current_mtime
        self._entries = entries

    def remember(
        self,
        *,
        tool_name: str,
        reason: str,
        evidence_fingerprint: str,
        now: float | None = None,
    ) -> PolicyMemoryEntry | None:
        if self.ttl_seconds <= 0:
            return None
        selected_tool = str(tool_name or "").strip().lower()
        selected_reason = str(reason or "").strip()
        if not selected_tool or not selected_reason:
            return None
        current_time = time.time() if now is None else float(now)
        entry = PolicyMemoryEntry(
            tool_name=selected_tool,
            reason=selected_reason,
            runtime_signature=runtime_signature(),
            created_at_epoch=current_time,
            expires_at_epoch=current_time + self.ttl_seconds,
            evidence_fingerprint=canonical_hash(str(evidence_fingerprint or "")),
        )
        append_jsonl(self.path, entry.as_record())
        self._signature = entry.runtime_signature
        self._entries[selected_tool] = entry
        try:
            self._mtime_ns = self.path.stat().st_mtime_ns
        except OSError:
            self._mtime_ns = 0
        return entry

    def blocked(self, tool_name: str, *, now: float | None = None) -> PolicyMemoryEntry | None:
        if self.ttl_seconds <= 0:
            return None
        self._refresh(now=now)
        selected = str(tool_name or "").strip().lower()
        entry = self._entries.get(selected)
        if entry is None:
            return None
        current_time = time.time() if now is None else float(now)
        if entry.expires_at_epoch <= current_time:
            self._entries.pop(selected, None)
            return None
        return entry
