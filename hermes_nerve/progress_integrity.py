"""Deterministic progress/tool classification for nervous-system QoL guards.

This module has no provider calls and no persistent state. It only classifies
the current turn/tool evidence so recovery controls cannot confuse activity
with task-relative progress.
"""
from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath
from typing import Any

CHANGE_REQUIRED_RE = re.compile(r"\b(?:fix|implement|modify|patch|repair|refactor|edit|update|add|create|remove|delete|rename|change|replace)\b", re.IGNORECASE)
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
    "rm ", "rm -", "unlink ", "mkdir ", "rmdir ",
)
TERMINAL_TEST_PREFIXES = (
    "pytest", "python -m pytest", "python3 -m pytest", "python -m unittest",
    "python3 -m unittest", "npm test", "pnpm test", "yarn test", "go test", "cargo test",
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
    if str(tool_name or "").strip().lower() == "terminal":
        command = str(args.get("command") or "")
        # Redirection provides an explicit write target.
        for raw in re.findall(r"(?<![0-9&])>{1,2}\s*([^\s;&|]+)", command):
            path = _normalize_path(raw)
            if path and path not in paths:
                paths.append(path)
        # Known mutation commands carry path operands even when the terminal tool
        # exposes only a command string. Ignore flags and non-path script tokens.
        for segment in _shell_segments(command):
            try:
                tokens = shlex.split(segment)
            except ValueError:
                continue
            lowered = " ".join(token.lower() for token in tokens)
            if not any(lowered == prefix.strip() or lowered.startswith(prefix) for prefix in TERMINAL_MUTATION_PREFIXES):
                continue
            for raw in tokens[1:]:
                if not raw or raw.startswith("-"):
                    continue
                if "/" in raw or "\\" in raw or re.search(r"\.[A-Za-z0-9]{1,12}$", raw):
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


def test_succeeded(
    status: str, result: str = "", error_message: str = "", exit_code: int | None = None
) -> bool:
    if str(status or "").strip().lower() not in {"ok", "success", "passed", "pass"}:
        return False
    if exit_code not in {None, 0}:
        return False
    text = f"{result} {error_message}".lower()
    if "traceback (most recent call last)" in text:
        return False
    if re.search(r"\b[1-9][0-9]*\s+(?:failed|failures|errors?)\b", text):
        return False
    if re.search(r"\b(?:failed|failures|errors?)\s*[:=]\s*[1-9][0-9]*\b", text):
        return False
    return True


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