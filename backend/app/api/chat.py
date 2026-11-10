"""Chat sessions, streaming (WebSocket + NDJSON) and operation rollback."""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db, session_scope
from app.deps import current_user
from app.logging_config import get_logger
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
async def list_sessions(db: AsyncSession = Depends(get_db)) -> list:
    return await chat_service.list_sessions(db)


@router.post("/sessions", response_model=SessionOut, status_code=201)
async def create_session(
    payload: SessionCreateIn,
    db: AsyncSession = Depends(get_db),
    user: str = CurrentUser,
):
    session = await chat_service.create_session(
        db,
        title=payload.title or "New chat",
        project_id=payload.project_id,
        provider=payload.provider,
        model=payload.model,
    )
    await db.commit()
    return session


@router.get("/sessions/{session_id}/messages", response_model=list[MessageOut])
async def session_messages(session_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> list:
    await chat_service.get_session(db, session_id)
    return await chat_service.all_messages(db, session_id)


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> dict:
    session = await chat_service.get_session(db, session_id)
    await chat_service.delete_session(db, session)
    await db.commit()
    return {"deleted": True}


@router.get("/sessions/{session_id}/operations", response_model=list[OperationOut])
async def session_operations(session_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> list:
    await chat_service.get_session(db, session_id)
    return await list_session_operations(db, session_id)


@router.post("/operations/{operation_id}/revert")
async def revert(operation_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        operation = await get_operation(db, operation_id)
        description = await revert_operation(db, operation)
    except (OperationError, NodeError) as exc:
        message = getattr(exc, "message", str(exc))
        raise HTTPException(status_code=400, detail=message) from exc
    await db.commit()
    return {"reverted": True, "operation_id": str(operation_id), "description": description}


@router.post("/rag/search", response_model=RagQueryOut)
async def rag_search(payload: RagQueryIn, db: AsyncSession = Depends(get_db)) -> RagQueryOut:
    result = await retrieve(
        db,
        payload.query,
        project_id=str(payload.project_id) if payload.project_id else None,
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
    user: str = CurrentUser,
):
    """NDJSON streaming fallback for clients that cannot use WebSockets."""
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
            async for event in run_agent(db, request):
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
    if settings.app_user_token and websocket.query_params.get("token") != settings.app_user_token:
        await websocket.close(code=4401)
        return
    try:
        identifier = uuid.UUID(session_id)
    except ValueError:
        await websocket.close(code=4400)
        return

    await websocket.accept()
    try:
        async with session_scope() as db:
            try:
                await chat_service.get_session(db, identifier)
            except NodeError:
                await websocket.send_json(
                    {"type": "error", "code": "not_found", "message": "Chat session not found"}
                )
                await websocket.close(code=4404)
                return

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
                    async for event in run_agent(db, request):
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

