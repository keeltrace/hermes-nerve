"""Opt-in Hermes pre_tool_call control gate — HITL v2.

v0.4.0 layered decision flow:

1. Deterministic risk classification (``risk.assess_tool_call``) for every call.
   Recognized safe / reversible-project-scope calls execute automatically.
   Recognized dangerous calls are blocked or escalated with a concrete
   explanation citing the exact evidence.
2. The probabilistic decision model (Jev) is consulted ONLY for calls the
   deterministic rules cannot recognize (``uncertain`` band).
3. Model confidence and p(ALLOW) are calibration signals, never interruption
   reasons. A 0.82 probability of ALLOW does not mean 18% risk; when the
   deterministic scan finds nothing risky in an ALLOW verdict, the call
   proceeds and the rationale is logged instead of interrupting the operator.
4. Approval escalation keys are CONTENT-SCOPED (tool + argument shape +
   workdir). A session or permanent approval authorizes exactly the approved
   action shape — never unrelated commands (replaces the legacy generic
   ``nerve:low-confidence`` / ``nerve:low-allow-probability`` /
   ``nerve:{tool_name}`` keys).
5. Provider outages fail to HUMAN approval for unrecognized calls; recognized
   safe/project calls continue deterministically (no new outage interruptions
   for routine work).

Hermes's own security floors (hardline blocks, dangerous-command detection,
container guards) run independently upstream of this hook and are never
bypassed or weakened by Nerve.
"""

from __future__ import annotations

from . import advisory
from . import catalog as catalog

import math
import os
import re
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypedDict

from . import preferences as prefs
from . import risk
from .engine import DecisionEngine, DecisionResult
from .paths import hermes_home
from .jsonl import append_jsonl, read_jsonl
from .privacy import canonical_hash, redact


class _Correlation(TypedDict):
    """Observability ids threaded through every gate event (exact keys, exact types)."""

    turn_id: str
    session_id: str
    tool_call_id: str

