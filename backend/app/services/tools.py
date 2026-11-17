"""AI tools: the only way the model can touch the workspace.

Every mutating tool records a reversible Operation so the UI can undo it.
"""

from __future__ import annotations

import io
import json
import re
import uuid
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.logging_config import get_logger
from app.llm.factory import registry
from app.llm.schemas import ChatMessage, ProviderError, TextDelta, ToolSpec
from app.models import Node, Operation
from app.services import nodes as node_service
from app.services import office_convert
from app.services import templates as template_service
from app.services.documents import build_pdf_parts, build_text_document, pdf_page_count
from app.services.indexer import enqueue_index
from app.services.parsing import parse_document
from app.services.storage import decode_bytes, storage
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
    owner_id: str | None = None
    user: Any = None
    is_admin: bool = False
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
        name="find_nodes",
        description=(
            "Search files and folders of the current project by name (case-insensitive "
            "substring). Returns ids and paths - use it instead of guessing node ids."
        ),
        parameters=_obj(
            {
                "query": {"type": "string", "description": "Part of the file or folder name"},
                "limit": {"type": "integer", "description": "Max results, default 30"},
            },
            ["query"],
        ),
    ),
    ToolSpec(
        name="read_file",
        description=(
            "Read the extracted text of a file (Excel cells, Word/PDF text, OCR text, code). "
            "Use offset to page through long documents."
        ),
        parameters=_obj(
            {
                "node_id": {"type": "string", "description": "File node id"},
                "max_chars": {"type": "integer", "description": "Truncate output, default 40000"},
                "sheet": {"type": "string", "description": "For .xlsx: read only this sheet"},
                "offset": {
                    "type": "integer",
                    "description": "Start character (use next_offset of the previous read)",
                },
            },
            ["node_id"],
        ),
    ),
    ToolSpec(
        name="write_file",
        description=(
            "Create or overwrite a text/Markdown/CSV/JSON/Excel/DOCX/PDF file. "
            "For .xlsx, .docx and .pdf the content is rendered into the real format: "
            "Markdown tables, **bold**, *italic*, `code` and fenced code blocks are "
            "supported; a JSON spec ({blocks:[...]} for DOCX, {sheets:[...]} for XLSX) "
            "gives precise tables, styles and cell formats."
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
        name="edit_file",
        description=(
            "Replace an exact text fragment inside an existing text file (txt, md, csv, json, "
            "yaml, log, code). Prefer this over write_file for small changes - the rest of the "
            "file stays byte-identical. Fails when old_text is not found."
        ),
        parameters=_obj(
            {
                "node_id": _STR,
                "old_text": {"type": "string", "description": "Exact fragment to replace"},
                "new_text": {"type": "string", "description": "Replacement (may be empty)"},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence, default false (first only)",
                },
            },
            ["node_id", "old_text", "new_text"],
        ),
    ),
    ToolSpec(
        name="append_file",
        description="Append text to the end of an existing text file (adds a newline when needed).",
        parameters=_obj(
            {
                "node_id": _STR,
                "content": {"type": "string", "description": "Text to append"},
            },
            ["node_id", "content"],
        ),
    ),
    ToolSpec(
        name="create_folder",
        description="Create a folder inside a project or another folder.",
        parameters=_obj({"parent_id": _STR, "name": _STR}, ["parent_id", "name"]),
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
        description=(
            "Generate a NEW PDF document from Markdown-ish text or a JSON block spec. "
            "The PDF is rendered through the OnlyOffice converter, so tables, bold text, "
            "code blocks and images keep their layout (falls back to reportlab)."
        ),
        parameters=_obj(
            {"parent_id": _STR, "name": _STR, "content": _STR},
            ["parent_id", "name", "content"],
        ),
    ),
    ToolSpec(
        name="convert_to_pdf",
        description=(
            "Convert an existing DOCX/XLSX/PPTX/ODT/ODS/ODP/CSV/RTF file to PDF "
            "with real layout using the OnlyOffice converter. Pass name to choose "
            "the output file name."
        ),
        parameters=_obj(
            {
                "node_id": _STR,
                "name": {"type": "string", "description": "Optional output PDF name"},
            },
            ["node_id"],
        ),
    ),
    ToolSpec(
        name="create_template_from_file",
        description=(
            "Save a DOCX or XLSX project file as a reusable PERSONAL template. "
            "Use this when the user says 'сделай из этого файла шаблон'. First read the file, "
            "then pass replacements that turn concrete values into placeholders, for example "
            "[{find:'ООО Ромашка', field:'tenant'}, {find:'123', field:'contract_number'}]. "
            "Existing {{ field }} placeholders are detected automatically. PDF is not a "
            "template format: use the Word/Excel source and convert the result to PDF later."
        ),
        parameters=_obj(
            {
                "node_id": _STR,
                "name": {"type": "string", "description": "Template name (without extension)"},
                "description": _STR,
                "replacements": {
                    "type": "array",
                    "description": "Concrete values to replace with {{ field }} placeholders",
                    "items": _obj(
                        {
                            "find": _STR,
                            "field": _STR,
                        },
                        ["find", "field"],
                    ),
                },
                "fields": {
                    "type": "array",
                    "description": "Optional explicit field definitions",
                    "items": _obj(
                        {
                            "name": _STR,
                            "label": _STR,
                            "type": {"type": "string"},
                            "required": {"type": "boolean"},
                        },
                        ["name"],
                    ),
                },
            },
            ["node_id"],
        ),
    ),
    ToolSpec(
        name="fill_template",
        description=(
            "Create a new DOCX/XLSX file from a saved template and a data object. "
            "Use list of available templates from the system context (never invent ids). "
            "For DOCX, Jinja loops/conditions and {{ field }} placeholders are supported. "
            "For XLSX, placeholders are rendered inside cells while styles and formulas stay. "
            "Template output is DOCX/XLSX; call convert_to_pdf if the user needs PDF."
        ),
        parameters=_obj(
            {
                "template_id": _STR,
                "data": {
                    "type": "object",
                    "description": "Field values; arrays are used by DOCX loops",
                },
                "parent_id": {
                    "type": "string",
                    "description": "Destination folder/project (defaults to the current project)",
                },
                "name": {
                    "type": "string",
                    "description": "Output file name including .docx or .xlsx",
                },
            },
            ["template_id", "data"],
        ),
    ),
    ToolSpec(
        name="pdf_split",
        description=(
            "Split an existing PDF into new PDF files by copying page ranges. Pages are "
            "copied as-is, so images, scans, stamps and the original layout are preserved. "
            "Use this (never create_pdf) to split or extract pages of an existing PDF. "
            "parts = [{name, pages}] where pages looks like '1-3,5' or '4'."
        ),
        parameters=_obj(
            {
                "node_id": _STR,
                "parts": {
                    "type": "array",
                    "items": _obj({"name": _STR, "pages": _STR}, ["name", "pages"]),
                },
                "parent_id": {
                    "type": "string",
                    "description": "Optional destination folder (defaults to the PDF's folder)",
                },
            },
            ["node_id", "parts"],
        ),
    ),
]

