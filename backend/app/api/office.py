"""OnlyOffice Document Server endpoints.

The browser receives a signed editor config; the Document Server fetches the
file and posts save callbacks through the internal compose network. Neither URL
depends on browser cookies or bearer tokens, so the Document Server can work
with them, while the HMAC signature keeps them short-lived and unguessable.
"""

from __future__ import annotations

import hashlib
import uuid

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.deps import current_user
from app.logging_config import get_logger
from app.models import User
from app.services import nodes as node_service
from app.services import office, office_convert
from app.services.indexer import enqueue_index
from app.services.storage import storage

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["office"])
CurrentUser = Depends(current_user)


@router.get("/office/config")
async def office_config(user: User = CurrentUser) -> dict:
    """Feature flag for the UI: only configured deployments use OnlyOffice."""
    return {
        "enabled": settings.office_configured,
        "documentServerUrl": settings.onlyoffice_url.rstrip("/") if settings.office_configured else "",
    }


@router.post("/nodes/{node_id}/office")
async def open_office_editor(
    node_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> dict:
    """Build a signed editor session for a node the caller can see."""
    node = await node_service.get_node_for(db, node_id, user)
    if node.kind != "file" or not node.storage_key:
        raise node_service.NodeError("This node has no editable content", status_code=404)
    return office.build_editor_config(node, user)


@router.get("/office/download/{token}")
async def office_download(token: str, db: AsyncSession = Depends(get_db)) -> FileResponse:
    """Serve a document to the Document Server using a signed, expiring link."""
    payload = office.verify_link(token, "dl")
    node = await node_service.get_node(db, uuid.UUID(str(payload["n"])))
    if node is None or node.kind != "file" or not node.storage_key:
        raise node_service.NodeError("File not found", status_code=404)
    if payload.get("u"):
        user = await db.get(User, uuid.UUID(str(payload["u"])))
        if user is None:
            raise office.OfficeError("Link owner no longer exists", status_code=403)
        node_service.ensure_access(node, user)
    return FileResponse(
        storage.open_path(node.storage_key),
        media_type=node.mime or "application/octet-stream",
        filename=node.name,
    )


@router.get("/office/convert-source/{token}")
async def office_convert_source(token: str) -> FileResponse:
    """Serve a staged document to the Document Server for conversion."""
    payload = office.verify_link(token, "cv")
    try:
        path = office_convert.source_path(str(payload.get("n") or ""))
    except office_convert.OfficeConversionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=path.name,
    )


@router.post("/office/callback/{token}")
async def office_callback(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Receive save notifications from the Document Server."""
    payload = office.verify_link(token, "cb")
    if not settings.office_configured:
        return JSONResponse(status_code=503, content={"error": 1})
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": 1})

    ds_token = ""
    if authorization and authorization.lower().startswith("bearer "):
        ds_token = authorization.split(" ", 1)[1].strip()
    ds_token = ds_token or str(body.get("token") or "")
    try:
        office.decode_jwt(ds_token)
    except office.OfficeError:
        logger.warning("OnlyOffice callback rejected: missing or invalid JWT")
        return JSONResponse(status_code=403, content={"error": 1})

    status = int(body.get("status") or 0)
    if status in (3, 7):
        logger.warning(
            "OnlyOffice could not save the document (status=%s, key=%s)",
            status,
            body.get("key"),
        )
        return JSONResponse(status_code=500, content={"error": 1})
    logger.info("OnlyOffice callback: status=%s key=%s", status, body.get("key"))
    if status not in (2, 6):
        # 1 = editing, 4 = closed without changes.
        return JSONResponse({"error": 0})

    url = str(body.get("url") or "")
    if not url:
        return JSONResponse(status_code=400, content={"error": 1})

    node = await node_service.get_node(db, uuid.UUID(str(payload["n"])), allow_missing=True)
    if node is None or node.kind != "file" or not node.storage_key:
        # The file was deleted while the editor was open - nothing to store.
        return JSONResponse({"error": 0})

    source = office.safe_source_url(url)
    try:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            response = await client.get(source)
            response.raise_for_status()
            data = response.content
    except Exception:  # noqa: BLE001
        logger.exception("Could not download the edited document from OnlyOffice")
        return JSONResponse({"error": 1})

    if not data:
        return JSONResponse(status_code=400, content={"error": 1})
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        logger.warning("OnlyOffice returned a file larger than the upload limit")
        return JSONResponse({"error": 1})

    if node_service.extension_of(node.name) == ".csv" and office.is_zip_bytes(data):
        # OnlyOffice stores CSV as an OOXML workbook; convert it back so the
        # node keeps its name, MIME type and text-based indexing.
        import anyio

        data = await anyio.to_thread.run_sync(office.spreadsheet_to_csv, data)

    if node.sha256 == hashlib.sha256(data).hexdigest():
        return JSONResponse({"error": 0})

    await node_service.write_file(
        db, node, data, created_by="onlyoffice", note="edited in OnlyOffice"
    )
    await db.commit()
    enqueue_index([node.id])
    logger.info("OnlyOffice saved node %s (%s bytes)", node.id, len(data))
    return JSONResponse({"error": 0})
