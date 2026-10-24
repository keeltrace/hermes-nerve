from __future__ import annotations

from typing import Any, Protocol


class BlackboardGateway(Protocol):
    def read_board(self, name: str, *, limit: int = 100) -> dict[str, Any]: ...

    def read_inbox(
        self, recipient: str, *, unread_only: bool = True, limit: int = 100
    ) -> dict[str, Any]: ...

    def post(
        self,
        *,
        board: str,
        title: str,
        body: str,
        author: str,
        kind: str = "freeform",
        tags: list[str] | None = None,
    ) -> dict[str, Any]: ...

    def send(
        self,
        *,
        to: str,
        body: str,
        from_id: str,
        priority: str = "normal",
        thread_id: str = "",
    ) -> dict[str, Any]: ...

    def ack(self, *, recipient: str, message_id: str) -> dict[str, Any]: ...