"""Deterministic progress/tool classification for nervous-system QoL guards.

This module has no provider calls and no persistent state. It only classifies
the current turn/tool evidence so recovery controls cannot confuse activity
with task-relative progress.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

# CHANGE_REQUIRED_RE matches a bare change verb anywhere in the text. That is too
# loose: the same word is a verb ("update the config") or a noun ("after the
# configuration update"), and treating a noun as an instruction makes the
# progress guard unsatisfiable for a read-only turn that merely mentions an update.
#
# The discriminator is the determiner. English marks the noun reading with an
# article or possessive immediately before the word ("the update", "a fix", "my
# patch"), while an imperative verb is either sentence-initial or follows a
# conjunction/comma ("then fix the client"). So a change word preceded by a
# determiner is a noun and is not evidence of mutation intent.
_CHANGE_WORDS = r"fix|implement|modify|patch|repair|refactor|edit|update|change|rewrite"
# "the/a/an/this/that/... <change word>" -> noun. Up to three modifier words may sit
# between the determiner and the change word ("the configuration update", "my new
# patch", "a minor refactor"), so the gap is bounded rather than adjacent.
_NOUN_CHANGE_RE = re.compile(
    rf"\b(?:the|a|an|this|that|these|those|my|your|its|our|their|any|no|each|every)\s+"
    rf"(?:[a-z0-9_-]+\s+){{0,3}}?"
    rf"(?:{_CHANGE_WORDS})\b",
    re.IGNORECASE,
)
CHANGE_REQUIRED_RE = re.compile(rf"\b(?:{_CHANGE_WORDS})\b", re.IGNORECASE)
VERIFY_REQUIRED_RE = re.compile(r"\b(?:test|tests|verify|verification|regression|check)\b", re.IGNORECASE)
PATH_RE = re.compile(r"(?<![\w.-])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.[A-Za-z0-9]{1,12})(?![\w.-])")

# Harness-supplied instruction files (Paperclip agent instructions, wake payloads,
# execution contracts) are not a task statement. Deriving "this turn must change
# code" from them produces an unsatisfiable completion block: the verbs are
# ordinary prose ("update the issue to a clear final disposition") and the
# extracted paths are harness-internal files the turn must not touch.
HARNESS_INSTRUCTION_MARKERS = (
    "paperclip wake payload",
    "paperclip_task_id",
    "paperclip_api_key",
    "paperclip_api_url",
    "paperclip_api_base",
    "paperclip_api_guidance",
    "paperclip runtime identity",
    "paperclip-run-scratch",
    "paperclip_scratch_dir",
    "paperclip_task_context",
    "execution contract",
    "agent instructions",
    "issue-thread interaction",
    "skills/paperclip/scripts/paperclip-upload-artifact.sh",
)

# Path prefixes/segments that belong to the agent harness, not to the user's
# project. They must never be treated as "requested change targets".
HARNESS_PATH_PREFIXES = (
    "skills/paperclip/",
    ".paperclip/",
    "hermes-agent/skills/paperclip/",
    "node_modules/@paperclipai/",
    "api/issues/",
    "api/agents/",
    "api/companies/",
    "api/projects/",
    "agents/companies/",
    "instances/default/",
)
HARNESS_PATH_SEGMENTS = frozenset({"@paperclipai"})
HARNESS_PATH_NAMES = frozenset({"paperclip-upload-artifact.sh"})
HARNESS_INSTRUCTIONS_NAMES = frozenset({"agents.md", "instructions.md"})
# camelCase segments are API/JSON field names ("metadata.resourceRef.kind"),
# never a file the turn is asked to change.
CAMEL_CASE_SEGMENT_RE = re.compile(r"^[a-z0-9]+[A-Z]")

TOOL_ALIASES: dict[str, frozenset[str]] = {
    "mutation": frozenset({
        "write_file", "edit_file", "patch", "patch_file", "apply_patch", "replace_file",
        "create_file", "delete_file", "move_file", "rename_file", "fs_write", "fs_replace",
        "write", "edit",
    }),
    "read": frozenset({"read_file", "fs_read", "cat", "open_file"}),
    "search": frozenset({"search_text", "fs_search", "grep", "find", "list_files", "fs_list"}),
    "test": frozenset({"test_run", "pytest", "unittest", "lint_run", "typecheck_run"}),
}

TERMINAL_MUTATION_PREFIXES = (
    "git apply", "apply_patch", "sed -i", "perl -pi", "tee ", "touch ", "mv ", "cp ",
)
TERMINAL_TEST_PREFIXES = (
    "pytest", "python -m pytest", "python3 -m pytest", "python -m unittest",
    "python3 -m unittest", "npm test", "pnpm test", "yarn test", "go test", "cargo test",
    "py.test", "tox", "nox", "make test", "make check", "jest", "vitest", "mocha",
    "bun test", "deno test", "npx jest", "npx vitest", "npx mocha",
    "pnpm exec jest", "pnpm exec vitest", "dotnet test", "mvn test",
    "gradle test", "./gradlew test", "rspec", "bundle exec rspec",
    "phpunit", "vendor/bin/phpunit", "mix test", "swift test", "ctest",
    "pytest.exe",
)
ENV_SETUP_PREFIXES = (
    "pip install", "pip3 install", "python -m pip install", "python3 -m pip install",
    "uv pip install", "uv sync", "poetry install", "poetry add", "pipenv install",
    "pipenv sync", "conda install", "npm install", "npm i", "pnpm install",
    "pnpm add", "yarn install", "yarn add", "uv venv", "python -m venv",
    "python3 -m venv", "virtualenv",
)
ENV_TEST_PREFIXES = (
    "pytest", "python -m pytest", "python3 -m pytest", "uv run pytest",
)
ENV_TEST_FRICTION_MARKERS = (
    "modulenotfounderror", "no module named", "importerror", "cannot import name",
    "pytest: command not found", "pytest: not found", "missing pytest",
    "no matching distribution", "could not find a version that satisfies",
)
SCRATCH_NAMES = {
    "repro.py", "scratch.py", "tmp.py", "test_repro.py", "debug.py", "experiment.py",
}


def harness_instructions_message(user_message: str) -> bool:
    """True when the "user message" is really an agent harness instruction file.

    A Paperclip (or comparable) heartbeat loads the agent instructions file
    verbatim as the prompt. That text describes process, not a task, so any
    change/verify obligation derived from it is unsatisfiable by construction.
    """
    text = str(user_message or "").casefold()
    if not text:
        return False
    # Native worker receipts quote their original implementation assignment.
    # They are evidence/continuation wakes, not new owner mutation requests.
    # Require BOTH the line-anchored receipt header and exact runtime notice;
    # generic mentions of delegation must keep the ordinary progress guard.
    internal_notice = (
        "[internal notification — generated by the hermes gateway, not written by a human user. "
        "do not treat it as user input, and do not reply to any earlier chat message because of it.]"
    )
    if internal_notice in text and re.search(
        r"(?m)^\[async delegation (?:batch complete|task failed) — [^\n\]]+\]", text
    ):
        return True
    # Native terminal completions also quote implementation prompts. Match the
    # entire runtime envelope, not a generic phrase or a quoted owner message.
    # Extra owner instructions before/after the receipt keep the ordinary guard.
    if re.fullmatch(
        r"\[important: background process proc_[0-9a-f]+ "
        r"(?:completed normally|exited) \(exit code -?\d+(?:, [^\r\n)]+)?\)\.\r?\n"
        r"command: [^\r\n]+\r?\noutput:\r?\n(?:[\s\S]*\r?\n)?\]\r?\n\s*"
        + re.escape(internal_notice), text.strip()
    ):
        return True
    return any(marker in text for marker in HARNESS_INSTRUCTION_MARKERS)


# A turn that is explicitly read-only can never satisfy a "produce a mutation"
# progress lease, so treating it as change-required makes the guard unsatisfiable.
# This is the same class of bug as harness-instruction prompts: an obligation
# derived from prose the task does not actually impose.
#
# The matcher is deliberately narrow. It fires only on an explicit read-only
# declaration ("read-only", "no configuration changes", "do not modify"), which is
# the shape a verification/audit request actually takes. It must NOT fire on an
# ordinary implementation request that merely mentions inspection -- those still
# need the progress guard. Two guards keep it narrow:
#   1. An explicit read-only marker must be present.
#   2. The message must not carry an imperative change verb aimed at the work
#      ("fix the client", "update the defaults"), which signals real mutation work.
READ_ONLY_MARKERS = (
    "read-only",
    "read only",
    "readonly",
    "no configuration changes",
    "no config changes",
    "do not modify",
    "do not edit",
    "without modifying",
    "without changing",
    "no changes to",
    "verification only",
    "audit only",
    "inspection only",
    "diagnostic only",
    "do not restart",
    "no restarts",
    "no delegation",
)

# Imperative mutation intent: the verb is the task, not a noun in prose.
# "after the configuration update" (noun) must not match; "update the config" must.
# "the configuration update" is a noun phrase, so it does not match: the regex
# requires the verb to be followed by its object.
IMPERATIVE_CHANGE_RE = re.compile(
    r"\b(?:fix|implement|modify|patch|repair|refactor|edit|update|change|rewrite)\b"
    r"\s+(?:the\s+|this\s+|that\s+|these\s+|those\s+)?"
    r"[a-z0-9_./-]{2,}",
    re.IGNORECASE,
)

# A change verb preceded by a negator is the opposite of mutation intent:
# "no configuration changes", "do not modify anything", "without updating files".
# Matching those as imperative work would defeat the whole exemption, so any
# negated occurrence is stripped before the imperative test runs.
_NEGATED_CHANGE_RE = re.compile(
    r"(?:\bno\b|\bnot\b|\bwithout\b|\bnever\b|\bdon't\b|\bdo\s+not\b)"
    r"[^.;:!?\n]{0,40}?"
    r"\b(?:fix|implement|modify|patch|repair|refactor|edit|update|change|rewrite|restart|delegat\w*)\b",
    re.IGNORECASE,
)


def read_only_audit_message(user_message: str) -> bool:
    """True when the turn explicitly declares itself a read-only audit/verification.

    A read-only turn cannot legitimately produce a file mutation, so the
    progress-stall guard must not demand one. Returns False whenever the message
    also states an imperative change target, so genuine implementation work --
    including work that involves reading files first -- keeps its guard.
    """
    text = str(user_message or "").casefold()
    if not text:
        return False
    if not any(marker in text for marker in READ_ONLY_MARKERS):
        return False
    # Negated change verbs ("do not modify", "no configuration changes") are
    # prohibitions, not instructions, so they must not disqualify the exemption.
    residual = _NEGATED_CHANGE_RE.sub(" ", text)
    if IMPERATIVE_CHANGE_RE.search(residual):
        return False
    return True


def change_required(user_message: str) -> bool:
    message = str(user_message or "")
    if harness_instructions_message(message):
        return False
    if read_only_audit_message(message):
        return False
    # Strip determiner-noun readings ("the update", "a fix") before testing for a
    # change verb, so prose that merely mentions an update is not mistaken for an
    # instruction to make one.
    residual = _NOUN_CHANGE_RE.sub(" ", message)
    return bool(CHANGE_REQUIRED_RE.search(residual))


def verification_required(user_message: str) -> bool:
    message = str(user_message or "")
    if harness_instructions_message(message):
        return False
    return bool(VERIFY_REQUIRED_RE.search(message))


def is_harness_path(path: str) -> bool:
    """Harness-internal paths are never legitimate change targets for a task."""
    normalized = _normalize_path(path).casefold().lstrip("/")
    if not normalized:
        return False
    if normalized.startswith(HARNESS_PATH_PREFIXES):
        return True
    pure = PurePosixPath(normalized)
    parts = pure.parts
    if pure.name in HARNESS_PATH_NAMES:
        return True
    if pure.name in HARNESS_INSTRUCTIONS_NAMES and "instructions" in parts[:-1]:
        return True
    return any(part in HARNESS_PATH_SEGMENTS for part in parts[:-1])


def looks_like_api_field(path: str) -> bool:
    """True for JSON/API field tokens the path regex mistakes for a filename."""
    normalized = _normalize_path(path)
    if not normalized or "/" in normalized:
        return False
    return any(CAMEL_CASE_SEGMENT_RE.match(part) for part in normalized.split("."))


def requested_paths(user_message: str) -> tuple[str, ...]:
    seen: list[str] = []
    cleaned = str(user_message or "")
    cleaned = re.sub(r"@?url:[^\s]+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"https?://[^\s`]+", "", cleaned, flags=re.IGNORECASE)
    for match in PATH_RE.finditer(cleaned):
        raw = match.group(1)
        if re.fullmatch(r"\d+(?:\.\d+)+", raw):
            continue
        # Numeric ranges such as Python 3.10-3.14 are not requested files.
        # Retain explicit files, including ./3.10-3.14 or "file 3.10-3.14".
        if re.fullmatch(r"\d+(?:\.\d+)+-\d+(?:\.\d+)+", raw):
            lead = cleaned[max(0, match.start(1) - 40):match.start(1)]
            if not (lead.endswith("./") or re.search(
                r"(?:file|path|filename|target|named)\s+[`\\\"']?$", lead, re.IGNORECASE
            )):
                continue
        path = _normalize_path(raw)
        # Assignments often abbreviate file lists as gate.py/risk.py. Treat
        # all-filename source lists as separate targets, not an impossible
        # nested path. Actual directories (including v0.4.0) stay intact.
        parts = path.split("/")
        source_name = r"[A-Za-z0-9_-]+\.(?:py|js|ts|tsx|jsx|rs|go|java|c|cpp|h)"
        candidates = parts if len(parts) > 1 and all(re.fullmatch(source_name, p) for p in parts) else [path]
        for candidate in candidates:
            if candidate and not is_harness_path(candidate) and not looks_like_api_field(candidate) and candidate not in seen:
                seen.append(candidate)
    return tuple(seen[:32])


def _shell_segments(command: str) -> list[str]:
    # Split actual shell separators, never separators embedded in quoted code.
    lexer = shlex.shlex(str(command or ""), posix=True, punctuation_chars=";&|\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    segments, current = [], []
    try:
        for token in lexer:
            if re.fullmatch(r"(?:&&|\|\||;|\n)+", token):
                if current:
                    segments.append(shlex.join(current).lower())
                    current = []
            else:
                current.append(token)
    except ValueError:
        return []
    if current:
        segments.append(shlex.join(current).lower())
    return segments


def _python_test_command(segment: str) -> bool:
    """Recognize runner argv behind explicitly parsed resource-control wrappers."""
    try:
        argv = shlex.split(segment)
    except ValueError:
        return False
    while argv and PurePosixPath(argv[0]).name in {"timeout", "taskset", "nice"}:
        wrapper = PurePosixPath(argv[0]).name
        if wrapper == "nice":
            if len(argv) < 4 or argv[1] != "-n" or not re.fullmatch(r"[+-]?[0-9]+", argv[2]):
                return False
            argv = argv[3:]
        elif wrapper == "taskset":
            cpu_list = r"[0-9]+(?:-[0-9]+(?::[1-9][0-9]*)?)?(?:,[0-9]+(?:-[0-9]+(?::[1-9][0-9]*)?)?)*"
            if len(argv) < 4 or argv[1] not in {"-c", "--cpu-list"} or not re.fullmatch(cpu_list, argv[2]):
                return False
            argv = argv[3:]
        else:
            index = 1
            while index < len(argv) and argv[index].startswith("-"):
                option = argv[index]
                if option in {"-k", "--kill-after", "-s", "--signal"}:
                    index += 2
                elif option.startswith(("--signal=", "--kill-after=")):
                    index += 1
                elif option in {"--preserve-status", "--foreground", "--verbose"}:
                    index += 1
                elif option == "--":
                    index += 1
                    break
                else:
                    return False
            if index >= len(argv) or not re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", argv[index]):
                return False
            argv = argv[index + 1:]
    if not argv:
        return False
    executable = PurePosixPath(argv[0]).name
    if executable == "pytest":
        return True
    if not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable):
        return False
    args = argv[1:]
    while args and args[0] in {"-b", "-u", "-i", "-e", "-s", "-o", "-oo", "-q"}:
        args = args[1:]
    if len(args) >= 2 and args[0] == "-m":
        return args[1] in {"pytest", "unittest"}
    return bool(args and not args[0].startswith("-")
                and re.fullmatch(r"(?:test_\w+|\w+_test)\.py", PurePosixPath(args[0]).name))


_ENV_ASSIGN_RE = re.compile(r"^(?:env\s+)?(?:[a-z_][a-z0-9_]*=\S*\s+)+")
_PYTHON_PATH_RE = re.compile(r"^\S*/(?:bin/)?(python3?(?:\.\d+)?|pytest|py\.test)(?=\s|$)")


# Only known value-taking options may consume the next word. Treating every
# long flag as value-taking would turn `uv run --offline echo pytest` into pytest.
_UV_OPTIONS = (
    r"(?:\s+(?:--(?:with|with-editable|with-requirements|project|directory|python|package|extra|group|from)"
    r"(?:[= ]\S+)|--[a-z-]+(?:=\S+)?|-[a-zA-Z]+))*"
)
_LAUNCHER_RE = re.compile(
    rf"^(?:uv\s+run{_UV_OPTIONS}|uvx{_UV_OPTIONS}"
    r"|(?:poetry|pipenv|pdm|hatch|rye)\s+run"
    r"|conda\s+run(?:\s+(?:-n|--name|-p|--prefix)\s+\S+)*"
    r")\s+"
)
# Only an actual shell -c wrapper is unwrapped, not arbitrary argument strings.
_SHELL_WRAP_RE = re.compile(r"^(?:bash|sh|zsh|dash)\s+-c\s+(['\"])(.*?)\1\s*$")
_TEST_SCRIPT_RE = re.compile(
    r"^(?:\./|bash\s+|sh\s+)?(?:\S+/)?(?:run[_-]?tests?|tests?|check)"
    r"(?:\.(?:sh|bash|py))?(?:\s|$)"
)
_WINDOWS_PYTEST_RE = re.compile(
    r"(?:^|\s)[a-z]:\\[^\s'\"\n]+\\(?:pytest|py\.test)\.exe(?:\s|$)",
    re.IGNORECASE,
)


def _strip_command_prefix(segment: str) -> str:
    """Normalize assignments, environment launchers and interpreter paths."""
    segment = _ENV_ASSIGN_RE.sub("", segment)
    segment = _LAUNCHER_RE.sub("", segment)
    wrapped = _SHELL_WRAP_RE.fullmatch(segment)
    if wrapped:
        return _strip_command_prefix(wrapped.group(2).strip())
    segment = _PYTHON_PATH_RE.sub(lambda m: m.group(1), segment)
    return segment


def tool_kind(tool_name: str, args: dict[str, Any] | None = None) -> str:
    selected = str(tool_name or "").strip().lower()
    for kind, aliases in TOOL_ALIASES.items():
        if selected in aliases:
            return kind
    if selected != "terminal":
        return ""
    command = str((args or {}).get("command") or "")
    # shlex treats backslashes as escapes on POSIX; recognize the fully
    # qualified Windows pytest executable before splitting it.
    if _WINDOWS_PYTEST_RE.search(command):
        return "test"
    for segment in _shell_segments(command):
        segment = _strip_command_prefix(segment)
        if any(segment == p.strip() or segment.startswith(p) for p in TERMINAL_MUTATION_PREFIXES):
            return "mutation"
        if ">" in segment and not any(op in segment for op in (">=", "2>", "&>")):
            return "mutation"
        if (_python_test_command(segment)
                or any(segment == p or segment.startswith(p + " ") for p in TERMINAL_TEST_PREFIXES)
                or _TEST_SCRIPT_RE.match(segment)):
            return "test"

    return ""


def test_result_masked(command: str) -> bool:
    """Never accept a successful shell exit obtained by masking test failure."""
    raw = str(command or "").lower()
    if re.search(r"\|\|\s*(?:true\b|:(?=\s|$)|exit\s+0(?:\s|$))", raw):
        return True
    return bool(re.search(r";\s*(?:true|exit\s+0)\s*$", raw))




def environment_action(tool_name: str, args: dict[str, Any] | None = None) -> str:
    selected = str(tool_name or "").strip().lower()
    command = str((args or {}).get("command") or "")
    segments = _shell_segments(command)
    if selected != "terminal":
        direct = " ".join((selected, command)).strip().lower()
        segments = [direct] if direct else [selected]
    for segment in segments:
        if any(segment == p or segment.startswith(p + " ") for p in ENV_SETUP_PREFIXES):
            return "setup"
        if any(segment == p or segment.startswith(p + " ") for p in ENV_TEST_PREFIXES):
            return "test"
    return ""


def environment_failure(
    tool_name: str,
    args: dict[str, Any] | None,
    status: str,
    result: str,
    error_message: str,
) -> bool:
    failed = str(status or "").strip().lower() in {"error", "failed", "blocked", "failure"} or bool(error_message)
    if not failed:
        return False
    kind = environment_action(tool_name, args)
    if kind == "setup":
        return True
    if kind != "test":
        return False
    text = f"{result} {error_message}".lower()
    return any(marker in text for marker in ENV_TEST_FRICTION_MARKERS)

def mutation_paths(tool_name: str, args: dict[str, Any] | None = None) -> tuple[str, ...]:
    args = args or {}
    paths: list[str] = []
    for key in ("path", "file", "file_path", "destination", "dest", "source"):
        value = args.get(key)
        if isinstance(value, str):
            path = _normalize_path(value)
            if path and path not in paths:
                paths.append(path)
    # Patch payloads often carry the only reliable path signal.
    patch = args.get("patch")
    if isinstance(patch, str):
        for raw in re.findall(r"(?m)^(?:\+\+\+|---)\s+(?:[ab]/)?([^\s]+)", patch):
            if raw == "/dev/null":
                continue
            path = _normalize_path(raw)
            if path and path not in paths:
                paths.append(path)
        # Hermes' native patch tool uses V4A envelopes ("*** Update File: <path>").
        for raw in re.findall(r"(?m)^\*\*\*\s+(?:Update|Add|Delete) File:\s*(.+?)\s*$|^\*\*\*\s+Move to:\s*(.+?)\s*$", patch):
            for candidate in raw:
                path = _normalize_path(candidate)
                if path and path not in paths:
                    paths.append(path)
    return tuple(paths[:32])


def mutation_is_relevant(tool_name: str, args: dict[str, Any] | None, requested: tuple[str, ...]) -> bool:
    if tool_kind(tool_name, args) != "mutation":
        return False
    paths = mutation_paths(tool_name, args)
    if not paths:
        # A concrete mutation with no inspectable path is allowed but does not
        # prove semantic progress when the task names target files.
        return not requested
    if requested:
        for path in paths:
            if any(_same_or_related(path, target) for target in requested):
                return True
        return False
    return any(not _looks_like_scratch(path) for path in paths)


def changed_paths_relevant(changed_paths: list[str] | tuple[str, ...], requested: tuple[str, ...]) -> bool:
    normalized = tuple(p for p in (_normalize_path(x) for x in changed_paths) if p)
    if not normalized:
        return False
    if requested:
        return any(_same_or_related(path, target) for path in normalized for target in requested)
    return any(not _looks_like_scratch(path) for path in normalized)


_NO_TESTS_RE = re.compile(r"no tests ran|collected 0 items|\bran 0 tests?\b|\b0 passed\b|no tests (were )?(found|collected)")
_ZERO_FAILURE_RE = re.compile(r"\b0 (failed|failures?|errors?)\b|failures?=0|errors?=0")
# Complete failure words, not embedded tokens in passing test IDs such as
# tests/test_error_handling.py or ErrorHandler.test.js.
_FAIL_WORD_RE = re.compile(
    r"(?<![\w.-])(?:fail|failed|failure|failures|error|errors)(?![\w.-])"
)


def test_succeeded(status: str, result: str = "", error_message: str = "") -> bool:
    if str(status or "").strip().lower() not in {"ok", "success", "passed", "pass"}:
        return False
    # Terminal tools may serialize their execution envelope into result. A
    # literal `error: null` is not a test failure, but a nonzero exit or an
    # actual transport error must never count as successful verification.
    try:
        envelope = json.loads(result)
    except (ValueError, TypeError):
        envelope = None
    if isinstance(envelope, dict) and "exit_code" in envelope:
        code = envelope.get("exit_code")
        output = envelope.get("output")
        if (type(code) is not int or code != 0 or envelope.get("error")
                or not isinstance(output, str) or not output.strip()):
            return False
        result = output

    text = f"{result} {error_message}".lower()
    # A run that executed no tests proves nothing.
    if _NO_TESTS_RE.search(text):
        return False
    text = _ZERO_FAILURE_RE.sub(" ", text)
    return "traceback" not in text and not _FAIL_WORD_RE.search(text)


def _normalize_path(value: str) -> str:
    raw = str(value or "").strip().strip("'\"")
    if not raw or "\x00" in raw or raw.startswith(("http://", "https://")):
        return ""
    raw = raw.replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    return raw


def _same_or_related(path: str, target: str) -> bool:
    a = path.casefold().rstrip("/")
    b = target.casefold().rstrip("/")
    if a == b:
        return True
    # Suffix matches are only trusted when the longer path is absolute (e.g. a
    # sandbox prefix). A relative decoy such as "evil/app/calc.py" must not
    # count as work on "app/calc.py".
    if a.endswith("/" + b):
        return a.startswith("/")
    if b.endswith("/" + a):
        return b.startswith("/")
    return False


def _looks_like_scratch(path: str) -> bool:
    p = PurePosixPath(path)
    name = p.name.casefold()
    if name in SCRATCH_NAMES:
        return True
    if name.startswith(("tmp_", "scratch_", "repro_", "debug_")):
        return True
    in_scratch = any(part.casefold() in {"tmp", "temp", ".tmp", "scratch"} for part in p.parts)
    if not in_scratch:
        return False
    # Isolated Git worktrees commonly live under Hermes' scratch directory.
    # Their tracked source is not a throwaway probe. Require real local Git
    # evidence, not a caller-supplied 'tracked' flag; unknown/untracked paths
    # and failures remain scratch. Probe filenames above never qualify.
    try:
        actual = Path(path)
        if not actual.is_absolute() or not actual.is_file() or actual.is_symlink():
            return True
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("GIT_")}
        check = subprocess.run(
            ["git", "-C", str(actual.parent), "ls-files", "--error-unmatch", "--", actual.name],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=1, env=env, check=False)
        return check.returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        return True
