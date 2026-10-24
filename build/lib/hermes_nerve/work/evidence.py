from __future__ import annotations

import json
import uuid
from typing import Any

from ..privacy import canonical_hash, redact
from .models import EvidenceRecord, RunIdentity


def bounded_preview(value: Any, limit: int = 1200) -> str:
    safe = redact(value)
    if isinstance(safe, str):
        text = safe
    else:
        text = json.dumps(safe, sort_keys=True, ensure_ascii=False, default=str)
    limit = max(160, min(4000, int(limit)))
    return text if len(text) <= limit else text[:limit] + "..."


def make_evidence(
    identity: RunIdentity,
    *,
    source: str,
    kind: str,
    value: Any,
    criterion_id: str | None = None,
    pointer: str = "",
    tool_name: str = "",
    is_error: bool = False,
    preview_chars: int = 1200,
) -> EvidenceRecord:
    safe = redact(value)
    digest = canonical_hash(safe)
    return EvidenceRecord(
        evidence_id="jevev-" + uuid.uuid4().hex,
        task_id=identity.task_id,
        run_id=identity.run_id,
        contract_hash=identity.contract_hash,
        source=str(source or "observed"),
        kind=str(kind or "observation"),
        criterion_id=criterion_id,
        sha256=digest,
        preview=bounded_preview(safe, preview_chars),
        pointer=str(pointer or ""),
        tool_name=str(tool_name or ""),
        is_error=bool(is_error),
    )
