from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Mapping

from .models import RunIdentity, utc_now
from .nerve import budget_criterion, estimate_task_budget
from .runtime import settings

_DOD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(definition\s+of\s+done|acceptance\s+criteria|done\s+criteria)\s*$", re.I)
_ANY_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(.+?)\s*$")


@dataclass(frozen=True)
class _LocalAccept:
    value: str = "ACCEPT"
    confidence: float = 1.0
    receipt_id: str = "local-structured-dod"


def parse_checkpoint_fractions(raw: str) -> tuple[float, ...]:
    values: list[float] = []
    for part in str(raw or "").split(","):
        try:
            value = float(part.strip())
        except ValueError:
            continue
        if 0.0 < value < 1.0 and value not in values:
            values.append(value)
    return tuple(sorted(values)) or (0.40, 0.70)


def extract_dod_lines(body: str) -> list[str]:
    lines = str(body or "").splitlines()
    start = None
    for idx, line in enumerate(lines):
        if _DOD_HEADING.match(line):
            start = idx + 1
            break
    if start is None:
        return []
    items: list[str] = []
    current = ""
    for raw in lines[start:]:
        if _ANY_HEADING.match(raw):
            break
        m = _LIST_ITEM.match(raw)
        if m:
            if current:
                items.append(current.strip())
            current = m.group(1).strip()
            continue
        text = raw.strip()
        if text and current:
            current += " " + text
    if current:
        items.append(current.strip())
    return [x for x in items if x]


def build_criteria(body: str, *, total_budget: int) -> tuple[list[dict[str, Any]], int]:
    lines = extract_dod_lines(body)
    if not lines:
        return [], 0
    total = max(1000, int(total_budget))
    reserve = max(1000, int(total * 0.15))
    usable = max(len(lines), total - reserve)
    per = max(1, usable // len(lines))
    remainder = max(0, usable - per * len(lines))
    criteria = [
        {
            "id": f"DOD-{idx:02d}",
            "description": text,
            "required": True,
            "weight": 1.0,
            "estimated_tokens": per + (1 if idx <= remainder else 0),
        }
        for idx, text in enumerate(lines, 1)
    ]
    return criteria, reserve


def _git_head(workspace: str) -> str:
    if not workspace:
        return ""
    try:
        p = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
            check=False,
        )
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""




def _task_attr(task: Any, name: str, default: Any = None) -> Any:
    if isinstance(task, Mapping):
        return task.get(name, default)
    return getattr(task, name, default)


def _load_canonical_task(task_id: str) -> Any:
    """Read a Kanban task through Hermes' canonical board connection.

    Hermes v0.21.3 split connection helpers out of ``kanban_db`` into
    ``kanban_db_connect``. Older builds still expose ``kanban_db.connect``.
    Support both layouts and prefer the dispatcher-pinned DB/board env so a
    headless worker cannot accidentally bind against whatever board happens
    to be globally selected by another controller process.
    """
    from hermes_cli import kanban_db as kb

    db_raw = str(os.getenv("HERMES_KANBAN_DB") or "").strip()
    board = str(os.getenv("HERMES_KANBAN_BOARD") or "").strip() or None
    db_path = Path(db_raw).expanduser() if db_raw else None

    try:
        from hermes_cli import kanban_db_connect as kbc
    except (ImportError, AttributeError):
        kbc = None

    if kbc is not None and hasattr(kbc, "connect_closing"):
        kwargs: dict[str, Any] = {}
        if db_path is not None:
            kwargs["db_path"] = db_path
        elif board is not None:
            kwargs["board"] = board
        with kbc.connect_closing(**kwargs) as con:
            return kb.get_task(con, task_id)

    connect = getattr(kb, "connect", None)
    if connect is None:
        raise RuntimeError(
            "Hermes Kanban connection API unavailable: expected "
            "hermes_cli.kanban_db_connect.connect_closing or hermes_cli.kanban_db.connect"
        )
    con = None
    try:
        try:
            con = connect(db_path=db_path, board=board)
        except TypeError:
            con = connect()
        return kb.get_task(con, task_id)
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def _raw_worker_scope(*, session_id: str = "") -> tuple[int, str, str | None] | None:
    raw_run = str(os.getenv("HERMES_KANBAN_RUN_ID") or "").strip()
    claim = str(
        os.getenv("HERMES_KANBAN_CLAIM_LOCK")
        or os.getenv("HERMES_KANBAN_CLAIM_IDENTITY")
        or os.getenv("HERMES_KANBAN_CLAIM_TOKEN")
        or ""
    ).strip()
    if not raw_run or not claim:
        return None
    try:
        run_id = int(raw_run)
    except ValueError:
        return None
    worker = str(os.getenv("HERMES_PROFILE") or os.getenv("HERMES_KANBAN_WORKER_ID") or session_id or "").strip() or None
    return run_id, claim, worker


def _worker_identity(contract_hash: str, *, task_id: str, session_id: str = "") -> RunIdentity | None:
    scope = _raw_worker_scope(session_id=session_id)
    if scope is None:
        return None
    run_id, claim, worker = scope
    return RunIdentity(task_id, run_id, contract_hash, claim, worker)


