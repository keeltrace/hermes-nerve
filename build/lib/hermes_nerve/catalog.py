"""Operation-aware tool catalog (Nerve 0.4.1, P1).

Name-only classification is unsound: ``skill_manage`` can create or delete a
skill, ``nerve_*`` tools can record state or dispatch work, and an MCP tool's
effects live in its operation, not its namespace. This module classifies the
*operation*: the tool identity plus the argument fields that determine what
actually happens.

Design rules
------------
1. No blanket namespace bypasses. Every Nerve/Jev/skill/MCP tool maps to an
   explicit operation spec; an unrecognized operation is UNCERTAIN, never
   auto-allowed.
2. Read operations with bounded, reversible effects are SAFE. Mutations inside
   the project are PROJECT. Anything that deletes, executes remote code,
   changes credentials, publishes, or is hard to reverse is CONSEQUENTIAL or
   PROHIBITED.
3. The catalog is auditable: ``audit_catalog`` reports coverage against a live
   tool inventory so new tools cannot silently fall through to UNCERTAIN.
4. Specs never inspect secret values; they only read structural argument keys.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import risk

SAFE = risk.SAFE
PROJECT = risk.PROJECT
UNCERTAIN = risk.UNCERTAIN
CONSEQUENTIAL = risk.CONSEQUENTIAL
PROHIBITED = risk.PROHIBITED

READ = "read"
WRITE = "write"
EXEC = "exec"
NETWORK = "network"
ADMIN = "admin"


@dataclass(frozen=True)
class Operation:
    """One operation shape for a tool, matched against its arguments."""

    name: str
    category: str
    effect: str
    impact: str
    why: str = ""
    reversibility: str = risk.REVERSIBLE
    match: Callable[[dict[str, Any]], bool] = field(default=lambda args: True)
    paths_from: tuple[str, ...] = ()
    label: str = ""

    def matches(self, args: dict[str, Any]) -> bool:
        try:
            return bool(self.match(args))
        except Exception:
            return False


def _action_is(*values: str) -> Callable[[dict[str, Any]], bool]:
    wanted = {v.lower() for v in values}
    return lambda args: str(args.get("action") or args.get("operation")
                            or args.get("command") or "").strip().lower() in wanted


def _absent(*keys: str) -> Callable[[dict[str, Any]], bool]:
    return lambda args: not any(k in args for k in keys)


_SUBJECT_KEYS = ("path", "file_path", "file", "target", "directory", "dir", "name",
                 "skill", "skill_name", "tool", "tool_name", "server", "url")

# --- explicit tool specifications ------------------------------------------------

TOOLS: dict[str, tuple[Operation, ...]] = {
    # Hermes native read tools
    "read_file": (Operation("read", SAFE, READ, "file contents enter the session context",
                            paths_from=("path", "file_path", "file")),),
    "read_many_files": (Operation("read-many", SAFE, READ, "file contents enter the session context",
                                  paths_from=("paths",)),),
    "search_files": (Operation("search", SAFE, READ, "matching paths/lines enter the context"),),
    "write_file": (Operation("write", PROJECT, WRITE, "the file changes inside the project scope",
                             paths_from=("path", "file_path"), reversibility=risk.REVERSIBLE),),
    "patch": (Operation("patch", PROJECT, WRITE, "the file changes inside the project scope",
                        paths_from=("path",), reversibility=risk.REVERSIBLE),),
    # Skills: read vs authoring are different operations
    "skills_list": (Operation("list", SAFE, READ, "the skill index enters the context"),),
    "skill_view": (Operation("view", SAFE, READ, "skill documentation enters the context",
                             paths_from=("name", "file_path")),),
    "skill_manage": (
        Operation("manage-view", SAFE, READ, "skill metadata enters the context",
                  match=_action_is("list", "view", "show"), label="Skill inspection"),
        Operation("manage-create", PROJECT, WRITE,
                  "a new skill (procedural memory) is written to the profile",
                  match=_action_is("create", "write"), reversibility=risk.REVERSIBLE,
                  label="Skill creation"),
        Operation("manage-update", PROJECT, WRITE,
                  "an existing skill's instructions change",
                  match=_action_is("patch", "update", "edit", "write_file"),
                  reversibility=risk.REVERSIBLE, label="Skill update"),
        Operation("manage-delete", CONSEQUENTIAL, ADMIN,
                  "a skill is removed from the profile",
                  match=_action_is("delete", "remove"), reversibility=risk.PARTIAL,
                  label="Skill deletion"),
        Operation("manage-unknown", UNCERTAIN, ADMIN,
                  "the skill operation could not be classified",
                  label="Skill mutation"),
    ),
    "tool_search": (Operation("search", SAFE, READ, "tool names/descriptions enter the context"),),
    "tool_describe": (Operation("describe", SAFE, READ, "tool schemas enter the context"),),
    "tool_call": (Operation("invoke", UNCERTAIN, EXEC,
                            "the deferred tool executes with its own effects",
                            why="deferred dispatch: the inner tool's effects are not statically known",
                            label="Deferred tool dispatch"),),
    "execute_code": (Operation("code", CONSEQUENTIAL, EXEC,
                               "arbitrary code runs with this user's permissions",
                               reversibility=risk.PARTIAL, label="Code execution"),),
    "browser_navigate": (Operation("navigate", PROJECT, NETWORK,
                                   "a page is fetched into the session"),),
    "browser_click": (Operation("click", UNCERTAIN, EXEC,
                                "the page performs an action with unknown effects"),),
    "browser_type": (Operation("type", UNCERTAIN, EXEC,
                               "text is submitted to a live page"),),
    "browser_press": (Operation("key", UNCERTAIN, EXEC,
                                "a key event is delivered to a live page"),),
    "browser_console": (Operation("console", SAFE, READ,
                                  "JavaScript runs inside the loaded page (no host access)"),),
    "browser_snapshot": (Operation("snapshot", SAFE, READ, "page text enters the context"),),
    "browser_get_images": (Operation("images", SAFE, READ, "image URLs enter the context"),),
    "browser_vision": (Operation("vision", SAFE, READ, "a screenshot enters the context"),),
    # Nerve's own surface: explicit per-tool operations, no namespace bypass
    "nerve_decide": (Operation("decide", UNCERTAIN, NETWORK,
                               "a hosted decision model is queried", label="Model decision"),),
    "nerve_assess": (Operation("assess", UNCERTAIN, NETWORK,
                               "a hosted model assesses supplied content"),),
    "nerve_rank": (Operation("rank", UNCERTAIN, NETWORK, "a hosted model ranks options"),),
    "nerve_verify": (Operation("verify", UNCERTAIN, NETWORK, "a hosted model verifies a claim"),),
    "nerve_stats": (Operation("stats", SAFE, READ, "local telemetry is summarized"),),
    "nerve_context_curate": (Operation("curate", PROJECT, WRITE,
                                       "the context ledger is compacted/rewritten"),),
    "nerve_context_rehydrate": (Operation("rehydrate", SAFE, READ,
                                          "stored context enters the session"),),
    "nerve_context_explain": (Operation("explain", SAFE, READ, "context decisions are described"),),
    "nerve_nervous_event": (Operation("event", PROJECT, WRITE,
                                      "a supervision event is recorded"),),
    "nerve_supervise_card": (Operation("card", PROJECT, WRITE, "a supervision card is written"),),
    "nerve_work_event": (Operation("work-event", PROJECT, WRITE, "work state is recorded"),),
    "nerve_work_status": (Operation("work-status", SAFE, READ, "work state is read"),),
    "nerve_remote_delegate_task": (Operation("delegate", CONSEQUENTIAL, EXEC,
                                             "a remote agent executes work on your behalf",
                                             reversibility=risk.PARTIAL),),
    "nerve_remote_worker_status": (Operation("remote-status", SAFE, READ, "remote state is read"),),
    "nerve_remote_worker_result": (Operation("remote-result", SAFE, READ,
                                             "a remote result enters the context"),),
    "nerve_remote_worker_cancel": (Operation("remote-cancel", PROJECT, ADMIN,
                                             "remote work is stopped", reversibility=risk.PARTIAL),),
    "nerve_remote_worker_control": (Operation("remote-control", CONSEQUENTIAL, ADMIN,
                                              "a remote worker is steered or terminated",
                                              reversibility=risk.PARTIAL),),
    # Memory
    "mnemosyne_recall": (Operation("recall", SAFE, READ, "stored memory enters the context"),),
    "mnemosyne_shared_recall": (Operation("shared-recall", SAFE, READ,
                                          "shared memory enters the context"),),
    "mnemosyne_stats": (Operation("stats", SAFE, READ, "memory statistics are read"),),
    "mnemosyne_shared_stats": (Operation("shared-stats", SAFE, READ,
                                         "shared memory statistics are read"),),
    "mnemosyne_get": (Operation("get", SAFE, READ, "a memory record is read"),),
    "mnemosyne_graph_query": (Operation("graph", SAFE, READ, "graph facts are read"),),
    "mnemosyne_sync_status": (Operation("sync-status", SAFE, READ, "sync state is read"),),
    "mnemosyne_remember": (Operation("remember", PROJECT, WRITE, "a memory record is written"),),
    "mnemosyne_shared_remember": (Operation("shared-remember", PROJECT, WRITE,
                                            "a shared memory record is written"),),
    # Terminal and shell-family tools
    "terminal": (Operation("shell", UNCERTAIN, EXEC,
                           "a shell command runs with this user's permissions"),),
    # Work / Kanban
    "kanban_show": (Operation("show", SAFE, READ, "task state is read"),),
    "kanban_update": (Operation("update", PROJECT, WRITE, "task state changes"),),
    "delegate_task": (Operation("delegate", CONSEQUENTIAL, EXEC,
                                "child agents execute work on your behalf",
                                reversibility=risk.PARTIAL),),
    "cronjob": (
        Operation("list", SAFE, READ, "scheduled job definitions are read",
                  match=_action_is("list", "show")),
        Operation("create", CONSEQUENTIAL, ADMIN,
                  "a recurring job is scheduled to run later without supervision",
                  match=_action_is("create", "add", "update", "edit", "run", "pause",
                                   "resume", "remove", "delete"), reversibility=risk.PARTIAL,
                  label="Scheduled automation change"),
    ),
    "todo": (Operation("todo", PROJECT, WRITE, "the session task list changes"),),
    "memory": (
        Operation("memory-read", SAFE, READ, "memory is read", match=_action_is("read", "view", "list")),
        Operation("memory-write", PROJECT, WRITE, "persistent memory changes",
                  match=_action_is("add", "write", "update", "edit", "replace", "remove", "delete")),
    ),
    # --- surface added in 0.4.2 after auditing the live tool registry -------
    "shared_context_read": (
        Operation("shared-context-read", SAFE, READ, "shared coordination context is read"),
    ),
    "shared_context_write": (
        Operation("shared-context-write", PROJECT, WRITE,
                  "an entry is added to the shared coordination log"),
    ),
    "session_search": (
        Operation("session-search", SAFE, READ, "past sessions are searched"),
    ),
    "vision_analyze": (
        Operation("vision-analyze", SAFE, READ, "an image is analysed locally"),
    ),
    "web_search": (
        Operation("web-search", SAFE, READ, "public web search"),
    ),
    "web_fetch": (
        Operation("web-fetch", SAFE, READ, "a public page is fetched (URL provenance unknown)"),
    ),
    "connections_list": (
        Operation("connections-list", SAFE, READ, "connected integrations are listed"),
    ),
    "clarify": (
        Operation("clarify", SAFE, READ, "asks the operator a question"),
    ),
    "process": (
        Operation("process-list", SAFE, READ, "running processes are listed",
                  match=_action_is("list", "status", "show", "inspect")),
        Operation("process-control", CONSEQUENTIAL, EXEC, "a process is signalled",
                  match=_action_is("kill", "stop", "terminate", "signal")),
    ),
    "text_to_speech": (
        Operation("tts", PROJECT, WRITE, "audio output is generated"),
    ),
    "image_generate": (
        Operation("image-generate", CONSEQUENTIAL, EXEC, "image generation runs externally"),
    ),
    "video_generate": (
        Operation("video-generate", CONSEQUENTIAL, EXEC, "video generation runs externally"),
    ),
    "computer_use": (
        Operation("computer-use", CONSEQUENTIAL, EXEC, "controls the desktop"),
    ),
    "computer": (
        Operation("computer-control", CONSEQUENTIAL, EXEC, "controls the desktop"),
    ),
}

# Verb prefixes used only to classify unknown MCP/plugin tools conservatively.
_READ_VERBS = ("get", "list", "search", "read", "show", "describe", "status",
               "stats", "recall", "view", "fetch", "query", "inspect", "find")
_MUTATE_VERBS = ("create", "update", "delete", "remove", "write", "set", "add",
                 "patch", "edit", "upload", "publish", "send", "post", "put",
                 "deploy", "install", "run", "exec", "execute", "kill", "stop",
                 "start", "restart", "cancel", "revoke", "grant", "rotate")
_EXEC_VERBS = ("run", "exec", "execute", "deploy", "install", "shell", "spawn")


def _verb(tool_name: str) -> str:
    """Extract the operation verb from a tool name (e.g. mcp__gh__create_issue)."""
    import re as _re

    tail = (tool_name.split("__")[-1] if "__" in tool_name else tool_name).strip().lower()
    parts = [part for part in _re.split(r"[_\-.]", tail) if part]
    if not parts:
        return tail
    known = set(_READ_VERBS) | set(_MUTATE_VERBS)
    if parts[0] in known:          # create_issue -> create
        return parts[0]
    return parts[-1]


# Token sets used to SCREEN unknown plugin/MCP operations. A read-style verb is
# never sufficient evidence of safety: names, argument keys, argument values and
# path-shaped arguments are all screened for credential/private targets.
_SENSITIVE_TOKENS = frozenset({
    "secret", "secrets", "credential", "credentials", "privatekey", "private_key",
    "password", "passwd", "passphrase", "token", "tokens", "apikey", "api_key",
    "access_key", "secret_key", "vault", "keychain", "keystore", "deploy_key",
    "identity", "dotenv", "env_file", "license_key", "session_key", "refresh_token",
    "cookie", "cookies", "shadow", "gpg", "kms", "sealed_secret",
    "sensitive", "confidential", "restricted",
})
# Compound fragments that are unambiguous even when split across tokens
# (read_private_key -> private+key) or embedded (readprivatekey).
_SENSITIVE_FRAGMENTS = ("private_key", "privatekey", "access_key", "accesskey",
                        "api_key", "apikey", "secret", "credential", "password",
                        "passwd", "vault", "keychain", "keystore")
_SENSITIVE_KEY_PREFIXES = ("password", "passwd", "secret", "token", "credential",
                           "apikey", "api_key", "private", "access_key")
_SENSITIVE_PATH_NAMES = frozenset({
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".netrc", ".pgpass", ".npmrc",
    ".pypirc", "credentials", "credentials.json", "hosts.yml", "config.json",
    "auth.json", ".git-credentials", "secring.gpg",
})
_SECRET_VALUE_MARKERS = ("sk-", "ghp_", "gho_", "github_pat_", "AKIA", "xoxb-",
                         "-----BEGIN", "AIza", "glpat-")


def _name_tokens(name: str) -> set[str]:
    import re as _re
    return {t for t in _re.split(r"[_\-.]+", name.lower()) if t}


def _walk_strings(value: Any, depth: int = 0) -> Iterable[str]:
    if depth > 4 or value is None:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _walk_strings(v, depth + 1)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _walk_strings(item, depth + 1)


def _looks_like_path(value: str) -> bool:
    return (value.startswith(("/", "~", "file://", "./", "../"))
            or "/" in value and " " not in value.strip())


def mcp_signals(tool_name: str, args: dict[str, Any]) -> list[str]:
    """Return concrete credential/sensitivity signals for an unknown operation.

    An empty list means the call was screened and matched no sensitive marker —
    it does NOT prove the operation is harmless, only that no reviewed hazard
    marker applies to this tool name and these arguments.
    """
    signals: list[str] = []
    tail = tool_name.split("__")[-1] if "__" in tool_name else tool_name
    name_tokens = _name_tokens(tail)
    hits = sorted(name_tokens & _SENSITIVE_TOKENS)
    if hits:
        signals.append("tool name targets sensitive data (" + ", ".join(hits) + ")")
    else:
        flat = tail.lower().replace("-", "_").replace(".", "_")
        frag = next((f for f in _SENSITIVE_FRAGMENTS if f in flat), "")
        if frag:
            signals.append(f"tool name contains sensitive fragment '{frag}'")

    for key, value in args.items():
        lk = str(key).lower()
        if lk in _SENSITIVE_TOKENS or lk.startswith(_SENSITIVE_KEY_PREFIXES):
            signals.append(f"argument '{lk}' is a credential-shaped field")
        for text in _walk_strings(value):
            low = text.strip().lower()
            if _looks_like_path(text):
                expanded = risk._expand(text.replace("file://", "", 1))
                if risk.sensitive_read_target(expanded):
                    signals.append(f"target path is credential/protected: {text[:80]}")
                elif expanded.name in _SENSITIVE_PATH_NAMES:
                    signals.append(f"target file is a credential store: {expanded.name}")
            if any(low.startswith(m) or m.lower() in low for m in _SECRET_VALUE_MARKERS):
                signals.append("argument contains a secret-shaped value")
            if " " not in low and len(low) < 48:
                # Field/column names such as password_hash or token are targets
                # for private data even when the wrapper key is neutral.
                vt = _name_tokens(low)
                vhits = sorted(vt & _SENSITIVE_TOKENS)
                if vhits:
                    signals.append("argument requests sensitive field(s): " + ", ".join(vhits))
            if low.startswith("-----begin") and "private key" in low:
                signals.append("argument contains private key material")
    # de-duplicate, keep order, cap for message hygiene
    seen, out = set(), []
    for s in signals:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out[:5]


def sensitive_path_args(args: dict[str, Any]) -> list[str]:
    """Paths anywhere in the argument tree that hit credential/protected targets."""
    found: list[str] = []
    for text in _walk_strings(args):
        if not _looks_like_path(text):
            continue
        candidate = text.replace("file://", "", 1)
        if risk.sensitive_read_target(risk._expand(candidate)):
            found.append(candidate)
        elif risk._expand(candidate).name in _SENSITIVE_PATH_NAMES:
            found.append(candidate)
    return list(dict.fromkeys(found))


def _classify_mcp(tool_name: str, args: dict[str, Any]) -> risk.RiskAssessment:
    """Effect-aware classification for plugin/MCP tools that are not cataloged.

    Read-shaped names are treated as *inferred bounded* only after screening the
    name, argument keys, argument values and path-shaped arguments for
    credential targets. Any signal escalates to CONSEQUENTIAL with the concrete
    reason, so `get_secret`/`read_private_key`/`fetch_sensitive_config` cannot
    inherit a read-only allowance.
    """
    verb = _verb(tool_name)
    display = f"{tool_name}({risk._short_args(args)}"[:160] + ")"
    signals = mcp_signals(tool_name, args)

    if signals:
        return risk.RiskAssessment(
            CONSEQUENTIAL, tool_name,
            action_label=f"{tool_name} sensitive-data access",
            summary=display,
            why="it targets credentials or private data: " + "; ".join(signals),
            impact="secret material or private records would enter the session transcript",
            risk_notes="credential exposure is not reversible once logged or transmitted",
            reversibility=risk.IRREVERSIBLE,
            shape={"tool": tool_name, "args_fp": risk._args_fingerprint(args),
                   "basis": "mcp-signals", "signals": signals},
            display=display)

    if verb in _READ_VERBS:
        return risk.RiskAssessment(
            SAFE, tool_name,
            action_label=f"{tool_name} (read-shaped, screened)",
            summary=display,
            why="",
            impact="returned data enters the session context",
            reversibility=risk.REVERSIBLE,
            shape={"tool": tool_name, "args_fp": risk._args_fingerprint(args),
                   "basis": "mcp-verb-screened"},
            display=display)

    if verb in _EXEC_VERBS:
        return risk.RiskAssessment(
            CONSEQUENTIAL, tool_name, action_label=f"{tool_name} execution", summary=display,
            why="the operation executes remote or external work whose effects are not listed",
            impact="external systems may change outside this session's review",
            reversibility=risk.PARTIAL,
            shape={"tool": tool_name, "args_fp": risk._args_fingerprint(args),
                   "basis": "mcp-exec-verb"},
            display=display)
    if verb in _MUTATE_VERBS:
        return risk.RiskAssessment(
            CONSEQUENTIAL, tool_name, action_label=f"{tool_name} mutation", summary=display,
            why="the operation changes external state and is not covered by a reviewed catalog entry",
            impact="state outside this repository may change",
            reversibility=risk.PARTIAL,
            shape={"tool": tool_name, "args_fp": risk._args_fingerprint(args),
                   "basis": "mcp-mutate-verb"},
            display=display)
    return risk.RiskAssessment(
        UNCERTAIN, tool_name, action_label=f"{tool_name} call", summary=display,
        why="unrecognized plugin/MCP operation: effects depend on arguments static rules cannot verify",
        shape={"tool": tool_name, "args_fp": risk._args_fingerprint(args),
               "basis": "mcp-unknown-verb"},
        display=display)


def _nested_target(tool_name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Return (inner_tool, inner_args) for deferred/dispatch wrappers."""
    if tool_name not in {"tool_call", "tool_execute", "invoke_tool", "call_tool"}:
        return None
    inner = args.get("name") or args.get("tool_name") or args.get("tool")
    inner_args = args.get("arguments") or args.get("args") or args.get("input") or {}
    if not isinstance(inner, str) or not inner.strip():
        return None
    return inner.strip(), inner_args if isinstance(inner_args, dict) else {}


