from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .gateway import BlackboardGateway
from .protocol import CANONICAL_BOARD, require_agent_id


@dataclass(frozen=True)
class StartupContext:
    agent_id: str
    board: dict[str, Any]
    inbox: dict[str, Any]


class ContextSession:
    def __init__(self, gateway: BlackboardGateway, agent_id: str):
        self.gateway = gateway
        self.agent_id = require_agent_id(agent_id)

    def startup(self, *, limit: int = 100) -> StartupContext:
        board = self.gateway.read_board(CANONICAL_BOARD, limit=limit)
        inbox = self.gateway.read_inbox(
            self.agent_id, unread_only=True, limit=limit
        )
        return StartupContext(self.agent_id, board, inbox)

    def handoff(self, title: str, body: str, *, tags: list[str] | None = None):
        clean_title = str(title).strip()
        clean_body = str(body).strip()
        if not clean_title or not clean_body:
            raise ValueError("handoff title and body are required")
        return self.gateway.post(
            board=CANONICAL_BOARD,
            title=clean_title,
            body=clean_body,
            author=self.agent_id,
            kind="freeform",
            tags=["handoff", self.agent_id, *(tags or [])],
        )

    def send(
        self,
        to: str,
        body: str,
        *,
        priority: str = "normal",
        thread_id: str = "",
    ):
        require_agent_id(to)
        clean_body = str(body).strip()
        if not clean_body:
            raise ValueError("message body is required")
        return self.gateway.send(
            to=to,
            body=clean_body,
            from_id=self.agent_id,
            priority=priority,
            thread_id=thread_id,
        )