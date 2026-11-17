"""Document template library: scope, placeholders, creation and rendering.

Templates are real DOCX/XLSX binaries stored separately from workspace files.
DOCX rendering uses docxtpl (Jinja2), which keeps styles, headers/footers and
images. XLSX rendering substitutes Jinja expressions in cells while preserving
styles and formulas. PDF is deliberately not a template format: it is a final
rendering target, so the workflow is DOCX/XLSX template -> PDF via the existing
OnlyOffice converter.
"""

from __future__ import annotations

import io
import re
import uuid
import zipfile
from pathlib import Path
from typing import Any, Iterable
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import unescape

import anyio
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.logging_config import get_logger
from app.models import DocumentTemplate, Node, User
from app.services import nodes as node_service
from app.services.parsing import extension_of
from app.services.storage import storage

logger = get_logger(__name__)

TEMPLATE_EXTENSIONS = {".docx": "docx", ".xlsx": "xlsx"}
TEMPLATE_MIME = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_FIELD_RE = re.compile(r"^[^\W\d]\w*$", re.UNICODE)
_TAG_RE = re.compile(r"{{\s*(.*?)\s*}}", re.DOTALL)
_OLD_LOOP_RE = re.compile(r"{{\s*#\s*([^\W\d]\w*)\s*}}", re.UNICODE)
_JINJA_FOR_RE = re.compile(r"{%\s*for\s+([^\W\d]\w*)\s+in\s+([^\W\d]\w*)\s*%}", re.UNICODE)
_JINJA_IF_RE = re.compile(r"{%\s*if\s+([^\W\d]\w*)", re.UNICODE)
_IDENT_RE = re.compile(r"\b([^\W\d]\w*)\b", re.UNICODE)
# ``{{ full name }}`` is not a Jinja expression: two identifiers separated by a
# space can only be a typo, so it gets a clear error instead of two bogus fields.
_SPACED_NAME_RE = re.compile(r"^\s*[^\W\d]\w*(\s+[^\W\d]\w*)+\s*$", re.UNICODE)
# Word splits a typed sentence into several ``<w:t>`` runs; placeholders are
# detected on the concatenated text so ``{{ na`` + ``mber }}`` still counts.
_DOCX_TEXT_RE = re.compile(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", re.DOTALL)
_JINJA_KEYWORDS = {
    "and", "or", "not", "in", "is", "if", "else", "for", "true", "false", "none",
    "True", "False", "None",
}


class TemplateError(Exception):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# --------------------------------------------------------------- inspection

def _field(name: str, kind: str = "string", *, required: bool = True) -> dict[str, Any]:
    return {"name": name, "label": name, "type": kind, "required": required}


def _merge_fields(
    detected: list[dict[str, Any]], provided: Iterable[Any] | None
) -> list[dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for item in detected:
        name = str(item.get("name") or "")
        if name:
            output[name] = item
    for item in provided or []:
        if hasattr(item, "model_dump"):
            item = item.model_dump()
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not _FIELD_RE.match(name):
            raise TemplateError(
                "Field names must start with a letter or underscore and contain only "
                "letters, digits and underscores"
            )
        merged = {**_field(name), **{k: v for k, v in item.items() if v is not None}}
        merged["name"] = name
        merged["type"] = str(merged.get("type") or "string")
        merged["required"] = bool(merged.get("required", True))
        output[name] = merged
    return list(output.values())


def _scan_expression(expr: str, fields: dict[str, dict[str, Any]], loop_vars: set[str]) -> None:
    # Filters are not fields. Attribute/index access is handled by taking only
    # the root identifier: ``item.name`` must not create a top-level ``name``.
    expression = expr.split("|", 1)[0]
    for match in _IDENT_RE.finditer(expression):
        ident = match.group(1)
        start = match.start(1)
        previous = expression[start - 1] if start else ""
        if previous and previous in ".[\"'":  # attribute/index access or a quoted key
            continue
        if ident in loop_vars or ident in _JINJA_KEYWORDS:
            continue
        if ident in {"item", "loop", "range"}:
            continue
        fields.setdefault(ident, _field(ident))


def _detect_text_fields(text: str) -> tuple[dict[str, dict[str, Any]], set[str], list[str]]:
    fields: dict[str, dict[str, Any]] = {}
    loop_vars: set[str] = set()
    loops: list[str] = []
    for match in _JINJA_FOR_RE.finditer(text):
        loop_var, source = match.groups()
        loop_vars.add(loop_var)
        loops.append(source)
        fields[source] = _field(source, "array")
    for match in _OLD_LOOP_RE.finditer(text):
        source = match.group(1)
        loops.append(source)
        fields[source] = _field(source, "array")
    for match in _JINJA_IF_RE.finditer(text):
        _scan_expression(match.group(1), fields, loop_vars)
    for match in _TAG_RE.finditer(text):
        expr = match.group(1).strip()
        if not expr or expr.startswith(("#", "/", "!") or "|" in expr and expr.split("|", 1)[0] == ""):
            continue
        if _SPACED_NAME_RE.match(expr):
            raise TemplateError(
                f"Placeholder '{{{{ {expr} }}}}' contains a space: field names cannot have "
                f"spaces - use an underscore ({expr.replace(' ', '_')}) or a Latin name"
            )
        _scan_expression(expr, fields, loop_vars)
    return fields, loop_vars, loops


def _docx_part_text(xml: str) -> str:
    """Concatenated text of all ``<w:t>`` runs of one document part.

    Word stores text in many small runs, so a placeholder typed with formatting
    changes (or pasted) may be split into ``{{ na`` + ``mber }}``. Runs are
    joined inside each paragraph (the renderer does the same) and paragraphs are
    separated by a newline, so unrelated paragraphs never merge into one value.
    """
    paragraphs: list[str] = []
    for chunk in xml.split("</w:p>"):
        pieces = _DOCX_TEXT_RE.findall(chunk)
        if pieces:
            paragraphs.append(unescape("".join(pieces)))
    return "\n".join(paragraphs)


def _docx_xml_parts(data: bytes) -> list[str]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return [
                name
                for name in archive.namelist()
                if name.startswith("word/")
                and name.endswith(".xml")
                and (
                    name == "word/document.xml"
                    or name.startswith("word/header")
                    or name.startswith("word/footer")
                    or name in {"word/footnotes.xml", "word/endnotes.xml", "word/comments.xml"}
                )
            ]
    except zipfile.BadZipFile as exc:
        raise TemplateError("The DOCX file is damaged or is not a real Word document") from exc


def detect_fields(kind: str, data: bytes) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read Jinja placeholders and return a field list plus engine metadata."""
    fields: dict[str, dict[str, Any]] = {}
    loops: list[str] = []
    if kind == "docx":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for name in _docx_xml_parts(data):
                    text = _docx_part_text(archive.read(name).decode("utf-8", "replace"))
                    if not text:
                        continue
                    part_fields, _, part_loops = _detect_text_fields(text)
                    fields.update(part_fields)
                    loops.extend(part_loops)
        except TemplateError:
            raise
        except zipfile.BadZipFile as exc:
            raise TemplateError("The DOCX file is damaged or is not a real Word document") from exc
        config = {"engine": "docxtpl", "loops": sorted(set(loops))}
    elif kind == "xlsx":
        try:
            from openpyxl import load_workbook

            workbook = load_workbook(io.BytesIO(data), data_only=False)
            try:
                for worksheet in workbook.worksheets:
                    for row in worksheet.iter_rows():
                        for cell in row:
                            value = cell.value
                            if not isinstance(value, str) or "{{" not in value and "{%" not in value:
                                continue
                            part_fields, _, part_loops = _detect_text_fields(value)
                            fields.update(part_fields)
                            loops.extend(part_loops)
            finally:
                workbook.close()
        except Exception as exc:  # noqa: BLE001
            raise TemplateError(f"Cannot read the XLSX file: {exc}") from exc
        config = {"engine": "jinja-cells", "loops": sorted(set(loops))}
    else:
        raise TemplateError("Templates support only DOCX and XLSX files", status_code=415)
    return list(fields.values()), config


# --------------------------------------------------------------- suggestions
# Concrete values that are almost always filled in per document. The admin or
# the agent picks the useful ones and turns them into {{ placeholders }}.
_SUGGESTION_RULES: tuple[dict[str, Any], ...] = (
    {
        "field": "contract_number",
        "reason": "number after 'No'",
        "pattern": r"№\s*([A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9/_-]{1,30})",
        "group": 1,
    },
    {"field": "date", "reason": "date", "pattern": r"\b\d{2}[./]\d{2}[./]\d{4}\b"},
    {
        "field": "date",
        "reason": "date",
        "pattern": (
            r"\b\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|"
            r"сентября|октября|ноября|декабря)\s+\d{4}(?:\s*г\.?)?"
        ),
    },
    {"field": "inn", "reason": "INN", "pattern": r"\bИНН\s*[:№]?\s*(\d{10}|\d{12})\b", "group": 1},
    {"field": "ogrn", "reason": "OGRN", "pattern": r"\bОГРНИП?\s*[:№]?\s*(\d{13}|\d{15})\b", "group": 1},
    {"field": "kpp", "reason": "KPP", "pattern": r"\bКПП\s*[:№]?\s*(\d{9})\b", "group": 1},
    {
        "field": "amount",
        "reason": "money amount",
        "pattern": r"\b\d{1,3}(?:[ \u00a0]\d{3})*(?:[.,]\d{2})?\s*(?:рубль|рубля|рублей|руб\.?|₽|RUB)",
    },
    {
        "field": "amount",
        "reason": "money amount",
        "pattern": r"(?:\$|€)\s?\d{1,3}(?:[ \u00a0]\d{3})*(?:[.,]\d{2})?",
    },
    {
        "field": "email",
        "reason": "e-mail",
        "pattern": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    },
    {
        "field": "phone",
        "reason": "phone",
        "pattern": r"(?:\+7|\b8)[\s(]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}\b",
    },
    {
        "field": "organization",
        "reason": "company name",
        "pattern": r"(?:ООО|АО|ПАО|ЗАО|ИП)\s+[«\"“]?[^»\"”\n,.;]{2,60}[»\"”]?",
    },
    {
        "field": "address",
        "reason": "address",
        "pattern": (
            r"\b(?:г\.|город)\s*[А-ЯЁ][^,\n]{1,40}"
            r"(?:,\s*(?:ул\.|улица|пр-т|проспект|пер\.|д\.)[^,\n]{1,60}){0,3}"
        ),
    },
    {
        "field": "full_name",
        "reason": "person name",
        "pattern": (
            r"\b[А-ЯЁ][а-яё]{1,20}\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.|"
            r"\b[А-ЯЁ][а-яё]{1,20}\s+[А-ЯЁ][а-яё]{1,20}\s+[А-ЯЁ][а-яё]{1,20}\b"
        ),
    },
)


def _document_text(kind: str, data: bytes) -> str:
    """Plain text of a template candidate: DOCX runs or XLSX cell values."""
    if kind == "docx":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                parts = [
                    _docx_part_text(archive.read(name).decode("utf-8", "replace"))
                    for name in _docx_xml_parts(data)
                ]
        except zipfile.BadZipFile as exc:
            raise TemplateError("The DOCX file is damaged or is not a real Word document") from exc
        return "\n".join(part for part in parts if part)
    if kind == "xlsx":
        try:
            from openpyxl import load_workbook

            workbook = load_workbook(io.BytesIO(data), data_only=False)
            try:
                values: list[str] = []
                for worksheet in workbook.worksheets:
                    for row in worksheet.iter_rows():
                        for cell in row:
                            if isinstance(cell.value, str) and cell.value.strip():
                                values.append(cell.value)
                return "\n".join(values)
            finally:
                workbook.close()
        except Exception as exc:  # noqa: BLE001
            raise TemplateError(f"Cannot read the XLSX file: {exc}") from exc
    raise TemplateError("Templates support only DOCX and XLSX files", status_code=415)


def _unique_field(base: str, used: set[str]) -> str:
    name = re.sub(r"\W+", "_", base, flags=re.UNICODE).strip("_") or "field"
    if not name[0].isalpha() and name[0] != "_":
        name = f"f_{name}"
    candidate = name
    index = 2
    while candidate in used:
        candidate = f"{name}_{index}"
        index += 1
    used.add(candidate)
    return candidate


def propose_replacements(kind: str, data: bytes) -> dict[str, Any]:
    """Suggest concrete values that should become ``{{ placeholders }}``.

    Rule-based on purpose: every suggestion must contain the *exact* text of the
    document, otherwise the replacement would silently do nothing.
    """
    text = _document_text(kind, data)
    placeholder_spans = [match.span() for match in _TAG_RE.finditer(text)]

    def inside_placeholder(start: int, end: int) -> bool:
        return any(
            start < span_end and end > span_start for span_start, span_end in placeholder_spans
        )

    candidates: list[tuple[int, int, str, dict[str, Any]]] = []
    for rule in _SUGGESTION_RULES:
        pattern = re.compile(rule["pattern"], re.UNICODE)
        group = int(rule.get("group") or 0)
        for match in pattern.finditer(text):
            start, end = match.span(group) if group else match.span(0)
            value = (match.group(group) if group else match.group(0)) or ""
            value = value.strip()
            if len(value) < 2 or inside_placeholder(start, end):
                continue
            candidates.append((start, end, value, rule))

    # Prefer earlier and longer matches and never let two suggestions overlap.
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    selected: list[tuple[int, int, str, dict[str, Any]]] = []
    for candidate in candidates:
        start, end = candidate[0], candidate[1]
        if any(
            start < other_end and end > other_start
            for other_start, other_end, _value, _rule in selected
        ):
            continue
        selected.append(candidate)

    used_fields: set[str] = set()
    field_by_value: dict[str, str] = {}
    suggestions: list[dict[str, Any]] = []
    for start, _end, value, rule in selected:
        field = field_by_value.get(value)
        if field is None:
            field = _unique_field(str(rule["field"]), used_fields)
            field_by_value[value] = field
        suggestions.append(
            {
                "find": value,
                "field": field,
                "type": rule.get("type", "string"),
                "reason": rule.get("reason", ""),
                "count": text.count(value),
                "offset": start,
            }
        )
    return {"kind": kind, "suggestions": suggestions[:40], "chars": len(text)}




# --------------------------------------------------------------- binary edits

def _loose_xml_pattern(find: str) -> re.Pattern[str] | None:
    """Regex that matches ``find`` even when Word split it into several runs."""
    parts = [r"\s+" if char.isspace() else re.escape(char) for char in find]
    if not parts:
        return None
    return re.compile(r"(?:<[^>]+>)*".join(parts), re.DOTALL)


def _apply_docx_replacements(data: bytes, replacements: list[dict[str, str]]) -> bytes:
    by_find: list[tuple[str, str, str]] = []
    for item in replacements:
        find = str(item.get("find") or "").strip()
        field = str(item.get("field") or "").strip()
        if not find or not _FIELD_RE.match(field):
            raise TemplateError(
                "Each replacement needs a non-empty find value and a valid field name "
                "(letters, digits, underscore)"
            )
        by_find.append((xml_escape(find), find, xml_escape("{{ " + field + " }}")))
    if not by_find:
        return data
    try:
        source = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise TemplateError("The DOCX file is damaged") from exc
    output = io.BytesIO()
    applied: set[str] = set()
    with source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            content = source.read(item.filename)
            if item.filename.startswith("word/") and item.filename.endswith(".xml"):
                text = content.decode("utf-8", "replace")
                for escaped, original, replacement in by_find:
                    if escaped in text:
                        applied.add(original)
                        text = text.replace(escaped, replacement)
                        continue
                    loose = _loose_xml_pattern(original)
                    if loose is None:
                        continue
                    updated, hits = loose.subn(replacement, text, count=1)
                    if hits:
                        applied.add(original)
                        text = updated
                content = text.encode("utf-8")
            target.writestr(item, content)
    missing = [original for _, original, _ in by_find if original not in applied]
    if missing:
        preview = ", ".join(f"'{text}'" for text in missing[:3])
        raise TemplateError(
            f"Text {preview} was not found in the document. Word often splits a phrase into "
            "several runs, so the exact string is not there: retype it in Word and try again, "
            "or type {{ field }} into the document yourself."
        )
    return output.getvalue()


def _apply_xlsx_replacements(data: bytes, replacements: list[dict[str, str]]) -> bytes:
    by_find: list[tuple[str, str]] = []
    for item in replacements:
        find = str(item.get("find") or "").strip()
        field = str(item.get("field") or "").strip()
        if not find or not _FIELD_RE.match(field):
            raise TemplateError(
                "Each replacement needs a non-empty find value and a valid field name"
            )
        by_find.append((find, "{{ " + field + " }}"))
    if not by_find:
        return data
    counts: dict[str, int] = {find: 0 for find, _ in by_find}
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(io.BytesIO(data), data_only=False)
        for worksheet in workbook.worksheets:
            for row in worksheet.iter_rows():
                for cell in row:
                    if not isinstance(cell.value, str):
                        continue
                    value = cell.value
                    for find, replacement in by_find:
                        hits = value.count(find)
                        if hits:
                            counts[find] += hits
                            value = value.replace(find, replacement)
                    cell.value = value
        output = io.BytesIO()
        workbook.save(output)
    except Exception as exc:  # noqa: BLE001
        raise TemplateError(f"Cannot prepare the XLSX template: {exc}") from exc
    missing = [find for find, hits in counts.items() if not hits]
    if missing:
        preview = ", ".join(f"'{text}'" for text in missing[:3])
        raise TemplateError(f"Text {preview} was not found in any cell of the workbook")
    return output.getvalue()


# --------------------------------------------------------------- persistence
async def _store_template(
    db: AsyncSession,
    owner: User,
    *,
    scope: str,
    name: str,
    description: str,
    source_name: str,
    data: bytes,
    fields: Iterable[Any] | None = None,
    replacements: list[dict[str, str]] | None = None,
    source_node_id: uuid.UUID | None = None,
) -> DocumentTemplate:
    if scope not in {"standard", "user"}:
        raise TemplateError("Unknown template scope")
    if scope == "standard" and not owner.is_admin:
        raise TemplateError("Only an administrator can manage standard templates", status_code=403)
    safe_source = node_service.sanitize_name(source_name)
    ext = extension_of(safe_source)
    kind = TEMPLATE_EXTENSIONS.get(ext)
    if kind is None:
        if ext == ".pdf":
            raise TemplateError(
                "PDF is a final format, not a fillable template. Use DOCX or XLSX and "
                "convert the result to PDF afterwards.",
                status_code=415,
            )
        raise TemplateError(
            "Templates support only DOCX and XLSX files; PDF/other formats are not editable templates",
            status_code=415,
        )
    if not data or len(data) > settings.max_upload_mb * 1024 * 1024:
        raise TemplateError("The template file is empty or too large", status_code=413)
    if not data.startswith(b"PK"):
        raise TemplateError("The file is not a valid DOCX/XLSX archive", status_code=415)

    if replacements:
        data = (
            _apply_docx_replacements(data, replacements)
            if kind == "docx"
            else _apply_xlsx_replacements(data, replacements)
        )
        if not data:
            raise TemplateError("The template became empty after replacements")

    detected, config = detect_fields(kind, data)
    clean_fields = _merge_fields(detected, fields)
    clean_name = (name or Path(safe_source).stem).strip()
    if clean_name.lower().endswith(ext):
        clean_name = clean_name[: -len(ext)].strip()
    clean_name = node_service.sanitize_name(clean_name or Path(safe_source).stem)

    key, size, digest = await storage.save_bytes(data)
    template = DocumentTemplate(
        owner_id=owner.id,
        scope=scope,
        name=clean_name[:200],
        description=(description or "")[:4000],
        kind=kind,
        extension=ext,
        mime=TEMPLATE_MIME.get(ext),
        storage_key=key,
        size=size,
        sha256=digest,
        source_node_id=source_node_id,
        fields=clean_fields,
        config=config,
        created_by=owner.username,
    )
    db.add(template)
    await db.flush()
    return template


async def create_from_bytes(
    db: AsyncSession,
    owner: User,
    *,
    scope: str,
    name: str | None,
    description: str | None,
    source_name: str,
    data: bytes,
    fields: Iterable[Any] | None = None,
) -> DocumentTemplate:
    return await _store_template(
        db,
        owner,
        scope=scope,
        name=name or Path(source_name).stem,
        description=description or "",
        source_name=source_name,
        data=data,
        fields=fields,
    )


async def create_from_node(
    db: AsyncSession,
    owner: User,
    node: Node,
    *,
    name: str | None = None,
    description: str | None = None,
    replacements: list[dict[str, str]] | None = None,
    fields: Iterable[Any] | None = None,
) -> DocumentTemplate:
    if node.kind != "file" or not node.storage_key:
        raise TemplateError("Only a file can be saved as a template")
    data = await storage.read_bytes(node.storage_key)
    return await _store_template(
        db,
        owner,
        scope="user",
        name=name or Path(node.name).stem,
        description=description or "",
        source_name=node.name,
        data=data,
        fields=fields,
        replacements=replacements,
        source_node_id=node.id,
    )


# ------------------------------------------------------------------- queries
def _can_manage(template: DocumentTemplate, user: User) -> bool:
    if template.scope == "standard":
        return user.is_admin
    return template.owner_id == user.id


async def list_templates(
    db: AsyncSession, user: User, scope: str = "available"
) -> list[DocumentTemplate]:
    if scope == "standard":
        stmt = select(DocumentTemplate).where(
            DocumentTemplate.scope == "standard", DocumentTemplate.is_active.is_(True)
        ).order_by(DocumentTemplate.name)
    elif scope == "mine":
        stmt = select(DocumentTemplate).where(
            DocumentTemplate.scope == "user",
            DocumentTemplate.owner_id == user.id,
            DocumentTemplate.is_active.is_(True),
        ).order_by(DocumentTemplate.name)
    elif scope == "available":
        stmt = select(DocumentTemplate).where(
            DocumentTemplate.is_active.is_(True),
            or_(
                DocumentTemplate.scope == "standard",
                (DocumentTemplate.scope == "user") & (DocumentTemplate.owner_id == user.id),
            ),
        ).order_by(DocumentTemplate.scope.desc(), DocumentTemplate.name)
    else:
        raise TemplateError("Unknown template scope")
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_template(
    db: AsyncSession, user: User, template_id: uuid.UUID | str
) -> DocumentTemplate:
    try:
        identifier = uuid.UUID(str(template_id))
    except (ValueError, AttributeError) as exc:
        raise TemplateError("Invalid template id", status_code=400) from exc
    template = await db.get(DocumentTemplate, identifier)
    if template is None or not template.is_active:
        raise TemplateError("Template not found", status_code=404)
    visible = template.scope == "standard" or template.owner_id == user.id
    if not visible:
        # 404 keeps other users' libraries private.
        raise TemplateError("Template not found", status_code=404)
    return template


async def update_template(
    db: AsyncSession,
    user: User,
    template: DocumentTemplate,
    *,
    name: str | None = None,
    description: str | None = None,
    fields: Iterable[Any] | None = None,
) -> DocumentTemplate:
    if not _can_manage(template, user):
        raise TemplateError("You cannot change this template", status_code=403)
    if name is not None:
        clean = name.strip()
        if not clean:
            raise TemplateError("Template name cannot be empty")
        template.name = node_service.sanitize_name(clean)[:200]
    if description is not None:
        template.description = description[:4000]
    if fields is not None:
        template.fields = _merge_fields([], fields)
    await db.flush()
    return template


async def delete_template(db: AsyncSession, user: User, template: DocumentTemplate) -> None:
    if not _can_manage(template, user):
        raise TemplateError("You cannot delete this template", status_code=403)
    storage.delete(template.storage_key)
    await db.delete(template)


# ------------------------------------------------------------------- render

def _validate_data(template: DocumentTemplate, data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise TemplateError("data must be an object with field values")
    missing: list[str] = []
    for item in template.fields or []:
        name = str(item.get("name") or "")
        if not name or not item.get("required", True):
            continue
        value = data.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(name)
            continue
        kind = item.get("type")
        if kind == "array" and not isinstance(value, list):
            raise TemplateError(f"Field '{name}' must be a list")
        if kind == "object" and not isinstance(value, dict):
            raise TemplateError(f"Field '{name}' must be an object")
    if missing:
        raise TemplateError("Missing required field(s): " + ", ".join(missing))
    return data


def _render_docx(template: DocumentTemplate, data: bytes, context: dict[str, Any]) -> bytes:
    try:
        from docxtpl import DocxTemplate
    except ImportError as exc:
        raise TemplateError(
            "DOCX template rendering is unavailable (docxtpl is not installed)",
            status_code=503,
        ) from exc
    try:
        document = DocxTemplate(io.BytesIO(data))
        document.render(context)
        output = io.BytesIO()
        document.save(output)
        result = output.getvalue()
    except Exception as exc:  # noqa: BLE001
        raise TemplateError(f"Cannot render DOCX template: {exc}") from exc
    if not result:
        raise TemplateError("The rendered DOCX is empty")
    return result


def _render_xlsx(template: DocumentTemplate, data: bytes, context: dict[str, Any]) -> bytes:
    try:
        from jinja2 import StrictUndefined
        from jinja2.sandbox import SandboxedEnvironment
        from openpyxl import load_workbook

        environment = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
        workbook = load_workbook(io.BytesIO(data), data_only=False)
        try:
            for worksheet in workbook.worksheets:
                for row in worksheet.iter_rows():
                    for cell in row:
                        value = cell.value
                        if not isinstance(value, str) or ("{{" not in value and "{%" not in value):
                            continue
                        try:
                            rendered = environment.from_string(value).render(**context)
                        except Exception as exc:  # noqa: BLE001
                            raise TemplateError(
                                f"Cannot render cell {worksheet.title}!{cell.coordinate}: {exc}"
                            ) from exc
                        # A value starting with = + - @ would become a formula in
                        # Excel; prefix it with an apostrophe so user data stays text.
                        if rendered[:1] in {"=", "+", "-", "@"}:
                            rendered = "'" + rendered
                        cell.value = rendered
        finally:
            workbook.close()
        output = io.BytesIO()
        workbook.save(output)
        result = output.getvalue()
    except TemplateError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TemplateError(f"Cannot render XLSX template: {exc}") from exc
    if not result:
        raise TemplateError("The rendered XLSX is empty")
    return result


def _read_back(kind: str, data: bytes) -> dict[str, Any]:
    if kind == "docx":
        import docx

        document = docx.Document(io.BytesIO(data))
        return {"paragraphs": len(document.paragraphs), "tables": len(document.tables)}
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(data), read_only=True)
    try:
        return {"sheets": list(workbook.sheetnames), "rows": sum(int(s.max_row or 0) for s in workbook.worksheets)}
    finally:
        workbook.close()


async def render_template(
    template: DocumentTemplate, data: Any
) -> tuple[bytes, dict[str, Any]]:
    context = _validate_data(template, data)
    raw = await storage.read_bytes(template.storage_key)
    if template.kind == "docx":
        output = await anyio.to_thread.run_sync(_render_docx, template, raw, context)
    elif template.kind == "xlsx":
        output = await anyio.to_thread.run_sync(_render_xlsx, template, raw, context)
    else:
        raise TemplateError("Unsupported template kind", status_code=415)
    if len(output) > settings.max_upload_mb * 1024 * 1024:
        raise TemplateError("The rendered file exceeds the upload limit", status_code=413)
    verification = await anyio.to_thread.run_sync(_read_back, template.kind, output)
    return output, verification


# ------------------------------------------------------------------- prompt
async def prompt_block(db: AsyncSession, user: User) -> str:
    """A compact, deterministic list of templates the model may use."""
    templates = await list_templates(db, user, "available")
    if not templates:
        return (
            "Available document templates: none yet. Templates are created by an administrator "
            "or by the user from a DOCX/XLSX file."
        )
    lines = [
        "Available document templates (use fill_template, never invent template ids):",
    ]
    for template in templates[:24]:
        field_parts = [
            f"{item.get('name')}:{item.get('type', 'string')}"
            for item in (template.fields or [])[:16]
        ]
        scope = "standard" if template.scope == "standard" else "my"
        lines.append(
            f"- id={template.id} | {scope} | {template.name} | {template.kind} | "
            f"fields: {', '.join(field_parts) if field_parts else 'none'}"
        )
    lines.append(
        "PDF is not a template format. Use DOCX/XLSX templates; after filling, call "
        "convert_to_pdf if the user needs a PDF."
    )
    return "\n".join(lines)


def template_to_dict(template: DocumentTemplate) -> dict[str, Any]:
    """Plain dict helper for places that cannot use the Pydantic response model."""
    return {
        "id": str(template.id),
        "owner_id": str(template.owner_id) if template.owner_id else None,
        "scope": template.scope,
        "name": template.name,
        "description": template.description,
        "kind": template.kind,
        "extension": template.extension,
        "mime": template.mime,
        "size": template.size,
        "source_node_id": str(template.source_node_id) if template.source_node_id else None,
        "fields": template.fields or [],
        "config": template.config or {},
        "is_active": template.is_active,
        "created_by": template.created_by,
        "created_at": template.created_at,
        "updated_at": template.updated_at,
    }
