"""AI tools: the only way the model can touch the workspace.

Every mutating tool records a reversible Operation so the UI can undo it.
"""

from __future__ import annotations

import io
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_config import get_logger
from app.llm.factory import registry
from app.llm.schemas import ChatMessage, TextDelta, ToolSpec
from app.models import Node, Operation
from app.services import nodes as node_service
from app.services.documents import build_text_document
from app.services.indexer import enqueue_index
from app.services.parsing import UnsupportedFormat, parse_document
from app.services.storage import storage
from app.services.vectorstore import vector_store

logger = get_logger(__name__)


class ToolError(Exception):
    pass


@dataclass(slots=True)
class ToolContext:
    db: AsyncSession
    session_id: uuid.UUID | None = None
    message_id: uuid.UUID | None = None
    project_id: str | None = None
    changed_nodes: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class ToolResult:
    ok: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    operation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "data": self.data,
            "operation_id": self.operation_id,
        }

    def as_prompt(self) -> str:
        payload: dict[str, Any] = {"ok": self.ok, "result": self.summary}
        if self.data:
            payload["data"] = self.data
        return json.dumps(payload, ensure_ascii=False)


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


_STR: dict[str, Any] = {"type": "string"}

TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="list_workspace",
        description=(
            "List the workspace tree (projects, folders, files with ids). "
            "Use this first to discover node ids before reading or editing."
        ),
        parameters=_obj(
            {
                "project_id": {"type": "string", "description": "Optional project id to limit the listing"},
                "max_depth": {"type": "integer", "description": "Depth limit, default 8"},
            }
        ),
    ),
    ToolSpec(
        name="read_file",
        description="Read the extracted text of a file (Excel cells, Word/PDF text, OCR text, code).",
        parameters=_obj(
            {
                "node_id": {"type": "string", "description": "File node id"},
                "max_chars": {"type": "integer", "description": "Truncate output, default 40000"},
                "sheet": {"type": "string", "description": "For .xlsx: read only this sheet"},
            },
            ["node_id"],
        ),
    ),
    ToolSpec(
        name="write_file",
        description=(
            "Create or overwrite a text/Markdown/CSV/JSON/Excel/DOCX/PDF file. "
            "For .xlsx, .docx and .pdf the plain text content is converted into the real format."
        ),
        parameters=_obj(
            {
                "parent_id": {"type": "string", "description": "Folder or project id"},
                "name": _STR,
                "content": {"type": "string", "description": "Full file content"},
            },
            ["parent_id", "name", "content"],
        ),
    ),
    ToolSpec(
        name="create_folder",
        description="Create a folder inside a project or another folder.",
        parameters=_obj({"parent_id": _STR, "name": _STR}, ["parent_id", "name"]),
    ),
    ToolSpec(
        name="create_project",
        description="Create a new top-level project (root folder).",
        parameters=_obj({"name": _STR}, ["name"]),
    ),
    ToolSpec(
        name="rename_node",
        description="Rename a file, folder or project.",
        parameters=_obj({"node_id": _STR, "new_name": _STR}, ["node_id", "new_name"]),
    ),
    ToolSpec(
        name="move_node",
        description="Move a file or folder into another folder/project.",
        parameters=_obj({"node_id": _STR, "new_parent_id": _STR}, ["node_id", "new_parent_id"]),
    ),
    ToolSpec(
        name="delete_node",
        description="Delete a file or folder (recoverable through the operation undo).",
        parameters=_obj({"node_id": _STR}, ["node_id"]),
    ),
    ToolSpec(
        name="excel_read",
        description="Read rows of an .xlsx sheet as cell references and values.",
        parameters=_obj(
            {"node_id": _STR, "sheet": _STR, "max_rows": {"type": "integer"}},
            ["node_id"],
        ),
    ),
    ToolSpec(
        name="excel_write",
        description="Write a block of rows into an .xlsx sheet (keeps other sheets intact).",
        parameters=_obj(
            {
                "node_id": _STR,
                "sheet": _STR,
                "start_cell": {"type": "string", "description": "Top-left cell, default A1"},
                "rows": {"type": "array", "items": {"type": "array", "items": {}}},
            },
            ["node_id", "sheet", "rows"],
        ),
    ),
    ToolSpec(
        name="excel_add_sheet",
        description="Add a new sheet to an existing .xlsx workbook, optionally with rows.",
        parameters=_obj(
            {
                "node_id": _STR,
                "sheet_name": _STR,
                "rows": {"type": "array", "items": {"type": "array", "items": {}}},
            },
            ["node_id", "sheet_name"],
        ),
    ),
    ToolSpec(
        name="search_documents",
        description=(
            "Semantic search over indexed workspace documents. Returns citations "
            "(file, fragment, score) to base answers on."
        ),
        parameters=_obj(
            {"query": _STR, "project_id": {"type": "string"}, "top_k": {"type": "integer"}},
            ["query"],
        ),
    ),
    ToolSpec(
        name="translate_file",
        description=(
            "Translate a text/DOCX/PDF/CSV file into another language using the active LLM "
            "and save the result as a new file."
        ),
        parameters=_obj(
            {
                "node_id": _STR,
                "target_language": {"type": "string", "description": "e.g. English, Russian, German"},
                "output_name": {"type": "string", "description": "Optional file name for the translation"},
            },
            ["node_id", "target_language"],
        ),
    ),
    ToolSpec(
        name="create_pdf",
        description="Generate a PDF document from Markdown-ish text.",
        parameters=_obj(
            {"parent_id": _STR, "name": _STR, "content": _STR},
            ["parent_id", "name", "content"],
        ),
    ),
]

