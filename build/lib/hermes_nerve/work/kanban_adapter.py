from __future__ import annotations

from typing import Any


class CanonicalKanbanAdapter:
    """Thin adapter over Hermes' canonical kanban_db API.

    No lifecycle state is mirrored here. If Hermes Kanban is unavailable, the
    adapter reports that explicitly instead of inventing a replacement board.
    """

    def __init__(self, kb_module: Any | None = None) -> None:
        self._kb = kb_module

    def _module(self):
        if self._kb is not None:
            return self._kb
        try:
            from hermes_cli import kanban_db as kb  # type: ignore
        except Exception as exc:
            raise RuntimeError("Hermes canonical kanban_db is not importable") from exc
        self._kb = kb
        return kb

    def _connection(self, kb):
        """Use Hermes' canonical connection owner across its API split."""
        connect_closing = getattr(kb, "connect_closing", None)
        if not callable(connect_closing):
            try:
                from hermes_cli.kanban_db_connect import connect_closing
            except ImportError as exc:
                raise RuntimeError("Hermes canonical Kanban connection API is unavailable") from exc
        return connect_closing()

    def request_review(
        self,
        *,
        task_id: str,
        expected_run_id: int,
        summary: str,
        metadata: dict[str, Any],
        reviewer: str = "",
    ) -> tuple[bool, str | None]:
        kb = self._module()
        with self._connection(kb) as conn:
            result = kb.request_review(
                conn,
                task_id,
                summary=summary,
                metadata=metadata,
                reviewer=reviewer or None,
                expected_run_id=int(expected_run_id),
                force=False,
                with_reason=True,
            )
        if isinstance(result, tuple):
            return bool(result[0]), result[1]
        return bool(result), None if result else "canonical review transition failed"

    def complete(
        self,
        *,
        task_id: str,
        expected_run_id: int,
        result: str | None = None,
        summary: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        kb = self._module()
        with self._connection(kb) as conn:
            return bool(
                kb.complete_task(
                    conn,
                    task_id,
                    result=result,
                    summary=summary,
                    metadata=metadata,
                    expected_run_id=int(expected_run_id),
                )
            )
