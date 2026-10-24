from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class GitWorkspaceError(RuntimeError):
    pass


def _run(repo: Path, argv: list[str], *, input_bytes: bytes | None = None) -> bytes:
    proc = subprocess.run(
        ["git", *argv],
        cwd=str(repo),
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise GitWorkspaceError(
            f"git {' '.join(argv)} failed ({proc.returncode}): "
            + proc.stderr.decode("utf-8", "replace")[:2000]
        )
    return proc.stdout


def _safe_rel(path: str) -> str:
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise GitWorkspaceError(f"unsafe untracked path: {path!r}")
    return candidate.as_posix()


@dataclass(frozen=True)
class WorkspaceSnapshot:
    head: str
    branch: str | None
    diff: bytes
    untracked_tar: bytes
    untracked: tuple[str, ...]

    def metadata(self) -> dict:
        return {
            "head": self.head,
            "branch": self.branch,
            "diff_bytes": len(self.diff),
            "untracked_tar_bytes": len(self.untracked_tar),
            "untracked": list(self.untracked),
        }


@dataclass(frozen=True)
class WorkspaceResult:
    ref: str
    commit: str
    base_head: str

    def as_dict(self) -> dict[str, str]:
        return {"ref": self.ref, "commit": self.commit, "base_head": self.base_head}


class GitWorkspaceManager:
    """Git-native per-run workspace helper.

    It moves data and dedicated refs, never the controller's current branch.
    """

    def capture(self, repo: str | Path) -> WorkspaceSnapshot:
        root = Path(repo).resolve()
        head = _run(root, ["rev-parse", "HEAD"]).decode().strip()
        branch_raw = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        branch = branch_raw.stdout.decode().strip() if branch_raw.returncode == 0 else None
        # Include staged + unstaged changes as a binary patch from HEAD.
        diff = _run(root, ["diff", "--binary", "HEAD", "--"])
        raw_untracked = _run(root, ["ls-files", "--others", "--exclude-standard", "-z"])
        names = tuple(_safe_rel(p.decode("utf-8", "surrogateescape")) for p in raw_untracked.split(b"\0") if p)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for name in names:
                source = root / name
                if source.is_symlink():
                    # Preserve symlink metadata but never dereference outside repo.
                    tf.add(source, arcname=name, recursive=False)
                elif source.is_file():
                    tf.add(source, arcname=name, recursive=False)
                elif source.is_dir():
                    tf.add(source, arcname=name, recursive=True)
        return WorkspaceSnapshot(head=head, branch=branch, diff=diff, untracked_tar=buf.getvalue(), untracked=names)

    def create_run_worktree(self, repo: str | Path, worktree: str | Path, snapshot: WorkspaceSnapshot) -> Path:
        root = Path(repo).resolve()
        target = Path(worktree).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise GitWorkspaceError(f"run worktree already exists: {target}")
        _run(root, ["worktree", "add", "--detach", str(target), snapshot.head])
        try:
            self.apply_overlay(target, snapshot)
        except Exception:
            subprocess.run(["git", "worktree", "remove", "--force", str(target)], cwd=str(root))
            raise
        return target

    def apply_overlay(self, worktree: str | Path, snapshot: WorkspaceSnapshot) -> None:
        target = Path(worktree).resolve()
        actual = _run(target, ["rev-parse", "HEAD"]).decode().strip()
        if actual != snapshot.head:
            raise GitWorkspaceError(f"workspace HEAD mismatch: expected {snapshot.head}, got {actual}")
        if snapshot.diff:
            _run(target, ["apply", "--binary", "--index", "-"], input_bytes=snapshot.diff)
            # Keep controller dirty state dirty in the worktree rather than staged.
            _run(target, ["reset"])
        if snapshot.untracked_tar:
            with tarfile.open(fileobj=io.BytesIO(snapshot.untracked_tar), mode="r:") as tf:
                for member in tf.getmembers():
                    _safe_rel(member.name)
                    if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                        raise GitWorkspaceError(f"unsafe tar member type: {member.name!r}")
                    if member.issym() or member.islnk():
                        link = Path(member.linkname)
                        if link.is_absolute() or ".." in link.parts:
                            raise GitWorkspaceError(f"unsafe tar link: {member.name!r}")
                # Python 3.12+ supports safe extraction filters. On 3.10/3.11
                # our explicit member/link validation above provides the guard.
                if sys.version_info >= (3, 12):
                    tf.extractall(target, filter="data")
                else:
                    tf.extractall(target)

    def publish_result_ref(
        self,
        controller_repo: str | Path,
        run_worktree: str | Path,
        *,
        task_id: str,
        run_id: int,
        message: str = "Hermes-Jev remote worker result",
    ) -> WorkspaceResult:
        controller = Path(controller_repo).resolve()
        worktree = Path(run_worktree).resolve()
        base_head = _run(controller, ["rev-parse", "HEAD"]).decode().strip()
        branch_before = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=str(controller), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        ).stdout.decode().strip()

        _run(worktree, ["add", "-A"])
        # Reuse repository identity; tests set it explicitly. If there is no
        # change, the result commit is simply current worktree HEAD.
        status = _run(worktree, ["status", "--porcelain"])
        if status:
            _run(worktree, ["commit", "-m", message])
        commit = _run(worktree, ["rev-parse", "HEAD"]).decode().strip()
        safe_task = "".join(c if c.isalnum() or c in "-_." else "-" for c in str(task_id))[:80] or "task"
        ref = f"refs/hermes-kanban-labs/results/{safe_task}-{int(run_id)}"
        _run(controller, ["update-ref", ref, commit])

        # Prove publishing did not alter canonical checkout branch or HEAD.
        after_head = _run(controller, ["rev-parse", "HEAD"]).decode().strip()
        branch_after = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=str(controller), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        ).stdout.decode().strip()
        if after_head != base_head or branch_after != branch_before:
            raise GitWorkspaceError("publishing result ref modified controller checkout")
        return WorkspaceResult(ref=ref, commit=commit, base_head=base_head)

    def remove_run_worktree(self, repo: str | Path, worktree: str | Path) -> None:
        _run(Path(repo).resolve(), ["worktree", "remove", "--force", str(Path(worktree).resolve())])
