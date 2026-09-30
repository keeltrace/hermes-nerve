"""Opt-in Hermes pre_tool_call control gate.

v0.1.5.5 uses a conservative local prefilter before Jev. Obviously read-only
introspection is bypassed locally; unknown or potentially mutating calls still
reach Jev. This keeps the gate useful without adding ~network-latency to every
harmless tool call.
"""

from __future__ import annotations

import math
import os
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .engine import DecisionEngine, DecisionResult
from .paths import hermes_home
from .jsonl import append_jsonl, read_jsonl
from .privacy import canonical_hash, redact

_SKIP_PREFIXES = ("nerve_", "jev_")
_DEFAULT_READ_ONLY_TOOLS = frozenset({
    "read_file",
    "search_files",
    "tool_search",
    "tool_describe",
    "remote_worker_status",
    "remote_worker_result",
    "mnemosyne_recall",
    "mnemosyne_shared_recall",
    "mnemosyne_stats",
    "mnemosyne_shared_stats",
    "mnemosyne_get",
    "mnemosyne_graph_query",
    "mnemosyne_sync_status",
    "spotify_search",
})
_SIMPLE_READ_ONLY_COMMANDS = frozenset({
    "pwd", "ls", "cat", "head", "tail", "wc", "stat", "du", "df", "file",
    "readlink", "basename", "dirname", "realpath", "which", "whereis", "whoami",
    "id", "uname", "uptime", "free", "ps", "printenv",
    "grep", "rg", "jq",
})
_SAFE_GIT_SUBCOMMANDS = frozenset({
    "status", "diff", "log", "show", "rev-parse", "ls-files", "ls-tree", "describe", "grep", "blame",
})
_SHELL_META_RE = re.compile(r"(?:&&|\|\||[;|><`]|\$\()")

_configured_mode: str | None = None
_configured_min_confidence: float | None = None
_configured_min_allow_probability: float | None = None
_configured_scope: str | None = None


def configure(*, mode: Any = None, min_confidence: Any = None, min_allow_probability: Any = None, scope: Any = None) -> None:
    """Apply Hermes plugin settings captured during ``register(ctx)``."""
    global _configured_mode, _configured_min_confidence, _configured_min_allow_probability, _configured_scope

    raw_mode = str(mode if mode is not None else "off").strip().lower()
    _configured_mode = raw_mode if raw_mode in {"off", "advisory", "enforce", "ask-only"} else "off"
    try:
        threshold = float(0.80 if min_confidence is None else min_confidence)
    except (TypeError, ValueError):
        threshold = 0.80
    _configured_min_confidence = min(1.0, max(0.0, threshold))
    try:
        allow_floor = float(0.90 if min_allow_probability is None else min_allow_probability)
    except (TypeError, ValueError):
        allow_floor = 0.90
    if not math.isfinite(allow_floor):
        allow_floor = 0.90
    _configured_min_allow_probability = min(1.0, max(0.0, allow_floor))
    raw_scope = str(scope if scope is not None else "selective").strip().lower()
    _configured_scope = raw_scope if raw_scope in {"selective", "all"} else "selective"


def gate_mode() -> str:
    if _configured_mode is not None:
        return _configured_mode
    mode = os.getenv("HERMES_NERVE_GATE_MODE", "off").strip().lower()
    return mode if mode in {"off", "advisory", "enforce", "ask-only"} else "off"


def gate_scope() -> str:
    if _configured_scope is not None:
        return _configured_scope
    scope = os.getenv("HERMES_NERVE_GATE_SCOPE", "selective").strip().lower()
    return scope if scope in {"selective", "all"} else "selective"


def minimum_confidence() -> float:
    if _configured_min_confidence is not None:
        return _configured_min_confidence
    try:
        value = float(os.getenv("HERMES_NERVE_MIN_CONFIDENCE", "0.80"))
    except ValueError:
        value = 0.80
    return min(1.0, max(0.0, value))