TOOL_NAMES = {spec.name for spec in TOOL_SPECS}


def tool_specs(include_translate: bool = True) -> list[ToolSpec]:
    return [spec for spec in TOOL_SPECS if include_translate or spec.name != "translate_file"]


# --------------------------------------------------------------- operations
async def _record_operation(
    ctx: ToolContext,
    *,
    tool_name: str,
    description: str,
    node_id: uuid.UUID | None,
    undo: dict[str, Any] | None,
) -> str | None:
    if undo is None:
        return None
    operation = Operation(
        session_id=ctx.session_id,
        message_id=ctx.message_id,
        tool_name=tool_name,
        description=description[:500],
        node_id=node_id,
        undo=undo,
    )
    ctx.db.add(operation)
    await ctx.db.flush()
    return str(operation.id)


def _version_undo(node: Node) -> dict[str, Any]:
    """Undo payload restoring the current bytes of a file."""
    return {
        "action": "restore_file",
        "node_id": str(node.id),
        "storage_key": node.storage_key,
        "size": node.size,
        "sha256": node.sha256,
        "mime": node.mime,
    }


async def _resolve_node(ctx: ToolContext, node_id: Any) -> Node:
    if not node_id:
        raise ToolError("node_id is required")
    try:
        return await node_service.get_node(ctx.db, str(node_id))
    except node_service.NodeError as exc:
        raise ToolError(exc.message) from exc


def _tree_lines(items: list[dict[str, Any]], depth: int = 0, max_depth: int = 8) -> list[str]:
    lines: list[str] = []
    for item in items:
        if depth > max_depth or len(lines) > 500:
            break
        indent = "  " * depth
        marker = {"project": "[P]", "folder": "[D]", "file": "[F]"}.get(item["kind"], "[?]")
        status = ""
        if item["kind"] == "file":
            status = f" index={item.get('index_status') or 'pending'}"
            if item.get("size"):
                status += f" size={item['size']}b"
        lines.append(f"{indent}{marker} {item['name']} (id={item['id']}){status}")
        children = item.get("children") or []
        if children:
            lines.extend(_tree_lines(children, depth + 1, max_depth))
    return lines


