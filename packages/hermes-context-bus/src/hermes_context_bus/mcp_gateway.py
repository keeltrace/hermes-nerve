from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .gateway import BlackboardGateway


class SharedContextError(RuntimeError):
    pass


@dataclass(frozen=True)
class MCPConfig:
    server: str = "megamcp"
    timeout: float = 15.0


def _unwrap(envelope: Any) -> Any:
    if not isinstance(envelope, dict):
        raise SharedContextError("MCP call returned a non-object envelope")
    if envelope.get("ok") is False:
        raise SharedContextError(str(envelope.get("error") or "MCP call failed"))
    result = envelope.get("result")
    if isinstance(result, dict) and result.get("ok") is False:
        error = result.get("error")
        if isinstance(error, dict):
            code = error.get("code") or "ERROR"
            message = error.get("message") or error
            raise SharedContextError(f"{code}: {message}")
        raise SharedContextError(str(error or "MegaMCP call failed"))
    if isinstance(result, dict) and result.get("ok") is True and "data" in result:
        return result["data"]
    return result


class MegaMCPBlackboardGateway(BlackboardGateway):
    """Adapter over Hermes PluginContext.call_mcp()."""

    def __init__(
        self,
        call_mcp: Callable[[str, str, dict[str, Any], float], dict[str, Any]],
        *,
        config: MCPConfig | None = None,
    ):
        self._call_mcp = call_mcp
        self.config = config or MCPConfig()

    def _call(self, tool: str, arguments: dict[str, Any]) -> Any:
        envelope = self._call_mcp(
            self.config.server,
            tool,
            arguments,
            self.config.timeout,
        )
        return _unwrap(envelope)

    def read_board(self, name: str, *, limit: int = 100) -> dict[str, Any]:
        data = self._call("bb.board", {"op": "read", "name": name, "limit": int(limit)})
        return data if isinstance(data, dict) else {"entries": [], "raw": data}

    def read_inbox(
        self, recipient: str, *, unread_only: bool = True, limit: int = 100
    ) -> dict[str, Any]:
        data = self._call(
            "bb.inbox",
            {
                "recipient": recipient,
                "unread_only": bool(unread_only),
                "limit": int(limit),
            },
        )
        return data if isinstance(data, dict) else {"messages": [], "raw": data}

    def post(
        self,
        *,
        board: str,
        title: str,
        body: str,
        author: str,
        kind: str = "freeform",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        data = self._call(
            "bb.post",
            {
                "board": board,
                "title": title,
                "body": body,
                "author": author,
                "kind": kind,
                "tags": list(tags or []),
            },
        )
        return data if isinstance(data, dict) else {"raw": data}

    def send(
        self,
        *,
        to: str,
        body: str,
        from_id: str,
        priority: str = "normal",
        thread_id: str = "",
    ) -> dict[str, Any]:
        data = self._call(
            "bb.send",
            {
                "to": to,
                "body": body,
                "from_id": from_id,
                "priority": priority,
                "thread_id": thread_id,
            },
        )
        return data if isinstance(data, dict) else {"raw": data}

    def ack(self, *, recipient: str, message_id: str) -> dict[str, Any]:
        data = self._call(
            "bb.ack",
            {"recipient": recipient, "message_id": message_id},
        )
        return data if isinstance(data, dict) else {"raw": data}

    def thread(
        self, thread_id: str, *, recipient: str = "", limit: int = 200
    ) -> dict[str, Any]:
        data = self._call(
            "bb.thread",
            {
                "thread_id": thread_id,
                "recipient": recipient,
                "limit": int(limit),
            },
        )
        return data if isinstance(data, dict) else {"messages": [], "raw": data}