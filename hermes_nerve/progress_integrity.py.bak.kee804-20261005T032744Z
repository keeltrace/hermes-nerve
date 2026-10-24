"""Deterministic progress/tool classification for nervous-system QoL guards.

This module has no provider calls and no persistent state. It only classifies
the current turn/tool evidence so recovery controls cannot confuse activity
with task-relative progress.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

CHANGE_REQUIRED_RE = re.compile(r"\b(?:fix|implement|modify|patch|repair|refactor|edit|update)\b", re.IGNORECASE)
VERIFY_REQUIRED_RE = re.compile(r"\b(?:test|tests|verify|verification|regression|check)\b", re.IGNORECASE)
PATH_RE = re.compile(r"(?<![\w.-])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.[A-Za-z0-9]{1,12})(?![\w.-])")

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


def change_required(user_message: str) -> bool:
    return bool(CHANGE_REQUIRED_RE.search(str(user_message or "")))


def verification_required(user_message: str) -> bool:
    return bool(VERIFY_REQUIRED_RE.search(str(user_message or "")))


def requested_paths(user_message: str) -> tuple[str, ...]:
    seen: list[str] = []
    for raw in PATH_RE.findall(str(user_message or "")):
        path = _normalize_path(raw)
        if path and path not in seen:
            seen.append(path)
    return tuple(seen[:32])


def _shell_segments(command: str) -> list[str]:
    return [part.strip().lower() for part in re.split(r"(?:&&|\|\||;|\n)", str(command or "")) if part.strip()]


def tool_kind(tool_name: str, args: dict[str, Any] | None = None) -> str:
    selected = str(tool_name or "").strip().lower()
    for kind, aliases in TOOL_ALIASES.items():
        if selected in aliases:
            return kind
    if selected != "terminal":
        return ""
    for segment in _shell_segments(str((args or {}).get("command") or "")):
        if any(segment == p.strip() or segment.startswith(p) for p in TERMINAL_MUTATION_PREFIXES):
            return "mutation"
        if ">" in segment and not any(op in segment for op in (">=", "2>", "&>")):
            return "mutation"
        if any(segment == p or segment.startswith(p + " ") for p in TERMINAL_TEST_PREFIXES):
            return "test"
    return ""



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


def test_succeeded(status: str, result: str = "", error_message: str = "") -> bool:
    if str(status or "").strip().lower() not in {"ok", "success", "passed", "pass"}:
        return False
    text = f"{result} {error_message}".lower()
    return not any(token in text for token in ("failed", "failure", "error", "traceback"))


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
    return a.endswith("/" + b) or b.endswith("/" + a)


def _looks_like_scratch(path: str) -> bool:
    p = PurePosixPath(path)
    name = p.name.casefold()
    if name in SCRATCH_NAMES:
        return True
    if name.startswith(("tmp_", "scratch_", "repro_", "debug_")):
        return True
    return any(part.casefold() in {"tmp", "temp", ".tmp", "scratch"} for part in p.parts)
