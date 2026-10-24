from __future__ import annotations

import logging
from typing import Any

from .bridge import (
    Blackboard,
    bounded_int,
    compact,
    remember_session,
    render_context,
    resolve_agent_id,
)
from .tools import bind_context, schemas

logger = logging.getLogger(__name__)
_CTX: Any = None
_TURN_INBOX_IDS: dict[tuple[str, str], list[str]] = {}


def _setting(key: str, default: Any) -> Any:
    try:
        value = _CTX.get_config(key, default)
    except Exception:
        return default
    return default if value is None else value


def _pre_llm_call(
    session_id: str = "",
    turn_id: str = "",
    platform: str = "",
    **_: Any,
):
    agent_id = resolve_agent_id(platform, session_id=session_id)
    if not agent_id:
        return None
    remember_session(session_id, agent_id)

    bb = Blackboard(_CTX)
    board_limit = bounded_int(_setting("board_limit", 20), 20, 1, 100)
    inbox_limit = bounded_int(_setting("inbox_limit", 20), 20, 1, 100)
    context_limit = bounded_int(_setting("context_limit", 3500), 3500, 500, 9000)

    notices: list[str] = []
    try:
        board = bb.board(limit=board_limit)
    except Exception as exc:
        board = {"entries": []}
        notices.append(f"shared board unavailable: {compact(exc, 180)}")
    try:
        inbox = bb.inbox(agent_id, unread_only=True, limit=inbox_limit)
    except Exception as exc:
        inbox = {"messages": []}
        notices.append(f"inbox unavailable: {compact(exc, 180)}")

    ids = [
        str(message.get("message_id") or "").strip()
        for message in list(inbox.get("messages") or [])
        if str(message.get("message_id") or "").strip()
    ]
    if session_id and turn_id:
        _TURN_INBOX_IDS[(str(session_id), str(turn_id))] = ids

    context = render_context(
        agent_id=agent_id,
        board=board,
        inbox=inbox,
        max_chars=context_limit,
    )
    if notices:
        context += "\n\nTransport status: " + "; ".join(notices)
    return {"context": context}


def _auto_ack(session_id: str, turn_id: str, agent_id: str, bb: Blackboard) -> None:
    ids = _TURN_INBOX_IDS.pop((str(session_id), str(turn_id)), [])
    for message_id in ids:
        try:
            bb.ack(agent_id, message_id)
        except Exception as exc:
            logger.debug("shared-context ack failed for %s: %s", message_id, exc)


def _post_llm_call(
    session_id: str = "",
    turn_id: str = "",
    platform: str = "",
    user_message: Any = "",
    assistant_response: Any = "",
    model: str = "",
    **_: Any,
) -> None:
    agent_id = resolve_agent_id(platform, session_id=session_id)
    if not agent_id:
        return
    remember_session(session_id, agent_id)
    bb = Blackboard(_CTX)

    # A completed model turn means the injected inbox messages were actually
    # delivered to the agent. Ack them now so durable inbox backpressure stays bounded.
    _auto_ack(session_id, turn_id, agent_id, bb)

    if not bool(_setting("auto_handoff", True)):
        return

    response = str(assistant_response or "").strip()
    minimum = bounded_int(_setting("auto_handoff_min_chars", 240), 240, 0, 5000)
    if len(response) < minimum:
        return

    max_body = bounded_int(_setting("auto_handoff_body_chars", 2400), 2400, 500, 8000)
    include_user = bool(_setting("include_user_message", True))
    pieces = [
        f"Surface: {agent_id}",
        f"Session: {session_id or 'unknown'}",
        f"Turn: {turn_id or 'unknown'}",
        f"Model: {model or 'unknown'}",
    ]
    if include_user:
        pieces.extend(["", "User goal:", compact(user_message, 700)])
    pieces.extend(["", "Hermes result:", compact(response, max(300, max_body - 900))])
    body = "\n".join(pieces)
    if len(body) > max_body:
        body = body[: max_body - 1].rstrip() + "…"

    try:
        bb.post(
            agent_id,
            f"Turn handoff from {agent_id}",
            body,
            ["auto-handoff", "turn", agent_id],
        )
    except Exception as exc:
        # Shared context must never make the user's normal turn fail.
        logger.warning("shared-context automatic handoff failed: %s", exc)


def register(ctx) -> None:
    global _CTX
    _CTX = ctx
    bind_context(ctx)

    for name, schema, handler, description in schemas():
        ctx.register_tool(
            name=name,
            toolset="shared_context",
            schema=schema,
            handler=handler,
            description=description,
            emoji="",
        )

    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("post_llm_call", _post_llm_call)