def classify_tool(tool_name: str, args: dict[str, Any],
                  workdir: Path | None = None) -> risk.RiskAssessment | None:
    """Classify a non-shell tool by operation, or None when not cataloged."""
    nested = _nested_target(tool_name, args)
    if nested is not None:
        inner_name, inner_args = nested
        inner = classify_tool(inner_name, inner_args, workdir)
        if inner is None:
            return risk.RiskAssessment(
                UNCERTAIN, tool_name, action_label=f"{tool_name} dispatch",
                summary=_short(tool_name, args),
                why=f"deferred dispatch of '{inner_name}', whose effects are not cataloged",
                shape={"tool": tool_name, "inner": inner_name,
                       "args_fp": risk._args_fingerprint(args)},
                display=_short(tool_name, args))
        inner.shape = dict(inner.shape or {})
        inner.shape["dispatched_via"] = tool_name
        return inner

    specs = TOOLS.get(tool_name)
    if specs:
        for op in specs:
            if not op.matches(args):
                continue
            paths = []
            for key in op.paths_from:
                raw = args.get(key)
                values = raw if isinstance(raw, list) else ([raw] if raw else [])
                paths.extend(str(v) for v in values if v)
            assessment = _build(tool_name, args, op, paths, workdir)
            return assessment
        # A cataloged tool whose operation matched nothing is not safe by default.
        return risk.RiskAssessment(
            UNCERTAIN, tool_name, action_label=f"{tool_name} call",
            summary=_short(tool_name, args),
            why="the requested operation is not one of this tool's cataloged shapes",
            shape={"tool": tool_name, "args_fp": risk._args_fingerprint(args)},
            display=_short(tool_name, args))
    if tool_name.startswith(("mcp__", "plugin__")) or "__" in tool_name:
        return _classify_mcp(tool_name, args)
    return None


