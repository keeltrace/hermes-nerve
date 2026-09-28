from __future__ import annotations

import ast
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from .models import RunIdentity, WorkEvent

_SAFE_TEST_PREFIXES = (
    ("python", "-m", "pytest"),
    ("python3", "-m", "pytest"),
    ("pytest",),
    ("python", "-m", "unittest"),
    ("python3", "-m", "unittest"),
)
_PATH_RE = re.compile(r"`([^`]+(?:\.py|\.txt|\.md|\.json|\.yaml|\.yml|\.toml))`")
_CMD_RE = re.compile(r"`([^`]*(?:pytest|unittest)[^`]*)`")


def _terminal_exit(result: Any, kwargs: dict[str, Any]) -> tuple[int | None, str]:
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    status = str(kwargs.get("status") or "").lower()
    if status in {"error", "failed", "failure"}:
        return 1, text
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            for key in ("exit_code", "returncode", "code"):
                if payload.get(key) is not None:
                    return int(payload[key]), text
    except Exception:
        pass
    low = text.lower()
    if re.search(r"\b[1-9]\d* failed\b", low) or "traceback (most recent call last)" in low:
        return 1, text
    if re.search(r"\b\d+ passed\b", low) and " failed" not in low:
        return 0, text
    return None, text


def observe_test_result(supervisor, identity: RunIdentity, *, tool_name: str, args: dict[str, Any], result: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    if tool_name not in {"terminal", "terminal.exec", "shell", "bash"}:
        return {"observed": False, "failed": False}
    command = str((args or {}).get("command") or (args or {}).get("cmd") or "")
    if "pytest" not in command and "unittest" not in command:
        return {"observed": False, "failed": False}
    rc, text = _terminal_exit(result, kwargs)
    if rc is None:
        return {"observed": False, "failed": False}
    if rc == 0:
        supervisor.record_event(identity, WorkEvent("", "TEST_PASSED", payload={"command": command}, source="controller"))
        contract = supervisor.active_contract(identity.task_id)
        if contract:
            evidence = supervisor.store.evidence(identity)
            evidence_ids = [str(e["evidence_id"]) for e in evidence[-8:]]
            for criterion in contract.criteria:
                expected = _safe_command_from_description(criterion.description)
                if expected and shlex.split(command) == expected:
                    supervisor.mark_deterministic_verdict(
                        identity,
                        criterion.id,
                        passed=True,
                        reason=f"Deterministic test command passed: {command}",
                        evidence_ids=evidence_ids,
                    )
        return {"observed": True, "failed": False, "command": command}
    else:
        # Fingerprint the stable failing-test identity rather than the entire
        # pytest transcript (durations/counts make raw-output hashes too
        # brittle to recognize a repeated wrong path).
        failed_ids = re.findall(r"(?m)^FAILED\s+([^\s]+)", text)
        if failed_ids:
            # Keep one stable fingerprint per failing test identity. This survives
            # changing failure-set order/cardinality across successive pytest runs.
            failure_fingerprints = [supervisor.hash_value(test_id) for test_id in sorted(set(failed_ids))]
            fingerprint_basis = "\n".join(sorted(set(failed_ids)))
        else:
            normalized = re.sub(r"\b\d+(?:\.\d+)?s\b", "<time>", text[-3000:])
            normalized = re.sub(r"\b\d+\b", "<n>", normalized)
            fingerprint_basis = normalized
            failure_fingerprints = [supervisor.hash_value(fingerprint_basis)]
        fingerprint = supervisor.hash_value(fingerprint_basis)
        supervisor.record_event(
            identity,
            WorkEvent(
                "", "TEST_FAILED",
                payload={
                    "command": command,
                    "fingerprint": fingerprint,
                    "failed_ids": sorted(set(failed_ids)),
                    "failure_fingerprints": failure_fingerprints,
                },
                source="controller",
            ),
        )
        # A failing deterministic command is authoritative negative evidence for
        # the matching locked DoD criterion. dev7 only logged TEST_FAILED, leaving
        # the criterion UNKNOWN and causing the 40% checkpoint router to see a
        # falsely healthy trajectory.
        contract = supervisor.active_contract(identity.task_id)
        if contract:
            evidence = supervisor.store.evidence(identity)
            evidence_ids = [str(e["evidence_id"]) for e in evidence[-8:]]
            for criterion in contract.criteria:
                expected = _safe_command_from_description(criterion.description)
                if expected and shlex.split(command) == expected:
                    supervisor.mark_deterministic_verdict(
                        identity,
                        criterion.id,
                        passed=False,
                        reason=f"Deterministic test command failed: {command}",
                        evidence_ids=evidence_ids,
                    )
        return {
            "observed": True,
            "failed": True,
            "command": command,
            "fingerprint": fingerprint,
            "failure_fingerprints": failure_fingerprints,
            "failed_ids": sorted(set(failed_ids)),
        }


_SYMBOL_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")


def _git_text(workspace: str, revision: str, relpath: str) -> str | None:
    rc, out = _git(workspace, "show", f"{revision}:{relpath}")
    return out if rc == 0 else None


def _python_paths_at_revision(workspace: str, revision: str) -> list[str]:
    rc, out = _git(workspace, "ls-tree", "-r", "--name-only", revision)
    if rc != 0:
        return []
    return [x.strip() for x in out.splitlines() if x.strip().endswith(".py")]


def _annotation(node: ast.AST | None) -> str:
    return ast.dump(node, include_attributes=False) if node is not None else ""


def _arguments_signature(args: ast.arguments) -> dict[str, Any]:
    def arg(a: ast.arg) -> tuple[str, str]:
        return (a.arg, _annotation(a.annotation))
    return {
        "posonly": [arg(a) for a in args.posonlyargs],
        "args": [arg(a) for a in args.args],
        "vararg": arg(args.vararg) if args.vararg else None,
        "kwonly": [arg(a) for a in args.kwonlyargs],
        "kwarg": arg(args.kwarg) if args.kwarg else None,
        "defaults": [ast.dump(x, include_attributes=False) for x in args.defaults],
        "kw_defaults": [ast.dump(x, include_attributes=False) if x is not None else None for x in args.kw_defaults],
    }


def _symbol_signature(source: str, symbol: str) -> dict[str, Any] | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol:
            return {
                "kind": "function",
                "args": _arguments_signature(node.args),
                "returns": _annotation(node.returns),
            }
        if isinstance(node, ast.ClassDef) and node.name == symbol:
            methods = {}
            fields = []
            decorators = []
            for deco in node.decorator_list:
                try:
                    decorators.append(ast.unparse(deco))
                except Exception:
                    decorators.append(ast.dump(deco, include_attributes=False))
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and (child.name == "__init__" or not child.name.startswith("_")):
                    methods[child.name] = {
                        "args": _arguments_signature(child.args),
                        "returns": _annotation(child.returns),
                    }
                elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                    fields.append((child.target.id, _annotation(child.annotation)))
            return {
                "kind": "class",
                "methods": methods,
                "fields": fields,
                "decorators": decorators,
            }
    return None


def _find_symbol_signature(workspace: str, *, revision: str | None, symbol: str) -> tuple[str, dict[str, Any]] | None:
    if revision:
        paths = _python_paths_at_revision(workspace, revision)
        for rel in paths:
            source = _git_text(workspace, revision, rel)
            if source is None:
                continue
            sig = _symbol_signature(source, symbol)
            if sig is not None:
                return rel, sig
        return None
    root = Path(workspace)
    # Prefer tracked files so generated/untracked helper scripts cannot satisfy
    # a public-interface criterion by accident.
    rc, out = _git(workspace, "ls-files", "*.py")
    paths = [x.strip() for x in out.splitlines() if x.strip()] if rc == 0 else [str(x.relative_to(root)) for x in root.rglob("*.py")]
    for rel in paths:
        path = root / rel
        try:
            source = path.read_text()
        except Exception:
            continue
        sig = _symbol_signature(source, symbol)
        if sig is not None:
            return rel, sig
    return None


def _verify_public_signatures(workspace: str, base: str, description: str) -> tuple[bool | None, str]:
    if not base:
        return None, "No baseline revision is available."
    symbols = _SYMBOL_RE.findall(description)
    if not symbols:
        return None, "No public symbols were named in the criterion."
    checked = []
    for symbol in symbols:
        before = _find_symbol_signature(workspace, revision=base, symbol=symbol)
        after = _find_symbol_signature(workspace, revision=None, symbol=symbol)
        if before is None or after is None:
            return False, f"Could not resolve public symbol {symbol!r} at baseline/current revision."
        before_path, before_sig = before
        after_path, after_sig = after
        if before_sig != after_sig:
            return False, f"Public signature changed for {symbol}: baseline={before_path}, current={after_path}."
        checked.append(f"{symbol}@{after_path}")
    return True, "Public signatures match baseline exactly: " + ", ".join(checked)


def _test_functions(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text())
    except Exception:
        return []
    return [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")]


def _verify_regression_test_count(workspace: str, description: str) -> tuple[bool | None, str]:
    paths = _PATH_RE.findall(description)
    if not paths:
        return None, "No regression-test path found."
    match = re.search(r"at least\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten)", description, re.I)
    if not match:
        return None, "No minimum regression-test count found."
    raw_required = match.group(1).lower()
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
    required = int(raw_required) if raw_required.isdigit() else words[raw_required]
    total = 0
    found = []
    for rel in paths:
        if "test" not in rel.lower():
            continue
        path = Path(workspace) / rel
        if not path.is_file():
            return False, f"Required regression-test file is missing: {rel}"
        names = _test_functions(path)
        total += len(names)
        found.append(f"{rel}:{len(names)}")
    if not found:
        return None, "No test file path was recognized."
    return total >= required, f"Regression tests found {total}, required {required} ({', '.join(found)})."


def _semantic_test_coverage(workspace: str, description: str) -> tuple[bool | None, str, list[str]]:
    """Prove retry/idempotency semantics from focused executable tests.

    dev12 overfit the evidence recognizer to exact test-name vocabulary and
    required a retry-queue clause that was not actually present in DOD-07. The
    live worker had strong behavioral tests (``retry_then_success_stable``,
    ``dead_event_short_circuits_without_reattempt``, etc.) but they were ignored,
    which forced a semantic completion call to veto machine-proven work.

    dev13 recognizes equivalent behavioral names, requires only clauses stated by
    the locked criterion, and re-runs the selected node ids. The names only select
    candidate executable proofs; the tests themselves must pass.
    """
    low = description.lower()
    required_words = ("transient", "duplicate", "dead", "idempotent")
    if not all(word in low for word in required_words):
        return None, "Criterion is not the retry/idempotency semantic shape.", []
    root = Path(workspace) / "tests"
    if not root.is_dir():
        return False, "tests/ directory is missing.", []
    names: list[tuple[str, str]] = []
    for path in sorted(root.rglob("test*.py")):
        for name in _test_functions(path):
            names.append((str(path.relative_to(Path(workspace))), name.lower()))

    clauses = {
        "transient_no_loss": lambda n: (
            ("transient" in n and ("deliver" in n or "loss" in n or "success" in n))
            or ("retry_then_success" in n and ("deliver" in n or "stable" in n or "success" in n))
        ),
        "duplicate_success": lambda n: (
            ("duplicate" in n and ("deliver" in n or "success" in n))
            or "never_redeliver" in n
            or "never_redelivered" in n
            or "not_duplicate" in n
        ),
        "dead_idempotent": lambda n: (
            "dead" in n
            and any(token in n for token in ("idempot", "repeated", "repeat", "reattempt", "short_circuit", "without_reattempt"))
        ),
        "retry_idempotent": lambda n: (
            "retry" in n
            and any(token in n for token in ("idempot", "repeated", "repeat", "stable", "dedup", "duplicate", "queue"))
        ),
    }
    hits: dict[str, list[str]] = {k: [] for k in clauses}
    for rel, name in names:
        nodeid = f"{rel}::{name}"
        for clause, pred in clauses.items():
            if pred(name):
                hits[clause].append(nodeid)
    missing = [k for k, v in hits.items() if not v]
    if missing:
        return False, "Focused semantic tests missing clauses: " + ", ".join(missing), []
    selected: list[str] = []
    for clause in clauses:
        nodeid = hits[clause][0]
        if nodeid not in selected:
            selected.append(nodeid)
    cmd = ["python3", "-m", "pytest", *selected, "-q"]
    rc, output = _run(cmd, workspace)
    detail = "; ".join(f"{k}={v[0]}" for k, v in hits.items())
    if rc != 0:
        return False, f"Focused idempotency tests failed ({detail}).", selected
    return True, f"Focused semantic clauses proved and passing ({detail}); {output.strip()[-300:]}", selected


def _retry_idempotency_probe(workspace: str, description: str) -> tuple[bool | None, str, dict[str, Any]]:
    """Run controller-owned DOD-07 behavior probes directly against the implementation.

    dev13 still inferred DOD-07 from worker-authored test names. The live dev13
    run proved why that is insufficient: the worker's suite was green while
    re-dispatching an already-dead event mutated its attempt counter. dev14
    executes the locked semantics itself, in-memory and without modifying the
    workspace, so the criterion is based on observed behavior rather than prose
    or test vocabulary.
    """
    low = description.lower()
    required_words = ("transient", "duplicate", "dead", "idempotent")
    if not all(word in low for word in required_words):
        return None, "Criterion is not the retry/idempotency semantic shape.", {}

    probe = r'''
import json
from delivery import DeliveryStore, Dispatcher, Event


def emit_fail(probe, detail, **extra):
    payload = {"ok": False, "probe": probe, "detail": detail}
    payload.update(extra)
    print(json.dumps(payload, sort_keys=True))
    raise SystemExit(1)


def require(condition, probe, detail, **extra):
    if not condition:
        emit_fail(probe, detail, **extra)

# 1. Once dead, repeated dispatch is a pure terminal read: no new attempt and
# no handler invocation.
dead_store = DeliveryStore()
dead_store.put(Event("dead", "payload"))
dead_calls = []
def always_fail(event):
    dead_calls.append(event.event_id)
    raise RuntimeError("hard failure")
dead_dispatcher = Dispatcher(dead_store, always_fail, max_attempts=2)
r1 = dead_dispatcher.dispatch("dead")
r2 = dead_dispatcher.dispatch("dead")
require(str(r1).startswith("retry:"), "dead_event_redispatch", f"first dispatch returned {r1!r}; expected retry")
require(r2 == "dead", "dead_event_redispatch", f"threshold dispatch returned {r2!r}; expected 'dead'")
require(dead_store.dead("dead"), "dead_event_redispatch", "event was not marked dead at threshold")
before_attempts = dead_store.attempts("dead")
before_calls = len(dead_calls)
r3 = dead_dispatcher.dispatch("dead")
after_attempts = dead_store.attempts("dead")
after_calls = len(dead_calls)
require(r3 == "dead", "dead_event_redispatch", f"dead-event redispatch returned {r3!r}; expected 'dead'")
require(
    after_attempts == before_attempts,
    "dead_event_redispatch",
    f"dead-event redispatch changed attempts {before_attempts}->{after_attempts}; expected unchanged after event is dead",
    before_attempts=before_attempts,
    after_attempts=after_attempts,
)
require(
    after_calls == before_calls,
    "dead_event_redispatch",
    f"dead-event redispatch invoked handler again ({before_calls}->{after_calls} calls); expected no handler invocation",
    before_calls=before_calls,
    after_calls=after_calls,
)

# 2. Once delivered, repeated dispatch must be duplicate with no handler or
# attempt mutation.
delivered_store = DeliveryStore()
delivered_store.put(Event("delivered", "payload"))
delivered_calls = []
delivered_dispatcher = Dispatcher(delivered_store, lambda event: delivered_calls.append(event.event_id))
require(delivered_dispatcher.dispatch("delivered") == "delivered", "delivered_redispatch", "initial successful dispatch did not deliver")
delivered_attempts = delivered_store.attempts("delivered")
require(delivered_dispatcher.dispatch("delivered") == "duplicate", "delivered_redispatch", "delivered-event redispatch did not return 'duplicate'")
require(len(delivered_calls) == 1, "delivered_redispatch", f"delivered-event redispatch invoked handler {len(delivered_calls)} times; expected 1 total call")
require(delivered_store.attempts("delivered") == delivered_attempts, "delivered_redispatch", f"delivered-event redispatch changed attempts {delivered_attempts}->{delivered_store.attempts('delivered')}")

# 3. A transient failure must remain retryable, then deliver exactly once, and
# subsequent dispatch must be stable duplicate behavior.
retry_store = DeliveryStore()
retry_store.put(Event("retry", "payload"))
retry_calls = []
def fail_once(event):
    retry_calls.append(event.event_id)
    if len(retry_calls) == 1:
        raise RuntimeError("transient")
retry_dispatcher = Dispatcher(retry_store, fail_once, max_attempts=3)
rr1 = retry_dispatcher.dispatch("retry")
rr2 = retry_dispatcher.dispatch("retry")
retry_attempts = retry_store.attempts("retry")
rr3 = retry_dispatcher.dispatch("retry")
require(str(rr1).startswith("retry:"), "retry_then_success", f"transient failure returned {rr1!r}; expected retry")
require(rr2 == "delivered", "retry_then_success", f"retry after transient failure returned {rr2!r}; expected 'delivered'")
require(rr3 == "duplicate", "retry_then_success", f"post-success redispatch returned {rr3!r}; expected 'duplicate'")
require(retry_calls == ["retry", "retry"], "retry_then_success", f"handler calls were {retry_calls!r}; expected exactly two calls")
require(retry_store.attempts("retry") == retry_attempts, "retry_then_success", "post-success redispatch changed attempt count")
require(retry_store.delivered("retry") and not retry_store.dead("retry"), "retry_then_success", "retry-success terminal state is inconsistent")

# 4. Retry queue work must remain unique under repeated enqueue observation.
queue_store = DeliveryStore()
queue_store.put(Event("queue", "payload"))
queue_store.enqueue_retry("queue")
queue_store.enqueue_retry("queue")
queue_store.enqueue_retry("queue")
queue_items = queue_store.retry_items()
require(queue_items == ["queue"], "retry_queue_unique", f"retry queue contains duplicate work: {queue_items!r}; expected ['queue']")

print(json.dumps({
    "ok": True,
    "probes": [
        "dead_event_redispatch",
        "delivered_redispatch",
        "retry_then_success",
        "retry_queue_unique",
    ],
    "dead_attempts": dead_store.attempts("dead"),
    "retry_attempts": retry_store.attempts("retry"),
}, sort_keys=True))
'''
    rc, output = _run(["python3", "-c", probe], workspace, timeout=30)
    payload: dict[str, Any] = {}
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except Exception:
            continue
        if isinstance(value, dict) and "ok" in value:
            payload = value
            break
    if rc == 0 and payload.get("ok") is True:
        probes = ", ".join(str(x) for x in payload.get("probes") or [])
        return True, f"Controller DOD-07 behavioral probe passed: {probes}.", payload
    detail = str(payload.get("detail") or output.strip()[-1200:] or "behavioral probe failed")
    probe_name = str(payload.get("probe") or "retry_idempotency")
    return False, f"Controller DOD-07 behavioral probe failed [{probe_name}]: {detail}", payload

def _is_completion_summary_criterion(description: str) -> bool:
    low = description.lower()
    return "final completion summary" in low or ("summary" in low and "evidence" in low)


def _verify_completion_summary(description: str, proposal: dict[str, Any] | None, criterion_ids: list[str]) -> tuple[bool | None, str]:
    """Legacy/model-authored summary validator retained for compatibility tests.

    dev13 no longer *depends* on model prose for a machine-verifiable summary
    criterion; the harness synthesizes the canonical summary from authoritative
    verdicts. This helper remains useful when validating an externally supplied
    summary.
    """
    if not _is_completion_summary_criterion(description):
        return None, "Criterion is not a completion-summary criterion."
    p = proposal or {}
    text = str(p.get("final_response") or p.get("response") or p.get("summary") or p.get("result") or "").strip()
    if not text:
        return False, "Completion summary is empty."
    text_low = text.lower()
    command_ok = "python3 -m pytest tests/ -q" in text_low or "pytest tests/ -q" in text_low
    result_ok = bool(re.search(r"\b\d+\s+passed\b", text_low)) and "failed" not in text_low
    missing_ids = [cid for cid in criterion_ids if cid.lower() not in text_low]
    if not command_ok:
        return False, "Completion summary does not include the exact full-suite pytest command."
    if not result_ok:
        return False, "Completion summary does not include a passing pytest result."
    if missing_ids:
        return False, "Completion summary lacks per-criterion evidence labels: " + ", ".join(missing_ids)
    return True, "Completion summary includes the test command/result and evidence for every locked criterion."


def canonical_completion_summary(supervisor, identity: RunIdentity) -> str | None:
    """Build the terminal summary from locked criteria + authoritative verdicts.

    The summary is generated by the controller, not trusted from the worker's
    narrative. It is returned only when every required criterion is VERIFIED_PASS.
    The exact full-suite command/result is re-run and embedded when the contract
    contains such a command, satisfying summary-format DoD without another model
    turn.
    """
    contract = supervisor.active_contract(identity.task_id)
    if contract is None:
        return None
    verdicts = supervisor.store.latest_contract_verdicts(identity.task_id, identity.contract_hash)
    for criterion in contract.criteria:
        if criterion.required and str((verdicts.get(criterion.id) or {}).get("state")) != "VERIFIED_PASS":
            return None

    ctx = supervisor.store.run_context(identity)
    workspace = str(ctx.get("workspace_path") or os.getenv("HERMES_KANBAN_WORKSPACE") or os.getenv("TERMINAL_CWD") or "")
    lines = ["Hermes-Jev deterministic completion evidence:"]
    for criterion in contract.criteria:
        row = verdicts.get(criterion.id) or {}
        reason = str(row.get("reason") or "Verified.").strip()
        lines.append(f"{criterion.id}: VERIFIED_PASS — {reason}")

    command: list[str] | None = None
    command_criterion_id = ""
    for criterion in contract.criteria:
        command = _safe_command_from_description(criterion.description)
        if command:
            command_criterion_id = criterion.id
            break
    if command:
        command_text = " ".join(command)
        output = ""
        rc: int | None = None
        # Reuse the authoritative DOD-01 deterministic evidence from this same
        # completion pass. This avoids re-running the entire suite once to prove
        # DOD-01, again to synthesize DOD-08, and again during native dispatch.
        evidence = supervisor.store.evidence(identity, criterion_id=command_criterion_id)
        for row in reversed(evidence):
            if row.get("kind") != "deterministic_check" or bool(row.get("is_error")):
                continue
            preview = str(row.get("preview") or "")
            if preview.endswith("..."):
                continue
            try:
                payload = json.loads(preview)
            except Exception:
                continue
            cmd_value = payload.get("command")
            cmd_text = " ".join(str(x) for x in cmd_value) if isinstance(cmd_value, list) else str(cmd_value or "")
            if cmd_text != command_text:
                continue
            try:
                rc = int(payload.get("returncode"))
            except Exception:
                rc = None
            output = str(payload.get("output") or "")
            break
        if rc is None and workspace and Path(workspace).is_dir():
            rc, output = _run(command, workspace)
        if rc != 0:
            return None
        matches = re.findall(r"(?m)^([^\n]*(?:passed)[^\n]*)$", output)
        if not matches:
            return None
        result_line = matches[-1].strip()
        lines.append(f"Exact full-suite command: {command_text}")
        lines.append(f"Exact full-suite result: {result_line}")
    return "\n".join(lines)


def _safe_command_from_description(description: str) -> list[str] | None:
    for raw in _CMD_RE.findall(description):
        try:
            parts = shlex.split(raw)
        except ValueError:
            continue
        for prefix in _SAFE_TEST_PREFIXES:
            if tuple(parts[: len(prefix)]) == prefix:
                return parts
    return None


def _run(parts: list[str], workspace: str, timeout: int = 180) -> tuple[int, str]:
    try:
        p = subprocess.run(parts, cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout, check=False)
        return int(p.returncode), p.stdout[-12000:]
    except Exception as exc:
        return 127, f"{type(exc).__name__}: {exc}"


def _git(workspace: str, *args: str) -> tuple[int, str]:
    return _run(["git", *args], workspace, timeout=20)


def _evaluate_criterion(supervisor, identity: RunIdentity, criterion, *, workspace: str, base: str) -> tuple[bool | None, str]:
    """Evaluate one machine-checkable locked criterion without model judgment."""
    desc = criterion.description
    low = desc.lower()
    passed: bool | None = None
    reason = ""
    # Dev16: the locked token target is itself a machine-verifiable DoD item.
    # Worker prose cannot waive it; only an explicit contract amendment can
    # change the target.
    if str(getattr(criterion, "id", "")) == "DOD-BUDGET" or "locked worker token target" in low:
        contract = supervisor.active_contract(identity.task_id)
        if contract is None:
            return None, "No active contract is available for token-budget verification."
        consumed = supervisor.store.api_usage_total(identity)
        target = max(1, int(contract.allocated_tokens))
        try:
            from .runtime import settings as _work_settings
            tolerance = max(1.0, float(_work_settings().get("budget_dod_tolerance", 1.10)))
        except Exception:
            tolerance = 1.10
        ceiling = int(target * tolerance)
        passed = consumed <= ceiling
        reason = (
            f"Worker token budget {'passed' if passed else 'exceeded'}: {consumed}/{target} accounted tokens "
            f"({consumed / target:.3f}x target), locked completion ceiling={ceiling} ({tolerance:.2f}x)."
        )
        supervisor.observe_evidence(
            identity,
            value={"consumed_tokens": consumed, "token_target": target, "completion_ceiling": ceiling, "tolerance": tolerance, "fraction": consumed / target},
            kind="deterministic_token_budget", criterion_id=criterion.id,
            source="controller_observed", tool_name="nerve_observer", is_error=not passed,
        )
        return passed, reason

    command = _safe_command_from_description(desc)
    if command:
        rc, output = _run(command, workspace)
        supervisor.observe_evidence(
            identity,
            value={"command": command, "returncode": rc, "output": output},
            kind="deterministic_check", criterion_id=criterion.id,
            source="controller_observed", tool_name="deterministic",
            is_error=rc != 0,
        )
        passed = rc == 0
        reason = f"Safe deterministic command {'passed' if passed else 'failed'}: {' '.join(command)}"
    elif "launched by paperclip" in low or "nerve detected the paperclip" in low:
        # Nerve-operation claims are controller facts, not judge prose: the
        # worker process either carries the Paperclip task/run env plus a
        # durable startup binding (Paperclip launched it and Nerve bound it)
        # or it does not. An interactive manual Hermes session has neither.
        env_task = str(os.getenv("PAPERCLIP_TASK_ID") or "").strip()
        env_run = str(os.getenv("PAPERCLIP_RUN_ID") or "").strip()
        binding = supervisor.store.current_identity(identity.task_id)
        passed = bool(
            env_task
            and env_task == identity.task_id
            and binding is not None
            and binding.contract_hash == identity.contract_hash
        )
        reason = (
            "Controller observed Paperclip run context: "
            f"PAPERCLIP_TASK_ID={env_task or '<missing>'}, PAPERCLIP_RUN_ID={'present' if env_run else '<missing>'}, "
            f"startup binding={'present' if binding is not None else '<missing>'}."
        )
        supervisor.observe_evidence(
            identity,
            value={"paperclip_task_env": bool(env_task), "run_id_env": bool(env_run), "startup_binding": binding is not None},
            kind="deterministic_paperclip_context", criterion_id=criterion.id,
            source="controller_observed", tool_name="deterministic",
            is_error=not passed,
        )
    elif "at least" in low and "regression test" in low:
        passed, reason = _verify_regression_test_count(workspace, desc)
    elif "clean git status" in low or ("committed" in low and "clean" in low):
        rc, out = _git(workspace, "status", "--porcelain")
        head_rc, head = _git(workspace, "rev-parse", "HEAD")
        passed = rc == 0 and not out.strip() and head_rc == 0 and (not base or head.strip() != base)
        reason = "Git working tree is clean and HEAD differs from the run baseline." if passed else "Git commit/clean-status requirement is not satisfied."
    elif "no new dependencies" in low or "no new dependency" in low:
        if base:
            rc, out = _git(workspace, "diff", "--name-only", f"{base}..HEAD")
            dependency_files = {
                "requirements.txt", "pyproject.toml", "poetry.lock", "uv.lock", "pdm.lock",
                "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
            }
            changed = {Path(x.strip()).name for x in out.splitlines() if x.strip()}
            passed = rc == 0 and not bool(changed & dependency_files)
            reason = "No dependency manifest changed from the run baseline." if passed else "A dependency manifest changed from the run baseline."
    elif "public signatures" in low and "remain compatible" in low:
        passed, reason = _verify_public_signatures(workspace, base, desc)
    elif all(word in low for word in ("transient", "duplicate", "dead", "idempotent")):
        passed, reason, probe_payload = _retry_idempotency_probe(workspace, desc)
        if passed is not None:
            supervisor.observe_evidence(
                identity,
                value={"probe": probe_payload, "passed": bool(passed), "reason": reason},
                kind="deterministic_behavior_probe", criterion_id=criterion.id,
                source="controller_observed", tool_name="deterministic",
                is_error=not bool(passed),
            )
    elif "byte-identical" in low or "must not be edited" in low or "must not appear in the diff" in low:
        paths = _PATH_RE.findall(desc)
        if base and paths:
            ok = True
            for rel in paths:
                rc, out = _git(workspace, "diff", f"{base}..HEAD", "--", rel)
                if rc != 0 or out.strip():
                    ok = False
                    break
            passed = ok
            reason = "Protected file(s) are unchanged from the run baseline." if passed else "A protected file changed from the run baseline."
    elif "exists" in low:
        paths = _PATH_RE.findall(desc)
        if paths:
            passed = all((Path(workspace) / rel).is_file() for rel in paths)
            reason = "Required file(s) exist." if passed else "A required file is missing."
    return passed, reason


def auto_verify_completion(supervisor, identity: RunIdentity, proposal: dict[str, Any] | None = None) -> None:
    """Refresh deterministic DoD verdicts, with controller-authored summary last.

    dev13 treats machine-verifiable facts as authoritative. Non-summary criteria
    are evaluated first. A completion-summary criterion is then VERIFIED_PASS only
    when all other required criteria are already machine-verified and the
    controller can synthesize a valid canonical terminal summary. No model prose
    is required to unlock completion.
    """
    contract = supervisor.active_contract(identity.task_id)
    if contract is None:
        return
    ctx = supervisor.store.run_context(identity)
    workspace = str(ctx.get("workspace_path") or os.getenv("HERMES_KANBAN_WORKSPACE") or os.getenv("TERMINAL_CWD") or "")
    if not workspace or not Path(workspace).is_dir():
        return
    base = str(ctx.get("base_revision") or "")

    # Phase 1: all objective criteria except the summary that the harness itself
    # is responsible for emitting during native completion.
    current = supervisor.store.latest_contract_verdicts(identity.task_id, identity.contract_hash)
    summary_criteria = []
    for criterion in contract.criteria:
        if _is_completion_summary_criterion(criterion.description):
            summary_criteria.append(criterion)
            continue
        # Re-check deterministic FAILs as work may have changed since the last
        # attempt. Stable PASS facts may be reused under the same locked contract.
        if str((current.get(criterion.id) or {}).get("state")) == "VERIFIED_PASS":
            continue
        passed, reason = _evaluate_criterion(
            supervisor, identity, criterion, workspace=workspace, base=base
        )
        if passed is not None:
            evidence = supervisor.store.evidence(identity, criterion_id=criterion.id)
            supervisor.mark_deterministic_verdict(
                identity,
                criterion.id,
                passed=bool(passed),
                reason=reason,
                evidence_ids=[str(e["evidence_id"]) for e in evidence[-16:]],
            )

    # Phase 2: summary is controller-owned. It may pass only after every other
    # required criterion is VERIFIED_PASS and a real canonical summary can be
    # generated from those verdicts plus a fresh full-suite result.
    for criterion in summary_criteria:
        verdicts = supervisor.store.latest_contract_verdicts(identity.task_id, identity.contract_hash)
        blockers = [
            c.id for c in contract.criteria
            if c.required and c.id != criterion.id
            and str((verdicts.get(c.id) or {}).get("state")) != "VERIFIED_PASS"
        ]
        if blockers:
            # Summary is downstream of the substantive DoD. Leave it UNKNOWN while
            # blockers remain instead of creating a noisy deterministic FAIL that
            # tempts the worker to rewrite prose before fixing the real criterion.
            continue

        # Temporarily mark the summary criterion pass so canonical builder can
        # verify the whole required projection; if synthesis fails we supersede
        # it immediately with FAIL below.
        supervisor.mark_deterministic_verdict(
            identity, criterion.id, passed=True,
            reason="Controller can synthesize the canonical final completion summary from authoritative DoD evidence.",
            evidence_ids=[],
        )
        summary = canonical_completion_summary(supervisor, identity)
        if summary is None:
            supervisor.mark_deterministic_verdict(
                identity, criterion.id, passed=False,
                reason="Canonical completion summary synthesis or fresh full-suite verification failed.",
                evidence_ids=[],
            )
            continue
        evidence_result = supervisor.observe_evidence(
            identity,
            value={"summary": summary},
            kind="deterministic_completion_summary", criterion_id=criterion.id,
            source="controller_observed", tool_name="deterministic",
        )
        evidence = supervisor.store.evidence(identity, criterion_id=criterion.id)
        supervisor.mark_deterministic_verdict(
            identity,
            criterion.id,
            passed=True,
            reason="Canonical completion summary synthesized with per-DoD evidence and fresh exact full-suite command/result.",
            evidence_ids=[str(e["evidence_id"]) for e in evidence[-16:]],
        )
