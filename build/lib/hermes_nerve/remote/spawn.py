from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..work.models import RunIdentity
from ..work.supervisor import CardSupervisor
from .execution import RemoteManager


@dataclass(frozen=True)
class RemoteRoute:
    host_alias: str
    profile: str | None = None


class KanbanRemoteSpawn:
    """Drop-in canonical Kanban ``spawn_fn`` adapter.

    Hermes Kanban still claims the card and creates the run. This adapter only
    decides where that already-claimed run executes. Local tasks are passed to
    the original spawn function unchanged.
    """

    def __init__(
        self,
        *,
        default_spawn: Callable[..., int | None],
        routes: dict[str, RemoteRoute | str],
        supervisor: CardSupervisor,
        manager: RemoteManager | None = None,
        require_supervision: bool = True,
        require_claim_fence: bool = True,
        reviewer: str = "",
    ) -> None:
        self.default_spawn = default_spawn
        self.routes = {
            str(k): (v if isinstance(v, RemoteRoute) else RemoteRoute(str(v)))
            for k, v in dict(routes or {}).items()
        }
        self.supervisor = supervisor
        self.manager = manager or RemoteManager()
        self.require_supervision = bool(require_supervision)
        self.require_claim_fence = bool(require_claim_fence)
        self.reviewer = str(reviewer or "").strip()

    def __call__(self, task: Any, workspace_path: str, board: str | None = None) -> int | None:
        route = self.routes.get(str(getattr(task, "assignee", "") or ""))
        if route is None:
            try:
                return self.default_spawn(task, workspace_path, board=board)
            except TypeError:
                return self.default_spawn(task, workspace_path)

        task_id = str(getattr(task, "id", "") or "")
        run_id = getattr(task, "current_run_id", None)
        claim = str(getattr(task, "claim_lock", "") or "")
        if not task_id or run_id is None or not claim:
            raise RuntimeError("remote execution requires canonical task_id, current_run_id, and claim_lock")
        contract = self.supervisor.active_contract(task_id)
        if contract is None:
            if self.require_supervision:
                raise RuntimeError("remote supervised card has no locked Definition of Done")
            return self.default_spawn(task, workspace_path, board=board) if board is not None else self.default_spawn(task, workspace_path)

        identity = RunIdentity(
            task_id=task_id,
            run_id=int(run_id),
            contract_hash=contract.contract_hash,
            claim_identity=claim,
            worker_id=f"ssh:{route.host_alias}",
        )
        projection = self.supervisor.bind_run(identity)
        execution = self.manager.start(
            host_alias=route.host_alias,
            goal=f"{getattr(task, 'title', task_id)}\n\n{getattr(task, 'body', '')}",
            context=f"Canonical Kanban board={board or ''}; workspace={workspace_path}",
            workspace=None,
            profile=route.profile,
            identity=identity,
            supervision={
                "contract": contract.as_dict(),
                "projection": projection.as_dict(),
                "reviewer": self.reviewer,
            },
            supervision_db=str(self.supervisor.store.path),
            supervision_mode=self.supervisor.mode,
            require_claim_fence=self.require_claim_fence,
            git={"controller_repo": workspace_path},
        )
        status = execution.status() or {}
        pid = status.get("runner_pid")
        return int(pid) if pid else None
