"""Deterministic risk classification for Nerve HITL v2.

Replaces "model confidence" as the interruption trigger with an explicit,
inspectable risk model. Every tool call is first classified by bounded
deterministic rules; the probabilistic decision model (Jev) is consulted only
for calls the rules cannot recognize.

Risk categories (ordered by severity):

  safe_routine          read-only, no effects; execute automatically
  authorized_project_work  reversible effects inside the approved project
                        scope (cwd/workdir, temp dirs); execute automatically
  uncertain             not recognized; consult the decision model, then a
                        bounded second deterministic pass
  consequential         externally visible, destructive, privileged,
                        sensitive, or paid; request specific human approval
  prohibited            clearly dangerous; block with a concrete explanation

Design constraints:

- Every field of RiskAssessment is plain text a human can read. Nothing in an
  approval message is ever derived from a model confidence number.
- The classifier is conservative: unrecognized commands are ``uncertain``,
  never ``safe_routine``.
- Rule matches record their evidence so the approval message can cite the
  exact argument, path, or flag that triggered the classification.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from .policy_tables import load_table as _load_policy_table
except ImportError:  # standalone module load (operand-type harness imports risk.py directly)
    import importlib.util as _importlib_util
    import sys as _sys

    _policy_loader_spec = _importlib_util.spec_from_file_location(
        "nerve_standalone_policy_tables",
        Path(__file__).resolve().parent / "policy_tables.py")
    if _policy_loader_spec is None or _policy_loader_spec.loader is None:
        raise ImportError("policy loader could not be located next to risk.py")
    _policy_loader = _importlib_util.module_from_spec(_policy_loader_spec)
    _sys.modules[_policy_loader_spec.name] = _policy_loader
    _policy_loader_spec.loader.exec_module(_policy_loader)
    _load_policy_table = _policy_loader.load_table

# --- Categories -----------------------------------------------------------------

SAFE = "safe_routine"
PROJECT = "authorized_project_work"
UNCERTAIN = "uncertain"
CONSEQUENTIAL = "consequential"
PROHIBITED = "prohibited"

_SEVERITY = {SAFE: 0, PROJECT: 1, UNCERTAIN: 2, CONSEQUENTIAL: 3, PROHIBITED: 4}

REVERSIBLE = "reversible"
PARTIAL = "partially reversible"
IRREVERSIBLE = "irreversible"


@dataclass
class RiskAssessment:
    """Result of classifying one tool call. All text fields are human-facing."""

    category: str
    tool_name: str
    action_label: str = "Operation"
    summary: str = ""          # What is the agent trying to do?
    why: str = ""              # Why does this need permission? (empty for auto-allow)
    impact: str = ""           # What happens if approved?
    risk_notes: str = ""       # What could go wrong?
    reversibility: str = PARTIAL
    evidence: list[str] = field(default_factory=list)   # concrete findings
    safer_alternative: str = ""                         # suggested equivalent, if any
    shape: dict[str, Any] = field(default_factory=dict) # approval-key material
    display: str = ""          # redacted one-line display of the action
    bounded: bool = False      # UNCERTAIN but bounded/reversible: run, don't ask

    @property
    def needs_approval(self) -> bool:
        return self.category in (CONSEQUENTIAL, UNCERTAIN)

    @property
    def auto_allow(self) -> bool:
        return self.category in (SAFE, PROJECT)

    def merge(self, other: "RiskAssessment") -> "RiskAssessment":
        """Combine two assessments of the same call (e.g. pipeline segments)."""
        if _SEVERITY[other.category] <= _SEVERITY[self.category]:
            return self
        merged = RiskAssessment(
            category=other.category,
            tool_name=self.tool_name,
            action_label=other.action_label,
            summary=self.summary and other.summary,
            why=other.why,
            impact=other.impact,
            risk_notes=other.risk_notes,
            reversibility=other.reversibility,
            evidence=self.evidence + other.evidence,
            safer_alternative=other.safer_alternative or self.safer_alternative,
            shape=self.shape,
            display=self.display or other.display,
        )
        return merged


# --- Path classification --------------------------------------------------------

_HOME = Path.home()

_SYSTEM_PREFIXES = ("/etc", "/boot", "/usr", "/bin", "/sbin", "/lib", "/lib64",
                    "/opt", "/var", "/sys", "/proc", "/dev")
_DEVICE_RE = re.compile(r"^/dev/")
_SENSITIVE_HOME_DIRS = (".ssh", ".aws", ".gnupg", ".docker", ".config/gcloud", ".config/gh", ".kube",
                        ".kube", ".docker", ".password-store", ".vault")
_SENSITIVE_HOME_FILES = (".netrc", ".git-credentials", ".wgetrc", ".curlrc",
                         ".npmrc", ".pypirc", ".gitconfig", ".bashrc", ".zshrc",
                         ".profile", ".bash_profile", ".zprofile", ".zshenv")
_SENSITIVE_NAME_RE = re.compile(
    r"(id_rsa|id_ed25519|id_ecdsa|identity|\.pem$|\.key$|\.p12$|\.pfx$|"
    r"\.kdbx$|\.keystore$|\.jceks$|"
    r"credential|secret|token|password|authorized_keys|known_hosts)",
    re.IGNORECASE,
)
_ENV_FILE_RE = re.compile(r"^\.env(\..+)?$")

# Files a backdoor would touch: persistence mechanisms.
_PERSISTENCE_TARGETS = (
    "authorized_keys", ".bashrc", ".zshrc", ".profile", ".bash_profile",
    ".zprofile", ".zshenv", "crontab", ".gitconfig",
)


def _expand(token: str, workdir=None) -> Path:
    """Expand a path token; relative tokens join the EXECUTION workdir.

    Round-3: relative command targets are relative to the command's working
    directory (the claimed one), not the gate process's cwd — otherwise
    `cat sshd_config` with workdir=/etc/ssh would classify the gate's own
    directory and miss the sensitive location entirely.
    """
    p = Path(os.path.expandvars(os.path.expanduser(token)))
    if workdir is not None and not p.is_absolute():
        p = Path(workdir) / p
    return p


def temp_roots() -> list[str]:
    roots = ["/tmp", "/var/tmp"]
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        roots.append(os.path.abspath(tmpdir))
    return roots


def _within(resolved: Path, root: Path) -> bool:
    """True when *resolved* is *root* or lies beneath it.

    Uses os.path.commonpath, NOT startswith: '/x/proj-secret'.startswith(
    '/x/proj') is True, which once classified a sibling directory as inside
    the project scope.
    """
    try:
        return os.path.commonpath([str(resolved), str(root)]) == str(root)
    except ValueError:
        return False


# Roots that can never define an authorized project scope. If a caller claims
# one of these as its workdir, nothing is project-local: every path falls back
# to system/home/outside classification.
_SCOPE_HOSTILE_ROOTS = frozenset({
    "/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/opt", "/proc",
    "/root", "/run", "/sbin", "/srv", "/sys", "/usr", "/var", "/tmp",
    "/var/tmp", str(_HOME), str(_HOME.parent),
})


def valid_scope_root(wd: Path) -> bool:
    """True when *wd* may act as an authorized project-scope boundary.

    A workdir is trustworthy only if it canonicalizes to an existing directory
    that is not / (or home, or an ancestor of home, or a system root). The
    agent supplies the workdir argument, so the gate verifies the claim instead
    of trusting it: claiming '/' must never make system files project-local.
    """
    try:
        wd = Path(wd).resolve(strict=False)
    except OSError:
        return False
    if str(wd) in _SCOPE_HOSTILE_ROOTS:
        return False
    if not wd.is_dir():
        return False
    # An ancestor of home is never a project scope (/, /home, ...).
    if _within(_HOME, wd) and str(wd) != str(_HOME):
        return False
    # Round-3: a sensitive home location (e.g. ~/.ssh) or a system location
    # (e.g. /etc/ssh) can never define an authorized project scope either.
    if _home_sensitive(wd) not in (None, "dotenv"):
        return False
    if _protected_system_match(str.__str__(str(wd)), "/"):
        return False
    if any(str(wd).startswith(p + "/") or str(wd) == p for p in _SYSTEM_PREFIXES):
        return False
    return True


# System locations whose contents are protected regardless of any claimed
# working directory (round-3 review: existing paths are not authorization).
# Protected system locations, loaded once from the packaged plaintext policy
# table (auditable data; see hermes_nerve/policy/blocked_system_prefixes.txt).
# A load failure raises TableLoadError at import: the policy never silently
# runs with a missing or weakened table.
_BLOCKED_SYSTEM_PREFIXES = _load_policy_table("blocked_system_prefixes.txt")


def _protected_system_match(query: str, suffix: str) -> bool:
    """Boolean-only policy; suffix '/' selects scope boundaries, '' all prefixes.

    The protected locations load from the packaged plaintext policy table
    ``hermes_nerve/policy/blocked_system_prefixes.txt`` (auditable data).
    """
    if type(query) is not str:
        return False
    if type(suffix) is not str:
        return False
    return any(query == p or query.startswith(p + suffix) for p in _BLOCKED_SYSTEM_PREFIXES)


def _home_sensitive(resolved: Path) -> str | None:
    """Scope token when *resolved* is a sensitive home location, else None."""
    try:
        if not _within(resolved, _HOME):
            return None
        rel = str(resolved.relative_to(_HOME))
    except (ValueError, OSError):
        return None
    parts = Path(rel).parts
    # Round-4 D2: nested entries like ".config/gh" or ".config/gcloud" must
    # match at any depth segment-exactly, not only the first path component.
    for d in _SENSITIVE_HOME_DIRS:
        if rel == d or rel.startswith(d + "/"):
            return "sensitive-home"
    if parts and parts[0] in _SENSITIVE_HOME_FILES:
        return "sensitive-home"
    if _SENSITIVE_NAME_RE.search(Path(rel).name):
        return "sensitive-home"
    if _ENV_FILE_RE.match(Path(rel).name):
        return "dotenv"
    return None


def _protected_system_read(resolved: Path) -> bool:
    # str(obj) may preserve a str subclass returned by obj.__str__. Strip that
    # subclass before comparing protected policy literals or calling methods.
    s = str.__str__(str(resolved))
    return _protected_system_match(s, "")


def sensitive_read_target(path: Path, scope: str | None = None) -> bool:
    """True when READING *path* must never be treated as routine.

    Round-3 review: sensitive reads escalate regardless of the supplied
    working directory - protected system locations (/etc/ssh, /etc/shadow,
    /root, ...) and credential-like names escalate even outside home.
    """
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        resolved = path
    if scope is None:
        scope = classify_scope(resolved)
    if scope in ("sensitive-home", "device", "kernelfs"):
        return True
    if _protected_system_read(resolved):
        return True
    if _SENSITIVE_NAME_RE.search(resolved.name):
        return True
    # Round-4 D2: .env files hold secrets wherever they live; reading one
    # puts credentials into the session context. (WRITES to a project .env
    # stay project work: reversible and reviewable via history.)
    if _ENV_FILE_RE.match(resolved.name):
        return True
    return False


def classify_scope(resolved: Path) -> str:
    """Scope buckets WITHOUT the workdir check (precedence-safe core)."""
    if _DEVICE_RE.match(str(resolved)):
        return "device"
    if str(resolved).startswith("/proc") or str(resolved).startswith("/sys"):
        return "kernelfs"
    # Precedence: _home_sensitive wins over tmp roots entirely. A Hermes home
    # may live under a temporary directory (isolated runners, profile
    # sandboxes); a credential path — including the home's own .env — inside
    # it must never classify as routine "tmp".
    hs = _home_sensitive(resolved)
    if hs is not None:
        return hs
    if any(_within(resolved, Path(r)) for r in temp_roots()):
        return "tmp"
    if any(str(resolved).startswith(p + "/") or str(resolved) == p for p in _SYSTEM_PREFIXES):
        return "system"
    return ""


def classify_path(path: Path, workdir: Path) -> str:
    """Bucket a path into a scope token used by rules and shape keys.

    Round-3 precedence: sensitive-home and protected system locations win
    over a claimed workdir; the workdir only ever classifies paths that are
    not already protected by an absolute rule.
    """
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        resolved = path
    home = _HOME
    if _DEVICE_RE.match(str(resolved)):
        return "device"
    if str(resolved).startswith("/proc") or str(resolved).startswith("/sys"):
        return "kernelfs"
    # Precedence: sensitive-home beats tmp roots (mirror classify_scope).
    # dotenv keeps its original tail position: a project .env inside an
    # approved workdir stays workdir work; only homes under tmp roots gain a
    # guarantee here — their credential files (non-dotenv) can never fall
    # through to routine "tmp".
    hs = _home_sensitive(resolved)
    if hs is not None and hs != "dotenv":
        return hs
    if any(_within(resolved, Path(r)) for r in temp_roots()):
        return "tmp"
    try:
        wd = workdir.resolve(strict=False)
    except OSError:
        wd = workdir
    if valid_scope_root(wd) and _within(resolved, wd):
        return "workdir"
    if any(str(resolved).startswith(p + "/") or str(resolved) == p for p in _SYSTEM_PREFIXES):
        return "system"
    if hs == "dotenv":
        return "dotenv"
    if _within(resolved, home):
        return "home"
    return "outside"


def _is_sensitive_scope(scope: str) -> bool:
    return scope in ("sensitive-home", "device", "kernelfs")


# --- Command parsing ------------------------------------------------------------

_SHELL_META_RE = re.compile(r"\$\(|`")
_PIPELINE_SPLIT_RE = re.compile(
    # && and || first; then ; | newline/CR; then a lone & that is not part
    # of && (alternation order) nor a redirect (&> / >& stay intact).
    r"&&|\|\||;|\||\r|\n|(?<![>&])&(?!>)")

# Wrapper executables we look through (their real command is the next token).
_WRAPPER_COMMANDS = {"nohup", "stdbuf", "env", "timeout", "nice", "setsid", "time", "watch"}

_SAFE_ENV_PREFIXES = re.compile(r"^(LC_[A-Z_]+|LANG|TZ|PYTHON[A-Z_]*|PYTHONDONTWRITEBYTECODE|"
                                r"GO[A-Z_]*|CARGO[A-Z_]*|GIT_[A-Z_]+|NO_COLOR|CI)=", re.IGNORECASE)

# Flags whose VALUES are inert (purely local, no targets): safe to normalize in keys.
_INERT_VALUE_FLAGS = {"-j", "--jobs", "-p", "--parallel", "-l", "--loglevel", "--log-level",
                      "-n", "--count", "-C", "--context"}


def split_pipeline(command: str) -> list[str]:
    """Split a shell command into pipeline segments. Returns [command] if no meta."""
    return [seg.strip() for seg in _PIPELINE_SPLIT_RE.split(command) if seg.strip()]


def strip_redirection(segment: str) -> tuple[str, list[tuple[str, str]]]:
    """Return (clean_segment, [(op, target_token), ...]) for shell redirects.

    Deliberately naive about quotes in exotic cases; redirect parsing is
    best-effort and the segment text is still classified as a whole when in
    doubt (redirects inside quotes degrade to extra tokens, which only ever
    makes classification MORE conservative, not less).
    """
    redirects: list[tuple[str, str]] = []
    tokens = segment.split()
    clean: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in (">", ">>", "<", ">&", "&>") and i + 1 < len(tokens):
            redirects.append((tok, tokens[i + 1]))
            i += 2
            continue
        m = re.match(r"^(>>|>|<)(\S+)$", tok)
        if m:
            redirects.append((m.group(1), m.group(2)))
            i += 1
            continue
        clean.append(tok)
        i += 1
    return " ".join(clean), redirects


def parse_argv(segment: str) -> list[str] | None:
    try:
        # shlex also accepts stream objects; their read() results can preserve
        # str subclasses through token concatenation. Policy comparisons must
        # use exact builtins, not attacker-defined equality/hash/lower methods.
        parts = [str.__str__(part) for part in shlex.split(segment, posix=True)]
    except ValueError:
        return None
    return parts or None


def strip_wrappers(parts: list[str]) -> tuple[list[str], list[str]]:
    """Look through wrapper commands and safe env assignments.

    Returns (argv, notes). Unsafe env prefixes are NOT stripped: the segment
    stays on its first token and will classify as unrecognized/uncertain.
    """
    notes: list[str] = []
    idx = 0
    while idx < len(parts):
        tok = parts[idx]
        if "=" in tok and not tok.startswith("/") and _SAFE_ENV_PREFIXES.match(tok):
            idx += 1
            continue
        if tok in _WRAPPER_COMMANDS and idx + 1 < len(parts):
            # `timeout 30 cmd` / `nice -n 5 cmd`: skip wrapper + its option values
            idx += 1
            while idx < len(parts) and parts[idx].startswith("-"):
                idx += 1
                if idx < len(parts) and parts[idx - 1] in _INERT_VALUE_FLAGS:
                    idx += 1
            # one bare numeric arg (timeout 30)
            if idx < len(parts) and re.fullmatch(r"\d+(\.\d+)?[smh]?", parts[idx] or ""):
                idx += 1
            continue
        break
    return parts[idx:], notes


# --- Command rule tables --------------------------------------------------------

_READ_ONLY_COMMANDS = frozenset({
    "pwd", "ls", "cat", "head", "tail", "wc", "stat", "du", "df", "file",
    "readlink", "basename", "dirname", "realpath", "which", "whereis", "whoami",
    "id", "uname", "uptime", "free", "ps", "printenv", "env", "hostname",
    "grep", "rg", "jq", "cut", "sort", "uniq", "tr", "comm", "cmp", "diff",
    "md5sum", "sha256sum", "sha1sum", "cksum", "tree", "hexdump", "xxd", "od",
    "strings", "lsof", "ss", "ip", "date", "sleep", "true", "false", "test",
    "column", "nl", "tac", "rev", "fold", "fmt", "join", "paste", "seq",
    "iconv", "base64", "xargs",  # xargs flagged below when it builds dangerous cmds
    "tput", "less", "more", "most", "bat", "fd", "locate", "history",
})

_SAFE_GIT_READ = frozenset({
    "status", "log", "diff", "show", "rev-parse", "ls-files", "ls-tree",
    "describe", "grep", "blame", "shortlog", "reflog", "cat-file", "version",
})

# Subcommand-restricted read-only forms: `git stash` bare is `stash push`
# (mutating), so only the listed sub-subcommands are reads.
_GIT_READ_SUBCOMMANDS = {
    "stash": frozenset({"list", "show", "status"}),
    "worktree": frozenset({"list"}),
    "remote": frozenset({"get-url", "show"}),
}

# Flags that keep `git tag` / `git branch` in listing (read-only) mode.
_GIT_LIST_FLAGS = frozenset({
    "-l", "--list", "-a", "-r", "-v", "-vv", "--show-current", "-n",
    "--merged", "--no-merged", "--contains", "--no-contains", "--points-at",
    "--sort", "--format", "--color", "--column", "--omit-empty", "-i",
})

# `git config` flags that always mutate, regardless of positional count.
_GIT_CONFIG_MUTATING_FLAGS = frozenset({
    "--add", "--unset", "--unset-all", "--replace-all", "--rename-section",
    "--remove-section", "-e", "--edit",
})

_PROJECT_GIT = frozenset({
    "add", "commit", "stash", "checkout", "switch", "branch", "tag", "merge",
    "rebase", "cherry-pick", "revert", "mv", "restore", "reset", "am", "apply",
    "fetch", "pull", "clone", "init", "config", "remote", "worktree", "stash",
})


def _nonflag_args(argv: list[str]) -> list[str]:
    return [a for a in argv[2:] if not a.startswith("-")]


def git_read_only(sub: str, argv: list[str]) -> bool:
    """True only for git invocations that provably change nothing.

    A subcommand having a read-only form is not enough: `git config user.email
    x`, `git stash`, `git tag v1`, `git branch b2`, and `git remote add origin
    URL` are all writes that used to ride the safe read-only path because the
    SUBCOMMAND alone was checked. The arguments decide.
    """
    if sub in _SAFE_GIT_READ:
        return True
    args_ = _nonflag_args(argv)
    flags = [a for a in argv[2:] if a.startswith("-")]
    if sub == "config":
        if any(f.split("=")[0] in _GIT_CONFIG_MUTATING_FLAGS for f in flags):
            return False
        # `git config`, `git config -l`, `git config --get KEY` read;
        # `git config KEY VALUE` (two positionals) writes.
        return len(args_) <= 1
    if sub in _GIT_READ_SUBCOMMANDS:
        if sub == "stash":
            # bare `git stash` == `git stash push`: requires an explicit read sub
            return bool(args_) and args_[0] in _GIT_READ_SUBCOMMANDS[sub]
        # worktree/remote: bare (with -v/--verbose flags) lists remotes/trees
        if not args_:
            return all(f.split("=")[0] in {"-v", "--verbose"} for f in flags)
        return args_[0] in _GIT_READ_SUBCOMMANDS[sub]
    if sub in ("tag", "branch"):
        if not args_:
            return all(f.split("=")[0] in _GIT_LIST_FLAGS for f in flags)
        # a single pattern position is a read only with an explicit list flag
        return (("-l" in flags or "--list" in flags) and len(args_) == 1
                and all(f.split("=")[0] in _GIT_LIST_FLAGS for f in flags))
    return False


_PUBLICATION_GIT = frozenset({"push", "remote"})

_INTERPRETERS = {"python", "python3", "python2", "node", "deno", "bun", "ruby",
                 "perl", "php", "lua", "Rscript"}

# Test runners that execute project code in-place: routine project work.
_TEST_RUNNERS = {"pytest", "py.test", "tox", "nox", "unittest", "mocha", "jest",
                 "vitest", "cargo-test"}

_BUILD_TOOLS = {"make", "cmake", "meson", "cargo", "go", "gradle", "mvn", "sbt",
                "bazel", "buck", "ninja"}

_PKG_MANAGERS = {"pip", "pip3", "npm", "pnpm", "yarn", "uv", "poetry", "cargo",
                 "gem", "composer", "dotnet"}

_SYSTEM_PKG = {"apt", "apt-get", "aptitude", "yum", "dnf", "zypper", "pacman",
               "brew", "snap", "flatpak", "dpkg", "rpm"}

_NETWORK_FETCH = {"curl", "wget", "http", "https", "fetch", "aria2c", "axel"}

_REMOTE_EXEC = {"ssh", "mosh", "telnet", "nc", "ncat", "netcat", "socat"}

_UPLOAD = {"scp", "rsync", "sftp", "ftp", "gcloud", "aws", "az", "gh"}

_PROCESS_CTRL = {"kill", "pkill", "killall", "pgrep"}

_SHUTDOWN = {"shutdown", "reboot", "halt", "poweroff", "init", "systemctl"}

_DELETION = {"rm", "shred", "unlink", "rmdir", "find"}

_PRIVILEGE = {"sudo", "su", "doas", "pkexec"}

# Disk-destroyer command names, loaded once from the packaged plaintext
# policy table (auditable data; see hermes_nerve/policy/disk_executables.txt).
_DISK_EXECUTABLES = frozenset(_load_policy_table("disk_executables.txt"))


def _is_disk_executable(query: str) -> bool:
    """Closed boolean policy boundary; no table or literal escapes to callers."""
    if type(query) is not str:
        return False
    return query in _DISK_EXECUTABLES

_ARTIFACT_NAMES = {"build", "dist", "out", "target", "node_modules", "__pycache__",
                   ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv",
                   ".tox", ".nox", "coverage", ".coverage", "htmlcov", ".eggs",
                   "vendor", ".next", ".nuxt", ".turbo", ".parcel-cache", "tmp",
                   "temp", ".cache", "logs"}


def _artifact_deletion(argv: list[str], workdir: Path) -> bool:
    """True when every rm target is a recognisable build artifact inside workdir."""
    targets = [a for a in argv[1:] if not a.startswith("-")]
    if not targets:
        return False
    for t in targets:
        p = _expand(t)
        scope = classify_path(p, workdir)
        if scope != "workdir":
            return False
        name = p.resolve(strict=False).name if p.name not in ("", ".") else str(p)
        if name in _ARTIFACT_NAMES or name.endswith((".pyc", ".pyo", ".log", ".tmp")):
            continue
        return False
    return True


def _has_flag(argv: list[str], *flags: str) -> bool:
    return any(a in flags for a in argv[1:])


def _flag_value(argv: list[str], flag: str) -> str | None:
    for i, a in enumerate(argv[1:], start=1):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(f"{flag}="):
            return a.split("=", 1)[1]
    return None


# --- Per-segment classification --------------------------------------------------

def classify_segment(segment: str, workdir: Path, evidence: list[str]) -> RiskAssessment:
    """Classify one pipeline segment. Unknown => UNCERTAIN (conservative)."""
    parts = parse_argv(segment)
    if parts is None:
        return RiskAssessment(UNCERTAIN, "terminal", action_label="Unparseable command",
                              why="command could not be safely parsed",
                              risk_notes="quoting/encoding prevented static analysis",
                              display=segment[:120])
    argv, _notes = strip_wrappers(parts)
    if not argv:
        return RiskAssessment(SAFE, "terminal", summary="no-op")
    exe = argv[0]
    base = os.path.basename(exe)

    def out(cat: str, label: str, summary: str, why: str = "", impact: str = "",
            risk: str = "", rev: str = PARTIAL, alt: str = "") -> RiskAssessment:
        return RiskAssessment(cat, "terminal", action_label=label, summary=summary,
                              why=why, impact=impact, risk_notes=risk, reversibility=rev,
                              evidence=list(evidence), safer_alternative=alt,
                              display=segment[:160])

    # Round-4 D1: a path-qualified executable is NEVER trusted merely because
    # its basename matches a recognized command. ./ls, ./cat, /tmp/pytest can
    # be entirely different programs — they go to the model band.
    if "/" in exe:
        return out(UNCERTAIN, "Path-qualified executable", segment,
                   why=f"'{exe}' is invoked by explicit path, so the trusted-command "
                       f"catalog does not apply (a different program can hide behind a "
                       f"familiar name like ./ls or /tmp/pytest)",
                   impact="the binary at that exact path runs with this user's permissions",
                   risk="unrecognized executable code",
                   rev=REVERSIBLE)
    known = exe

    # ---- environment disclosure -------------------------------------------------
    if known in {"printenv", "env", "set", "export", "declare", "typeset", "compgen"}:
        return out(CONSEQUENTIAL, "Environment disclosure",
                   f"{segment}",
                   why="it prints process environment variables, which hold API keys and session secrets",
                   impact="credentials would enter the session transcript",
                   risk="exposed keys must be rotated",
                   rev=IRREVERSIBLE)

    # ---- shutdown / host-level -------------------------------------------------
    if known in _SHUTDOWN:
        if known == "systemctl" and (argv[1:2] == ["status"] or argv[1:2] == ["list-units"]):
            return out(SAFE, "Service status", f"shows the status of a systemd unit")
        if known == "systemctl" and argv[1:2] in (["stop"], ["restart"], ["start"], ["disable"], ["enable"]):
            return out(CONSEQUENTIAL, "Service control",
                       f"{segment}",
                       why="it changes the state of a system service",
                       impact="the service starts/stops/restarts on this machine",
                       risk="stopping a needed service can break other work",
                       rev=REVERSIBLE)
        return out(PROHIBITED, "Host power/state control",
                   f"{segment}",
                   why="it changes the power state of the host",
                   impact="the machine may shut down or reboot, killing every running session",
                   risk="all running agents, shells, and unsaved work are lost",
                   rev=IRREVERSIBLE)

    # ---- disk destroyers --------------------------------------------------------
    if _is_disk_executable(known):
        return out(PROHIBITED, "Disk-level operation",
                   f"{segment}",
                   why="it writes directly to disk devices or partitions",
                   impact="filesystems or partitions can be destroyed",
                   risk="total, unrecoverable data loss on the target device",
                   rev=IRREVERSIBLE)

    # ---- privilege escalation ---------------------------------------------------
    if known in _PRIVILEGE:
        sub = argv[1:]
        if known in ("sudo", "doas") and (sub and sub[0] in ("-l", "--list")):
            return out(SAFE, "Privilege listing", f"lists allowed sudo operations")
        # Reclassify the wrapped command, then force at least CONSEQUENTIAL.
        inner = classify_segment(" ".join(sub) if sub else "", workdir, evidence + [f"under '{known}'"])
        if inner.category == PROHIBITED:
            inner.evidence.append(f"runs as root: {segment}")
            inner.why = f"it runs a destructive operation with root privileges ({known})"
            return inner
        # Always surface the privilege escalation in the wording, even when the
        # wrapped command would be consequential on its own (sudo apt install...).
        inner.category = CONSEQUENTIAL if _SEVERITY[inner.category] < _SEVERITY[CONSEQUENTIAL] else inner.category
        if inner.category == CONSEQUENTIAL and "Privileged" not in inner.action_label:
            inner.action_label = f"Privileged operation (via {known})"
        inner.why = (f"it executes '{' '.join(sub[:3])}' with elevated (root) privileges — "
                     f"{inner.why}" if inner.why else
                     f"it starts a shell or command as another user via '{known}'")
        inner.risk_notes = inner.risk_notes or (
            "root actions can modify any file on the system and are not scoped to this project")
        inner.evidence.append(f"wrapped in '{known}'")
        return inner

    # ---- remote code execution / shells ------------------------------------------
    if known in ("bash", "sh", "zsh", "dash", "fish", "ksh"):
        script = next((a for a in argv[1:] if not a.startswith("-")), None)
        if script is None:
            return out(UNCERTAIN, "Shell invocation", segment,
                       why="an interactive/inline shell cannot be statically verified",
                       risk="the shell can run any command")
        scope = classify_path(_expand(script), workdir)
        if scope == "workdir":
            return out(UNCERTAIN, "Project script", segment,
                       why="project scripts can contain arbitrary commands",
                       impact="the script runs with this user's permissions",
                       risk="script contents were not verified")
        return out(UNCERTAIN, "Script execution", segment,
                   why="running a script executes arbitrary code",
                   impact="the script runs with this user's permissions",
                   risk=f"script at '{script}' (scope: {scope}) was not verified",
                   alt="read the script before running it")

    # ---- interpreters -------------------------------------------------------------
    if known in _INTERPRETERS:
        if _has_flag(argv, "-c", "-e", "--eval", "-m"):
            if known in ("python", "python3", "python2") and _flag_value(argv, "-m") in (
                    "pytest", "py.test"):
                return out(PROJECT, "Project tests", segment,
                           impact="runs the project test suite in-place",
                           risk="test code executes with user permissions",
                           rev=REVERSIBLE)
            return out(UNCERTAIN, "Inline code execution", segment,
                       why="inline code cannot be statically verified",
                       impact="the code runs with this user's permissions",
                       risk="arbitrary code effects")
        script = next((a for a in argv[1:] if not a.startswith("-") and "=" not in a), None)
        if script:
            scope = classify_path(_expand(script), workdir)
            if scope == "workdir":
                return out(UNCERTAIN, "Interpreter on project file", segment,
                           why="project code can have arbitrary side effects",
                           impact="runs with user permissions inside this project",
                           risk="code was not verified")
            return out(UNCERTAIN, "Interpreter execution", segment,
                       why="running code from outside the project",
                       impact="runs with user permissions",
                       risk=f"file scope: {scope}")
        return out(UNCERTAIN, "Interpreter (interactive)", segment,
                   why="cannot verify what will be executed")

    # ---- deletion ------------------------------------------------------------------
    if known in ("rm", "shred", "unlink"):
        dangerous = [t for t in argv[1:] if not t.startswith("-") and classify_path(
            _expand(t, workdir), workdir) in ("system", "sensitive-home", "device", "kernelfs")]
        home_str = str(_HOME)

        def _broad_target(t: str) -> bool:
            """True when the target resolves to /, the home dir, an ancestor of it, or a home-level glob."""
            p = _expand(t)
            try:
                resolved = str(Path(os.path.abspath(p)))
            except OSError:
                resolved = str(p)
            if resolved in ("/", home_str):
                return True
            if home_str.startswith(resolved.rstrip("/") + "/"):
                return True  # an ancestor of home (e.g. /home)
            if t.endswith("/*") or t == "*":
                base = _expand(t[:-2]) if t.endswith("/*") else workdir
                try:
                    base_resolved = str(Path(os.path.abspath(base)))
                except OSError:
                    base_resolved = str(base)
                if base_resolved in ("/", home_str):
                    return True
            return False

        dangerous += [t for t in argv[1:] if not t.startswith("-") and _broad_target(t)]
        recursive = _has_flag(argv, "-r", "-R", "--recursive")
        parent_escape = any(".." in t for t in argv[1:] if not t.startswith("-"))
        if dangerous:
            return out(PROHIBITED, "Destructive deletion",
                       segment,
                       why=f"it deletes protected or broad target(s): {', '.join(dict.fromkeys(dangerous[:3]))}",
                       impact="files in system, credential, or home-directory locations are destroyed",
                       risk="can break the OS, SSH access, stored credentials, or destroy the entire home directory; deletion is unrecoverable",
                       rev=IRREVERSIBLE)
        if recursive and parent_escape:
            return out(PROHIBITED, "Destructive deletion",
                       segment,
                       why="it recursively deletes parent-directory targets",
                       impact="mass deletion outside any single project scope",
                       risk="unrecoverable; may destroy credentials, configs, and unrelated projects",
                       rev=IRREVERSIBLE)
        if _artifact_deletion(argv, workdir):
            return out(PROJECT, "Artifact cleanup", segment,
                       impact="removes build artifacts/cache inside this project",
                       risk="artifacts are regenerated by the build; effectively reversible",
                       rev=REVERSIBLE)
        return out(CONSEQUENTIAL, "File deletion",
                   segment,
                   why="deletion cannot be undone",
                   impact="the named files are permanently removed",
                   risk="if any target is unexpected, its data is unrecoverable",
                   rev=IRREVERSIBLE,
                   alt="move files to /tmp instead of deleting, if they might be needed")
    if known == "rmdir":
        return out(PROJECT, "Directory removal", segment,
                   impact="removes empty directories only", rev=REVERSIBLE)

    # ---- find with side effects ------------------------------------------------------
    if known == "find":
        joined = " ".join(argv[1:])
        if "-delete" in argv[1:] or re.search(r"-exec\s+(rm|shred|dd|mkfs)\b", joined):
            return out(CONSEQUENTIAL, "Deletion via find",
                       segment,
                       why="find -delete/-exec rm permanently removes matched files",
                       impact="all matched paths are deleted",
                       risk="an over-broad match expression deletes unintended files",
                       rev=IRREVERSIBLE,
                       alt="run find without -delete first and review the list")
        if re.search(r"-exec\b|-execdir\b|-fprintf\b|-fprint\b", joined):
            return out(UNCERTAIN, "find with -exec", segment,
                       why="find -exec can run arbitrary commands per matched file")
        return out(SAFE, "File search", segment)

    # ---- git ----------------------------------------------------------------------------
    if known == "git":
        sub = argv[1] if len(argv) > 1 else ""
        if not sub or sub.startswith("-"):
            return out(UNCERTAIN, "git invocation", segment)
        if git_read_only(sub, argv):
            if any(a in {"--ext-diff", "--textconv"} or a.startswith("--ext-diff=") or a.startswith("--textconv=")
                   for a in argv[2:]):
                return out(UNCERTAIN, "Git with external helpers",
                           segment,
                           why="--ext-diff/--textconv make git execute external helper programs",
                           impact="configured helper binaries run against repository content",
                           risk="helpers are arbitrary code registered in git config")
            return out(SAFE, "Git inspection", segment)
        # ---- destructive git (round-3): never auto-run as project work ----
        stash_args = [a for a in argv[2:] if not a.startswith("-")]
        if sub == "stash" and stash_args and stash_args[0] in ("clear", "drop"):
            what = "every stash entry" if stash_args[0] == "clear" else "the top stash entry"
            return out(CONSEQUENTIAL, "Destroy stashed work", segment,
                       why=f"git stash {stash_args[0]} discards {what} across this repository",
                       impact="stashed (uncommitted) work is removed from the stash list",
                       risk="recovery is only possible via git fsck before garbage collection",
                       rev=PARTIAL)
        if sub == "branch":
            force_delete = (_has_flag(argv, "-D") or _has_flag(argv, "-B")
                            or (_has_flag(argv, "--delete") and _has_flag(argv, "--force")))
            if force_delete:
                return out(CONSEQUENTIAL, "Force-delete branch", segment,
                           why="it deletes a branch even if it has unmerged commits",
                           impact="commits unique to that branch become unreachable from any branch",
                           risk="recoverable only via reflog until garbage collection",
                           rev=PARTIAL,
                           alt="git branch -d refuses unmerged branches; merge or tag first")
        if sub == "config":
            cfg_args = [a for a in argv[2:] if not a.startswith("-")]
            mutating = (len(cfg_args) >= 2
                        or any(f.split("=")[0] in _GIT_CONFIG_MUTATING_FLAGS for f in argv[2:]))
            global_scope = _has_flag(argv, "--global", "--system")
            if mutating and global_scope:
                return out(CONSEQUENTIAL, "Global Git configuration change", segment,
                           why="it rewrites git configuration for EVERY repository on this machine",
                           impact="settings like hooksPath, credential.helper or core.pager apply machine-wide",
                           risk="hook/credential settings can change what code runs on later git operations",
                           rev=REVERSIBLE)
        if sub == "worktree" and [a for a in argv[2:] if not a.startswith("-")][:1] == ["remove"]:
            forced = _has_flag(argv, "--force")
            return out(CONSEQUENTIAL, "Remove worktree", segment,
                       why="it deletes a linked working tree and its administrative data",
                       impact="uncommitted changes in that worktree are deleted",
                       risk=("forced removal deletes even dirty worktrees" if forced
                             else "git refuses dirty worktrees unless --force is given"),
                       rev=IRREVERSIBLE if forced else PARTIAL)
        if sub in ("push",):
            target = next((a for a in argv[2:] if not a.startswith("-")), "remote")
            force = _has_flag(argv, "--force", "-f", "--force-with-lease")
            return out(CONSEQUENTIAL, "External publication",
                       segment,
                       why=f"it publishes local commits to the remote repository '{target}'",
                       impact="the pushed branch becomes visible on the remote; collaborators can fetch it",
                       risk=("force-push can discard others' work on the remote branch"
                             if force else "published commits are hard to fully retract once seen/pulled"),
                       rev=PARTIAL,
                       alt=("use --force-with-lease instead of --force/-f" if force else ""))
        if sub == "remote" and argv[2:3] in (["add"], ["remove"], ["set-url"]):
            return out(PROJECT, "Git config change", segment,
                       impact="updates this repository's remote configuration",
                       rev=REVERSIBLE)
        if sub == "reset":
            if _has_flag(argv, "--hard"):
                return out(CONSEQUENTIAL, "Discard uncommitted work",
                           segment,
                           why="git reset --hard permanently discards uncommitted changes",
                           impact="all uncommitted modifications in the working tree are erased",
                           risk="uncommitted work cannot be recovered",
                           rev=IRREVERSIBLE,
                           alt="git stash push --include-untracked keeps the changes recoverable")
            return out(PROJECT, "Git index operation", segment,
                       impact="moves the branch pointer / unstages changes; committed history preserved",
                       rev=REVERSIBLE)
        if sub == "clean":
            if _has_flag(argv, "-n", "--dry-run"):
                return out(SAFE, "Git inspection", segment + " (dry run)")
            return out(CONSEQUENTIAL, "Delete untracked files",
                       segment,
                       why="git clean permanently deletes untracked files",
                       impact="untracked files (possibly including secrets or local-only work) are removed",
                       risk="untracked means unrecoverable once deleted",
                       rev=IRREVERSIBLE,
                       alt="run 'git clean -nd' first and review the list")
        if sub in ("checkout", "restore"):
            if "--" in argv[1:] or (sub == "restore" and not _has_flag(argv, "--staged")):
                return out(CONSEQUENTIAL, "Discard file changes",
                           segment,
                           why="it discards uncommitted modifications to the named paths",
                           impact="those uncommitted edits are erased",
                           risk="uncommitted work cannot be recovered",
                           rev=IRREVERSIBLE,
                           alt="git stash push -- <paths> keeps the changes recoverable")
            return out(PROJECT, "Git branch operation", segment,
                       impact="switches branches / updates the working tree from git objects",
                       rev=REVERSIBLE)
        if sub in _PROJECT_GIT:
            return out(PROJECT, "Git operation", segment,
                       impact="modifies repository state locally (commits/stages/branches)",
                       risk="git operations are recoverable via reflog",
                       rev=REVERSIBLE)
        return out(UNCERTAIN, "git operation", segment,
                   why=f"unrecognized git subcommand '{sub}'")

    # ---- package managers -----------------------------------------------------------------
    if known in _PKG_MANAGERS:
        sub = argv[1] if len(argv) > 1 else ""
        if known in ("pip", "pip3", "uv") and sub in ("install",):
            url_install = any(a.startswith(("http://", "https://", "git+")) for a in argv[2:])
            if url_install:
                return out(CONSEQUENTIAL, "Install from URL",
                           segment,
                           why="installing directly from a URL executes code from that source",
                           impact="the package (and its setup code) runs on this machine",
                           risk="URL-sourced packages bypass registry review",
                           rev=PARTIAL,
                           alt="pin the dependency in requirements.txt/pyproject and install by name")
            if _has_flag(argv, "-g", "--global") or (known == "npm" and _has_flag(argv, "-g", "--global")):
                return out(CONSEQUENTIAL, "Global package install",
                           segment,
                           why="it installs a package machine-wide",
                           impact="new global executables on PATH",
                           risk="affects every project and shell on this machine",
                           rev=PARTIAL)
            return out(UNCERTAIN, "Package installation",
                       segment,
                       why="package installation runs third-party code (setup hooks/postinstall scripts)",
                       impact="new dependencies are added to the project environment",
                       risk="the package's install code runs with user permissions",
                       rev=REVERSIBLE,
                       alt="review the package on its registry page before installing")
        if known == "npm" and sub in ("install", "ci"):
            if sub == "ci":
                return out(PROJECT, "Dependency install (lockfile)", segment,
                           impact="installs exactly the locked dependency tree",
                           risk="runs lifecycle scripts of locked packages",
                           rev=REVERSIBLE)
            return out(UNCERTAIN, "Dependency installation", segment,
                       why="npm install runs third-party lifecycle scripts",
                       impact="node_modules and possibly lockfile change",
                       rev=REVERSIBLE)
        if known in ("npm", "pnpm", "yarn") and sub == "publish":
            return out(CONSEQUENTIAL, "External publication",
                       segment,
                       why="it publishes the package to the public registry",
                       impact="the package becomes publicly installable worldwide",
                       risk="publishing is effectively irreversible; secrets in the package are exposed",
                       rev=IRREVERSIBLE)
        if known in ("twine",) or (known in ("uv",) and sub == "publish"):
            return out(CONSEQUENTIAL, "External publication",
                       segment, why="it uploads the package to a package index",
                       impact="publicly available", risk="cannot be fully retracted",
                       rev=IRREVERSIBLE)
        if sub in ("test", "run") and known in ("npm", "pnpm", "yarn"):
            return out(UNCERTAIN, "Package script", segment,
                       why="package scripts can run arbitrary commands")
        if sub in ("list", "ls", "show", "info", "view", "outdated", "audit"):
            return out(SAFE, "Package inspection", segment)
        return out(UNCERTAIN, "Package manager", segment,
                   why=f"unrecognized {known} subcommand '{sub}'")

    # ---- system package managers -----------------------------------------------------------
    if known in _SYSTEM_PKG:
        sub = argv[1] if len(argv) > 1 else ""
        if sub in ("search", "list", "info", "show", "query"):
            return out(SAFE, "Package inspection", segment)
        if sub in ("install", "remove", "upgrade", "update", "purge", "autoremove"):
            return out(CONSEQUENTIAL, "System package change",
                       segment,
                       why="it modifies system-wide packages",
                       impact="installed software changes for the whole machine",
                       risk="dependency changes can break system tools",
                       rev=PARTIAL)
        return out(UNCERTAIN, "System package manager", segment)

    # ---- network ----------------------------------------------------------------------------
    if known in _NETWORK_FETCH:
        url = next((a for a in argv[1:] if a.startswith(("http://", "https://", "ftp://"))), "")
        method = (_flag_value(argv, "-X") or _flag_value(argv, "--request") or
                  ("POST" if _has_flag(argv, "-d", "--data", "--data-raw", "--data-binary", "-F", "--form") else "GET"))
        upload = _has_flag(argv, "-T", "--upload-file")
        pipe_to_shell = False  # handled at pipeline level
        if upload:
            return out(CONSEQUENTIAL, "Network upload",
                       segment,
                       why=f"it uploads a file to {url or 'a remote host'}",
                       impact="the file leaves this machine",
                       risk="uploaded data may be stored or shared by the remote party",
                       rev=IRREVERSIBLE)
        if method not in ("GET", "HEAD", "OPTIONS"):
            return out(CONSEQUENTIAL, "External mutation",
                       segment,
                       why=f"it sends a {method} request to {url or 'a remote host'}",
                       impact="the remote service is asked to change state",
                       risk="external effects may be irreversible and are audited only remotely",
                       rev=PARTIAL)
        if _flag_value(argv, "-o") or _has_flag(argv, "--output", "-O"):
            return out(UNCERTAIN, "Network download",
                       segment,
                       why="downloads write a file from an external source",
                       impact="a file from the network is written locally",
                       risk="downloaded content is untrusted",
                       rev=REVERSIBLE)
        return out(UNCERTAIN, "Network fetch",
                   segment,
                   why="it contacts an external host",
                   impact="sends a read request to the remote service",
                   risk="external network access; response content is untrusted")

    # ---- remote execution / raw sockets ---------------------------------------------------------
    if known in _REMOTE_EXEC:
        if known == "ssh":
            has_cmd = len(argv) > 2 and not argv[2].startswith("-")
            if has_cmd:
                return out(CONSEQUENTIAL, "Remote execution",
                           segment,
                           why=f"it executes a command on the remote host '{argv[1] if len(argv)>1 else '?'}'",
                           impact="the command runs on another machine with your account",
                           risk="remote effects cannot be inspected or undone from here",
                           rev=PARTIAL)
            return out(UNCERTAIN, "Remote session", segment,
                       why="interactive remote shell cannot be statically verified")
        return out(CONSEQUENTIAL, "Raw network connection",
                   segment,
                   why="raw socket tools can connect/expose arbitrary ports",
                   impact="network connection to the specified host/port",
                   risk="can be used to exfiltrate data or bind listening ports",
                   rev=PARTIAL)

    # ---- upload tools ------------------------------------------------------------------------------
    if known in ("scp", "rsync", "sftp", "gcloud", "aws", "az", "gh"):
        sub = argv[1] if len(argv) > 1 else ""
        if known == "gh" and sub in ("pr", "issue", "repo", "release", "gist"):
            create_like = argv[2:3] and argv[2] in ("create", "upload", "fork", "edit", "delete", "merge")
            if create_like:
                return out(CONSEQUENTIAL, "External publication",
                           segment,
                           why="it creates or changes content on GitHub",
                           impact="content becomes visible on github.com",
                           risk="published content is public; deletion may not fully retract it",
                           rev=PARTIAL)
            return out(SAFE, "GitHub inspection", segment)
        if known == "aws" and sub in ("s3",) and argv[2:3] and argv[2] in ("cp", "sync", "mv"):
            return out(CONSEQUENTIAL, "External transfer",
                       segment,
                       why="it transfers objects to/from cloud storage",
                       impact="data moves to or from a remote bucket",
                       risk="uploads leave the machine; wrong-direction copies can overwrite remote data",
                       rev=PARTIAL)
        if known == "gcloud" and sub in ("compute", "storage", "deployment-manager", "projects"):
            return out(CONSEQUENTIAL, "Cloud resource change",
                       segment, why="it modifies cloud resources (billed, externally visible)",
                       impact="cloud infrastructure or storage changes",
                       risk="may incur cost or affect production resources",
                       rev=PARTIAL)
        return out(CONSEQUENTIAL, "External transfer",
                   segment,
                   why="it transfers data to a remote host",
                   impact="data leaves this machine",
                   risk="remote copy may persist indefinitely",
                   rev=IRREVERSIBLE)

    # ---- process control ----------------------------------------------------------------------------
    if known in _PROCESS_CTRL:
        if known == "pgrep":
            return out(SAFE, "Process lookup", segment)
        if known in ("kill", "pkill", "killall"):
            if known == "kill" and any(re.fullmatch(r"-9", a) for a in argv[1:1+1]) is False and not _has_flag(argv, "-9"):
                pass
            return out(CONSEQUENTIAL, "Process termination",
                       segment,
                       why="it terminates processes",
                       impact="matching processes are stopped immediately",
                       risk="may kill unrelated processes (including this agent's own helpers)",
                       rev=PARTIAL)

    # ---- writes via tee -----------------------------------------------------------------------------
    if known == "tee":
        targets = [a for a in argv[1:] if not a.startswith("-")]
        scopes = [classify_path(_expand(t, workdir), workdir) for t in targets]
        if any(s in ("system", "sensitive-home", "device", "kernelfs") for s in scopes):
            return out(PROHIBITED, "Protected file write",
                       segment,
                       why=f"it writes to protected location(s): {', '.join(t for t, s in zip(targets, scopes) if s in ('system','sensitive-home','device','kernelfs'))}",
                       impact="system or credential files are overwritten/appended",
                       risk="can break the OS or insert persistence/backdoors",
                       rev=IRREVERSIBLE)
        if any(s == "outside" for s in scopes):
            return out(CONSEQUENTIAL, "Write outside project",
                       segment,
                       why="it writes files outside the project scope",
                       impact="files outside this project are created/modified",
                       rev=PARTIAL)
        return out(PROJECT, "Project file write", segment, rev=REVERSIBLE)

    # ---- chmod / chown / mount -------------------------------------------------------------------------
    if known in ("chmod", "chown", "chgrp", "umask"):
        targets = [a for a in argv[2:] if not a.startswith("-")] if len(argv) > 2 else []
        bad = [t for t in targets if classify_path(_expand(t, workdir), workdir) in ("system", "sensitive-home", "device")]
        world_writable = any(mode in ("777", "a+rwx", "+x") or "777" in mode for mode in argv[1:2])
        if bad:
            return out(PROHIBITED if known != "umask" else UNCERTAIN,
                       "Protected permission change",
                       segment,
                       why=f"it changes permissions/ownership on protected location(s): {', '.join(bad[:3])}",
                       impact="system or credential files change access mode",
                       risk="can expose credentials to every user or break system security boundaries",
                       rev=PARTIAL)
        if world_writable and targets:
            return out(CONSEQUENTIAL, "Broad permission change",
                       segment,
                       why="it makes files world-writable/executable",
                       impact="any local user can modify or execute the targets",
                       risk="world-writable files are a local privilege-escalation vector",
                       rev=PARTIAL)
        return out(PROJECT, "Permission change", segment, rev=REVERSIBLE)
    if known in ("mount", "umount", "losetup", "swapon", "swapoff"):
        return out(CONSEQUENTIAL, "Mount operation",
                   segment,
                   why="it changes the system's mounted filesystems",
                   impact="new filesystems become visible or existing ones are detached",
                   rev=PARTIAL)

    # ---- cron / persistence ---------------------------------------------------------------------------------
    if known == "crontab":
        if _has_flag(argv, "-r", "--remove"):
            return out(PROHIBITED, "Crontab removal", segment,
                       why="it removes the user's scheduled jobs",
                       impact="all cron jobs are deleted",
                       rev=IRREVERSIBLE)
        if _has_flag(argv, "-e", "-l"):
            return out(UNCERTAIN, "Crontab edit", segment,
                       why="crontab changes persist and run automatically")
        return out(UNCERTAIN, "Crontab change", segment,
                   why="crontab changes install scheduled execution")

    # ---- file info / misc safe ---------------------------------------------------------------------------------
    if known in _READ_ONLY_COMMANDS:
        if known == "xargs":
            return out(UNCERTAIN, "xargs construction", segment,
                       why="xargs can build and run arbitrary commands")
        if known == "rg" and any(a == "--pre" or a.startswith("--pre=") or a == "--pre-glob"
                                 or a.startswith("--pre-glob=") for a in argv[1:]):
            return out(UNCERTAIN, "ripgrep with preprocessor",
                       segment,
                       why="--pre/--pre-glob execute an external program on every searched file",
                       impact="the configured preprocessor binary runs against repository content",
                       risk="the preprocessor is arbitrary code from rg flags or config")
        if known == "base64" and _has_flag(argv, "-d", "--decode"):
            return out(UNCERTAIN, "Decoded content", segment,
                       why="decoded output may hide obfuscated commands")
        if known in ("less", "more", "most", "bat"):
            return out(SAFE, "Paged reading", segment)
        if known == "history":
            return out(SAFE, "History listing", segment)
        # Reading tools pointed at credential/sensitive locations are NOT
        # routine: copying a private key to the model's context is exposure.
        sensitive_targets = [t for t in argv[1:] if not t.startswith("-")
                             and sensitive_read_target(_expand(t, workdir))]
        if sensitive_targets:
            return out(CONSEQUENTIAL, "Sensitive file access",
                       segment,
                       why=f"it reads credential/protected location(s): {', '.join(dict.fromkeys(sensitive_targets[:3]))}",
                       impact="the file contents (potentially private keys or credentials) enter the session context",
                       risk="exposed credentials are hard to rotate; treat any copy as a leak",
                       rev=REVERSIBLE)
        return out(SAFE, "Read-only inspection", segment)
    if known in ("echo", "printf", "set", "export", "alias", "unalias", "type", "command", "builtin"):
        return out(SAFE, "Shell builtin", segment)
    if known in ("mkdir", "touch", "cp", "ln", "install"):
        # copy targets
        if known == "cp" or known == "install":
            srcs = [a for a in argv[1:-1] if not a.startswith("-")]
            dest = argv[-1] if argv[-1:] and not argv[-1].startswith("-") else ""
            src_scopes = [classify_path(_expand(s, workdir), workdir) for s in srcs]
            dest_scope = classify_path(_expand(dest, workdir), workdir) if dest else "outside"
            # destination severity FIRST: an authorized_keys overwrite is a
            # prohibited persistence write even when the source is also sensitive
            if dest_scope in ("system", "sensitive-home", "device", "kernelfs"):
                return out(PROHIBITED if dest_scope in ("sensitive-home", "device", "kernelfs") else CONSEQUENTIAL,
                           "Protected write",
                           segment,
                           why=f"it writes to a protected location ({dest})",
                           impact="system/credential files are overwritten",
                           risk="can break the system or insert backdoors",
                           rev=IRREVERSIBLE)
            if any(s in ("sensitive-home", "device", "kernelfs") for s in src_scopes):
                return out(CONSEQUENTIAL, "Credential copy",
                           segment,
                           why="it copies credential/system files",
                           impact="sensitive data is duplicated to the destination",
                           risk="copies of credentials are easy to lose track of",
                           rev=PARTIAL)
            if dest_scope == "outside":
                return out(CONSEQUENTIAL, "Write outside project",
                           segment,
                           why="it writes outside the project scope",
                           impact="files outside this project are modified",
                           rev=PARTIAL)
            return out(PROJECT, "Project file copy", segment, rev=REVERSIBLE)
        return out(PROJECT, "Project file creation", segment, rev=REVERSIBLE)
    if known in ("sed", "awk", "perl"):
        inplace = _has_flag(argv, "-i", "--in-place") or ("-i" in " ".join(argv[1:2] or []) )
        prog = " ".join(a for a in argv[1:] if not a.startswith("-"))[:200]
        writes_files = bool(re.search(r"system\s*\(|open\s*\(|>\s*\"", prog))
        if inplace or writes_files:
            if inplace:
                files = [a for a in argv[1:] if not a.startswith("-") and not re.match(r"^[sypq]", a)]
                bad = [f for f in files if classify_path(_expand(f, workdir), workdir) in ("system", "sensitive-home")]
                if bad:
                    return out(PROHIBITED, "Protected in-place edit",
                               segment,
                               why=f"in-place edit of protected file(s): {', '.join(bad[:3])}",
                               rev=IRREVERSIBLE)
                return out(PROJECT, "In-place project edit", segment, rev=PARTIAL)
            return out(UNCERTAIN, "Scripting with side effects", segment,
                       why="the program text can write files or run commands")
        return out(SAFE, "Text transformation", segment)

    # ---- build tools / test runners ---------------------------------------------------------------------------------
    if known in ("pytest", "py.test", "python-pytest"):
        return out(PROJECT, "Project tests", segment,
                   impact="runs the project test suite",
                   risk="test code executes with user permissions",
                   rev=REVERSIBLE)
    if known in _TEST_RUNNERS:
        return out(PROJECT, "Project tests", segment,
                   impact="runs the project's tests",
                   rev=REVERSIBLE)
    if known in _BUILD_TOOLS:
        return out(UNCERTAIN, "Build invocation",
                   segment,
                   why="build systems execute project-defined scripts with arbitrary effects",
                   impact="compiles the project; build scripts run with user permissions",
                   rev=REVERSIBLE)
    if known in ("docker", "podman", "nerdctl", "containerd", "kubectl", "helm", "terraform", "pulumi", "ansible-playbook"):
        sub = argv[1] if len(argv) > 1 else ""
        if known == "docker" and sub in ("ps", "images", "inspect", "logs", "version", "info", "stats", "top"):
            return out(SAFE, "Container inspection", segment)
        if known == "kubectl" and sub in ("get", "describe", "logs", "top", "config", "version"):
            return out(SAFE, "Cluster inspection", segment)
        if sub in ("apply", "delete", "destroy", "create", "up", "down", "run", "exec", "rm", "kill", "scale", "rollback", "import", "load", "prune", "system"):
            return out(CONSEQUENTIAL, "Infrastructure change",
                       segment,
                       why="it creates/destroys infrastructure or containers",
                       impact="running services, containers, or cloud resources change state",
                       risk="can terminate workloads; terraform destroy deletes managed resources",
                       rev=PARTIAL)
        if sub in ("build", "plan", "diff", "validate", "push", "pull", "tag"):
            if sub == "push":
                return out(CONSEQUENTIAL, "External publication",
                           segment, why="it publishes an image/artifact to a registry",
                           impact="the artifact becomes available remotely", rev=PARTIAL)
            return out(UNCERTAIN, "Build/plan", segment,
                       why="builds and plans execute project code")
        return out(UNCERTAIN, "Container/IaC command", segment)

    # ---- unknown ------------------------------------------------------------------------------------------------------
    label = f"Unknown executable '{exe}'"
    return out(UNCERTAIN, label, segment,
               why="the command is not in the recognized-safe catalog",
               impact="unknown; the command runs with this user's permissions",
               risk="unverified behavior")


# --- Top-level entry ----------------------------------------------------------------


def _tool_command(tool_name: str, args: dict[str, Any]) -> str:
    for key in ("command", "cmd", "script"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _workdir_for(tool_name: str, args: dict[str, Any]) -> Path:
    """Resolve the scope boundary for a call — WITHOUT trusting the caller.

    The workdir argument comes from the agent being gated, so it is only a
    claim. It counts as an authorized scope only when it canonicalizes to an
    existing directory that is not / (or home, or an ancestor of home, or a
    system root). Missing/file/lying claims fall back to the process working
    directory, which is where Hermes actually executes; if even that is a
    scope-hostile root, a sentinel path grants no scope at all.
    """
    raw = None
    for key in ("workdir", "cwd", "working_dir"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            raw = v.strip()
            break
    if raw is not None:
        p = Path(os.path.expandvars(os.path.expanduser(raw)))
        try:
            claimed = p.resolve(strict=False)
        except OSError:
            claimed = p
        # Round 3: the claimed dir (if it exists) is where the command would
        # actually RUN, so relative targets must resolve against it even when
        # the location is sensitive/system. It confers NO authorization:
        # classify_path grants "workdir" scope only when valid_scope_root()
        # accepts the claim, and protected locations win precedence outright.
        if claimed.is_dir():
            return claimed
    try:
        cwd = Path(os.getcwd()).resolve(strict=False)
    except OSError:
        cwd = Path("/")
    if valid_scope_root(cwd):
        return cwd
    return Path("/nonexistent-nerve-scope")


# --- bounded-uncertainty policy (0.4.2) -------------------------------------
# Autonomous operation requires that an unrecognized but ordinary local command
# run without an operator prompt. These markers identify the cases where
# "unrecognized" is genuinely unbounded or irreversible; everything else that is
# a plain local command is treated as bounded (recorded, not interrupted).
# Unbounded-executable command names, loaded once from the packaged plaintext
# policy table (auditable data; see hermes_nerve/policy/unbounded_executables.txt).
_UNBOUNDED_EXECUTABLES = frozenset(_load_policy_table("unbounded_executables.txt"))


def _is_unbounded_executable(query: str) -> bool:
    """Closed boolean policy boundary; unknown operand types never see entries."""
    if type(query) is not str:
        return False
    return query in _UNBOUNDED_EXECUTABLES
_INTERPRETERS = frozenset({"python", "python3", "python3.12", "node", "bun",
                           "deno", "perl", "ruby", "php", "lua", "rscript"})
_UNBOUNDED_FLAG_MARKERS = (
    "--force", "-rf", "--recursive", "--delete", "--hard", "--no-verify",
    "--privileged", "--remote", "--ext-diff", "--textconv", "--pre",
    "--pre-glob", "--exec", "-exec", "-execdir", "--eval", "--upload-file",
    "--output-document", "-o", "--config", "--insecure", "-k",
)
_UNBOUNDED_SUBSTRINGS = ("| sh", "| bash", "| zsh", "| python", "$(", "`",
                         ">/dev/", "2>/dev/")
_SEGMENT_SPLIT = None


def _segments(command: str) -> list[str]:
    import re as _re
    return [seg.strip() for seg in _re.split(r"\|\||&&|\||;|\n", command) if seg.strip()]


_ENV_DISCLOSURE = frozenset({"printenv", "env", "set", "export", "declare",
                             "typeset", "compgen"})


def _segment_is_bounded(segment: str) -> bool:
    try:
        # shlex also accepts stream objects; their read() results can preserve
        # str subclasses through token concatenation. Policy comparisons must
        # use exact builtins, not attacker-defined equality/hash/lower methods.
        parts = [str.__str__(part) for part in shlex.split(segment, posix=True)]
    except ValueError:
        return False
    if not parts:
        return True
    executable = parts[0]
    if "/" in executable:
        return False
    base = executable.lower()
    if _is_unbounded_executable(base):
        return False
    if base in _ENV_DISCLOSURE:
        return False
    if any(arg in _UNBOUNDED_FLAG_MARKERS or arg.startswith("--force=") for arg in parts[1:]):
        return False
    if base in _INTERPRETERS:
        # Running a script file or inline code is unbounded; `python -m pytest`
        # and similar module invocations are ordinary development work.
        for arg in parts[1:]:
            if arg in {"-c", "-e", "--eval", "--exec"}:
                return False
            if arg.startswith(("/", "./", "../")):
                return False
    if base in {"git"}:
        joined = " ".join(parts[1:6])
        if any(v in joined for v in ("push", "clean", "reset --hard", "filter-branch", "submodule")):
            return False
    if base in {"rm", "mv", "cp"} and any(p in {"/*", "/", "~", "~/*"} for p in parts[1:]):
        return False
    if base == "find" and "-delete" in parts:
        return False
    return True


def bounded_uncertainty(tool_name: str, args: dict[str, Any],
                        workdir: Path | None = None) -> bool:
    """True when an unrecognized call is a plain, local, bounded command.

    Every pipeline segment is checked: a routine `grep ... | head` is bounded,
    while a segment that reaches another host, runs inline code, executes a
    script by path, escalates privilege, controls services/devices, publishes,
    or forces a remote rewrite makes the whole call unbounded.
    """
    command = _tool_command(tool_name, args)
    if not command:
        return False
    if any(marker in command for marker in _UNBOUNDED_SUBSTRINGS):
        return False
    segments = _segments(command)
    if not segments:
        return False
    return all(_segment_is_bounded(segment) for segment in segments)


def assess_tool_call(tool_name: str, args: dict[str, Any],
                     workdir: Path | None = None) -> RiskAssessment:
    """Classify a tool call. Main entry point used by the gate."""
    if workdir is None:
        workdir = _workdir_for(tool_name, args)

    # File-tool writes
    if tool_name in ("write_file", "patch", "apply_patch", "edit_file"):
        # The CONTENT is part of the authorized operation: approving a benign
        # config write must not pre-authorize writing a persistence line to
        # the same path. Keys therefore carry a digest of what would be written.
        content_fp = _content_digest(args)
        target = args.get("path") or args.get("file_path") or args.get("file") or ""
        p = _expand(str(target)) if target else None
        scope = classify_path(p, workdir) if p else "unknown"
        name = p.name if p else ""
        display = f"{tool_name} {target}"
        if scope in ("sensitive-home", "device", "kernelfs") or name in _PERSISTENCE_TARGETS:
            return RiskAssessment(
                PROHIBITED, tool_name,
                action_label="Protected file write",
                summary=display,
                why=f"'{target}' is a protected/credential location or a persistence mechanism",
                impact="credential files, shell startup files, or device nodes would be modified",
                risk_notes="writes to these locations can expose secrets or install backdoors",
                reversibility=IRREVERSIBLE,
                shape={"tool": tool_name, "path": str(p), "scope": scope, "content_sha256": content_fp},
                display=display)
        if scope == "system":
            return RiskAssessment(
                CONSEQUENTIAL, tool_name,
                action_label="System file write",
                summary=display,
                why=f"'{target}' is outside the project (system location)",
                impact="system configuration or program files change",
                risk_notes="can affect every user of this machine",
                reversibility=PARTIAL,
                shape={"tool": tool_name, "path": str(p), "scope": scope, "content_sha256": content_fp},
                display=display)
        if scope == "outside":
            return RiskAssessment(
                CONSEQUENTIAL, tool_name,
                action_label="Write outside project",
                summary=display,
                why=f"'{target}' is outside the approved project scope",
                impact="files outside this project are created or modified",
                risk_notes="changes outside the project are not covered by the session's working scope",
                reversibility=PARTIAL,
                shape={"tool": tool_name, "path": str(p), "scope": scope, "content_sha256": content_fp},
                display=display)
        if scope in ("workdir", "tmp", "home"):
            return RiskAssessment(
                PROJECT, tool_name,
                action_label="Project file write",
                summary=display,
                impact="the file changes inside the project scope",
                risk_notes="edits are reviewable via git diff / file history",
                reversibility=REVERSIBLE,
                shape={"tool": tool_name, "path": str(p), "scope": scope, "content_sha256": content_fp},
                display=display)
        return RiskAssessment(
            UNCERTAIN, tool_name, action_label="File write",
            summary=display, why="target path could not be classified",
            shape={"tool": tool_name, "path": str(p), "scope": scope, "content_sha256": content_fp}, display=display)

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
        expanded = [_expand(str(p_)) for p_ in paths if p_]
        sensitive = [str(p_) for p_ in expanded if sensitive_read_target(p_)]
        # Nested/alternate argument shapes (uri, nested objects, lists) must not
        # smuggle a protected target past the documented path keys.
        try:
            from . import catalog as _catalog
            sensitive.extend(_catalog.sensitive_path_args(args))
        except Exception:
            pass
        sensitive = list(dict.fromkeys(sensitive))
        display = f"{tool_name}({', '.join(str(p_) for p_ in paths[:2])}{'...' if len(paths) > 2 else ''})"
        if sensitive:
            return RiskAssessment(
                CONSEQUENTIAL, tool_name,
                action_label="Sensitive file access",
                summary=display,
                why=f"it reads credential/protected location(s): {', '.join(dict.fromkeys(sensitive[:3]))}",
                impact="the file contents (potentially private keys or credentials) enter the session context",
                risk_notes="exposed credentials are hard to rotate; treat any copy as a leak",
                reversibility=REVERSIBLE,
                shape={"tool": tool_name,
                       "paths": [str(p_) for p_ in expanded][:128],
                       "paths_sha256": _paths_digest(expanded),
                       "sensitive": sensitive[:8]},
                display=display)
        return RiskAssessment(
            SAFE, tool_name,
            action_label="File read",
            summary=display,
            impact="file contents enter the session context",
            shape={"tool": tool_name, "paths": [str(p_) for p_ in expanded][:128],
                   "paths_sha256": _paths_digest(expanded)},
            display=display)

    command = _tool_command(tool_name, args)
    if not command:
        # Non-shell tool: classify the OPERATION (tool identity + effect-bearing
        # arguments), not just the name. Unknown operations stay UNCERTAIN.
        from . import catalog as _catalog
        assessed = _catalog.classify_tool(tool_name, args, _workdir_for(tool_name, args))
        if assessed is not None:
            return assessed
        return RiskAssessment(
            UNCERTAIN, tool_name,
            action_label=f"{tool_name} call",
            summary=_short_args(args),
            why="tool effects depend on arguments that static rules cannot verify",
            shape={"tool": tool_name, "args_fp": _args_fingerprint(args)},
            display=f"{tool_name}({_short_args(args)}"[:160] + ")")

    if _SHELL_META_RE.search(command):
        # command substitution can hide anything: never auto-allow
        base = _assess_command_text(command, workdir)
        base.category = UNCERTAIN if _SEVERITY[base.category] < _SEVERITY[UNCERTAIN] else base.category
        base.why = (base.why or "") + " (command substitution present; full analysis unavailable)"
        return base
    assessed = _assess_command_text(command, workdir)
    if assessed.category == UNCERTAIN and bounded_uncertainty(tool_name, args, workdir):
        assessed.bounded = True
        assessed.why = ("unrecognized but bounded local command: no privilege, "
                        "remote, service, device, publish or force operations")
    return assessed


def _assess_segment_with_redirects(segment: str, workdir: Path) -> RiskAssessment:
    """Classify one segment including any shell redirection targets."""
    clean, redirects = strip_redirection(segment)
    assessment = classify_segment(clean or segment, workdir, [])
    for op, target in redirects:
        tscope = classify_path(_expand(target, workdir), workdir)
        if op in (">", ">>", ">&", "&>"):
            if tscope in ("system", "sensitive-home", "device", "kernelfs"):
                assessment = assessment.merge(RiskAssessment(
                    PROHIBITED, "terminal", action_label="Protected file write",
                    summary=f"{op} {target}",
                    why=f"it writes to protected location '{target}'",
                    impact="system or credential files are overwritten/appended",
                    risk_notes="can break the OS or insert persistence/backdoors",
                    reversibility=IRREVERSIBLE))
            elif tscope in ("workdir", "tmp"):
                assessment = assessment.merge(RiskAssessment(
                    PROJECT, "terminal", action_label="Local file write",
                    summary=f"{op} {target}",
                    impact=f"{'appends to' if op == '>>' else 'writes'} '{target}' inside the project scope",
                    reversibility=REVERSIBLE))
            else:
                assessment = assessment.merge(RiskAssessment(
                    CONSEQUENTIAL, "terminal",
                    action_label="Write outside project",
                    summary=f"{op} {target}",
                    why=f"it writes to '{target}' outside the project scope",
                    impact="files outside this project are modified",
                    reversibility=PARTIAL))
    if assessment.shape == {} or not assessment.shape:
        assessment.shape = _shape_key_material(assessment, clean, workdir)
    return assessment


def _assess_command_text(command: str, workdir: Path) -> RiskAssessment:
    segments = split_pipeline(command)
    if len(segments) > 1:
        results = [_assess_segment_with_redirects(s, workdir) for s in segments]
        combined = RiskAssessment(UNCERTAIN, "terminal", summary=command[:160],
                                  display=command[:160])
        # The authorized operation is the WHOLE pipeline: the shape embeds every
        # segment's argv/workdir/category material. A count alone ("2 segments")
        # once made `ls | head` and `curl evil | head` share one approval key.
        combined.shape = {"kind": "pipeline",
                          "segments": [r.shape for r in results]}
        for r in results:
            combined = combined.merge(r)
        combined.summary = command[:160]
        combined.display = command[:160]
        # Composition rules: streaming fetched/decoded content into a shell is
        # remote code execution regardless of how benign each side looks.
        _SHELL_NO_SCRIPT = {"sh", "bash", "zsh", "dash", "ksh", "fish"}

        def _feeds_shell(seg_result: RiskAssessment) -> bool:
            text = seg_result.display
            parts_ = parse_argv(text.split("&&")[-1].split("|")[-1].split(";")[-1].strip() or text)
            if not parts_:
                return False
            base_ = os.path.basename(parts_[0])
            return base_ in _SHELL_NO_SCRIPT and not any(
                a for a in parts_[1:] if not a.startswith("-"))

        sh_readers = [r for r in results if _feeds_shell(r)]
        if sh_readers and len(segments) >= 2:
            producers = [
                s for s, r in zip(segments, results)
                if os.path.basename((parse_argv(s) or [""])[0]) in
                (_NETWORK_FETCH | {"base64", "gzip", "gunzip", "xxd", "openssl", "echo", "printf", "cat"})
                and os.path.basename((parse_argv(s) or [""])[0]) not in _SHELL_NO_SCRIPT
            ]
            if producers:
                return RiskAssessment(
                    PROHIBITED, "terminal",
                    action_label="Remote/streamed code execution",
                    summary=command[:160],
                    why=f"'{producers[0]}' pipes content directly into a shell interpreter",
                    impact="the streamed content runs as code with this user's permissions",
                    risk_notes="the content comes from outside this project and was not reviewed before execution",
                    reversibility=IRREVERSIBLE,
                    evidence=[f"pipe-to-shell: {producers[0]} | <shell>"],
                    display=command[:160],
                    safer_alternative="download the script to a file, read it, then run it explicitly")
        # A pipeline whose every segment is recognized-safe/project work has no
        # unrecognized component: downgrade the UNCERTAIN baseline accordingly.
        if all(r.category in (SAFE, PROJECT) for r in results):
            combined.category = PROJECT if any(r.category == PROJECT for r in results) else SAFE
            if combined.category == SAFE:
                combined.action_label = "Read-only pipeline"
            else:
                combined.action_label = combined.action_label or "Pipeline"
        return combined

    assessment = _assess_segment_with_redirects(segments[0], workdir)
    if not assessment.display:
        assessment.display = command[:160]
    if not assessment.summary:
        assessment.summary = command[:160]
    return assessment


def _shape_key_material(assessment: RiskAssessment, clean_segment: str, workdir: Path) -> dict[str, Any]:
    argv = parse_argv(clean_segment) or []
    normalized: list[str] = []
    skip_next = False
    for i, tok in enumerate(argv):
        if skip_next:
            normalized.append("#")
            skip_next = False
            continue
        if tok in _INERT_VALUE_FLAGS:
            normalized.append(tok)
            skip_next = True
            continue
        # Positional numbers are NOT normalized away: `kill -9 1234` and
        # `kill -9 1` are different authorized operations (different PIDs),
        # as are `chmod 755 X` and `chmod 777 X`. Only the VALUES of inert
        # flags (-j 8, -n 50) are collapsed above.
        normalized.append(tok)
    return {"tool": "terminal", "argv": normalized,
            "workdir": str(workdir.resolve(strict=False)),
            "category": assessment.category}


def resolve_git_refs(workdir, refspecs, timeout=3.0):
    """Resolve git refspecs to commit SHAs (bounded, read-only).

    Used to bind consequential approvals to the actual EFFECT: an approved
    push authorizes publishing exactly the resolved commits, not whatever the
    branch points at later. Returns {refspec: sha} for refs that resolved.
    """
    out: dict[str, str] = {}
    wd = str(workdir)
    for ref in refspecs:
        try:
            proc = subprocess.run(
                ["git", "-C", wd, "rev-parse", "--verify", ref + "^{commit}"],
                capture_output=True, text=True, timeout=timeout)
            if proc.returncode == 0 and proc.stdout.strip():
                out[ref] = proc.stdout.strip().splitlines()[0]
        except (OSError, subprocess.SubprocessError):
            continue
    return out


def target_stat_signature(path) -> tuple:
    """Filesystem identity of a deletion/upload target (monkeypatchable)."""
    try:
        st = os.stat(path)
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    except OSError:
        return ("absent",)


def resolve_remote_url(workdir, remote, timeout=3.0):
    """Resolve a git remote's URL (bounded, read-only)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(workdir), "remote", "get-url", remote],
            capture_output=True, text=True, timeout=timeout)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def resolve_all_refs(workdir, mode, timeout=3.0):
    """Resolve every ref a --all/--tags/--mirror push would publish."""
    spec = {"--all": ["refs/heads"], "--tags": ["refs/tags"],
            "--mirror": ["refs/heads", "refs/tags"]}.get(mode, [])
    if not spec:
        return {}
    try:
        proc = subprocess.run(
            ["git", "-C", str(workdir), "for-each-ref",
             "--format=%(objectname) %(refname)"] + spec,
            capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            return {}
        out = {}
        for line in proc.stdout.splitlines():
            if line.strip():
                sha, _, ref = line.strip().partition(" ")
                out[ref] = sha
        return out
    except (OSError, subprocess.SubprocessError):
        return {}


def effect_identity(tool_name: str, args: dict[str, Any],
                    assessment: "RiskAssessment") -> str | None:
    """Content signature of the distinct EFFECT of a consequential call.

    Round-3 review: a permanent approval must authorize one distinct effect,
    not a command pattern. git push binds to the resolved commit SHAs (new
    commits -> new approval); deletions bind to the target's filesystem
    identity (recreated file -> new approval); uploads bind to the source
    file identities. Unresolvable effects get a per-invocation nonce so the
    operator re-approves every time instead of blanket-running.
    """
    if assessment.category not in (CONSEQUENTIAL, PROHIBITED):
        return None
    import hashlib as _hashlib

    def digest(material: str) -> str:
        return _hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()[:16]

    command = _tool_command(tool_name, args)
    workdir = _workdir_for(tool_name, args)
    if command:
        argv = parse_argv(command) or []
        base = os.path.basename(argv[0]) if argv else ""
        if base == "git" and len(argv) > 1 and argv[1] == "push":
            flags = [a for a in argv[2:] if a.startswith("-")]
            positional = [a for a in argv[2:] if not a.startswith("-")]
            remote = positional[0] if positional else "origin"
            # Round-4 D3: the DESTINATION is part of the effect. A previous
            # approval must not authorize publishing to a different remote URL.
            url = resolve_remote_url(workdir, remote)
            parts = [f"remote={remote}", f"url={url or 'UNRESOLVED-URL'}"]
            mode = next((f for f in flags if f in ("--all", "--tags", "--mirror")), None)
            if mode == "--mirror":
                # Round-5: --mirror publishes every ref in refs/* (heads, tags,
                # notes, remotes...). heads+tags resolution cannot prove the
                # full effect, so conservatively re-approve every invocation.
                return digest("git-push|mirror|nonce:" + os.urandom(8).hex())
            if mode:
                # Round-4 D3: bulk publications bind to EVERY ref they publish.
                refs = resolve_all_refs(workdir, mode)
                parts.append(f"mode={mode}")
                parts += [f"{k}={v}" for k, v in sorted(refs.items())]
                if not refs:
                    parts.append("nonce:" + os.urandom(8).hex())
            else:
                refspecs = positional[1:] or ["HEAD"]
                resolved = resolve_git_refs(workdir, refspecs)
                parts += [f"{r}={resolved.get(r, 'UNRESOLVED')}" for r in refspecs]
                if len(resolved) < len(refspecs):
                    parts.append("nonce:" + os.urandom(8).hex())
            return digest("git-push|" + "|".join(parts))
        if base in _DELETION and base != "find":
            targets = [a for a in argv[1:] if not a.startswith("-")]
            sigs = ";".join(f"{t}={target_stat_signature(_expand(t, workdir))}" for t in targets)
            return digest(f"delete|{sigs}")
        if base in _UPLOAD and base in ("scp", "rsync"):
            nonflag = [a for a in argv[1:] if not a.startswith("-")]
            srcs = nonflag[:-1] if len(nonflag) > 1 else []
            sigs = ";".join(f"{s}={target_stat_signature(_expand(s, workdir))}" for s in srcs)
            return digest(f"upload|{sigs}")
    if tool_name in ("write_file", "patch", "apply_patch", "edit_file"):
        # securely identifiable: path + exact content
        target = args.get("path") or args.get("file_path") or args.get("file") or ""
        content = args.get("content") or args.get("new_string") or args.get("text") or ""
        import hashlib as _h
        return _h.sha256(f"write|{target}|{content}".encode("utf-8", "replace")).hexdigest()[:16]
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
        import hashlib as _h
        return _h.sha256(("read|" + "|".join(sorted(str(p_) for p_ in paths)))
                          .encode("utf-8", "replace")).hexdigest()[:16]
    # Round-4 D4: no secure binder for this consequential effect (API POSTs,
    # ssh, chmod, installs, ...). The permanent approval key must NOT remain
    # reusable, so it is bound to a per-invocation nonce: every invocation
    # re-approves. Routine safe/project work never reaches this path.
    # search_files: content-scoped by searched paths (same scheme as read_file).
    import hashlib as _h
    return _h.sha256("unbound|" + os.urandom(8).hex()).hexdigest()[:16]


_SENSITIVE_ARG_KEY_RE = re.compile(
    r"token|secret|password|passwd|api[_-]?key|auth|credential|private[_-]?key", re.IGNORECASE)


def _args_fingerprint(args: dict[str, Any]) -> str:
    """Stable digest of a non-shell tool call's arguments.

    The arguments ARE the authorized operation for non-shell tools: approving
    `browser_navigate(docs-url)` must never pre-authorize
    `browser_navigate(attack-url)`. Sensitive-looking values are replaced by
    per-value digests so two different credentials still produce different
    fingerprints (they are different operations) without the credential
    itself landing in rule keys or logs.
    """

    def scrub(obj: Any) -> Any:
        if isinstance(obj, dict):
            out: dict[str, Any] = {}
            for k, v in obj.items():
                if _SENSITIVE_ARG_KEY_RE.search(str(k)):
                    out[str(k)] = "<digest:" + hashlib.sha256(
                        str(v).encode("utf-8", "replace")).hexdigest()[:12] + ">"
                else:
                    out[str(k)] = scrub(v)
            return out
        if isinstance(obj, (list, tuple)):
            return [scrub(v) for v in obj]
        return obj

    payload = json.dumps(scrub(args), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:16]


def _paths_digest(paths: list[Path]) -> str:
    """Digest over EVERY path in a multi-file request (round-4 D5)."""
    import hashlib as _h
    return _h.sha256("|".join(sorted(str(p_) for p_ in paths)).encode("utf-8", "replace")
                     ).hexdigest()[:16]


def _content_digest(args: dict[str, Any]) -> str:
    """Digest of the content a file-write tool would commit to disk."""
    for key in ("content", "new_string", "text", "patch", "diff"):
        v = args.get(key)
        if v:
            return hashlib.sha256(str(v).encode("utf-8", "replace")).hexdigest()[:16]
    return ""


def _short_args(args: dict[str, Any]) -> str:
    parts = []
    for k, v in list(args.items())[:4]:
        s = str(v)
        parts.append(f"{k}={s[:60]}{'…' if len(s) > 60 else ''}")
    return ", ".join(parts)