TOOL_NAMES = {spec.name for spec in TOOL_SPECS}


def tool_specs() -> list[ToolSpec]:
    """Every tool the agent may call, in the order they are advertised."""
    return list(TOOL_SPECS)


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
    """Fetch a node and refuse anything outside the chat's project."""
    if not node_id:
        raise ToolError("node_id is required")
    try:
        node = await node_service.get_node(ctx.db, str(node_id))
    except node_service.NodeError as exc:
        raise ToolError(exc.message) from exc
    if ctx.project_id:
        same_project = str(node.project_id or node.id) == str(ctx.project_id)
        if not same_project:
            raise ToolError(
                "That node belongs to a different project. The chat can only touch "
                "files of the project it was opened in."
            )
    return node


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
    """List the current project only - the model cannot widen this scope."""
    max_depth = int(args.get("max_depth") or 8)
    items = await node_service.build_tree(ctx.db, ctx.project_id)
    lines = _tree_lines(items, 0, max_depth)
    body = "\n".join(lines) if lines else "(this project is empty - create files first)"
    return ToolResult(True, f"Project tree:\n{body}", {"count": len(lines)})


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
    limit = max(1000, int(args.get("max_chars") or 40000))
    offset = max(0, int(args.get("offset") or 0))
    total = len(text)
    window = text[offset : offset + limit]
    next_offset = offset + len(window) if offset + len(window) < total else None
    truncated = next_offset is not None
    if truncated:
        window += "\n…[truncated]"
    path = await node_service.node_path(ctx.db, node)
    pages = parsed.meta.get("pages") if isinstance(parsed.meta, dict) else None
    header = f"File: {path} ({node.size} bytes"
    if pages:
        header += f", {pages} pages"
    if offset:
        header += f", chars {offset}-{offset + len(window)} of {total}"
    header += ")\n"
    summary = f"{header}{window}"
    info: dict[str, Any] = {
        "node_id": str(node.id),
        "path": path,
        "chars": len(window),
        "total_chars": total,
        "offset": offset,
        "next_offset": next_offset,
        "truncated": truncated,
    }
    if pages:
        info["pages"] = pages
    return ToolResult(True, summary, info)


