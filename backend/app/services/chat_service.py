"""Chat session/message persistence and LLM usage logging."""

from __future__ import annotations

import uuid
from typing import Any, Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChatMessage, ChatSession, LLMLog
from app.services.nodes import NodeError


async def create_session(
    db: AsyncSession,
    *,
    title: str = "New chat",
    project_id: uuid.UUID | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> ChatSession:
    session = ChatSession(title=title[:250] or "New chat", project_id=project_id, provider=provider, model=model)
    db.add(session)
    await db.flush()
    return session


async def get_session(db: AsyncSession, session_id: uuid.UUID | str) -> ChatSession:
    try:
        identifier = uuid.UUID(str(session_id))
    except (ValueError, AttributeError) as exc:
        raise NodeError("Invalid session id") from exc
    session = await db.get(ChatSession, identifier)
    if session is None:
        raise NodeError("Chat session not found", status_code=404)
    return session


async def list_sessions(db: AsyncSession, limit: int = 50) -> list[ChatSession]:
    result = await db.execute(
        select(ChatSession).order_by(ChatSession.updated_at.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def delete_session(db: AsyncSession, session: ChatSession) -> None:
    await db.execute(delete(ChatMessage).where(ChatMessage.session_id == session.id))
    await db.delete(session)
    await db.flush()


async def add_message(
    db: AsyncSession,
    session_id: uuid.UUID,
    *,
    role: str,
    content: str = "",
    provider: str | None = None,
    model: str | None = None,
    tool_calls: list[Any] | None = None,
    attachments: list[Any] | None = None,
    citations: list[Any] | None = None,
    usage: dict[str, Any] | None = None,
    error: str | None = None,
) -> ChatMessage:
    message = ChatMessage(
        session_id=session_id,
        role=role,
        content=content or "",
        provider=provider,
        model=model,
        tool_calls=tool_calls,
        attachments=attachments,
        citations=citations,
        usage=usage,
        error=error,
    )
    db.add(message)
    await db.flush()
    return message


async def history(db: AsyncSession, session_id: uuid.UUID, limit: int = 30) -> list[ChatMessage]:
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(limit)
    )
    messages = list(result.scalars().all())
    messages.reverse()
    return messages


async def all_messages(db: AsyncSession, session_id: uuid.UUID) -> list[ChatMessage]:
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at)
    )
    return list(result.scalars().all())


async def ensure_title(db: AsyncSession, session: ChatSession, text: str) -> None:
    if session.title and session.title != "New chat":
        return
    cleaned = " ".join(text.split())
    session.title = (cleaned[:80] or "New chat") if cleaned else "New chat"
    await db.flush()


async def log_llm_call(
    db: AsyncSession,
    *,
    provider: str,
    model: str,
    kind: str = "chat",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    duration_ms: float = 0.0,
    session_id: uuid.UUID | None = None,
    status: str = "ok",
    error: str | None = None,
) -> None:
    db.add(
        LLMLog(
            provider=provider,
            model=model,
            kind=kind,
            status=status,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            duration_ms=round(duration_ms, 1),
            session_id=session_id,
            error=(error or "")[:2000] or None,
        )
    )
    await db.flush()
