from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .bridge import RemoteEventBridge
from .config import host_from_dict
from .errors import RemoteWorkerError
from .git_transport import PreparedRemoteGit, RemoteGitTransport
from .protocol import ProtocolState
from .ssh import build_ssh_argv, classify_failure
from .storage import append_text, get_job, init_storage, job_dir, update_job
from .util import now_ts, sanitized_subprocess_env, truncate
from ..work.models import RunIdentity
from ..work.supervisor import CardSupervisor


class _Tail:
    def __init__(self, limit: int = 16000):
        self.limit = limit
        self.parts: deque[str] = deque()
        self.size = 0
        self.lock = threading.Lock()

    def append(self, text: str) -> None:
        with self.lock:
            self.parts.append(text)
            self.size += len(text)
            while self.size > self.limit and self.parts:
                self.size -= len(self.parts.popleft())

    def get(self) -> str:
        with self.lock:
            return "".join(self.parts)[-self.limit:]


def _terminate(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate(); proc.wait(timeout=5); return
    except Exception:
        pass
    try:
        proc.kill(); proc.wait(timeout=5)
    except Exception:
        pass


def _read_spec(path: Path) -> dict[str, Any]:
    return json.loads((path / "runner-spec.json").read_text(encoding="utf-8"))


def _identity(spec: dict[str, Any]) -> RunIdentity | None:
    raw = spec.get("identity")
    if not isinstance(raw, dict):
        return None
    try:
        return RunIdentity(
            task_id=str(raw["task_id"]), run_id=int(raw["run_id"]),
            contract_hash=str(raw["contract_hash"]), claim_identity=str(raw["claim_identity"]),
            worker_id=str(raw.get("worker_id") or "") or None,
        )
    except Exception:
        return None


def _claim_current(identity: RunIdentity, required: bool) -> tuple[bool, str]:
    try:
        from hermes_cli import kanban_db as kb
    except Exception:
        return (False, "kanban_unavailable") if required else (True, "unchecked")
    try:
        with kb.connect_closing() as conn:
            task = kb.get_task(conn, identity.task_id)
        if task is None:
            return False, "task_missing"
        current_run = getattr(task, "current_run_id", None)
        claim = getattr(task, "claim_lock", None)
        # A closed/reviewed run has neither live run nor claim; it is no longer
        # authoritative even though there is no successor yet.
        status = str(getattr(task, "status", "") or "")
        if status != "running":
            return False, f"status_{status or 'unknown'}"
        if current_run is None or int(current_run) != identity.run_id:
            return False, "run_changed"
        if claim is None or str(claim) != identity.claim_identity:
            return False, "claim_changed"
        return True, "current"
    except Exception as exc:
        return (False, f"claim_check_error:{type(exc).__name__}") if required else (True, "unchecked_error")


def _collect_git(
    git_transport: RemoteGitTransport | None,
    git_host,
    prepared: PreparedRemoteGit | None,
    runner_log: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    if git_transport is None or prepared is None:
        return None, None
    try:
        result = git_transport.collect(git_host, prepared)
        return result, None
    except Exception as exc:
        msg = f"Git result collection failed: {type(exc).__name__}: {exc}"
        append_text(runner_log, msg + "\n")
        return {"error": msg, **prepared.metadata()}, msg


def run_job(job_id: str, data_dir: Path) -> int:
    init_storage(data_dir)
    path = job_dir(job_id, data_dir)
    events_path, stderr_path, runner_log = path / "events.jsonl", path / "stderr.log", path / "runner.log"
    cancel_event = threading.Event()
    timed_out = False
    fenced_reason = ""
    handed_off = False

    def on_signal(signum, _frame):
        cancel_event.set(); append_text(runner_log, f"signal={signum} cancellation requested\n")

    if hasattr(signal, "SIGTERM"): signal.signal(signal.SIGTERM, on_signal)
    if hasattr(signal, "SIGINT"): signal.signal(signal.SIGINT, on_signal)
    if not get_job(job_id, data_dir):
        return 2
    update_job(job_id, data_dir, runner_pid=os.getpid(), status="starting")

    git_transport: RemoteGitTransport | None = None
    prepared_git: PreparedRemoteGit | None = None
    git_host = None
    try:
        spec = _read_spec(path)
        host = host_from_dict(spec["host"])
        task_text = Path(spec["task_path"]).read_text(encoding="utf-8")
        identity = _identity(spec)
        if isinstance(spec.get("git"), dict) and spec["git"].get("controller_repo"):
            git_transport = RemoteGitTransport()
            result_key = str(spec["git"].get("result_key") or "")
            host, prepared_git = git_transport.prepare(
                host, job_id=job_id, controller_repo=str(spec["git"]["controller_repo"]), result_key=result_key
            )
            git_host = host
            update_job(job_id, data_dir, workspace=host.workspace, workspace_result_json={"prepared": prepared_git.metadata()})
        control_path = str(spec.get("control_path") or "")
        remote_env = {"HERMES_NERVE_REMOTE_MODE": "1"}
        if identity:
            remote_env.update({
                "HERMES_KANBAN_TASK_ID": identity.task_id,
                "HERMES_KANBAN_RUN_ID": str(identity.run_id),
                "HERMES_NERVE_DOD_HASH": identity.contract_hash,
                "HERMES_KANBAN_CLAIM_IDENTITY": identity.claim_identity,
            })
            if identity.worker_id:
                remote_env["HERMES_KANBAN_WORKER_ID"] = identity.worker_id
        if control_path:
            remote_env["HERMES_NERVE_REMOTE_CONTROL_FILE"] = control_path
        argv = build_ssh_argv(host, env=remote_env)
    except Exception as exc:
        code = exc.code if isinstance(exc, RemoteWorkerError) else "internal_error"
        msg = exc.message if isinstance(exc, RemoteWorkerError) else f"Runner setup failed: {type(exc).__name__}: {truncate(exc)}"
        update_job(job_id, data_dir, status="failed", finished_at=now_ts(), error_code=code, error_message=msg)
        return 1

    bridge = None
    if identity and spec.get("supervision_db"):
        try:
            bridge = RemoteEventBridge(
                CardSupervisor(store_path=spec["supervision_db"], mode=str(spec.get("supervision_mode") or "shadow")),
                identity,
                reviewer=str(spec.get("reviewer") or ""),
            )
        except Exception as exc:
            append_text(runner_log, f"bridge init failed: {type(exc).__name__}: {exc}\n")
    proto = ProtocolState(on_event=bridge.consume if bridge else None)
    stderr_tail, stdout_tail = _Tail(), _Tail()
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=sanitized_subprocess_env(),
        )
    except Exception as exc:
        update_job(job_id, data_dir, status="failed", finished_at=now_ts(), error_code="ssh_start_failed", error_message=f"Could not start OpenSSH: {type(exc).__name__}: {exc}")
        return 1
    update_job(job_id, data_dir, ssh_pid=proc.pid)
    lock = threading.Lock()

    def out_reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            stdout_tail.append(line)
            append_text(events_path, line[:65536] + ("\n" if len(line) > 65536 else ""))
            with lock:
                event = proto.consume_line(line)
                rid, last, warn = proto.remote_session_id, proto.last_activity, proto.warnings
            fields = {"protocol_warnings": warn, "last_activity_json": last, "protocol": proto.protocol}
            if rid: fields["remote_session_id"] = rid
            if event and event.get("type") == "system" and event.get("subtype") == "init": fields["status"] = "running"
            update_job(job_id, data_dir, **fields)

    def err_reader() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            append_text(stderr_path, line); stderr_tail.append(line)

    t1 = threading.Thread(target=out_reader, daemon=True); t2 = threading.Thread(target=err_reader, daemon=True)
    t1.start(); t2.start()
    try:
        assert proc.stdin is not None
        proc.stdin.write(task_text); proc.stdin.flush(); proc.stdin.close()
        try: Path(spec["task_path"]).unlink()
        except OSError: pass
    except Exception as exc:
        append_text(runner_log, f"stdin failed: {type(exc).__name__}: {exc}\n"); _terminate(proc)

    started = time.monotonic(); deadline = started + float(host.task_timeout_seconds); last_claim_check = 0.0
    while proc.poll() is None:
        current = get_job(job_id, data_dir)
        if bridge and bridge.stop_requested:
            handed_off = True; _terminate(proc); break
        if cancel_event.is_set() or (current and current.get("cancel_requested")):
            _terminate(proc); cancel_event.set(); break
        if time.monotonic() >= deadline:
            timed_out = True; _terminate(proc); break
        if identity and time.monotonic() - last_claim_check >= 1.0:
            last_claim_check = time.monotonic()
            ok, reason = _claim_current(identity, bool(spec.get("require_claim_fence")))
            if not ok:
                fenced_reason = reason; _terminate(proc); break
        time.sleep(.15)

    exit_code = proc.poll(); t1.join(timeout=5); t2.join(timeout=5)
    duration_ms = int((time.monotonic() - started) * 1000)
    with lock:
        result_event = dict(proto.result_event) if proto.result_event else None
        remote_session_id, warnings, last_activity = proto.remote_session_id, proto.warnings, proto.last_activity
    stderr_text = stderr_tail.get()
    legacy = "__JEV_REMOTE_PROTOCOL__:legacy-text" in stderr_text
    if not result_event and exit_code == 0 and legacy:
        proto.protocol = "legacy-text"; text = stdout_tail.get().strip()
        match = re.search(r"(?m)^session_id:\s*(\S+)\s*$", stderr_text)
        if match and not remote_session_id: remote_session_id = match.group(1)
        if text:
            result_event = {"type":"result","session_id":remote_session_id,"exit_code":0,"text":text,"tokens":None,"duration_ms":duration_ms,"protocol":"legacy-text"}
            last_activity = {"type":"result","exit_code":0,"protocol":"legacy-text"}

    workspace_result, git_error = _collect_git(git_transport, git_host, prepared_git, runner_log)
    if workspace_result is not None:
        update_job(job_id, data_dir, workspace_result_json=workspace_result)

    common = dict(
        finished_at=now_ts(), duration_ms=duration_ms, exit_code=exit_code,
        remote_session_id=remote_session_id, last_activity_json=last_activity,
        protocol_warnings=warnings, protocol=proto.protocol,
    )
    if handed_off:
        if git_error:
            update_job(job_id, data_dir, status="failed", error_code="git_result_failed", error_message=git_error, **common); return 1
        update_job(job_id, data_dir, status="handed_off", error_code=None, error_message=None, **common); return 0
    if fenced_reason:
        update_job(job_id, data_dir, status="fenced", error_code="stale_claim", error_message=f"Canonical claim fence stopped remote run: {fenced_reason}", **common); return 0
    if cancel_event.is_set():
        update_job(job_id, data_dir, status="cancelled", error_code="cancelled", error_message="Remote job cancelled by controller.", **common); return 0
    if timed_out:
        update_job(job_id, data_dir, status="failed", error_code="timeout", error_message=f"Remote job exceeded {host.task_timeout_seconds}s timeout.", **common); return 1

    result_exit = None; result_text = None; tokens = None; result_duration = None
    if result_event:
        try: result_exit = int(result_event.get("exit_code"))
        except Exception: result_exit = exit_code
        result_text = str(result_event.get("text") or "")
        tokens = result_event.get("tokens") or result_event.get("usage")
        try: result_duration = int(result_event.get("duration_ms")) if result_event.get("duration_ms") is not None else None
        except Exception: pass
    if git_error and result_event and result_exit == 0:
        update_job(job_id, data_dir, status="failed", duration_ms=result_duration or duration_ms, result_text=result_text, tokens_json=tokens, error_code="git_result_failed", error_message=git_error, **{k:v for k,v in common.items() if k not in {"duration_ms"}}); return 1
    if result_event and result_exit == 0 and exit_code == 0:
        update_job(job_id, data_dir, status="completed", duration_ms=result_duration or duration_ms, exit_code=0, result_text=result_text, tokens_json=tokens, error_code=None, error_message=None, **{k:v for k,v in common.items() if k not in {"duration_ms","exit_code"}}); return 0
    if result_exit == 75 or exit_code == 75:
        update_job(job_id, data_dir, status="rate_limited", duration_ms=result_duration or duration_ms, exit_code=75, result_text=result_text, tokens_json=tokens, error_code="rate_limited", error_message="Remote Hermes reported provider quota/rate limiting; canonical Kanban should requeue without incrementing failure count.", **{k:v for k,v in common.items() if k not in {"duration_ms","exit_code"}}); return 75
    code, msg = classify_failure(process_exit=exit_code, result_exit=result_exit, stderr_text=stderr_text, had_result=bool(result_event))
    update_job(job_id, data_dir, status="failed", duration_ms=result_duration or duration_ms, exit_code=result_exit if result_exit is not None else exit_code, result_text=result_text, tokens_json=tokens, error_code=code, error_message=msg, **{k:v for k,v in common.items() if k not in {"duration_ms","exit_code"}})
    return 1


def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("--job-id",required=True); p.add_argument("--data-dir",required=True); a=p.parse_args(argv)
    try: return run_job(a.job_id, Path(a.data_dir).expanduser().resolve())
    except Exception as exc:
        try: update_job(a.job_id,Path(a.data_dir),status="failed",finished_at=now_ts(),error_code="internal_error",error_message=f"Unhandled runner error: {type(exc).__name__}: {exc}")
        except Exception: pass
        return 1

if __name__=="__main__": raise SystemExit(main())