def _short(tool_name: str, args: dict[str, Any]) -> str:
    return f"{tool_name}({risk._short_args(args)}"[:160] + ")"


def _build(tool_name: str, args: dict[str, Any], op: Operation,
           paths: list[str], workdir: Path | None) -> risk.RiskAssessment:
    display = f"{tool_name}:{op.name}(" + ", ".join(paths[:2]) + ")" if paths else _short(tool_name, args)
    shape: dict[str, Any] = {"tool": tool_name, "operation": op.name,
                             "args_fp": risk._args_fingerprint(args)}
    category = op.category
    why = op.why
    # Use the same reviewed credential-store names as the main risk screen.
    # A path outside the current HOME must not gain a catalog-only bypass.
    extra_sensitive = sorted(set(sensitive_path_args(args)))
    if extra_sensitive and category == SAFE:
        category = CONSEQUENTIAL
        why = ("it reads credential/protected location(s) supplied through an "
               f"alternate argument shape: {extra_sensitive[0][:80]}")
        shape["sensitive_args"] = extra_sensitive[:5]
    if paths:
        shape["paths"] = paths[:128]
        sensitive = [p for p in paths if risk.sensitive_read_target(risk._expand(p))]
        if sensitive and op.effect == READ:
            category = CONSEQUENTIAL
            why = f"it reads credential/protected location(s): {', '.join(dict.fromkeys(sensitive[:3]))}"
        if op.effect == WRITE:
            base = workdir or risk._workdir_for(tool_name, args)
            scope = risk.classify_path(risk._expand(paths[0]), base)
            if scope == "outside":
                category = max(category, CONSEQUENTIAL, key=lambda c: risk._SEVERITY[c])
                why = why or "the write target is outside the approved project scope"
            shape["scope"] = scope
    return risk.RiskAssessment(
        category, tool_name,
        action_label=op.label or f"{tool_name} {op.name}",
        summary=display, why=why or "", impact=op.impact,
        reversibility=op.reversibility, shape=shape, display=display)