async def _find_nodes(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Search the current project by name - the model never has to guess ids."""
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("query is required")
    if not ctx.project_id:
        raise ToolError("This chat has no project scope")
    limit = max(1, min(int(args.get("limit") or 30), 100))
    pattern = f"%{query.lower()}%"
    from sqlalchemy import func, select

    result = await ctx.db.execute(
        select(Node)
        .where(
            Node.project_id == uuid.UUID(str(ctx.project_id)),
            func.lower(Node.name).like(pattern),
        )
        .order_by(Node.kind, Node.name)
        .limit(limit)
    )
    nodes = list(result.scalars().all())
    if not nodes:
        return ToolResult(True, f"No files or folders matching '{query}'.", {"matches": []})
    lines: list[str] = []
    matches: list[dict[str, Any]] = []
    for node in nodes:
        path = await node_service.node_path(ctx.db, node)
        lines.append(f"{node.kind}: {path} (id={node.id})")
        matches.append(
            {"id": str(node.id), "name": node.name, "kind": node.kind, "path": path}
        )
    return ToolResult(
        True,
        f"Matches for '{query}':\n" + "\n".join(lines),
        {"matches": matches},
    )


async def _search_documents(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from app.services.rag import retrieve

    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("query is required")
    if not ctx.project_id:
        raise ToolError("This chat has no project scope")
    result = await retrieve(
        ctx.db, query, project_id=str(ctx.project_id), top_k=int(args.get("top_k") or 6)
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


async def _create_template_from_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if ctx.user is None:
        raise ToolError("Authentication is required to manage templates")
    node = await _resolve_node(ctx, args.get("node_id"))
    replacements = args.get("replacements") or []
    if not isinstance(replacements, list):
        raise ToolError("replacements must be a list")
    template = await template_service.create_from_node(
        ctx.db,
        ctx.user,
        node,
        name=str(args.get("name") or "").strip() or None,
        description=str(args.get("description") or ""),
        replacements=replacements,
        fields=args.get("fields"),
    )
    field_names = [str(item.get("name")) for item in (template.fields or [])]
    detail = f"Template '{template.name}' ({template.kind}) created from {node.name}"
    if field_names:
        detail += f"; placeholders: {', '.join(field_names)}."
    else:
        detail += (
            ". No placeholders were found: recreate it with replacements so the template "
            "can actually be filled."
        )
    return ToolResult(
        True,
        detail,
        {
            "template_id": str(template.id),
            "name": template.name,
            "kind": template.kind,
            "scope": template.scope,
            "fields": template.fields,
        },
    )


async def _fill_template(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if ctx.user is None:
        raise ToolError("Authentication is required to use templates")
    template_id = args.get("template_id") or args.get("template_node_id")
    if not template_id:
        raise ToolError("template_id is required")
    template = await template_service.get_template(ctx.db, ctx.user, str(template_id))
    parent_id = args.get("parent_id") or ctx.project_id
    if not parent_id:
        raise ToolError("parent_id is required when the chat has no project")
    parent = await _resolve_node(ctx, parent_id)
    if parent.kind == "file":
        raise ToolError("parent_id must be a folder or project")
    data = args.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ToolError(f"data must be a JSON object: {exc}") from exc
    if data is None:
        data = {}

    output_name = str(args.get("name") or f"{template.name}{template.extension}").strip()
    if not output_name.lower().endswith(template.extension):
        raise ToolError(
            f"This template produces a {template.kind.upper()} file; the output name must "
            f"end with {template.extension}. Convert the result to PDF separately if needed."
        )
    output_name = node_service.sanitize_name(output_name)
    rendered, verification = await template_service.render_template(template, data)

    existing = await node_service.find_child(ctx.db, parent.id, output_name)
    undo: dict[str, Any] | None = None
    if existing is not None:
        if existing.kind != "file":
            raise ToolError("A folder with this name already exists")
        undo = _version_undo(existing)

    node, replaced = await node_service.create_file(
        ctx.db,
        parent.id,
        output_name,
        rendered,
        created_by="ai",
        note=f"filled from template {template.name}",
    )
    if not replaced:
        undo = {"action": "delete_node", "node_id": str(node.id)}
    operation_id = await _record_operation(
        ctx,
        tool_name="fill_template",
        description=f"Filled template '{template.name}' -> {node.name}",
        node_id=node.id,
        undo=undo,
    )
    enqueue_index([node.id])
    ctx.changed_nodes.append({"id": str(node.id), "event": "updated" if replaced else "created"})
    path = await node_service.node_path(ctx.db, node)
    note = ""
    if verification.get("paragraphs") is not None:
        note = (
            f" {verification.get('paragraphs', 0)} paragraph(s), "
            f"{verification.get('tables', 0)} table(s)."
        )
    elif verification.get("sheets"):
        note = f" Sheets: {', '.join(verification['sheets'])}."
    return ToolResult(
        True,
        f"Created {path} from template '{template.name}' ({len(rendered)} bytes).{note}",
        {
            "node_id": str(node.id),
            "path": path,
            "template_id": str(template.id),
            "bytes": len(rendered),
            "replaced": replaced,
            "verification": verification,
        },
        operation_id,
    )


# ------------------------------------------------------------ verification
def _verify_bytes(name: str, data: bytes) -> dict[str, Any]:
    """Read the written bytes back and report what the file really contains."""
    ext = node_service.extension_of(name)
    info: dict[str, Any] = {"bytes": len(data), "ok": bool(data)}
    if not data:
        info["problem"] = "the file is empty"
        return info
    try:
        if ext in (".xlsx", ".xlsm"):
            from openpyxl import load_workbook

            workbook = load_workbook(io.BytesIO(data), read_only=True)
            try:
                info["sheets"] = list(workbook.sheetnames)
                info["rows"] = sum(int(sheet.max_row or 0) for sheet in workbook.worksheets)
            finally:
                workbook.close()
            info["ok"] = bool(info["sheets"]) and info["rows"] > 0
        elif ext == ".pdf":
            info["pages"] = pdf_page_count(data)
            info["ok"] = info["pages"] > 0
        elif ext == ".docx":
            import docx

            document = docx.Document(io.BytesIO(data))
            info["paragraphs"] = len(document.paragraphs)
            info["tables"] = len(document.tables)
            info["ok"] = bool(info["paragraphs"] or info["tables"])
        else:
            text = decode_bytes(data)
            info["lines"] = text.count("\n") + (1 if text else 0)
            info["chars"] = len(text)
            info["ok"] = bool(text.strip())
    except Exception as exc:  # noqa: BLE001 - verification must never crash a tool
        info["ok"] = False
        info["problem"] = f"cannot read the result back: {exc}"
        return info
    if not info["ok"]:
        info["problem"] = "the file looks empty"
    return info


def _verification_note(info: dict[str, Any]) -> str:
    if "sheets" in info:
        return f"Verified: {len(info['sheets'])} sheet(s), {info.get('rows', 0)} row(s)."
    if "pages" in info:
        return f"Verified: {info['pages']} page(s)."
    if "paragraphs" in info:
        return (
            f"Verified: {info.get('paragraphs', 0)} paragraph(s), "
            f"{info.get('tables', 0)} table(s)."
        )
    if "lines" in info:
        return f"Verified: {info['lines']} line(s), {info.get('chars', 0)} char(s)."
    return f"Verified: {info.get('bytes', 0)} byte(s)."


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
    verification = _verify_bytes(name, payload)
    summary = f"{'Updated' if replaced else 'Created'} {path} ({len(payload)} bytes, id={node.id})."
    if verification.get("ok"):
        summary = f"{summary} {_verification_note(verification)}"
    else:
        summary = f"{summary} Verification failed: {verification.get('problem')}"
    return ToolResult(
        bool(verification.get("ok")),
        summary,
        {
            "node_id": str(node.id),
            "path": path,
            "bytes": len(payload),
            "replaced": replaced,
            "verification": verification,
        },
        operation_id,
    )


async def _edit_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Replace an exact fragment - keeps the rest of the file byte-identical."""
    node = await _resolve_node(ctx, args.get("node_id"))
    if not node_service.is_text_editable(node):
        raise ToolError(
            "edit_file works with text files only; use write_file or excel_write for Office formats"
        )
    old_text = str(args.get("old_text") or "")
    if not old_text:
        raise ToolError("old_text is required")
    new_text = "" if args.get("new_text") is None else str(args.get("new_text"))
    replace_all = bool(args.get("replace_all"))
    data = await storage.read_bytes(node.storage_key)
    source = decode_bytes(data)
    occurrences = source.count(old_text)
    if not occurrences:
        raise ToolError(
            "old_text was not found in the file. Read the file again and copy the exact fragment."
        )
    undo = _version_undo(node)
    updated = source.replace(old_text, new_text) if replace_all else source.replace(old_text, new_text, 1)
    payload = updated.encode("utf-8")
    await node_service.write_file(ctx.db, node, payload, created_by="ai", note="edited by AI")
    replaced = occurrences if replace_all else 1
    operation_id = await _record_operation(
        ctx,
        tool_name="edit_file",
        description=f"Replaced {replaced} fragment(s) in {node.name}",
        node_id=node.id,
        undo=undo,
    )
    enqueue_index([node.id])
    ctx.changed_nodes.append({"id": str(node.id), "event": "updated"})
    verification = _verify_bytes(node.name, payload)
    summary = f"Replaced {replaced} occurrence(s) in {node.name} ({len(payload)} bytes)."
    if verification.get("ok"):
        summary = f"{summary} {_verification_note(verification)}"
    else:
        summary = f"{summary} Verification failed: {verification.get('problem')}"
    return ToolResult(
        bool(verification.get("ok")),
        summary,
        {
            "node_id": str(node.id),
            "replaced": replaced,
            "bytes": len(payload),
            "verification": verification,
        },
        operation_id,
    )


async def _append_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Append text to an existing text file without rewriting the whole body."""
    node = await _resolve_node(ctx, args.get("node_id"))
    if not node_service.is_text_editable(node):
        raise ToolError("append_file works with text files only")
    content = "" if args.get("content") is None else str(args.get("content"))
    if not content:
        raise ToolError("content is required")
    data = await storage.read_bytes(node.storage_key)
    source = decode_bytes(data)
    separator = "" if not source or source.endswith("\n") else "\n"
    payload = f"{source}{separator}{content}".encode("utf-8")
    undo = _version_undo(node)
    await node_service.write_file(ctx.db, node, payload, created_by="ai", note="appended by AI")
    operation_id = await _record_operation(
        ctx,
        tool_name="append_file",
        description=f"Appended {len(content)} char(s) to {node.name}",
        node_id=node.id,
        undo=undo,
    )
    enqueue_index([node.id])
    ctx.changed_nodes.append({"id": str(node.id), "event": "updated"})
    verification = _verify_bytes(node.name, payload)
    summary = f"Appended {len(content)} char(s) to {node.name} ({len(payload)} bytes)."
    if verification.get("ok"):
        summary = f"{summary} {_verification_note(verification)}"
    else:
        summary = f"{summary} Verification failed: {verification.get('problem')}"
    return ToolResult(
        bool(verification.get("ok")),
        summary,
        {"node_id": str(node.id), "bytes": len(payload), "verification": verification},
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
TRANSLATE_CHUNK_CHARS = 12_000


def _translate_windows(text: str, size: int = TRANSLATE_CHUNK_CHARS) -> list[str]:
    """Split text into paragraph-aligned windows so long files are fully translated."""
    if len(text) <= size:
        return [text]
    windows: list[str] = []
    buffer: list[str] = []
    length = 0
    for paragraph in text.split("\n\n"):
        block = paragraph
        while len(block) > size:
            # One huge paragraph (log dump, single-line CSV): hard split.
            windows.append(block[:size])
            block = block[size:]
        if length and length + len(block) + 2 > size:
            windows.append("\n\n".join(buffer))
            buffer = []
            length = 0
        if block:
            buffer.append(block)
            length += len(block) + 2
    if buffer:
        windows.append("\n\n".join(buffer))
    return [window for window in windows if window.strip()] or [text]


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

    windows = _translate_windows(source)
    resolved = await registry.resolve(ctx.db, ctx.user)
    translated_parts: list[str] = []
    for index, window in enumerate(windows, start=1):
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
                content=(
                    f"Translate part {index}/{len(windows)} of the document into {language}:"
                    f"\n\n{window}"
                ),
            ),
        ]
        pieces: list[str] = []
        try:
            async for event in resolved.provider.chat_stream(
                messages, model=resolved.model, temperature=0.1
            ):
                if isinstance(event, TextDelta):
                    pieces.append(event.text)
        except ProviderError as exc:
            raise ToolError(
                f"Translation failed on part {index}/{len(windows)}: {exc.message}"
            ) from exc
        part = "".join(pieces).strip()
        if not part:
            raise ToolError(
                f"The model returned an empty translation for part {index}/{len(windows)}"
            )
        translated_parts.append(part)
    translated = "\n\n".join(translated_parts).strip()
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
        f"Translated {node.name} into {language} using {resolved.provider_name}/{resolved.model} "
        f"({len(source)} char(s) in {len(windows)} part(s) → {len(translated)} char(s)). "
        f"Saved as {path} (id={created.id}).",
        {
            "node_id": str(created.id),
            "path": path,
            "provider": resolved.provider_name,
            "model": resolved.model,
            "chars": len(translated),
            "source_chars": len(source),
            "parts": len(windows),
        },
        operation_id,
    )


_CONVERTIBLE_TO_PDF = {
    ".docx",
    ".xlsx",
    ".xlsm",
    ".pptx",
    ".odt",
    ".ods",
    ".odp",
    ".csv",
    ".rtf",
}


async def _convert_to_pdf(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    node = await _resolve_node(ctx, args.get("node_id"))
    if node.kind != "file" or not node.storage_key:
        raise ToolError("convert_to_pdf requires an existing file")
    extension = node_service.extension_of(node.name)
    if extension not in _CONVERTIBLE_TO_PDF:
        raise ToolError(f"Cannot convert {extension or 'this file'} to PDF")
    if not settings.office_conversion_available:
        raise ToolError("PDF conversion is not configured on this deployment")
    data = await storage.read_bytes(node.storage_key)
    try:
        pdf = await anyio.to_thread.run_sync(
            partial(
                office_convert.convert_document,
                data,
                extension,
                target="pdf",
                title=node.name,
            )
        )
    except office_convert.OfficeConversionError as exc:
        raise ToolError(f"OnlyOffice conversion failed: {exc}") from exc
    if len(pdf) > settings.max_upload_mb * 1024 * 1024:
        raise ToolError(
            f"The converted PDF exceeds the {settings.max_upload_mb} MB limit"
        )
    raw_name = str(args.get("name") or f"{node.name.rsplit('.', 1)[0]}.pdf")
    name = node_service.sanitize_name(raw_name)
    if not name.lower().endswith(".pdf"):
        name = f"{name}.pdf"
    if node.parent_id is None:
        raise ToolError("The source file has no parent folder")
    created, replaced = await node_service.create_file(
        ctx.db,
        node.parent_id,
        name,
        pdf,
        created_by="ai",
        note=f"converted from {node.name}",
    )
    undo = (
        _version_undo(created)
        if replaced
        else {"action": "delete_node", "node_id": str(created.id)}
    )
    operation_id = await _record_operation(
        ctx,
        tool_name="convert_to_pdf",
        description=f"Converted {node.name} to {name}",
        node_id=created.id,
        undo=undo,
    )
    enqueue_index([created.id])
    ctx.changed_nodes.append(
        {"id": str(created.id), "event": "updated" if replaced else "created"}
    )
    path = await node_service.node_path(ctx.db, created)
    return ToolResult(
        True,
        f"Converted {node.name} to {path} ({len(pdf)} bytes, id={created.id}).",
        {
            "node_id": str(created.id),
            "path": path,
            "bytes": len(pdf),
            "replaced": replaced,
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


def _parse_page_ranges(spec: str, total: int) -> list[int]:
    pages: list[int] = []
    seen: set[int] = set()
    for chunk in re.split(r"[,\s]+", (spec or "").strip()):
        if not chunk:
            continue
        if "-" in chunk:
            start_raw, _, end_raw = chunk.partition("-")
            try:
                start, end = int(start_raw), int(end_raw)
            except ValueError as exc:
                raise ToolError(f"Invalid page range '{chunk}'") from exc
        else:
            try:
                start = end = int(chunk)
            except ValueError as exc:
                raise ToolError(f"Invalid page number '{chunk}'") from exc
        if start > end:
            start, end = end, start
        if start < 1 or end > total:
            raise ToolError(f"Page range '{chunk}' is outside 1..{total}")
        for number in range(start, end + 1):
            if number not in seen:
                seen.add(number)
                pages.append(number)
    if not pages:
        raise ToolError("pages is required, e.g. '1-3,5'")
    return pages


async def _pdf_split(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    node = await _resolve_node(ctx, args.get("node_id"))
    if node.kind != "file" or not node.storage_key:
        raise ToolError("pdf_split requires an existing PDF file")
    if node_service.extension_of(node.name) != ".pdf":
        raise ToolError("pdf_split supports .pdf files only")
    data = await storage.read_bytes(node.storage_key)
    try:
        total_pages = await anyio.to_thread.run_sync(pdf_page_count, data)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"Cannot open {node.name}: {exc}") from exc

    raw_parts = args.get("parts") or []
    if not isinstance(raw_parts, list) or not raw_parts:
        raise ToolError("parts must be a non-empty list of {name, pages}")
    if len(raw_parts) > 20:
        raise ToolError("at most 20 parts per call")

    parent_id = args.get("parent_id") or node.parent_id
    if not parent_id:
        raise ToolError("Cannot determine the destination folder")
    parent = await _resolve_node(ctx, parent_id)
    if parent.kind not in ("project", "folder"):
        raise ToolError("parent_id must point to a project or folder")

    plans: list[tuple[str, list[int]]] = []
    names: set[str] = set()
    for index, raw in enumerate(raw_parts, start=1):
        if not isinstance(raw, dict):
            raise ToolError("Each part must be an object {name, pages}")
        name = node_service.sanitize_name(str(raw.get("name") or f"part-{index}.pdf"))
        if not name.lower().endswith(".pdf"):
            name = f"{name}.pdf"
        if name.lower() in names:
            raise ToolError(f"Duplicate part name: {name}")
        names.add(name.lower())
        existing = await node_service.find_child(ctx.db, parent.id, name)
        if existing is not None:
            raise ToolError(
                f"'{name}' already exists in the destination folder; choose another name"
            )
        plans.append((name, _parse_page_ranges(str(raw.get("pages") or ""), total_pages)))

    try:
        payloads = await anyio.to_thread.run_sync(build_pdf_parts, data, plans)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"Cannot split {node.name}: {exc}") from exc

    created: list[dict[str, Any]] = []
    for (name, pages), (_, payload) in zip(plans, payloads, strict=True):
        created_node, _ = await node_service.create_file(
            ctx.db, parent.id, name, payload, created_by="ai", note="pdf_split"
        )
        enqueue_index([created_node.id])
        ctx.changed_nodes.append({"id": str(created_node.id), "event": "created"})
        path = await node_service.node_path(ctx.db, created_node)
        created.append(
            {
                "node_id": str(created_node.id),
                "path": path,
                "pages": pages,
                "bytes": len(payload),
            }
        )

    operation_id = await _record_operation(
        ctx,
        tool_name="pdf_split",
        description=(
            f"Split {node.name} into {len(created)} PDF(s) "
            f"({', '.join(item['path'] for item in created)})"
        ),
        node_id=None,
        undo={"action": "delete_nodes", "node_ids": [item["node_id"] for item in created]},
    )
    pages_summary = ", ".join(
        f"{item['path']} (pages {item['pages'][0]}-{item['pages'][-1]})" for item in created
    )
    return ToolResult(
        True,
        f"Split {node.name} ({total_pages} pages) into {len(created)} PDF(s) with pages "
        f"copied as-is (images and layout preserved): {pages_summary}.",
        {"source_pages": total_pages, "parts": created},
        operation_id,
    )


# ------------------------------------------------------------- dispatcher
_HANDLERS = {
    "list_workspace": _list_workspace,
    "find_nodes": _find_nodes,
    "read_file": _read_file,
    "write_file": _write_file,
    "edit_file": _edit_file,
    "append_file": _append_file,
    "create_folder": _create_folder,
    "rename_node": _rename_node,
    "move_node": _move_node,
    "delete_node": _delete_node,
    "excel_read": _excel_read,
    "excel_write": _excel_write,
    "excel_add_sheet": _excel_add_sheet,
    "search_documents": _search_documents,
    "translate_file": _translate_file,
    "create_pdf": _create_pdf,
    "convert_to_pdf": _convert_to_pdf,
    "pdf_split": _pdf_split,
    "create_template_from_file": _create_template_from_file,
    "fill_template": _fill_template,
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






