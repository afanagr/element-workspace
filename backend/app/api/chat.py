"""Chat sessions, streaming (WebSocket + NDJSON) and operation rollback."""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db, session_scope
from app.deps import bearer_token, current_user
from app.logging_config import get_logger
from app.models import User
from app.schemas import (
    MessageOut,
    OperationOut,
    RagQueryIn,
    RagQueryOut,
    SendMessageIn,
    SessionCreateIn,
    SessionOut,
)
from app.services import chat_service
from app.services import nodes as node_service
from app.services import users as user_service
from app.services.agent import AgentRequest, run_agent
from app.services.nodes import NodeError
from app.services.operations import (
    OperationError,
    get_operation,
    list_session_operations,
    revert_operation,
)
from app.services.rag import retrieve

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["chat"])
CurrentUser = Depends(current_user)


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(
    project_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> list:
    """Chat history. Every chat belongs to one project and one account."""
    return await chat_service.list_sessions(
        db,
        owner_id=None if user.is_admin else user.id,
        project_id=project_id,
    )


@router.post("/sessions", response_model=SessionOut, status_code=201)
async def create_session(
    payload: SessionCreateIn,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
):
    """Create a chat bound to a project - the AI only ever sees that project."""
    if payload.project_id is None:
        raise HTTPException(
            status_code=400,
            detail="project_id is required: select a project before chatting",
        )
    project = await node_service.get_node_for(db, payload.project_id, user)
    if project.kind != "project":
        raise HTTPException(status_code=400, detail="project_id must point to a project")
    session = await chat_service.create_session(
        db,
        title=payload.title or "New chat",
        project_id=project.id,
        provider=payload.provider,
        model=payload.model,
        owner_id=user.id,
    )
    await db.commit()
    return session


@router.get("/sessions/{session_id}/messages", response_model=list[MessageOut])
async def session_messages(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> list:
    await chat_service.get_session(db, session_id, user)
    return await chat_service.all_messages(db, session_id)


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> dict:
    session = await chat_service.get_session(db, session_id, user)
    await chat_service.delete_session(db, session)
    await db.commit()
    return {"deleted": True}


@router.get("/sessions/{session_id}/operations", response_model=list[OperationOut])
async def session_operations(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> list:
    await chat_service.get_session(db, session_id, user)
    return await list_session_operations(db, session_id)


@router.post("/operations/{operation_id}/revert")
async def revert(
    operation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> dict:
    try:
        operation = await get_operation(db, operation_id)
        if operation.session_id:
            await chat_service.get_session(db, operation.session_id, user)
        elif not user.is_admin:
            raise NodeError("Operation not found", status_code=404)
        description = await revert_operation(db, operation)
    except (OperationError, NodeError) as exc:
        message = getattr(exc, "message", str(exc))
        status_code = getattr(exc, "status_code", 400)
        raise HTTPException(status_code=status_code, detail=message) from exc
    await db.commit()
    return {"reverted": True, "operation_id": str(operation_id), "description": description}


@router.post("/rag/search", response_model=RagQueryOut)
async def rag_search(
    payload: RagQueryIn,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> RagQueryOut:
    """Semantic search. Always scoped to one project the caller can access."""
    if payload.project_id is None:
        return RagQueryOut(citations=[], error="project_id is required")
    try:
        await node_service.get_project_for(db, payload.project_id, user)
    except NodeError as exc:
        return RagQueryOut(citations=[], error=exc.message)
    result = await retrieve(
        db,
        payload.query,
        project_id=str(payload.project_id),
        node_ids=[str(item) for item in payload.node_ids] or None,
        top_k=payload.top_k,
    )
    return RagQueryOut(citations=[c.to_dict() for c in result.citations], error=result.error)


def _agent_request(session_id: uuid.UUID, payload: dict[str, Any]) -> AgentRequest:
    return AgentRequest(
        session_id=session_id,
        text=str(payload.get("content") or "").strip(),
        provider=(payload.get("provider") or None),
        model=(payload.get("model") or None),
        attachments=[str(item) for item in (payload.get("attachments") or [])],
        use_rag=bool(payload.get("use_rag", True)),
    )


@router.post("/sessions/{session_id}/message")
async def send_message(
    session_id: uuid.UUID,
    payload: SendMessageIn,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
):
    """NDJSON streaming fallback for clients that cannot use WebSockets."""
    await chat_service.get_session(db, session_id, user)
    request = _agent_request(
        session_id,
        {
            "content": payload.content,
            "provider": payload.provider,
            "model": payload.model,
            "attachments": [str(item) for item in payload.attachments],
            "use_rag": payload.use_rag,
        },
    )

    async def event_stream():
        try:
            async for event in run_agent(db, request, user):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:  # noqa: BLE001
            logger.exception("NDJSON agent failed")
            yield json.dumps(
                {"type": "error", "code": "internal_error", "message": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            ) + "\n"
        yield json.dumps({"type": "stream_end"}, ensure_ascii=False) + "\n"

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.websocket("/ws/chat/{session_id}")
async def chat_websocket(websocket: WebSocket, session_id: str) -> None:
    token = (
        websocket.query_params.get("token")
        or bearer_token(websocket.headers.get("authorization"))
    )
    try:
        identifier = uuid.UUID(session_id)
    except ValueError:
        await websocket.close(code=4400)
        return

    async with session_scope() as db:
        user = await user_service.resolve_token(db, token or "")
        if user is None:
            await websocket.close(code=4401)
            return
        try:
            await chat_service.get_session(db, identifier, user)
        except NodeError:
            await websocket.accept()
            await websocket.send_json(
                {"type": "error", "code": "not_found", "message": "Chat session not found"}
            )
            await websocket.close(code=4404)
            return

    await websocket.accept()
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json(
                    {"type": "error", "code": "bad_json", "message": "Expected a JSON frame"}
                )
                continue
            frame_type = payload.get("type") or "message"
            if frame_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue
            if frame_type != "message":
                await websocket.send_json(
                    {"type": "error", "code": "bad_frame", "message": f"Unknown frame: {frame_type}"}
                )
                continue

            request = _agent_request(identifier, payload)
            if not request.text:
                await websocket.send_json(
                    {"type": "error", "code": "empty", "message": "Message content is empty"}
                )
                continue

            try:
                async with session_scope() as db:
                    current = await user_service.resolve_token(db, token or "")
                    if current is None:
                        await websocket.send_json(
                            {"type": "error", "code": "auth", "message": "Session expired"}
                        )
                        await websocket.close(code=4401)
                        return
                    await chat_service.get_session(db, identifier, current)
                    async for event in run_agent(db, request, current):
                        await websocket.send_json(event)
            except WebSocketDisconnect:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Chat websocket agent failed")
                await websocket.send_json(
                    {
                        "type": "error",
                        "code": "internal_error",
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                )
            await websocket.send_json({"type": "stream_end"})
    except WebSocketDisconnect:
        logger.info("Chat websocket %s disconnected", session_id)
    except Exception:  # noqa: BLE001
        logger.exception("Chat websocket crashed")
        try:
            await websocket.close(code=1011)
        except Exception:  # pragma: no cover
            pass