# Model primitives that would recurse if the gate evaluated them again. This is
# a narrow, named anti-recursion allowance — NOT a namespace prefix bypass:
# every other Nerve tool is classified by operation like any other tool.
_INTERNAL_PRIMITIVES = frozenset({
    "nerve_decide", "nerve_verify", "nerve_assess", "nerve_rank",
    "jev_decide", "jev_verify", "jev_assess",
})
_DEFAULT_READ_ONLY_TOOLS = frozenset({
    "read_file",
    "search_files",
    "skills_list",
    "skill_view",
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
    # Introspective Kanban/skill reads: no side effects, previously escalated as
    # per-argument block hashes that bloated command_allowlist.
    "kanban_list",
    "kanban_show",
    "kanban_attachments",
    "skill_view",
    "skills_list",
})
_SIMPLE_READ_ONLY_COMMANDS = frozenset({
    "pwd", "ls", "cat", "head", "tail", "wc", "stat", "du", "df", "file",
    "readlink", "basename", "dirname", "realpath", "which", "whereis", "whoami",
    "id", "uname", "uptime", "free", "ps",
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
_configured_risk_preflight: bool | None = True
_configured_learned_preferences: bool | None = True
_configured_preference_min_samples: int | None = 2


def configure(*, mode: Any = None, min_confidence: Any = None, min_allow_probability: Any = None,
              scope: Any = None, risk_preflight: Any = None, learned_preferences: Any = None,
              preference_min_samples: Any = None) -> None:
    """Apply Hermes plugin settings captured during ``register(ctx)``."""
    global _configured_mode, _configured_min_confidence, _configured_min_allow_probability
    global _configured_scope, _configured_risk_preflight, _configured_learned_preferences
    global _configured_preference_min_samples

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
    _configured_risk_preflight = True if risk_preflight is None else bool(risk_preflight)
    _configured_learned_preferences = True if learned_preferences is None else bool(learned_preferences)
    try:
        _configured_preference_min_samples = max(1, int(preference_min_samples if preference_min_samples is not None else 2))
    except (TypeError, ValueError):
        _configured_preference_min_samples = 2


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


def risk_preflight_enabled() -> bool:
    if _configured_risk_preflight is not None:
        return bool(_configured_risk_preflight)
    return str(os.getenv("HERMES_NERVE_RISK_PREFLIGHT", "1")).strip().lower() not in {"0", "false", "no", "off"}


def learned_preferences_enabled() -> bool:
    if _configured_learned_preferences is not None:
        return bool(_configured_learned_preferences)
    return str(os.getenv("HERMES_NERVE_LEARNED_PREFERENCES", "1")).strip().lower() not in {"0", "false", "no", "off"}


def preference_min_samples() -> int:
    if _configured_preference_min_samples is not None:
        return max(1, int(_configured_preference_min_samples))
    try:
        return max(1, int(os.getenv("HERMES_NERVE_PREF_MIN_SAMPLES", "2")))
    except ValueError:
        return 2


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
    risk_category: str | None = None,
    detail: str | None = None,
    rule_key: str | None = None,
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
    if risk_category is not None:
        record["risk_category"] = str(risk_category)
    if detail is not None:
        record["detail"] = str(detail)[:400]
    if rule_key is not None:
        record["rule_key"] = str(rule_key)
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


def _read_only_terminal(command: str, workdir: str | None = None) -> bool:
    """Return True only for deliberately narrow, obvious read-only shell shapes."""
    if not command or _SHELL_META_RE.search(command):
        return False
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        return False
    if not parts:
        return False

    # Round-5 D1: any command separator (single &, ;, |, newline) means more
    # than the first command may run — the prefilter must never bypass that.
    if any(sep in command for sep in ("&", ";", "|", "\n", "\r")):
        return False
    # Do not bypass environment-prefixed or path-qualified executables.
    # `PATH=/tmp ls` can resolve attacker-controlled code and `LD_PRELOAD=... ls`
    # can execute arbitrary code before the nominally read-only program starts.
    if "=" in parts[0] or "/" in parts[0]:
        return False

    executable = parts[0]
    if executable in _SIMPLE_READ_ONLY_COMMANDS:
        if executable == "ps" and any(
                a.startswith("-") and "e" in a and not a.startswith("--") for a in parts[1:]):
            # `ps e`/`ps eww` print each process's environment (credentials).
            return False
        if executable == "cat" and any(
                a in {"/proc/self/environ", "/proc/1/environ"} for a in parts[1:]):
            return False
        if executable == "rg":
            # ripgrep --pre/--pre-glob can execute an external preprocessor.
            if any(arg == "--pre" or arg.startswith("--pre=") or arg == "--pre-glob" or arg.startswith("--pre-glob=") for arg in parts[1:]):
                return False
        # Reading credential/protected locations is not routine even for cat/grep:
        # defer to the full risk classifier, which escalates sensitive targets.
        try:
            cwd = Path.cwd()
        except OSError:
            cwd = Path("/")
        if any(risk.sensitive_read_target(risk._expand(a, Path(workdir) if workdir else None))
               for a in parts[1:] if not a.startswith("-")):
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
    """Return a deterministic bypass reason, or None when evaluation should run."""
    if tool_name in _INTERNAL_PRIMITIVES:
        return "nerve-internal"
    if gate_scope() == "all":
        return None
    # No blanket namespace bypass: Nerve/Jev/skill/MCP tools are classified by
    # their OPERATION. Only operations that are provably read-only skip the
    # model; mutations and unknown shapes are evaluated like any other tool.
    if catalog.operation_effect(tool_name, args) == catalog.READ:
        assessed = catalog.classify_tool(tool_name, args, risk._workdir_for(tool_name, args))
        if assessed is not None and assessed.category == risk.SAFE:
            return "catalog-read-only"
        # A known read that targets sensitive data must not fall through to the
        # name-based fallback, including skill_view's referenced file paths.
        return None
    if tool_name in _DEFAULT_READ_ONLY_TOOLS:
        # Round-3: read_file/read_many_files lose the bypass when the path is
        # a credential or protected-system location (no sensitive-file bypass).
        # search_files is included here: searching a credential or protected
        # location must require approval instead of auto-allowing.
        if tool_name in ("read_file", "read_many_files", "search_files"):
            raw = args.get("path") or args.get("file_path") or args.get("file")
            if tool_name == "read_many_files":
                raw = args.get("paths") or []
            elif tool_name == "search_files":
                raw = (args.get("path") or args.get("dir") or args.get("root")
                       or args.get("directory") or args.get("paths") or [])
                if raw is not None and not isinstance(raw, list):
                    raw = [raw]
            paths = raw if isinstance(raw, list) else ([raw] if raw else [])
            if any(risk.sensitive_read_target(risk._expand(str(p)))
                   for p in paths if p):
                return None
            # Nested/alternate argument shapes must not smuggle a sensitive path
            # past the documented key check.
            if catalog.mcp_signals(tool_name, args):
                return None
        return "read-only-tool"
    if tool_name in {"terminal", "shell", "bash"} and _read_only_terminal(
            _terminal_command(args), risk._workdir_for(tool_name, args)):
        return "read-only-terminal"
    return None


def evaluate_tool_call(
    *,
    tool_name: str,
    args: dict[str, Any],
    task_id: str | None,
    engine_factory: Callable[[], DecisionEngine] = DecisionEngine,
) -> DecisionResult | None:
    """Probabilistic model evaluation (uncertain band only in the v2 flow)."""
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


# --- HITL v2 message formatting -------------------------------------------------

def format_approval_message(assessment: risk.RiskAssessment, *, model_note: str = "",
                            uncertainty_note: str = "") -> str:
    """Build the four-question approval message (what/why/if-approved/risks).

    Confidence numbers are never part of the message. If uncertainty is the
    genuine cause, the uncertainty note says so plainly — no invented dangers.
    """
    lines = [f"APPROVAL REQUIRED — {assessment.action_label}"]
    lines.append(f"Action: {assessment.display or assessment.summary}")
    if assessment.why:
        lines.append(f"Why: {assessment.why}")
    if assessment.impact:
        lines.append(f"If approved: {assessment.impact}")
    if assessment.risk_notes:
        lines.append(f"Risks: {assessment.risk_notes} ({assessment.reversibility}).")
    if assessment.safer_alternative:
        lines.append(f"Safer alternative: {assessment.safer_alternative}")
    if model_note:
        lines.append(model_note)
    if uncertainty_note:
        lines.append(uncertainty_note)
    return "\n".join(lines)


def format_consequence_message(assessment: risk.RiskAssessment) -> str:
    """Concise operator-facing approval text: Do this -> / Skip ->."""
    do_this = assessment.impact or assessment.risk_notes or assessment.summary
    if assessment.impact and assessment.risk_notes:
        do_this = f"{assessment.impact} ({assessment.risk_notes})"
    skip = assessment.safer_alternative or "Nothing changes; the action is not performed."
    lines = [f"APPROVAL REQUIRED — {assessment.action_label}",
             f"Action: {assessment.display or assessment.summary}"]
    if assessment.why:
        lines.append(f"Why: {assessment.why}")
    lines.append(f"Do this -> {do_this}")
    lines.append(f"Skip -> {skip}")
    return "\n".join(lines)


def format_block_message(assessment: risk.RiskAssessment) -> str:
    lines = [f"BLOCKED — {assessment.action_label}"]
    lines.append(f"Action: {assessment.display or assessment.summary}")
    if assessment.why:
        lines.append(f"Why: {assessment.why}")
    if assessment.risk_notes:
        lines.append(f"Risks: {assessment.risk_notes}")
    if assessment.safer_alternative:
        lines.append(f"Safer alternative: {assessment.safer_alternative}")
    return "\n".join(lines)


_UNCERTAINTY_MODEL_NOTE = (
    "Note: the decision model flagged this for human review. The deterministic "
    "scan found no specific hazard, so caution — not a known danger — is the reason."
)
_UNCERTAINTY_OUTAGE_NOTE = (
    "Note: the decision model is currently unavailable, so this unrecognized "
    "command could not be verified. Recognized routine commands are unaffected."
)
_UNCERTAINTY_WEAK_ALLOW_NOTE = (
    "Note: the decision model leaned toward allowing this, but its evidence was "
    "weak, so the operation could not be verified confidently. No danger was "
    "detected — this prompt exists because the risk could not be ruled out."
)


# --- Layered decision flow --------------------------------------------------------


def pre_tool_call(tool_name: str, args: dict, task_id: str | None = None, **kwargs):
    mode = gate_mode()
    correlation: _Correlation = {
        "turn_id": str(kwargs.get("turn_id") or ""),
        "session_id": str(kwargs.get("session_id") or ""),
        "tool_call_id": str(kwargs.get("tool_call_id") or ""),
    }
    if mode == "off":
        # v0.2.1 records hook observation even while the synchronous gate is
        # disabled. This makes gate telemetry advance during nervous-only runs
        # and distinguishes "hook did not fire" from "gate intentionally off".
        if str(tool_name) not in _INTERNAL_PRIMITIVES:
            _record_gate_event(
                tool_name=tool_name, action="disabled", reason="gate-mode-off",
                provider_call=False, **correlation,
            )
        return None

    reason = bypass_reason(tool_name, args)
    if reason is not None:
        _record_gate_event(tool_name=tool_name, action="bypass", reason=reason, provider_call=False, **correlation)
        return None

    # ---- Layer 1: deterministic risk classification ----------------------------
    try:
        assessment = risk.assess_tool_call(tool_name, args)
    except Exception:
        assessment = risk.RiskAssessment(
            risk.UNCERTAIN, tool_name,
            action_label="Unclassified operation",
            summary=risk._short_args(args),
            why="the deterministic risk scan itself failed, so the call must be treated as unrecognized",
        )

    if assessment.category in (risk.CONSEQUENTIAL, risk.PROHIBITED):
        # Round-3: permanent approvals bind to the distinct EFFECT (resolved
        # commit SHAs for pushes, target identities for deletions/uploads).
        # A previously approved git push must not authorize publishing
        # different commits later. Failure to bind degrades to a per-invocation
        # nonce, which forces re-approval instead of blanket-running.
        try:
            effect = risk.effect_identity(tool_name, args, assessment)
        except Exception:
            effect = "unbound-" + os.urandom(8).hex()
        if effect:
            shape = dict(assessment.shape or {})
            shape["effect_sha256"] = effect
            assessment.shape = shape

    if risk_preflight_enabled() and assessment.auto_allow:
        _record_gate_event(
            tool_name=tool_name, action="auto-allow",
            reason=f"deterministic:{assessment.category}", provider_call=False,
            risk_category=assessment.category, detail=assessment.display,
            **correlation,
        )
        return None

    if assessment.category == risk.PROHIBITED:
        message = format_block_message(assessment)
        if mode == "enforce":
            _record_gate_event(tool_name=tool_name, action="blocked",
                               reason="deterministic:prohibited", provider_call=False,
                               risk_category=assessment.category, detail=message,
                               **correlation)
            return {"action": "block", "message": message}
        if mode == "ask-only":
            rule_key = prefs.scoped_rule_key(assessment.category, tool_name, assessment.shape)
            _record_gate_event(tool_name=tool_name, action="escalated",
                               reason="deterministic:prohibited-ask-only", provider_call=False,
                               risk_category=assessment.category, rule_key=rule_key,
                               **correlation)
            return {"action": "approve",
                    "message": message + "\n(ask-only mode: a human may override this block.)",
                    "rule_key": rule_key}
        _record_gate_event(tool_name=tool_name, action="evaluated-advisory",
                           reason="deterministic:prohibited", provider_call=False,
                           risk_category=assessment.category, detail=message,
                           **correlation)
        return None

    if assessment.category == risk.CONSEQUENTIAL:
        if mode == "advisory":
            _record_gate_event(tool_name=tool_name, action="evaluated-advisory",
                               reason="deterministic:consequential", provider_call=False,
                               risk_category=assessment.category, detail=assessment.display,
                               **correlation)
            return None
        rule_key = prefs.scoped_rule_key(assessment.category, tool_name, assessment.shape)
        message = format_consequence_message(assessment)
        _record_gate_event(tool_name=tool_name, action="escalated",
                           reason="deterministic:consequential", provider_call=False,
                           risk_category=assessment.category, rule_key=rule_key,
                           detail=assessment.display, **correlation)
        return {"action": "approve", "message": message, "rule_key": rule_key}

    # ---- bounded uncertainty: run, record, do not interrupt --------------------
    if (assessment.category == risk.UNCERTAIN and assessment.bounded
            and mode in {"enforce", "ask-only"}):
        _record_gate_event(tool_name=tool_name, action="auto-allow",
                           reason="bounded-uncertainty", provider_call=False,
                           risk_category=assessment.category,
                           detail=assessment.display, **correlation)
        return None

    # ---- Layer 1.5: learned preference (uncertain band only) -------------------
    if learned_preferences_enabled() and prefs.learned_allow(
            assessment.category, tool_name, assessment.shape, preference_min_samples()):
        _record_gate_event(tool_name=tool_name, action="auto-allow",
                           reason="learned-preference", provider_call=False,
                           risk_category=assessment.category, detail=assessment.display,
                           **correlation)
        return None

    # ---- Layer 2: probabilistic model evaluation --------------------------------
    # P0 (0.4.1): the pre_tool_call hook is bounded by Hermes's 30s callback
    # timeout; on overrun the worker is abandoned and Hermes fails closed,
    # skipping subsequent tools. Therefore:
    #   - advisory mode NEVER waits on remote inference here: the evaluation is
    #     scheduled on the bounded advisory worker and its outcome is recorded
    #     as telemetry only (never retroactive authorize/block);
    #   - enforce/ask-only keep a SINGLE bounded synchronous attempt (no
    #     double retry) and fail to human approval when the provider stalls;
    #   - a provider stall opens a circuit-breaker cooldown (advisory) and
    #     deterministic verdicts cover the critical path meanwhile.
    result: DecisionResult | None = None
    provider_error: Exception | None = None
    sync_budget_s = _sync_provider_budget()
    if mode == "advisory":
        from . import advisory as _advisory
        scheduled = _advisory.submit(
            tool_name,
            {"tool_name": tool_name, "arguments": redact(args), "task_id": task_id or ""},
            evaluate=lambda: evaluate_tool_call(tool_name=tool_name, args=args, task_id=task_id),
            record=lambda evt: _record_gate_event(
                tool_name=tool_name,
                provider_call=True,
                risk_category=assessment.category,
                action=str(evt.get("outcome") or "advisory-async"),
                reason=str(evt.get("error_type") or "nonblocking-advisory"),
                value=evt.get("advisory_value"),
                latency_ms=evt.get("latency_ms"),
                detail=str(evt.get("model") or ""),
                **{k: v for k, v in correlation.items()}),
            correlation={k: str(v or "") for k, v in correlation.items()},
        )
        _record_gate_event(tool_name=tool_name,
                           action="advisory-scheduled" if scheduled else "advisory-dropped",
                           reason=("nonblocking-advisory" if scheduled
                                   else "cooldown-or-saturation"), provider_call=False,
                           risk_category=assessment.category,
                           detail=assessment.display, **correlation)
        return None
    if advisory.provider_available():
        # Hard-bounded synchronous attempt via the shared bounded executor:
        # the provider call runs on a tracked daemon thread with a
        # request-scoped timeout (never a process-global mutation), and the
        # pipeline joins it for at most the budget. A stalled evaluation is
        # abandoned in bounded numbers (advisory.MAX_ABANDONED) and opens the
        # circuit breaker, so no retry storm follows. ask-only/enforce safety
        # is preserved: overrun fails to human approval below.
        def _scoped_evaluate() -> Any:
            from .client import timeout_scope
            with timeout_scope(sync_budget_s):
                return evaluate_tool_call(tool_name=tool_name, args=args, task_id=task_id)

        result, _err, _stalled = advisory.run_bounded(_scoped_evaluate, sync_budget_s)
        provider_error = _err if isinstance(_err, Exception) else (
            RuntimeError(f"provider evaluation failed: {type(_err).__name__}") if _err else None)
        if provider_error is None and result is not None:
            advisory.note_provider_success()
        elif isinstance(provider_error, TimeoutError):
            # provider timeout / stall: open the breaker so an outage cannot
            # produce a retry storm of budget-consuming attempts
            advisory.note_provider_failure()
    else:
        provider_error = TimeoutError("provider circuit-breaker open after earlier stall")

    if result is None:
        _record_gate_event(tool_name=tool_name, action="provider-error",
                           reason="provider-unavailable", provider_call=True,
                           risk_category=assessment.category,
                           detail=str(provider_error)[:200] if provider_error else "",
                           **correlation)
        if mode in {"enforce", "ask-only"}:
            # Fail-to-human with an honest uncertainty message: the model could
            # not verify an unrecognized call. Deterministic safe classes above
            # were already handled, so routine work is unaffected by outages.
            rule_key = prefs.scoped_rule_key(assessment.category, tool_name, assessment.shape)
            message = format_approval_message(
                risk.RiskAssessment(
                    assessment.category, tool_name,
                    action_label="Unverified command (decision model unavailable)",
                    summary=assessment.display or assessment.summary,
                    why="this command is not in the recognized-safe catalog and the "
                        "decision model could not be reached",
                    impact="runs with this user's permissions if approved",
                    risk_notes="unverified behavior; the model usually evaluates this band",
                    reversibility=assessment.reversibility,
                    display=assessment.display or assessment.summary,
                ),
                uncertainty_note=_UNCERTAINTY_OUTAGE_NOTE,
            )
            return {"action": "approve", "message": message, "rule_key": rule_key}
        return None

    _record_gate_event(
        tool_name=tool_name,
        action="evaluated",
        reason="uncertain-band",
        provider_call=True,
        value=result.value,
        confidence=result.confidence,
        latency_ms=result.latency_ms,
        probabilities=result.probabilities,
        risk_category=assessment.category,
        **correlation,
    )
    if mode == "advisory":
        return None

    if result.value == "ALLOW":
        p_allow = _probability_of(result.probabilities, "ALLOW")
        floor = minimum_allow_probability()
        min_conf = minimum_confidence()
        strong = (p_allow is not None and p_allow >= floor
                  and result.confidence is not None and result.confidence >= min_conf)
        if strong:
            _record_gate_event(tool_name=tool_name, action="auto-allow",
                               reason="model:allow", provider_call=False, value="ALLOW",
                               confidence=result.confidence,
                               probabilities=result.probabilities,
                               risk_category=assessment.category,
                               detail=assessment.display, **correlation)
            return None
        # A weak-ALLOW verdict (low p(ALLOW), low confidence, or missing
        # calibration numbers) is NOT sufficient evidence that an unrecognized
        # command is safe: the deterministic scan found no hazard, but nobody
        # verified the operation either. Escalate with an honest uncertainty
        # explanation — never an invented danger, never a bare confidence
        # number as the reason. (Recognized routine work never reaches this
        # path; the reported 0.82-vs-0.90 case is deterministic-safe.)
        rule_key = prefs.scoped_rule_key(assessment.category, tool_name, assessment.shape)
        evidence = "; ".join(assessment.evidence[:2]) if assessment.evidence else ""
        message = format_approval_message(
            risk.RiskAssessment(
                assessment.category, tool_name,
                action_label=assessment.action_label or "Unrecognized command",
                summary=assessment.display or assessment.summary,
                why="this command is not in the recognized-safe catalog and the "
                    "decision model's assessment of it was weak",
                impact="runs with this user's permissions if approved",
                risk_notes="no specific danger was identified by the deterministic scan; "
                           "the interruption is about unverified uncertainty, not a detected threat",
                reversibility=assessment.reversibility,
                evidence=list(assessment.evidence),
                display=assessment.display or assessment.summary,
            ),
            model_note=_UNCERTAINTY_WEAK_ALLOW_NOTE,
        )
        if evidence:
            message += f"\nScan context: {evidence}"
        prefs.remember_pending(str(correlation.get("tool_call_id") or ""),
                               assessment.category, tool_name, assessment.shape)
        _record_gate_event(tool_name=tool_name, action="escalated",
                           reason=("model:weak-allow p(ALLOW)=%s confidence=%s"
                                   % (f"{p_allow:.3f}" if p_allow is not None else "n/a",
                                      f"{result.confidence:.2f}" if result.confidence is not None else "n/a")),
                           provider_call=False, value="ALLOW",
                           confidence=result.confidence,
                           probabilities=result.probabilities,
                           risk_category=assessment.category, rule_key=rule_key,
                           detail=assessment.display, **correlation)
        return {"action": "approve", "message": message, "rule_key": rule_key}

    if result.value == "APPROVAL":
        rule_key = prefs.scoped_rule_key(assessment.category, tool_name, assessment.shape)
        evidence = "; ".join(assessment.evidence[:2]) if assessment.evidence else ""
        message = format_approval_message(assessment, model_note=_UNCERTAINTY_MODEL_NOTE)
        if evidence:
            message += f"\nScan context: {evidence}"
        prefs.remember_pending(str(correlation.get("tool_call_id") or ""), assessment.category,
                               tool_name, assessment.shape)
        _record_gate_event(tool_name=tool_name, action="escalated",
                           reason="model:approval", provider_call=False,
                           value="APPROVAL", confidence=result.confidence,
                           probabilities=result.probabilities,
                           risk_category=assessment.category, rule_key=rule_key,
                           **correlation)
        return {"action": "approve", "message": message, "rule_key": rule_key}

    # BLOCK verdict
    if mode == "ask-only":
        rule_key = _block_approval_rule_key(tool_name, args)
        scan = "; ".join(assessment.evidence[:2]) if assessment.evidence else "no specific hazard"
        message = (
            f"APPROVAL REQUIRED (model BLOCK verdict) — {assessment.action_label}\n"
            f"Action: {assessment.display or assessment.summary}\n"
            f"Why: the decision model judged this action dangerous.\n"
            f"Risks: {scan}."
        )
        _record_gate_event(tool_name=tool_name, action="escalated",
                           reason="model:block-ask-only", provider_call=False,
                           value="BLOCK", confidence=result.confidence,
                           probabilities=result.probabilities,
                           risk_category=assessment.category, rule_key=rule_key,
                           **correlation)
        return {"action": "approve", "message": message, "rule_key": rule_key}
    if mode == "enforce":
        message = format_block_message(assessment)
        if not assessment.why:
            message = (
                f"BLOCKED — {assessment.action_label}\n"
                f"Action: {assessment.display or assessment.summary}\n"
                f"Why: the decision model judged this action dangerous; the deterministic "
                f"scan found no specific hazard to cite."
            )
        _record_gate_event(tool_name=tool_name, action="blocked",
                           reason="model:block", provider_call=False,
                           value="BLOCK", confidence=result.confidence,
                           probabilities=result.probabilities,
                           risk_category=assessment.category, detail=message,
                           **correlation)
        return {"action": "block", "message": message}
    _record_gate_event(tool_name=tool_name, action="evaluated-advisory",
                       reason="model:block-advisory", provider_call=False,
                       value="BLOCK", confidence=result.confidence,
                       probabilities=result.probabilities,
                       risk_category=assessment.category, **correlation)
    return None


def post_tool_call(**kwargs):
    """Preference learning: count operator-approved executions of a shape.

    A pending entry exists only for uncertain-band calls this gate escalated.
    If the call afterwards completed successfully, a human approved it (there
    is no other path past our escalation), and the exact shape gains one
    learned sample. Denials and errors never count. Generic Hermes approvals
    of unrelated keys never reach this path because pending is keyed by
    tool_call_id.
    """
    tool_call_id = str(kwargs.get("tool_call_id") or "")
    if not tool_call_id:
        return None
    entry = prefs.consume_pending(tool_call_id)
    if entry is None:
        return None
    if kwargs.get("error") or kwargs.get("exception"):
        return None
    result = kwargs.get("result")
    if isinstance(result, dict) and (result.get("error") or result.get("is_error")):
        return None
    prefs.record_approval(entry.get("category", "uncertain"),
                          entry.get("tool_name", str(kwargs.get("tool_name") or "")),
                          entry.get("shape") or {})
    return None


def report(path: Path | None = None, *, recent_limit: int = 8) -> dict[str, Any]:
    """Aggregate local gate telemetry without making a provider call."""
    selected = path or gate_event_path()
    rows = read_jsonl(selected)

    by_action: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    by_tool: dict[str, int] = {}
    by_risk: dict[str, int] = {}
    provider_calls = 0
    provider_latency_ms = 0.0
    for row in rows:
        action = str(row.get("action") or "unknown")
        reason = str(row.get("reason") or "unknown")
        tool = str(row.get("tool_name") or "unknown")
        by_action[action] = by_action.get(action, 0) + 1
        by_reason[reason] = by_reason.get(reason, 0) + 1
        by_tool[tool] = by_tool.get(tool, 0) + 1
        if row.get("risk_category"):
            rc = str(row["risk_category"])
            by_risk[rc] = by_risk.get(rc, 0) + 1
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

    # Lifetime totals are useful for cumulative accounting but can badly misstate
    # present health after an old outage. Keep them unchanged and add a bounded
    # recent window that answers the operational question: is the gate healthy now?
    health_window = rows[-500:]
    recent_actions: dict[str, int] = {}
    recent_provider_calls = 0
    recent_provider_latency_ms = 0.0
    for row in health_window:
        action = str(row.get("action") or "unknown")
        recent_actions[action] = recent_actions.get(action, 0) + 1
        if row.get("provider_call"):
            recent_provider_calls += 1
            try:
                recent_provider_latency_ms += float(row.get("latency_ms") or 0.0)
            except (TypeError, ValueError):
                pass
    recent_evaluated = recent_actions.get("evaluated", 0)
    recent_provider_errors = recent_actions.get("provider-error", 0)
    interruptions = by_action.get("escalated", 0) + by_action.get("blocked", 0)
    recent_health = {
        "window_events": len(health_window),
        "provider_calls": recent_provider_calls,
        "provider_errors": recent_provider_errors,
        "provider_error_rate": round(recent_provider_errors / recent_provider_calls, 6) if recent_provider_calls else 0.0,
        "evaluated": recent_evaluated,
        "bypassed": recent_actions.get("bypass", 0),
        "disabled": recent_actions.get("disabled", 0),
        "auto_allowed": recent_actions.get("auto-allow", 0),
        "escalated": recent_actions.get("escalated", 0),
        "blocked": recent_actions.get("blocked", 0),
        "average_provider_latency_ms": (
            round(recent_provider_latency_ms / recent_evaluated, 3) if recent_evaluated else 0.0
        ),
    }
    catalog_bypasses = by_reason.get("catalog-read-only", 0)
    primitive_bypasses = by_reason.get("nerve-internal", 0)
    uncatalogued_evaluations = sum(
        count for tool, count in by_tool.items()
        if catalog.classify_tool(tool, {}) is None and tool != "unknown")
    catalog_comparison = {
        "catalog_read_only_bypasses": catalog_bypasses,
        "internal_primitive_bypasses": primitive_bypasses,
        "uncatalogued_tools_seen": uncatalogued_evaluations,
        "interruptions": interruptions,
        "interruption_ratio": round(interruptions / len(rows), 4) if rows else 0.0,
        "bypass_ratio": round(bypassed / len(rows), 4) if rows else 0.0,
    }
    return {
        "path": str(selected),
        "metric_scope": "profile-lifetime gate-event ledger",
        "catalog": catalog_comparison,
        "gate_mode": gate_mode(),
        "event_count": len(rows),
        "scope": gate_scope(),
        "hook_observations": len(rows),
        "disabled": disabled,
        "bypassed": bypassed,
        "evaluated": evaluated,
        "auto_allowed": by_action.get("auto-allow", 0),
        "escalated": by_action.get("escalated", 0),
        "blocked": by_action.get("blocked", 0),
        "interruptions": interruptions,
        "interruption_rate": round(interruptions / len(rows), 6) if rows else 0.0,
        "provider_errors": by_action.get("provider-error", 0),
        "provider_calls": provider_calls,
        "provider_latency_ms": round(provider_latency_ms, 3),
        "average_provider_latency_ms": round(provider_latency_ms / evaluated, 3) if evaluated else 0.0,
        "estimated_provider_calls_avoided": bypassed + by_action.get("auto-allow", 0),
        "bypass_rate": round(bypassed / len(rows), 6) if rows else 0.0,
        "by_risk_category": dict(sorted(by_risk.items())),
        "recent_health": recent_health,
        "note": "event_count is gate-hook observations in this profile ledger; decision receipts use a separate profile-lifetime receipt ledger.",
        "by_action": dict(sorted(by_action.items())),
        "by_reason": dict(sorted(by_reason.items())),
        "by_tool": dict(sorted(by_tool.items())),
        "recent": rows[-limit:] if limit else [],
    }


# --- P0 (0.4.1): pre-tool hook budget and composite-chain instrumentation --------

_SYNC_PROVIDER_BUDGET_S = 8.0  # strictly below Hermes's 30s hook callback timeout


def _sync_provider_budget() -> float:
    """Hard synchronous deadline for enforce/ask-only provider attempts.

    One attempt only (retries removed): a stalled provider must fail to human
    approval in bounded time, never consume the 30-second hook budget.
    """
    try:
        raw = float(os.getenv("NERVE_SYNC_PROVIDER_BUDGET_S", "") or _SYNC_PROVIDER_BUDGET_S)
    except ValueError:
        raw = _SYNC_PROVIDER_BUDGET_S
    return max(1.0, min(raw, 15.0))


def _make_client(**kwargs):
    """Test seam: build the hosted Reflex client (patched in P0 tests)."""
    from .client import JevClient
    return JevClient(**kwargs)


def instrument_pre_tool_chain(callbacks, sink):
    """Wrap pre_tool_call callbacks with per-callback timing and failure policy.

    Callback entries are `(name, callback)` tuples or
    `(name, callback, critical)` triples. Each callback runs in isolation and
    the sanitized record (identity, duration_ms, outcome, tool name,
    session/tool-call ids — never arguments) is emitted to `sink`.

    Failure semantics are explicit rather than uniformly fail-open:
      - telemetry (`sink`) failures never block tools;
      - non-critical callbacks (remote control, work/nervous supervision) are
        advisory side integrations: a failure is recorded and the chain
        continues, so ordinary execution is not interrupted;
      - a CRITICAL callback (the action gate: approvals, deterministic
        safeguards) failing means the operation could not be verified. The
        chain then hands the decision to Hermes's native approval boundary
        (an `approve` directive) instead of permitting the call. Deterministic
        prohibitions still run inside the gate; this covers the case where the
        gate callback itself crashed before producing a verdict.
    """
    chain_start = time.monotonic()
    normalized = []
    for entry in callbacks:
        if len(entry) == 3:
            normalized.append((entry[0], entry[1], bool(entry[2])))
        else:
            normalized.append((entry[0], entry[1], False))

    def pre_tool_control(*args, **kwargs):
        kwargs = args[0] if args else kwargs
        chain_start = time.monotonic()
        for name, callback, critical in normalized:
            t0 = time.monotonic()
            outcome = "none"
            directive = None
            try:
                directive = callback(**kwargs)
                outcome = "none" if directive is None else "directive"
            except Exception as exc:
                outcome = f"exception:{type(exc).__name__}"
            duration_ms = round((time.monotonic() - t0) * 1000.0, 1)
            try:
                sink({
                    "schema": "nerve-pretool-trace/v1",
                    "callback": str(name)[:64],
                    "duration_ms": duration_ms,
                    "outcome": outcome,
                    "critical": critical,
                    "tool_name": str(kwargs.get("tool_name") or "")[:64],
                    "session_id": str(kwargs.get("session_id") or "")[:64],
                    "tool_call_id": str(kwargs.get("tool_call_id") or "")[:64],
                    "chain_elapsed_ms": round((time.monotonic() - chain_start) * 1000.0, 1),
                })
            except Exception:
                pass  # telemetry must never break the control chain
            if directive is not None:
                return directive
            if outcome.startswith("exception") and critical:
                # Security-critical callback failed before producing a
                # verdict: do not permit. Fail to the native human approval
                # boundary with an honest reason (never a silent allow).
                return {
                    "action": "approve",
                    "message": (
                        "APPROVAL REQUIRED — action gate unavailable\n"
                        f"Action: {str(kwargs.get('tool_name') or 'tool call')}\n"
                        f"Why: Nerve's action gate failed ({outcome.split(':', 1)[-1]}) "
                        "before it could verify this call.\n"
                        "If approved: runs with this user's permissions if approved\n"
                        "Risks: verification did not complete, so no assurance was "
                        "produced for this specific call."
                    ),
                    # Scoped + one-shot: a session/permanent grant for a gate
                    # failure must never authorize a later, different call.
                    "rule_key": (
                        "nerve:hitl:gate-failure:"
                        f"{str(kwargs.get('tool_name') or 'call')[:24]}:"
                        f"{risk._args_fingerprint(kwargs.get('arguments') or {})[:12]}:"
                        f"{os.urandom(4).hex()}"
                    ),
                    "source": "nerve-action-gate-failure",
                }
        return None

    return pre_tool_control
