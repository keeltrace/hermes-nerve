from __future__ import annotations
import json
from typing import Any
from .errors import RemoteWorkerError
MAX_GOAL_CHARS=50000;MAX_CONTEXT_CHARS=200000
def build_task_envelope(goal:object,context:object=None,*,supervision:dict[str,Any]|None=None)->str:
 goal_text=str(goal or "").strip()
 if not goal_text:raise RemoteWorkerError("invalid_task","goal must not be empty")
 if len(goal_text)>MAX_GOAL_CHARS:raise RemoteWorkerError("invalid_task","goal is too large")
 context_text=str(context or "").strip()
 if len(context_text)>MAX_CONTEXT_CHARS:raise RemoteWorkerError("invalid_task","context is too large")
 sections=["You are a remote Hermes sub-worker delegated by another Hermes agent.","","GOAL",goal_text]
 if context_text:sections += ["","CONTEXT",context_text]
 if supervision:
  sections += ["","LOCKED DEFINITION OF DONE / SUPERVISION","The following success contract is controller-owned. Do not weaken or rewrite it. Emit nerve_work_event as criteria start/pass/fail and when checkpointing.",json.dumps(supervision,sort_keys=True,ensure_ascii=False,default=str)]
 sections += ["","COMPLETION REPORT","When complete, report work performed, files changed, tests/checks and results, commit/ref if any, and unresolved blockers.","Do not claim work, tests, commits, or artifacts that you did not actually produce.",""]
 return "\n".join(sections)