def minimum_allow_probability() -> float:
    if _configured_min_allow_probability is not None:
        return _configured_min_allow_probability
    try:
        value = float(os.getenv("HERMES_NERVE_MIN_ALLOW_PROBABILITY", "0.90"))
    except ValueError:
        value = 0.90
    if not math.isfinite(value):
        value = 0.90
    return min(1.0, max(0.0, value))


def _probability_of(probabilities: Any, choice: str) -> float | None:
    """Return a cleaned probability for ``choice``, or None when unavailable.

    Shares the event-telemetry validation rules: non-numeric, non-finite, and
    out-of-range values are treated as absent rather than trusted.
    """
    if not isinstance(probabilities, dict) or choice not in probabilities:
        return None
    try:
        number = float(probabilities[choice])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        return None
    return number


def _block_approval_rule_key(tool_name: str, args: dict[str, Any]) -> str:
    """Stable, non-sensitive approval grain for one blocked tool action."""
    fingerprint = canonical_hash({"tool_name": str(tool_name), "arguments": redact(args)})[:16]
    return f"nerve:block-escalated:{tool_name}:{fingerprint}"


def gate_event_path() -> Path:
    explicit = str(os.getenv("HERMES_NERVE_GATE_EVENTS") or "").strip()
    return Path(explicit).expanduser() if explicit else hermes_home() / "nerve" / "gate-events.jsonl"


def _record_gate_event(
    *,
    tool_name: str,
    action: str,
    reason: str,
    provider_call: bool,
    value: str | None = None,
    confidence: float | None = None,
    latency_ms: float | None = None,
    probabilities: dict[str, float] | None = None,
    turn_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
) -> None:
    record = {
        "schema": "hermes-nerve-gate-event/v1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool_name": str(tool_name),
        "mode": gate_mode(),
        "scope": gate_scope(),
        "action": action,
        "reason": reason,
        "provider_call": bool(provider_call),
        "turn_id": str(turn_id or ""),
        "session_id": str(session_id or ""),
        "tool_call_id": str(tool_call_id or ""),
    }
    if value is not None:
        record["value"] = str(value)
    if confidence is not None:
        record["confidence"] = round(float(confidence), 6)
    if latency_ms is not None:
        record["latency_ms"] = round(float(latency_ms), 3)
    # ``confidence`` is Jev's calibration signal, not the probability of the chosen answer. Reviewing
    # advisory-mode data needs the answer distribution itself (and the argmax probability and margin).
    probs = _clean_probabilities(probabilities)
    if probs:
        record["probabilities"] = probs
        ranked = sorted(probs.values(), reverse=True)
        record["p_top"] = ranked[0]
        if len(ranked) > 1:
            record["margin"] = round(ranked[0] - ranked[1], 6)
    append_jsonl(gate_event_path(), record)


