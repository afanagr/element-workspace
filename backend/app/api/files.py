"""Workspace file tree, uploads, downloads, versions and OCR endpoints."""

from __future__ import annotations

from functools import partial

import uuid

import anyio
from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.deps import current_user
from app.logging_config import get_logger
from app.models import Node, NodeVersion, User
from app.schemas import (
    CreateFolderIn,
    CreateProjectIn,
    ExtractResult,
    NodeOut,
    PdfPagesSaveIn,
    SheetSaveIn,
    TextPreviewOut,
    TextSaveIn,
    UpdateNodeIn,
    UploadItem,
    UploadResult,
    VersionOut,
)
from app.services import nodes as node_service
from app.services.archive import build_zip, extract_archive, zip_response
from app.services.documents import apply_sheet_edits, build_pdf_page_edit
from app.services.indexer import enqueue_index
from app.services.parsing import (
    IMAGE_EXTENSIONS,
    OcrUnavailable,
    ParsedDocument,
    UnsupportedFormat,
    parse_document,
    view_kind,
)
from app.services.storage import decode_bytes, storage
from app.services.vectorstore import vector_store

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["files"])
CurrentUser = Depends(current_user)


@router.get("/tree", response_model=list[NodeOut])
async def get_tree(
    project_id: uuid.UUID | None = Query(default=None),
    owner_id: uuid.UUID | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> list[dict]:
    """Workspace tree.

    Users only ever see their own projects. An administrator sees everything and
    may narrow the listing to a single account with ``owner_id``.
    """
    if user.is_admin:
        return await node_service.build_tree(
            db, project_id, owner_id=owner_id, with_owners=True
        )
    return await node_service.build_tree(db, project_id, owner_id=user.id)


@router.post("/projects", response_model=NodeOut, status_code=201)
async def create_project(
    payload: CreateProjectIn,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> dict:
    node = await node_service.create_project(
        db, payload.name, created_by=user.username, owner_id=user.id
    )
    await db.commit()
    return node_service.node_to_dict(node)


@router.post("/folders", response_model=NodeOut, status_code=201)
async def create_folder(
    payload: CreateFolderIn,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> dict:
    parent = await node_service.get_node_for(db, payload.parent_id, user)
    node = await node_service.create_folder(
        db, parent.id, payload.name, created_by=user.username
    )
    await db.commit()
    return node_service.node_to_dict(node)


@router.post("/upload", response_model=UploadResult)
async def upload_files(
    parent_id: uuid.UUID = Form(...),
    files: list[UploadFile] = File(...),
    paths: list[str] | None = Form(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> UploadResult:
    parent = await node_service.get_node_for(db, parent_id, user)
    if parent.kind == "file":
        raise node_service.NodeError("Cannot upload into a file")
    author = user.username
    items: list[UploadItem] = []
    errors: list[str] = []
    to_index: list[uuid.UUID] = []
    max_bytes = settings.max_upload_mb * 1024 * 1024

    for index, upload in enumerate(files):
        display = upload.filename or f"file-{index}"
        try:
            raw = await upload.read()
            if len(raw) > max_bytes:
                raise node_service.NodeError(
                    f"File exceeds {settings.max_upload_mb} MB limit", status_code=413
                )
            relative = (paths[index] if paths and index < len(paths) else "") or display
            parts = [
                node_service.sanitize_name(part)
                for part in relative.replace("\\", "/").split("/")
                if part.strip()
            ]
            if not parts:
                raise node_service.NodeError("Invalid upload path")
            filename = node_service.validate_upload(parts[-1], raw)
            current = parent
            for folder_name in parts[:-1]:
                existing = await node_service.find_child(db, current.id, folder_name)
                if existing is not None and existing.kind != "file":
                    current = existing
                else:
                    current = await node_service.create_folder(
                        db, current.id, folder_name, created_by=author
                    )
            node, replaced = await node_service.create_file(
                db,
                current.id,
                filename,
                raw,
                created_by=author,
                note="uploaded from browser",
            )
            to_index.append(node.id)
            items.append(
                UploadItem(
                    node=NodeOut.model_validate(node_service.node_to_dict(node)), replaced=replaced
                )
            )
        except node_service.NodeError as exc:
            errors.append(f"{display}: {exc.message}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Upload failed for %s", display)
            errors.append(f"{display}: {type(exc).__name__}: {exc}")
        finally:
            await upload.close()

    await db.commit()
    if to_index:
        enqueue_index(to_index)
    return UploadResult(items=items, errors=errors)


@router.get("/nodes/{node_id}/download")
async def download_node(
    node_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
):
    node = await node_service.get_node_for(db, node_id, user)
    if node.kind == "file":
        if not node.storage_key:
            raise node_service.NodeError("This file has no content", status_code=404)
        return FileResponse(
            storage.path_for(node.storage_key),
            media_type=node.mime or "application/octet-stream",
            filename=node.name,
        )
    archive = await build_zip([node], db)
    return zip_response(archive, node.name)


@router.get("/nodes/{node_id}/content")
async def node_content(
    node_id: uuid.UUID,
    download: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
):
    node = await node_service.get_node_for(db, node_id, user)
    if node.kind != "file" or not node.storage_key:
        raise node_service.NodeError("This node has no viewable content", status_code=404)
    headers = {"Cache-Control": "private, max-age=0, must-revalidate"}
    if node.sha256:
        headers["ETag"] = f'"{node.sha256}"'
    return FileResponse(
        storage.path_for(node.storage_key),
        media_type=node.mime or "application/octet-stream",
        filename=node.name,
        content_disposition_type="attachment" if download else "inline",
        headers=headers,
    )


def _text_preview(node: Node, parsed: ParsedDocument, max_chars: int) -> TextPreviewOut:
    return TextPreviewOut(
        node_id=node.id,
        name=node.name,
        view_kind=view_kind(node.name, node.mime),
        text=parsed.text[:max_chars],
        segments=[
            {"label": segment.label, "text": segment.text[:20000]} for segment in parsed.segments
        ],
        meta=parsed.meta,
        truncated=len(parsed.text) > max_chars,
    )


@router.get("/nodes/{node_id}/text", response_model=TextPreviewOut)
async def node_text(
    node_id: uuid.UUID,
    max_chars: int = Query(default=60000, ge=1000, le=200000),
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> TextPreviewOut:
    node = await node_service.get_node_for(db, node_id, user)
    if node.kind != "file" or not node.storage_key:
        raise node_service.NodeError("This node has no extractable text", status_code=404)
    data = await storage.read_bytes(node.storage_key)
    try:
        parsed = await anyio.to_thread.run_sync(parse_document, node.name, data)
    except UnsupportedFormat as exc:
        raise node_service.NodeError(str(exc), status_code=422) from exc
    except OcrUnavailable as exc:
        raise node_service.NodeError(f"OCR unavailable: {exc}", status_code=503) from exc
    return _text_preview(node, parsed, max_chars)


@router.post("/nodes/{node_id}/ocr", response_model=TextPreviewOut)
async def node_ocr(
    node_id: uuid.UUID,
    max_chars: int = Query(default=60000, ge=1000, le=200000),
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> TextPreviewOut:
    """Recognise text on a scanned PDF (or an image) and queue re-indexing.

    OCR runs in a worker thread; the result is cached by content hash, so the
    following index pass and repeated clicks are cheap.
    """
    node = await node_service.get_node_for(db, node_id, user)
    if node.kind != "file" or not node.storage_key:
        raise node_service.NodeError("This node has no content", status_code=404)
    extension = node_service.extension_of(node.name)
    if extension != ".pdf" and extension not in IMAGE_EXTENSIONS:
        raise node_service.NodeError(
            "Text recognition is available for PDF and image files", status_code=415
        )
    data = await storage.read_bytes(node.storage_key)
    try:
        parsed = await anyio.to_thread.run_sync(
            partial(parse_document, node.name, data, True)
        )
    except UnsupportedFormat as exc:
        raise node_service.NodeError(str(exc), status_code=422) from exc
    except OcrUnavailable as exc:
        raise node_service.NodeError(f"OCR unavailable: {exc}", status_code=503) from exc
    enqueue_index([node.id])
    return _text_preview(node, parsed, max_chars)


@router.get("/nodes/{node_id}/source")
async def node_source_text(
    node_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> dict:
    """Raw decoded text of an editable text file (used by the CSV editor)."""
    node = await node_service.get_node_for(db, node_id, user)
    if not node_service.is_text_editable(node):
        raise node_service.NodeError("This file type cannot be edited as text", status_code=415)
    data = await storage.read_bytes(node.storage_key)
    if len(data) > 5_000_000:
        raise node_service.NodeError("This file is too large to edit", status_code=413)
    return {"content": decode_bytes(data)}


@router.put("/nodes/{node_id}/content", response_model=NodeOut)
async def save_text_content(
    node_id: uuid.UUID,
    payload: TextSaveIn,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> dict:
    """Save a text file edited directly in the viewer (creates a new version)."""
    node = await node_service.get_node_for(db, node_id, user)
    if not node_service.is_text_editable(node):
        raise node_service.NodeError("This file type cannot be edited as text", status_code=415)
    data = payload.content.encode("utf-8")
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise node_service.NodeError(
            f"File exceeds {settings.max_upload_mb} MB limit", status_code=413
        )
    await node_service.write_file(
        db, node, data, created_by=user.username, note="edited in viewer"
    )
    await db.commit()
    enqueue_index([node.id])
    return node_service.node_to_dict(node)


@router.put("/nodes/{node_id}/sheet", response_model=NodeOut)
async def save_sheet_cells(
    node_id: uuid.UUID,
    payload: SheetSaveIn,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> dict:
    """Apply direct cell edits to an .xlsx workbook (creates a new version)."""
    node = await node_service.get_node_for(db, node_id, user)
    if node.kind != "file" or not node.storage_key:
        raise node_service.NodeError("This node has no editable content", status_code=404)
    if node_service.extension_of(node.name) not in (".xlsx", ".xlsm"):
        raise node_service.NodeError("Direct cell editing supports .xlsx/.xlsm only", status_code=415)
    if len(payload.cells) > 5000:
        raise node_service.NodeError("Too many cells in one request", status_code=413)
    if not payload.cells:
        return node_service.node_to_dict(node)
    data = await storage.read_bytes(node.storage_key)
    try:
        updated = await anyio.to_thread.run_sync(
            apply_sheet_edits, data, [cell.model_dump() for cell in payload.cells]
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Saving sheet cells failed")
        raise node_service.NodeError(f"Cannot save cells: {exc}", status_code=422) from exc
    if len(updated) > settings.max_upload_mb * 1024 * 1024:
        raise node_service.NodeError(
            f"File exceeds {settings.max_upload_mb} MB limit", status_code=413
        )
    await node_service.write_file(
        db, node, updated, created_by=user.username, note="edited in viewer"
    )
    await db.commit()
    enqueue_index([node.id])
    return node_service.node_to_dict(node)


@router.put("/nodes/{node_id}/pages", response_model=NodeOut)
async def save_pdf_pages(
    node_id: uuid.UUID,
    payload: PdfPagesSaveIn,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> dict:
    """Re-order / rotate / delete / merge PDF pages and save a new version."""
    node = await node_service.get_node_for(db, node_id, user)
    if node.kind != "file" or not node.storage_key:
        raise node_service.NodeError("This node has no editable content", status_code=404)
    if node_service.extension_of(node.name) != ".pdf":
        raise node_service.NodeError("Page editing supports .pdf files only", status_code=415)
    if not payload.pages:
        raise node_service.NodeError("The result must contain at least one page", status_code=400)
    if len(payload.pages) > 2000:
        raise node_service.NodeError("Too many pages in one request", status_code=413)

    target_id = str(node.id)
    target_data = await storage.read_bytes(node.storage_key)
    sources: dict[str, bytes] = {}
    for ref in payload.pages:
        if ref.node_id is None or str(ref.node_id) == target_id:
            continue
        key = str(ref.node_id)
        if key in sources:
            continue
        source = await node_service.get_node_for(db, ref.node_id, user)
        if source.kind != "file" or not source.storage_key:
            raise node_service.NodeError("Source PDF has no content", status_code=400)
        if node_service.extension_of(source.name) != ".pdf":
            raise node_service.NodeError("Only PDF files can be merged", status_code=400)
        if str(source.project_id) != str(node.project_id):
            raise node_service.NodeError(
                "Cannot merge a PDF from another project", status_code=403
            )
        sources[key] = await storage.read_bytes(source.storage_key)

    refs = [
        {
            "node_id": None
            if ref.node_id is None or str(ref.node_id) == target_id
            else str(ref.node_id),
            "page": ref.page,
            "rotate": ref.rotate,
        }
        for ref in payload.pages
    ]
    try:
        updated = await anyio.to_thread.run_sync(
            build_pdf_page_edit, target_data, sources, refs
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Saving PDF pages failed")
        raise node_service.NodeError(f"Cannot save pages: {exc}", status_code=422) from exc
    if len(updated) > settings.max_upload_mb * 1024 * 1024:
        raise node_service.NodeError(
            f"File exceeds {settings.max_upload_mb} MB limit", status_code=413
        )
    await node_service.write_file(
        db, node, updated, created_by=user.username, note="pages edited in viewer"
    )
    await db.commit()
    enqueue_index([node.id])
    return node_service.node_to_dict(node)


@router.get("/nodes/{node_id}/versions", response_model=list[VersionOut])
async def list_versions(
    node_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> list[NodeVersion]:
    node = await node_service.get_node_for(db, node_id, user)
    result = await db.execute(
        select(NodeVersion)
        .where(NodeVersion.node_id == node.id)
        .order_by(NodeVersion.version_no.desc())
    )
    return list(result.scalars().all())


@router.post("/nodes/{node_id}/versions/{version_id}/revert", response_model=NodeOut)
async def revert_version(
    node_id: uuid.UUID,
    version_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> dict:
    node = await node_service.get_node_for(db, node_id, user)
    version = await db.get(NodeVersion, version_id)
    if version is None or version.node_id != node.id:
        raise node_service.NodeError("Version not found", status_code=404)
    await node_service.revert_to_version(db, node, version, created_by=user.username)
    await db.commit()
    enqueue_index([node.id])
    return node_service.node_to_dict(node)


@router.patch("/nodes/{node_id}", response_model=NodeOut)
async def update_node(
    node_id: uuid.UUID,
    payload: UpdateNodeIn,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> dict:
    node = await node_service.get_node_for(db, node_id, user)
    if payload.parent_id is not None and payload.parent_id != node.parent_id:
        target = await node_service.get_node_for(db, payload.parent_id, user)
        node = await node_service.move_node(db, node, target.id)
    if payload.name:
        node = await node_service.rename_node(db, node, payload.name)
    if node.kind == "file":
        node.index_status = "pending"
        node.chunk_count = 0
    await db.commit()
    if node.kind == "file":
        enqueue_index([node.id])
    return node_service.node_to_dict(node)


@router.delete("/nodes/{node_id}")
async def delete_node(
    node_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> dict:
    node = await node_service.get_node_for(db, node_id, user)
    snapshot = await node_service.delete_node(db, node)
    await db.commit()
    for item in snapshot.get("nodes", []):
        if item.get("kind") == "file":
            try:
                await vector_store.delete_node(item["id"])
            except Exception:  # noqa: BLE001
                logger.debug("Vector cleanup failed for %s", item["id"], exc_info=True)
    return {
        "deleted": True,
        "node_id": snapshot.get("root_id"),
        "items": len(snapshot.get("nodes", [])),
    }


@router.post("/nodes/{node_id}/extract", response_model=ExtractResult)
async def extract_into_folder(
    node_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> ExtractResult:
    parent = await node_service.get_node_for(db, node_id, user)
    data = await file.read()
    await file.close()
    try:
        created = await extract_archive(db, parent, data, created_by=user.username)
    except node_service.NodeError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Archive extraction failed")
        raise node_service.NodeError(f"Extraction failed: {exc}", status_code=422) from exc
    await db.commit()
    return ExtractResult(
        created=[NodeOut.model_validate(node_service.node_to_dict(node)) for node in created],
        count=len(created),
    )


@router.post("/nodes/{node_id}/reindex")
async def reindex_node(
    node_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> dict:
    node = await node_service.get_node_for(db, node_id, user)
    if node.kind == "file":
        targets: list[Node] = [node]
    else:
        project_id = node.id if node.kind == "project" else node.project_id
        result = await db.execute(
            select(Node).where(Node.kind == "file", Node.project_id == project_id)
        )
        targets = list(result.scalars().all())
    for target in targets:
        target.index_status = "pending"
        target.chunk_count = 0
    await db.commit()
    enqueue_index([target.id for target in targets])
    return {"queued": len(targets)}


@router.get("/index/status")
async def index_status(
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> dict:
    from sqlalchemy import func

    stmt = (
        select(Node.index_status, func.count(Node.id))
        .where(Node.kind == "file")
        .group_by(Node.index_status)
    )
    chunk_stmt = select(func.coalesce(func.sum(Node.chunk_count), 0)).where(Node.kind == "file")
    if not user.is_admin:
        stmt = stmt.where(Node.owner_id == user.id)
        chunk_stmt = chunk_stmt.where(Node.owner_id == user.id)
    result = await db.execute(stmt)
    counts = {status: count for status, count in result.all()}
    chunk_result = await db.execute(chunk_stmt)
    try:
        points = await vector_store.count_points()
    except Exception:  # noqa: BLE001
        points = -1
    return {"files": counts, "chunks": int(chunk_result.scalar() or 0), "vectors": points}


