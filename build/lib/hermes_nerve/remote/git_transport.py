from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from .config import RemoteHost
from .ssh import resolve_ssh_binary
from .util import sanitized_subprocess_env
from .git_workspace import GitWorkspaceManager, GitWorkspaceError


def _q(v: object) -> str:
    return shlex.quote(str(v))


def _ssh_base(host: RemoteHost) -> list[str]:
    return [
        resolve_ssh_binary(host.ssh_binary),
        "-T",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={host.connect_timeout_seconds}",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        host.ssh_host,
    ]


def _run_local(repo: Path, args: list[str]) -> bytes:
    p = subprocess.run(["git", *args], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise GitWorkspaceError(f"git {' '.join(args)} failed: {p.stderr.decode('utf-8','replace')[:1200]}")
    return p.stdout


@dataclass(frozen=True)
class PreparedRemoteGit:
    controller_repo: Path
    remote_workspace: str
    base_head: str
    branch: str | None
    result_ref: str
    job_id: str

    def metadata(self) -> dict[str, Any]:
        return {
            "controller_repo": str(self.controller_repo),
            "remote_workspace": self.remote_workspace,
            "base_head": self.base_head,
            "branch": self.branch,
            "result_ref": self.result_ref,
            "job_id": self.job_id,
        }


class RemoteGitTransport:
    """Exact-HEAD + dirty/untracked overlay transport over ordinary SSH.

    The controller checkout is never reset, switched, or merged. Results return
    only as a dedicated ref under refs/hermes-kanban-labs/results/.
    """

    def __init__(self) -> None:
        self.local = GitWorkspaceManager()

    def _run_root(self, host: RemoteHost) -> PurePosixPath:
        root = host.git_cache_root or (
            str(PurePosixPath(host.workspace_root) / ".hermes-jev-git")
            if host.workspace_root else "/tmp/hermes-jev-git"
        )
        return PurePosixPath(root)

    def prepare(self, host: RemoteHost, *, job_id: str, controller_repo: str | Path, result_key: str = "") -> tuple[RemoteHost, PreparedRemoteGit]:
        repo = Path(controller_repo).expanduser().resolve()
        snapshot = self.local.capture(repo)
        root = self._run_root(host)
        remote_run = root / "runs" / job_id
        remote_repo = remote_run / "repo"

        with tempfile.TemporaryDirectory() as td:
            bundle = Path(td) / "base.bundle"
            p = subprocess.run(
                ["git", "bundle", "create", str(bundle), "HEAD"],
                cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            if p.returncode != 0:
                raise GitWorkspaceError(f"git bundle create failed: {p.stderr.decode('utf-8','replace')[:1200]}")
            payload = io.BytesIO()
            with tarfile.open(fileobj=payload, mode="w") as tf:
                tf.add(bundle, arcname="base.bundle")
                info = tarfile.TarInfo("dirty.patch")
                info.size = len(snapshot.diff)
                tf.addfile(info, io.BytesIO(snapshot.diff))
                info2 = tarfile.TarInfo("untracked.tar")
                info2.size = len(snapshot.untracked_tar)
                tf.addfile(info2, io.BytesIO(snapshot.untracked_tar))
                meta = json.dumps(snapshot.metadata(), sort_keys=True).encode()
                info3 = tarfile.TarInfo("snapshot.json")
                info3.size = len(meta)
                tf.addfile(info3, io.BytesIO(meta))
            body = payload.getvalue()

        cmd = "; ".join([
            "set -eu",
            "umask 077",
            f"RUN={_q(remote_run)}",
            'rm -rf -- "$RUN"',
            'mkdir -p -- "$RUN"',
            'tar -xf - -C "$RUN"',
            'git clone -q "$RUN/base.bundle" "$RUN/repo"',
            'cd -- "$RUN/repo"',
            f"git checkout -q --detach {_q(snapshot.head)}",
            'if [ -s "$RUN/dirty.patch" ]; then git apply --binary "$RUN/dirty.patch"; fi',
            'if [ -s "$RUN/untracked.tar" ]; then tar -xf "$RUN/untracked.tar" -C "$RUN/repo"; fi',
            'test "$(git rev-parse HEAD)" = ' + _q(snapshot.head),
        ])
        proc = subprocess.run(
            [*_ssh_base(host), cmd], input=body, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=sanitized_subprocess_env(), timeout=max(30, host.connect_timeout_seconds * 4),
        )
        if proc.returncode != 0:
            raise GitWorkspaceError(f"remote Git workspace prepare failed: {proc.stderr.decode('utf-8','replace')[:2000]}")
        safe_job = "".join(c if c.isalnum() or c in "-_." else "-" for c in (result_key or job_id))[:100]
        result_ref = f"refs/hermes-kanban-labs/results/{safe_job}"
        prepared = PreparedRemoteGit(
            controller_repo=repo,
            remote_workspace=str(remote_repo),
            base_head=snapshot.head,
            branch=snapshot.branch,
            result_ref=result_ref,
            job_id=job_id,
        )
        # Preserve all host policy; only choose the generated contained run workspace.
        return replace(host, workspace=str(remote_repo)), prepared

    def collect(self, host: RemoteHost, prepared: PreparedRemoteGit) -> dict[str, Any]:
        remote_repo = prepared.remote_workspace
        cmd = "; ".join([
            "set -eu",
            f"cd -- {_q(remote_repo)}",
            "git add -A",
            "if ! git diff --cached --quiet; then git -c user.name='Hermes-Jev Remote Worker' -c user.email='hermes-jev@local' commit -q -m 'Hermes-Jev remote worker result'; fi",
            "git bundle create - HEAD",
        ])
        proc = subprocess.run(
            [*_ssh_base(host), cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=sanitized_subprocess_env(), timeout=max(30, host.connect_timeout_seconds * 4),
        )
        if proc.returncode != 0:
            raise GitWorkspaceError(f"remote Git result collection failed: {proc.stderr.decode('utf-8','replace')[:2000]}")
        if not proc.stdout:
            raise GitWorkspaceError("remote Git result bundle was empty")
        controller = prepared.controller_repo
        head_before = _run_local(controller, ["rev-parse", "HEAD"]).decode().strip()
        branch_before = subprocess.run(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], cwd=controller, stdout=subprocess.PIPE).stdout.decode().strip()
        with tempfile.NamedTemporaryFile(prefix="hermes-jev-result-", suffix=".bundle", delete=False) as h:
            h.write(proc.stdout); bundle_path = Path(h.name)
        try:
            _run_local(controller, ["fetch", "--quiet", str(bundle_path), "HEAD"])
            commit = _run_local(controller, ["rev-parse", "FETCH_HEAD"]).decode().strip()
            _run_local(controller, ["update-ref", prepared.result_ref, commit])
        finally:
            try: bundle_path.unlink()
            except OSError: pass
        head_after = _run_local(controller, ["rev-parse", "HEAD"]).decode().strip()
        branch_after = subprocess.run(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], cwd=controller, stdout=subprocess.PIPE).stdout.decode().strip()
        if head_after != head_before or branch_after != branch_before:
            raise GitWorkspaceError("remote result collection modified controller checkout")
        return {
            "result_ref": prepared.result_ref,
            "result_commit": commit,
            "base_head": prepared.base_head,
            "controller_head_unchanged": True,
            "controller_branch_unchanged": True,
        }

    def cleanup(self, host: RemoteHost, prepared: PreparedRemoteGit) -> None:
        run_dir = PurePosixPath(prepared.remote_workspace).parent
        cmd = f"set -eu; rm -rf -- {_q(run_dir)}"
        subprocess.run([*_ssh_base(host), cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=sanitized_subprocess_env(), timeout=max(20, host.connect_timeout_seconds * 3))
