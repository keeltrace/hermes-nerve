from __future__ import annotations

from typing import Any

from .bridge import (
    AGENT_IDS,
    Blackboard,
    agent_for_tool_call,
    bounded_int,
    json_error,
    json_result,
)


def _agent(kwargs: dict[str, Any]) -> str:
    agent_id = agent_for_tool_call(kwargs.get("session_id"))
    if not agent_id:
        raise ValueError("shared-context surface identity could not be resolved")
    return agent_id


def handle_post(args: dict, **kwargs) -> str:
    try:
        agent_id = _agent(kwargs)
        title = str(args.get("title") or "").strip()
        body = str(args.get("body") or "").strip()
        if not title or not body:
            raise ValueError("title and body are required")
        tags = args.get("tags") or []
        if not isinstance(tags, list):
            raise ValueError("tags must be a list")
        clean_tags = [str(x).strip() for x in tags if str(x).strip()]
        out = Blackboard(_CTX).post(
            agent_id,
            title,
            body,
            ["handoff", agent_id, *clean_tags],
        )
        return json_result(out)
    except Exception as exc:
        return json_error(exc)


def handle_send(args: dict, **kwargs) -> str:
    try:
        agent_id = _agent(kwargs)
        to = str(args.get("to") or "").strip()
        if to not in AGENT_IDS:
            raise ValueError(f"to must be one of: {', '.join(AGENT_IDS)}")
        body = str(args.get("body") or "").strip()
        if not body:
            raise ValueError("body is required")
        priority = str(args.get("priority") or "normal").strip()
        thread_id = str(args.get("thread_id") or "").strip()
        out = Blackboard(_CTX).send(
            agent_id, to, body, priority=priority, thread_id=thread_id
        )
        return json_result(out)
    except Exception as exc:
        return json_error(exc)


def handle_inbox(args: dict, **kwargs) -> str:
    try:
        agent_id = _agent(kwargs)
        limit = bounded_int(args.get("limit"), 20, 1, 100)
        unread_only = bool(args.get("unread_only", True))
        out = Blackboard(_CTX).inbox(
            agent_id, unread_only=unread_only, limit=limit
        )
        return json_result(out)
    except Exception as exc:
        return json_error(exc)


def handle_thread(args: dict, **kwargs) -> str:
    try:
        agent_id = _agent(kwargs)
        thread_id = str(args.get("thread_id") or "").strip()
        if not thread_id:
            raise ValueError("thread_id is required")
        limit = bounded_int(args.get("limit"), 50, 1, 200)
        out = Blackboard(_CTX).thread(
            thread_id, recipient=agent_id, limit=limit
        )
        return json_result(out)
    except Exception as exc:
        return json_error(exc)


def handle_ack(args: dict, **kwargs) -> str:
    try:
        agent_id = _agent(kwargs)
        message_id = str(args.get("message_id") or "").strip()
        if not message_id:
            raise ValueError("message_id is required")
        return json_result(Blackboard(_CTX).ack(agent_id, message_id))
    except Exception as exc:
        return json_error(exc)


def handle_health(args: dict, **kwargs) -> str:
    try:
        bb = Blackboard(_CTX)
        return json_result({
            "status": "ok",
            "plugin": "hermes-context-bus",
            "version": "0.2.0",
            "configured_backend": bb.backend_mode,
            "authority": "coordination-data-only",
            "agents": list(AGENT_IDS),
        })
    except Exception as exc:
        return json_error(exc)


_CTX: Any = None


def bind_context(ctx: Any) -> None:
    global _CTX
    _CTX = ctx


def schemas() -> list[tuple[str, dict, Any, str]]:
    agent_enum = list(AGENT_IDS)
    return [
        (
            "shared_context_health",
            {
                "name": "shared_context_health",
                "description": "Report HermesContextBus compatibility and configured backend without mutating shared state.",
                "parameters": {"type": "object", "properties": {}},
            },
            handle_health,
            "Report shared-context plugin health and compatibility.",
        ),
        (
            "shared_context_post",
            {
                "name": "shared_context_post",
                "description": "Post a concise durable handoff to the transparent shared Hermes context board.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["title", "body"],
                },
            },
            handle_post,
            "Post a durable shared-context handoff.",
        ),
        (
            "shared_context_send",
            {
                "name": "shared_context_send",
                "description": "Send a direct coordination message to another Hermes surface.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "enum": agent_enum},
                        "body": {"type": "string"},
                        "priority": {
                            "type": "string",
                            "enum": ["low", "normal", "high", "urgent"],
                            "default": "normal",
                        },
                        "thread_id": {"type": "string"},
                    },
                    "required": ["to", "body"],
                },
            },
            handle_send,
            "Send a direct agent-to-agent message.",
        ),
        (
            "shared_context_inbox",
            {
                "name": "shared_context_inbox",
                "description": "Read this Hermes surface's shared-context inbox.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "unread_only": {"type": "boolean", "default": True},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                },
            },
            handle_inbox,
            "Read this surface's inbox.",
        ),
        (
            "shared_context_thread",
            {
                "name": "shared_context_thread",
                "description": "Read a shared-context conversation thread addressed to this Hermes surface.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "thread_id": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                    "required": ["thread_id"],
                },
            },
            handle_thread,
            "Read one agent-to-agent thread.",
        ),
        (
            "shared_context_ack",
            {
                "name": "shared_context_ack",
                "description": "Acknowledge a shared-context inbox message after it has been processed.",
                "parameters": {
                    "type": "object",
                    "properties": {"message_id": {"type": "string"}},
                    "required": ["message_id"],
                },
            },
            handle_ack,
            "Acknowledge one inbox message.",
        ),
    ]