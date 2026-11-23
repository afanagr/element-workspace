"""Document template library API (standard + per-user)."""

from __future__ import annotations

import uuid

import anyio
from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import current_user
from app.models import User
from app.schemas import (
    NodeOut,
    TemplateFillIn,
    TemplateFromNodeIn,
    TemplateOut,
    TemplateProposeIn,
    TemplateProposeOut,
    TemplateUpdateIn,
)
from app.services import nodes as node_service
from app.services import templates as template_service
from app.services.indexer import enqueue_index
from app.services.storage import storage

router = APIRouter(prefix="/api/templates", tags=["templates"])
CurrentUser = Depends(current_user)


def _scope(value: str) -> str:
    allowed = {"standard", "user"}
    if value not in allowed:
        raise template_service.TemplateError("scope must be standard or user")
    return value


@router.get("", response_model=list[TemplateOut])
async def list_templates(
    scope: str = Query(default="available", pattern="^(available|standard|mine)$"),
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> list[dict]:
    templates = await template_service.list_templates(db, user, scope)
    return [template_service.template_to_dict(item) for item in templates]  # type: ignore[return-value]


@router.post("", response_model=TemplateOut, status_code=201)
async def upload_template(
    file: UploadFile = File(...),
    name: str | None = Form(default=None),
    description: str | None = Form(default=None),
    scope: str = Form(default="user"),
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> dict:
    target_scope = _scope(scope)
    if target_scope == "standard" and not user.is_admin:
        raise template_service.TemplateError(
            "Only an administrator can manage standard templates", status_code=403
        )
    source_name = file.filename or "template.docx"
    data = await file.read()
    template = await template_service.create_from_bytes(
        db,
        user,
        scope=target_scope,
        name=name,
        description=description,
        source_name=source_name,
        data=data,
    )
    await db.commit()
    await db.refresh(template)
    return template_service.template_to_dict(template)  # type: ignore[return-value]


@router.post("/from-node", response_model=TemplateOut, status_code=201)
async def create_template_from_node(
    payload: TemplateFromNodeIn,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> dict:
    if not payload.node_id:
        raise template_service.TemplateError("node_id is required")
    node = await node_service.get_node_for(db, payload.node_id, user)
    template = await template_service.create_from_node(
        db,
        user,
        node,
        name=payload.name,
        description=payload.description,
        replacements=payload.replacements,
        fields=payload.fields,
    )
    await db.commit()
    await db.refresh(template)
    return template_service.template_to_dict(template)  # type: ignore[return-value]


@router.patch("/{template_id}", response_model=TemplateOut)
async def update_template(
    template_id: uuid.UUID,
    payload: TemplateUpdateIn,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> dict:
    template = await template_service.get_template(db, user, template_id)
    updated = await template_service.update_template(
        db,
        user,
        template,
        name=payload.name,
        description=payload.description,
        fields=payload.fields,
    )
    await db.commit()
    await db.refresh(updated)
    return template_service.template_to_dict(updated)  # type: ignore[return-value]


@router.post("/{template_id}/markup", response_model=TemplateOut)
async def markup_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> dict:
    """Use the account's AI model to replace document values with placeholders."""
    template = await template_service.get_template(db, user, template_id)
    updated, applied, skipped = await template_service.markup_template_with_ai(
        db, user, template
    )
    await db.commit()
    await db.refresh(updated)
    payload = template_service.template_to_dict(updated)
    payload["markup_applied"] = applied
    payload["markup_skipped"] = skipped
    return payload  # type: ignore[return-value]


@router.post("/{template_id}/fill", response_model=NodeOut)
async def fill_template(
    template_id: uuid.UUID,
    payload: TemplateFillIn,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> dict:
    """Create a project file from a template without using the chat."""
    template = await template_service.get_template(db, user, template_id)
    parent = await node_service.get_node_for(db, payload.parent_id, user)
    if parent.kind == "file":
        raise template_service.TemplateError("parent_id must be a folder or project")
    output_name = (payload.name or f"{template.name}{template.extension}").strip()
    if not output_name.lower().endswith(template.extension):
        raise template_service.TemplateError(
            f"This template produces {template.kind.upper()}; the file name must end with "
            f"{template.extension}"
        )
    rendered, verification = await template_service.render_template(template, payload.data)
    node, replaced = await node_service.create_file(
        db,
        parent.id,
        node_service.sanitize_name(output_name),
        rendered,
        created_by=user.username,
        note=f"filled from template {template.name}",
    )
    await db.commit()
    await db.refresh(node)
    enqueue_index([node.id])
    result = node_service.node_to_dict(node)
    result["verification"] = verification
    return result  # type: ignore[return-value]


@router.delete("/{template_id}")
async def delete_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> dict[str, bool]:
    template = await template_service.get_template(db, user, template_id)
    await template_service.delete_template(db, user, template)
    await db.commit()
    return {"deleted": True}


@router.post("/propose", response_model=TemplateProposeOut)
async def propose_template_fields(
    payload: TemplateProposeIn,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> dict:
    """Suggest concrete values (dates, INN, amounts, names...) to turn into placeholders."""
    node = await node_service.get_node_for(db, payload.node_id, user)
    if node.kind != "file" or not node.storage_key:
        raise template_service.TemplateError("Only a file can become a template")
    kind = template_service.TEMPLATE_EXTENSIONS.get(node_service.extension_of(node.name))
    if kind is None:
        raise template_service.TemplateError(
            "Templates support only DOCX and XLSX files", status_code=415
        )
    data = await storage.read_bytes(node.storage_key)
    fields, _config = await anyio.to_thread.run_sync(
        template_service.detect_fields, kind, data
    )
    proposal = await anyio.to_thread.run_sync(
        template_service.propose_replacements, kind, data
    )
    return {
        "kind": kind,
        "placeholders": [str(item["name"]) for item in fields],
        "suggestions": proposal["suggestions"],
    }


@router.get("/{template_id}/download")
async def download_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
) -> FileResponse:
    template = await template_service.get_template(db, user, template_id)
    path = await storage.open_path(template.storage_key)
    filename = f"{template.name}{template.extension}"
    return FileResponse(
        path=path,
        media_type=template.mime or "application/octet-stream",
        filename=filename,
    )