def _clean_probabilities(probabilities: Any) -> dict[str, float]:
    if not isinstance(probabilities, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in probabilities.items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            continue
        out[str(key)] = round(number, 6)
    return out


def _terminal_command(args: dict[str, Any]) -> str:
    for key in ("command", "cmd", "script"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _read_only_terminal(command: str) -> bool:
    """Return True only for deliberately narrow, obvious read-only shell shapes."""
    if not command or _SHELL_META_RE.search(command):
        return False
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        return False
    if not parts:
        return False

    # Do not bypass environment-prefixed or path-qualified executables.
    # `PATH=/tmp ls` can resolve attacker-controlled code and `LD_PRELOAD=... ls`
    # can execute arbitrary code before the nominally read-only program starts.
    if "=" in parts[0] or "/" in parts[0]:
        return False

    executable = parts[0]
    if executable in _SIMPLE_READ_ONLY_COMMANDS:
        if executable == "rg":
            # ripgrep --pre/--pre-glob can execute an external preprocessor.
            if any(arg == "--pre" or arg.startswith("--pre=") or arg == "--pre-glob" or arg.startswith("--pre-glob=") for arg in parts[1:]):
                return False
        return True
    if executable == "git" and len(parts) >= 2:
        if parts[1] not in _SAFE_GIT_SUBCOMMANDS:
            return False
        # diff/show/log/blame can be configured to invoke external helpers.
        if any(arg in {"--ext-diff", "--textconv"} or arg.startswith("--ext-diff=") or arg.startswith("--textconv=") for arg in parts[2:]):
            return False
        return True
    return False


def bypass_reason(tool_name: str, args: dict[str, Any]) -> str | None:
    """Return a deterministic bypass reason, or None when Jev should evaluate."""
    if tool_name.startswith(_SKIP_PREFIXES):
        return "nerve-internal"
    if gate_scope() == "all":
        return None
    if tool_name in _DEFAULT_READ_ONLY_TOOLS:
        return "read-only-tool"
    if tool_name in {"terminal", "shell", "bash"} and _read_only_terminal(_terminal_command(args)):
        return "read-only-terminal"
    return None


def evaluate_tool_call(
    *,
    tool_name: str,
    args: dict[str, Any],
    task_id: str | None,
    engine_factory: Callable[[], DecisionEngine] = DecisionEngine,
) -> DecisionResult | None:
    if gate_mode() == "off":
        return None
    if bypass_reason(tool_name, args) is not None:
        return None
    state = {"tool_name": tool_name, "arguments": redact(args), "task_id": task_id or ""}
    return engine_factory().decide(
        state=state,
        instructions=(
            "Choose how Hermes should handle this proposed tool call. ALLOW means routine and low-risk. "
            "APPROVAL means a human should explicitly approve before execution. BLOCK means the action is "
            "clearly destructive, credential-exfiltrating, privilege-escalating, or inconsistent with a safe agent action."
        ),
        choices=["ALLOW", "APPROVAL", "BLOCK"],
        criteria={
            "ALLOW": "Routine, bounded, reversible, and consistent with normal agent work.",
            "APPROVAL": "Consequential, ambiguous, externally visible, hard to reverse, or confidence is insufficient.",
            "BLOCK": "Clearly dangerous/destructive, credential theft/exfiltration, or unacceptable blast radius.",
        },
        contract="hermes/pre-tool-gate/v1",
    )


def pre_tool_call(tool_name: str, args: dict, task_id: str | None = None, **kwargs):
    mode = gate_mode()
    correlation = {
        "turn_id": str(kwargs.get("turn_id") or ""),
        "session_id": str(kwargs.get("session_id") or ""),
        "tool_call_id": str(kwargs.get("tool_call_id") or ""),
    }
    if mode == "off":
        # v0.2.1 records hook observation even while the synchronous gate is
        # disabled. This makes gate telemetry advance during nervous-only runs
        # and distinguishes "hook did not fire" from "gate intentionally off".
        if not str(tool_name).startswith(_SKIP_PREFIXES):
            _record_gate_event(
                tool_name=tool_name, action="disabled", reason="gate-mode-off",
                provider_call=False, **correlation,
            )
        return None

    reason = bypass_reason(tool_name, args)
    if reason is not None:
        _record_gate_event(tool_name=tool_name, action="bypass", reason=reason, provider_call=False, **correlation)
        return None

    try:
        result = evaluate_tool_call(tool_name=tool_name, args=args, task_id=task_id)
    except Exception:
        _record_gate_event(tool_name=tool_name, action="provider-error", reason="provider-unavailable", provider_call=True, **correlation)
        # A remote classifier must never silently become a single point of failure.
        # Advisory fails open; enforce and ask-only fail toward HUMAN approval, not
        # execution or hard block.
        if mode in {"enforce", "ask-only"}:
            return {
                "action": "approve",
                "message": "Nerve could not obtain a decision; human approval is required (fail-to-human).",
                "rule_key": "nerve:provider-unavailable",
            }
        return None
    if result is None:
        return None

    _record_gate_event(
        tool_name=tool_name,
        action="evaluated",
        reason="material-or-unknown",
        provider_call=True,
        value=result.value,
        confidence=result.confidence,
        latency_ms=result.latency_ms,
        probabilities=result.probabilities,
        **correlation,
    )
    if mode == "advisory":
        return None

    min_conf = minimum_confidence()
    if result.confidence < min_conf:
        return {
            "action": "approve",
            "message": f"Nerve confidence {result.confidence:.3f} is below {min_conf:.3f}; human approval required.",
            "rule_key": "nerve:low-confidence",
        }
    if result.value == "BLOCK":
        # Ask-only enforce variant: a single classifier call is a better second
        # opinion that escalates to a human than a hard blocker. The verdict,
        # confidence, and full distribution are already in the gate event above,
        # so an operator can count near-blocks by filtering evaluated rows on
        # value=BLOCK under mode=ask-only.
        if mode == "ask-only":
            return {
                "action": "approve",
                "message": f"Nerve verdict is BLOCK ({result.confidence:.3f} confidence); human approval required (ask-only mode).",
                "rule_key": _block_approval_rule_key(tool_name, args),
            }
        return {
            "action": "block",
            "message": f"Blocked by Nerve ({result.confidence:.3f} confidence).",
        }
    if result.value == "APPROVAL":
        return {
            "action": "approve",
            "message": f"Nerve requests human approval ({result.confidence:.3f} confidence).",
            "rule_key": f"nerve:{tool_name}",
        }
    # ALLOW is the only verdict that continues without a human, so it carries the
    # strictest gate: the probability of the chosen answer itself, not just its
    # calibration signal. Labeled replays (keeltrace/hermes-nerve#19) show
    # dangerous calls that reach an ALLOW verdict cluster at p(ALLOW) 0.72-0.81,
    # where ``confidence`` does not separate them from benign traffic.
    p_allow = _probability_of(result.probabilities, "ALLOW")
    if p_allow is not None:
        min_allow = minimum_allow_probability()
        if p_allow < min_allow:
            return {
                "action": "approve",
                "message": f"Nerve p(ALLOW) {p_allow:.3f} is below {min_allow:.3f}; human approval required.",
                "rule_key": "nerve:low-allow-probability",
            }
    # No usable ALLOW probability (provider without a distribution): the
    # confidence gate above remains the only automatic-allow signal.
    return None


def report(path: Path | None = None, *, recent_limit: int = 8) -> dict[str, Any]:
    """Aggregate local selective-gate telemetry without making a provider call."""
    selected = path or gate_event_path()
    rows = read_jsonl(selected)

    by_action: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    by_tool: dict[str, int] = {}
    provider_calls = 0
    provider_latency_ms = 0.0
    for row in rows:
        action = str(row.get("action") or "unknown")
        reason = str(row.get("reason") or "unknown")
        tool = str(row.get("tool_name") or "unknown")
        by_action[action] = by_action.get(action, 0) + 1
        by_reason[reason] = by_reason.get(reason, 0) + 1
        by_tool[tool] = by_tool.get(tool, 0) + 1
        if row.get("provider_call"):
            provider_calls += 1
            try:
                provider_latency_ms += float(row.get("latency_ms") or 0.0)
            except (TypeError, ValueError):
                pass

    limit = max(0, min(50, int(recent_limit or 0)))
    bypassed = by_action.get("bypass", 0)
    evaluated = by_action.get("evaluated", 0)
    disabled = by_action.get("disabled", 0)
    return {
        "path": str(selected),
        "metric_scope": "profile-lifetime gate-event ledger",
        "gate_mode": gate_mode(),
        "event_count": len(rows),
        "scope": gate_scope(),
        "hook_observations": len(rows),
        "disabled": disabled,
        "bypassed": bypassed,
        "evaluated": evaluated,
        "provider_errors": by_action.get("provider-error", 0),
        "provider_calls": provider_calls,
        "provider_latency_ms": round(provider_latency_ms, 3),
        "average_provider_latency_ms": round(provider_latency_ms / evaluated, 3) if evaluated else 0.0,
        "estimated_provider_calls_avoided": bypassed,
        "bypass_rate": round(bypassed / len(rows), 6) if rows else 0.0,
        "note": "event_count is gate-hook observations in this profile ledger; decision receipts use a separate profile-lifetime receipt ledger.",
        "by_action": dict(sorted(by_action.items())),
        "by_reason": dict(sorted(by_reason.items())),
        "by_tool": dict(sorted(by_tool.items())),
        "recent": rows[-limit:] if limit else [],
    }