def operation_effect(tool_name: str, args: dict[str, Any]) -> str | None:
    """Return the cataloged effect of the matched operation (or None)."""
    for op in TOOLS.get(tool_name, ()):
        if op.matches(args):
            return op.effect
    # Unknown plugin/MCP tools are never "proven read-only" by name: they are
    # classified (and screened) by _classify_mcp, which can escalate on
    # credential targets. Returning None here means "no bypass".
    return None


def inventory_audit(tool_names: Iterable[str]) -> dict[str, Any]:
    """Report catalog coverage for a live tool inventory.

    Buckets are deliberately distinct:
      verified_safe       explicit catalog operation whose effect is READ
      verified_bounded    explicit catalog operation, project-scoped mutation
      consequential       explicit catalog operation or credential signal
      inferred_bounded    unknown MCP/plugin tool, read-shaped and screened clean
      unclassified        no rule matched; gate treats as UNCERTAIN

    `inferred_bounded` is NOT counted as verified-safe coverage: it is a
    screened heuristic that keeps ordinary remote reads from prompting while
    remaining visible to the audit.
    """
    verified_safe, verified_bounded, consequential = [], [], []
    inferred_bounded, unclassified = [], []
    for name in sorted(set(tool_names)):
        specs = TOOLS.get(name)
        if specs:
            cats = {op.category for op in specs}
            effects = {op.effect for op in specs}
            if cats - {SAFE, PROJECT}:
                consequential.append(name)
            elif effects <= {READ}:
                verified_safe.append(name)
            else:
                verified_bounded.append(name)
            continue
        if name.startswith(("mcp__", "plugin__")) or "__" in name:
            assessed = _classify_mcp(name, {})
            basis = str((assessed.shape or {}).get("basis") or "")
            if assessed.category == SAFE and basis == "mcp-verb-screened":
                inferred_bounded.append(name)
            elif assessed.category == CONSEQUENTIAL:
                consequential.append(name)
            else:
                unclassified.append(name)
            continue
        unclassified.append(name)
    total = (len(verified_safe) + len(verified_bounded) + len(consequential)
             + len(inferred_bounded) + len(unclassified))
    reviewed = total - len(unclassified) - len(inferred_bounded)
    return {
        "total": total,
        "verified_safe": verified_safe,
        "verified_bounded": verified_bounded,
        "consequential": consequential,
        "inferred_bounded": inferred_bounded,
        "unclassified": unclassified,
        "explicitly_reviewed": reviewed,
        "coverage": round((total - len(unclassified)) / total, 3) if total else 1.0,
        # retained for existing reports
        "read_only": verified_safe,
        "mutating_or_exec": verified_bounded + consequential,
    }
