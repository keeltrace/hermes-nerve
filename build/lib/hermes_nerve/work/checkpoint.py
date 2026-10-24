from __future__ import annotations

import uuid
from typing import Any

from .models import CheckpointPacket, RunIdentity, WorkProjection


def build_checkpoint(
    identity: RunIdentity,
    projection: WorkProjection,
    *,
    decision_id: str,
    reason: str,
    current_plan: str = "",
    test_summary: str = "",
    artifacts: list[dict[str, Any]] | None = None,
    workspace_result: dict[str, Any] | None = None,
    complete: bool = False,
) -> CheckpointPacket:
    verified = tuple(c.criterion_id for c in projection.criteria if c.state == "VERIFIED_PASS")
    failed = tuple(c.criterion_id for c in projection.criteria if c.state in {"FAIL", "NEEDS_EVIDENCE"})
    return CheckpointPacket(
        checkpoint_id="jevcp-" + uuid.uuid4().hex,
        identity=identity,
        decision_id=decision_id,
        reason=reason,
        frontier=projection.frontier,
        verified_criteria=verified,
        failed_criteria=failed,
        budget=projection.budget,
        current_plan=current_plan,
        test_summary=test_summary,
        artifacts=tuple(artifacts or ()),
        workspace_result=dict(workspace_result or {}),
        checkpoint_complete=bool(complete),
    )