def _validate_canonical_scope(supervisor, *, task_id: str, task: Any, session_id: str = "") -> tuple[int, str, str | None] | None:
    scope = _raw_worker_scope(session_id=session_id)
    if scope is None:
        supervisor.store.add_diagnostic(
            task_id=task_id, kind="autobind_missing_run_identity", payload={}, created_at=utc_now(),
        )
        return None
    run_id, claim, worker = scope
    canonical_run = _task_attr(task, "current_run_id", None)
    try:
        canonical_run_int = int(canonical_run) if canonical_run is not None else None
    except (TypeError, ValueError):
        canonical_run_int = None
    if canonical_run_int is not None and canonical_run_int != run_id:
        supervisor.store.add_diagnostic(
            task_id=task_id, run_id=run_id, kind="autobind_run_mismatch",
            payload={"canonical_run_id": canonical_run}, created_at=utc_now(),
        )
        return None
    canonical_claim = str(_task_attr(task, "claim_lock", "") or "").strip()
    if canonical_claim and canonical_claim != claim:
        supervisor.store.add_diagnostic(
            task_id=task_id, run_id=run_id, kind="autobind_claim_mismatch",
            payload={"canonical_claim_present": True}, created_at=utc_now(),
        )
        return None
    return scope


def ensure_kanban_binding(supervisor, *, task_id: str = "", session_id: str = "") -> RunIdentity | None:
    cfg = settings()
    if not cfg.get("auto_bind_kanban", True):
        return None
    # The dispatcher-pinned environment is canonical. Hook-local ``task_id``
    # values may be a session/subtask identifier on fallback lifecycle paths.
    env_tid = str(os.getenv("HERMES_KANBAN_TASK_ID") or os.getenv("HERMES_KANBAN_TASK") or "").strip()
    tid = env_tid or str(task_id or "").strip()
    if not tid:
        return None
    # Make the canonical worker env visible to the rest of the plugin even on
    # Hermes builds that only set HERMES_KANBAN_TASK. Only propagate a
    # dispatcher-pinned value: a hook-local fallback may be a session id on
    # non-worker processes, and writing it back would make later reads treat
    # it as canonical (issue #60).
    if env_tid:
        os.environ.setdefault("HERMES_KANBAN_TASK_ID", env_tid)

    # Read and fence against canonical Kanban state before creating any Jev
    # contract. A stale/reclaimed worker must not be able to lock a DoD for a
    # run it no longer owns.
    try:
        task = _load_canonical_task(tid)
    except Exception as exc:
        supervisor.store.add_diagnostic(
            task_id=tid, kind="autobind_task_lookup_failed",
            payload={"error": f"{type(exc).__name__}: {exc}"}, created_at=utc_now(),
        )
        return None
    if task is None:
        supervisor.store.add_diagnostic(task_id=tid, kind="autobind_task_missing", payload={}, created_at=utc_now())
        return None
    scope = _validate_canonical_scope(supervisor, task_id=tid, task=task, session_id=session_id)
    if scope is None:
        return None
    run_id, claim, worker = scope

    active = supervisor.active_contract(tid)
    if active is None:
        body = str(_task_attr(task, "body", "") or "")
        dod_lines = extract_dod_lines(body)
        if bool(cfg.get("auto_estimate_task_budget", True)):
            task_budget = estimate_task_budget(
                body,
                len(dod_lines),
                floor_tokens=int(cfg.get("default_task_budget_tokens", 70000)),
                base_tokens=int(cfg.get("budget_estimator_base_tokens", 120000)),
                per_criterion_tokens=int(cfg.get("budget_estimator_per_criterion_tokens", 75000)),
                body_char_factor=float(cfg.get("budget_estimator_body_char_factor", 25.0)),
                safety_multiplier=float(cfg.get("budget_estimator_safety_multiplier", 1.25)),
                max_tokens=int(cfg.get("budget_estimator_max_tokens", 2000000)),
            )
        else:
            task_budget = int(cfg.get("default_task_budget_tokens", 70000))
        criteria, reserve = build_criteria(body, total_budget=task_budget)
        if criteria and bool(cfg.get("budget_dod_required", True)):
            criteria.append(budget_criterion(task_budget, tolerance=float(cfg.get("budget_dod_tolerance", 1.10))))
        if not criteria:
            supervisor.store.add_diagnostic(
                task_id=tid, run_id=run_id, kind="autobind_no_structured_dod",
                payload={"title": str(_task_attr(task, "title", "") or "")}, created_at=utc_now(),
            )
            return None
        out = supervisor.bind_contract(
            task_id=tid,
            goal=str(_task_attr(task, "title", "") or tid),
            criteria=criteria,
            reserve_tokens=reserve,
            checkpoint_fractions=list(parse_checkpoint_fractions(cfg.get("checkpoint_fractions", "0.40,0.70"))),
            actor="orchestrator",
            preflight_result=_LocalAccept(),
        )
        active = supervisor.active_contract(tid)
        if not out.get("accepted") or active is None:
            return None

    ident = RunIdentity(tid, run_id, active.contract_hash, claim, worker)
    current = supervisor.store.current_identity(tid)
    if current != ident:
        supervisor.bind_run(ident)
    os.environ["HERMES_NERVE_DOD_HASH"] = active.contract_hash
    workspace = str(os.getenv("HERMES_KANBAN_WORKSPACE") or os.getenv("TERMINAL_CWD") or "").strip()
    supervisor.store.save_run_context(
        ident,
        workspace_path=workspace,
        base_revision=_git_head(workspace),
        session_id=session_id,
        bound_at=utc_now(),
    )
    return ident