# ------------------------------------------------------------- read tools
async def _list_workspace(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    project_id = args.get("project_id") or ctx.project_id
    max_depth = int(args.get("max_depth") or 8)
    items = await node_service.build_tree(ctx.db, project_id)
    lines = _tree_lines(items, 0, max_depth)
    body = "\n".join(lines) if lines else "(workspace is empty - create a project first)"
    return ToolResult(True, f"Workspace tree:\n{body}", {"count": len(lines)})


async def _read_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    node = await _resolve_node(ctx, args.get("node_id"))
    if node.kind != "file" or not node.storage_key:
        raise ToolError("Node is not a readable file")
    data = await storage.read_bytes(node.storage_key)
    try:
        parsed = await anyio.to_thread.run_sync(parse_document, node.name, data)
    except Exception as exc:  # noqa: BLE001 - any parser failure becomes a tool error
        raise ToolError(f"Cannot read {node.name}: {exc}") from exc

    sheet = str(args.get("sheet") or "").strip()
    segments = parsed.segments
    if sheet:
        matching = [s for s in segments if s.label.lower().endswith(sheet.lower())]
        if not matching:
            available = ", ".join(s.label for s in segments) or "none"
            raise ToolError(f"Sheet '{sheet}' not found. Available: {available}")
        segments = matching
    text = "\n\n".join(f"[{segment.label}]\n{segment.text}" for segment in segments) or parsed.text
    limit = int(args.get("max_chars") or 40000)
    truncated = len(text) > limit
    if truncated:
        text = text[:limit] + "\n…[truncated]"
    path = await node_service.node_path(ctx.db, node)
    summary = f"File: {path} ({node.size} bytes)\n{text}"
    return ToolResult(
        True,
        summary,
        {
            "node_id": str(node.id),
            "path": path,
            "chars": len(text),
            "truncated": truncated,
        },
    )


async def _search_documents(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from app.services.rag import retrieve

    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("query is required")
    project_id = args.get("project_id") or ctx.project_id
    result = await retrieve(
        ctx.db, query, project_id=project_id, top_k=int(args.get("top_k") or 6)
    )
    if result.error:
        return ToolResult(False, f"Search unavailable: {result.error}")
    if not result.citations:
        return ToolResult(True, "No matching fragments found in the indexed documents.", {"citations": []})
    blocks = [
        f"[{citation.index}] {citation.path} ({citation.label}) score={citation.score:.3f}\n{citation.text}"
        for citation in result.citations
    ]
    return ToolResult(
        True,
        "Relevant fragments:\n\n" + "\n\n".join(blocks),
        {"citations": [citation.to_dict() for citation in result.citations]},
    )


# ------------------------------------------------------------ write tools
def _force_reindex(node: Node) -> None:
    node.index_status = "pending"
    node.chunk_count = 0
    node.indexed_at = None


async def _create_project(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    name = str(args.get("name") or "").strip()
    if not name:
        raise ToolError("name is required")
    node = await node_service.create_project(ctx.db, name, created_by="ai")
    operation_id = await _record_operation(
        ctx,
        tool_name="create_project",
        description=f"Create project '{node.name}'",
        node_id=node.id,
        undo={"action": "delete_node", "node_id": str(node.id)},
    )
    ctx.changed_nodes.append({"id": str(node.id), "event": "created"})
    return ToolResult(
        True,
        f"Created project '{node.name}' (id={node.id}). Use this id as parent_id.",
        {"node_id": str(node.id), "name": node.name},
        operation_id,
    )


async def _create_folder(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    parent = await _resolve_node(ctx, args.get("parent_id"))
    name = str(args.get("name") or "").strip()
    if not name:
        raise ToolError("name is required")
    node = await node_service.create_folder(ctx.db, parent.id, name, created_by="ai")
    operation_id = await _record_operation(
        ctx,
        tool_name="create_folder",
        description=f"Create folder '{node.name}'",
        node_id=node.id,
        undo={"action": "delete_node", "node_id": str(node.id)},
    )
    ctx.changed_nodes.append({"id": str(node.id), "event": "created"})
    path = await node_service.node_path(ctx.db, node)
    return ToolResult(
        True,
        f"Created folder {path} (id={node.id}).",
        {"node_id": str(node.id), "path": path},
        operation_id,
    )


async def _write_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    parent = await _resolve_node(ctx, args.get("parent_id"))
    raw_name = str(args.get("name") or "").strip()
    if not raw_name:
        raise ToolError("name is required")
    if args.get("content") is None:
        raise ToolError("content is required")
    name = node_service.sanitize_name(raw_name)
    content = str(args.get("content"))
    ext = node_service.extension_of(name)
    if ext in (".xlsx", ".xlsm", ".docx", ".pdf", ".csv"):
        payload = await anyio.to_thread.run_sync(build_text_document, name, content)
    else:
        payload = content.encode("utf-8")

    existing = await node_service.find_child(ctx.db, parent.id, name)
    undo: dict[str, Any] | None = None
    if existing is not None:
        if existing.kind != "file":
            raise ToolError("A folder with this name already exists")
        undo = _version_undo(existing)

    node, replaced = await node_service.create_file(
        ctx.db, parent.id, name, payload, created_by="ai", note="written by AI"
    )
    if not replaced:
        undo = {"action": "delete_node", "node_id": str(node.id)}
    operation_id = await _record_operation(
        ctx,
        tool_name="write_file",
        description=f"{'Updated' if replaced else 'Created'} {name} ({len(payload)} bytes)",
        node_id=node.id,
        undo=undo,
    )
    enqueue_index([node.id])
    ctx.changed_nodes.append({"id": str(node.id), "event": "updated" if replaced else "created"})
    path = await node_service.node_path(ctx.db, node)
    return ToolResult(
        True,
        f"{'Updated' if replaced else 'Created'} {path} ({len(payload)} bytes, id={node.id}).",
        {"node_id": str(node.id), "path": path, "bytes": len(payload), "replaced": replaced},
        operation_id,
    )


async def _rename_node(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    node = await _resolve_node(ctx, args.get("node_id"))
    new_name = str(args.get("new_name") or "").strip()
    if not new_name:
        raise ToolError("new_name is required")
    old_name = node.name
    updated = await node_service.rename_node(ctx.db, node, new_name)
    operation_id = await _record_operation(
        ctx,
        tool_name="rename_node",
        description=f"Rename '{old_name}' to '{updated.name}'",
        node_id=node.id,
        undo={"action": "rename", "node_id": str(node.id), "old_name": old_name},
    )
    if updated.kind == "file":
        _force_reindex(updated)
        enqueue_index([updated.id])
    ctx.changed_nodes.append({"id": str(updated.id), "event": "updated"})
    return ToolResult(
        True,
        f"Renamed '{old_name}' to '{updated.name}'.",
        {"node_id": str(updated.id), "name": updated.name},
        operation_id,
    )


async def _move_node(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    node = await _resolve_node(ctx, args.get("node_id"))
    old_parent_id = str(node.parent_id) if node.parent_id else None
    updated = await node_service.move_node(ctx.db, node, str(args.get("new_parent_id") or ""))
    operation_id = await _record_operation(
        ctx,
        tool_name="move_node",
        description=f"Move '{updated.name}'",
        node_id=updated.id,
        undo={"action": "move", "node_id": str(updated.id), "old_parent_id": old_parent_id},
    )
    path = await node_service.node_path(ctx.db, updated)
    ctx.changed_nodes.append({"id": str(updated.id), "event": "updated"})
    return ToolResult(True, f"Moved to {path}.", {"node_id": str(updated.id), "path": path}, operation_id)


async def _delete_node(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    node = await _resolve_node(ctx, args.get("node_id"))
    path = await node_service.node_path(ctx.db, node)
    snapshot = await node_service.delete_node(ctx.db, node)
    operation_id = await _record_operation(
        ctx,
        tool_name="delete_node",
        description=f"Delete {path}",
        node_id=uuid.UUID(snapshot["root_id"]),
        undo=snapshot,
    )
    file_ids = [item["id"] for item in snapshot.get("nodes", []) if item.get("kind") == "file"]
    for file_id in file_ids:
        try:
            await vector_store.delete_node(file_id)
        except Exception:  # noqa: BLE001 - index cleanup is best effort
            logger.debug("Could not delete vectors for %s", file_id, exc_info=True)
    ctx.changed_nodes.append({"id": snapshot["root_id"], "event": "deleted"})
    return ToolResult(True, f"Deleted {path}.", {"node_id": snapshot["root_id"]}, operation_id)


# ------------------------------------------------------------ excel tools
def _excel_write_sync(
    data: bytes, sheet: str, start_cell: str, rows: list[list[Any]]
) -> tuple[bytes, int, list[str]]:
    from openpyxl import load_workbook
    from openpyxl.utils import coordinate_to_tuple

    workbook = load_workbook(io.BytesIO(data))
    if sheet in workbook.sheetnames:
        worksheet = workbook[sheet]
    else:
        worksheet = workbook.create_sheet(title=(sheet or "Sheet")[:31])
    start_row, start_col = coordinate_to_tuple(start_cell or "A1")
    written = 0
    for row_offset, row in enumerate(rows or []):
        values = row if isinstance(row, (list, tuple)) else [row]
        for col_offset, value in enumerate(values):
            worksheet.cell(
                row=start_row + row_offset, column=start_col + col_offset, value=value
            )
            written += 1
    buffer = io.BytesIO()
    workbook.save(buffer)
    sheet_names = list(workbook.sheetnames)
    workbook.close()
    return buffer.getvalue(), written, sheet_names


def _excel_read_sync(data: bytes, sheet: str, max_rows: int) -> tuple[str, list[str]]:
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter

    workbook = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    names = list(workbook.sheetnames)
    target = sheet if sheet in names else (names[0] if names else "Sheet1")
    worksheet = workbook[target]
    lines: list[str] = []
    for row_index, row in enumerate(worksheet.iter_rows(values_only=True), start=1):
        if row_index > max_rows:
            lines.append(f"… truncated after {max_rows} rows")
            break
        cells = [
            f"{get_column_letter(col_index + 1)}{row_index}={value}"
            for col_index, value in enumerate(row)
            if value not in (None, "")
        ]
        if cells:
            lines.append(" | ".join(cells))
    workbook.close()
    return "\n".join(lines), names


async def _excel_read(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    node = await _resolve_node(ctx, args.get("node_id"))
    if node.kind != "file" or not node.storage_key:
        raise ToolError("Node is not a readable file")
    data = await storage.read_bytes(node.storage_key)
    ext = node_service.extension_of(node.name)
    max_rows = int(args.get("max_rows") or 200)
    sheet = str(args.get("sheet") or "")
    if ext in (".xlsx", ".xlsm"):
        text, sheets = await anyio.to_thread.run_sync(_excel_read_sync, data, sheet, max_rows)
        used = sheet if sheet in sheets else (sheets[0] if sheets else "Sheet1")
        summary = f"Sheet '{used}' of {node.name} (sheets: {', '.join(sheets)}):\n{text}"
    else:
        parsed = await anyio.to_thread.run_sync(parse_document, node.name, data)
        text = parsed.text[:40000]
        sheets = [segment.label for segment in parsed.segments]
        summary = f"{node.name} ({', '.join(sheets)}):\n{text}"
    path = await node_service.node_path(ctx.db, node)
    return ToolResult(
        True,
        summary,
        {"node_id": str(node.id), "path": path, "sheets": sheets},
    )


async def _excel_write(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    node = await _resolve_node(ctx, args.get("node_id"))
    if node.kind != "file" or not node.storage_key:
        raise ToolError("excel_write requires an existing spreadsheet file")
    if node_service.extension_of(node.name) not in (".xlsx", ".xlsm"):
        raise ToolError("excel_write supports .xlsx/.xlsm only (use write_file to create a new one)")
    rows = args.get("rows") or []
    if not isinstance(rows, list) or any(not isinstance(row, (list, tuple)) for row in rows):
        raise ToolError("rows must be a list of lists")
    sheet = str(args.get("sheet") or "Sheet1")
    start_cell = str(args.get("start_cell") or "A1")
    data = await storage.read_bytes(node.storage_key)
    undo = _version_undo(node)
    payload, written, sheets = await anyio.to_thread.run_sync(
        _excel_write_sync, data, sheet, start_cell, rows
    )
    await node_service.write_file(
        ctx.db, node, payload, created_by="ai", note=f"excel_write {sheet}!{start_cell}"
    )
    operation_id = await _record_operation(
        ctx,
        tool_name="excel_write",
        description=f"Write {written} cell(s) to {node.name} [{sheet}]",
        node_id=node.id,
        undo=undo,
    )
    enqueue_index([node.id])
    ctx.changed_nodes.append({"id": str(node.id), "event": "updated"})
    return ToolResult(
        True,
        f"Wrote {written} cell(s) into '{sheet}' starting at {start_cell} of {node.name}. "
        f"Sheets now: {', '.join(sheets)}.",
        {"node_id": str(node.id), "sheet": sheet, "cells": written, "sheets": sheets},
        operation_id,
    )


async def _excel_add_sheet(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    sheet_name = str(args.get("sheet_name") or "").strip()
    if not sheet_name:
        raise ToolError("sheet_name is required")
    return await _excel_write(
        ctx,
        {
            "node_id": args.get("node_id"),
            "sheet": sheet_name,
            "start_cell": "A1",
            "rows": args.get("rows") or [],
        },
    )


# ------------------------------------------------------- translate / pdf
async def _translate_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    node = await _resolve_node(ctx, args.get("node_id"))
    if node.kind != "file" or not node.storage_key:
        raise ToolError("translate_file requires an existing file")
    language = str(args.get("target_language") or "").strip()
    if not language:
        raise ToolError("target_language is required")
    data = await storage.read_bytes(node.storage_key)
    try:
        parsed = await anyio.to_thread.run_sync(parse_document, node.name, data)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"Cannot extract text from {node.name}: {exc}") from exc
    source = parsed.text.strip()
    if not source:
        raise ToolError("This file contains no extractable text to translate")

    resolved = await registry.resolve(ctx.db)
    messages = [
        ChatMessage(
            role="system",
            content=(
                "You are a professional translator. Translate the document faithfully, "
                "preserving structure, markdown, tables, numbers and proper names. "
                "Return only the translated text without commentary."
            ),
        ),
        ChatMessage(
            role="user",
            content=f"Translate the following document into {language}:\n\n{source[:14000]}",
        ),
    ]
    parts: list[str] = []
    async for event in resolved.provider.chat_stream(
        messages, model=resolved.model, temperature=0.1
    ):
        if isinstance(event, TextDelta):
            parts.append(event.text)
    translated = "".join(parts).strip()
    if not translated:
        raise ToolError("The model returned an empty translation")

    stem = Path(node.name).stem
    ext = Path(node.name).suffix.lower()
    if ext not in (".txt", ".md", ".markdown", ".docx", ".pdf", ".csv"):
        ext = ".md"
    slug = re.sub(r"[^a-z0-9]+", "-", language.lower()).strip("-") or "translation"
    target_name = str(args.get("output_name") or f"{stem}.{slug}{ext}")
    payload = await anyio.to_thread.run_sync(build_text_document, target_name, translated)
    parent_id = node.parent_id
    if parent_id is None:
        raise ToolError("Cannot determine the parent folder of this file")
    created, _replaced = await node_service.create_file(
        ctx.db,
        parent_id,
        target_name,
        payload,
        created_by="ai",
        note=f"translation to {language}",
    )
    operation_id = await _record_operation(
        ctx,
        tool_name="translate_file",
        description=f"Translate {node.name} to {language} as {created.name}",
        node_id=created.id,
        undo={"action": "delete_node", "node_id": str(created.id)},
    )
    enqueue_index([created.id])
    ctx.changed_nodes.append({"id": str(created.id), "event": "created"})
    path = await node_service.node_path(ctx.db, created)
    return ToolResult(
        True,
        f"Translated {node.name} into {language} using {resolved.provider_name}/{resolved.model}. "
        f"Saved as {path} (id={created.id}).",
        {
            "node_id": str(created.id),
            "path": path,
            "provider": resolved.provider_name,
            "model": resolved.model,
            "chars": len(translated),
        },
        operation_id,
    )


async def _create_pdf(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    name = str(args.get("name") or "document.pdf").strip()
    if not name.lower().endswith(".pdf"):
        name = f"{name}.pdf"
    payload = {
        "parent_id": args.get("parent_id"),
        "name": name,
        "content": args.get("content") or "",
    }
    return await _write_file(ctx, payload)


# ------------------------------------------------------------- dispatcher
_HANDLERS = {
    "list_workspace": _list_workspace,
    "read_file": _read_file,
    "write_file": _write_file,
    "create_folder": _create_folder,
    "create_project": _create_project,
    "rename_node": _rename_node,
    "move_node": _move_node,
    "delete_node": _delete_node,
    "excel_read": _excel_read,
    "excel_write": _excel_write,
    "excel_add_sheet": _excel_add_sheet,
    "search_documents": _search_documents,
    "translate_file": _translate_file,
    "create_pdf": _create_pdf,
}


async def execute_tool(
    ctx: ToolContext, name: str, arguments: dict[str, Any] | None = None
) -> ToolResult:
    handler = _HANDLERS.get(name)
    if handler is None:
        return ToolResult(
            False,
            f"Unknown tool '{name}'. Available tools: {', '.join(sorted(TOOL_NAMES))}",
        )
    try:
        return await handler(ctx, arguments or {})
    except ToolError as exc:
        return ToolResult(False, f"{name} failed: {exc}")
    except node_service.NodeError as exc:
        return ToolResult(False, f"{name} failed: {exc.message}")
    except Exception as exc:  # noqa: BLE001 - never crash the agent loop
        logger.exception("Tool %s crashed", name)
        return ToolResult(False, f"{name} failed: {type(exc).__name__}: {exc}")






