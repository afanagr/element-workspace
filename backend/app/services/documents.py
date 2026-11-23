"""Document generation: xlsx / docx / pdf / csv from AI tool calls."""

from __future__ import annotations

import base64
import io
import json
import re
from typing import Any, Sequence

from app.config import settings
from app.logging_config import get_logger
from app.services.document_specs import parse_docx_text, parse_xlsx_text

logger = get_logger(__name__)

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_MD_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_INLINE = re.compile(r"(\*\*[^*]+\*\*|__[^_]+__|\*[^*]+\*|_[^_]+_|`[^`]+`)")
_TABLE_SEPARATOR = re.compile(r"^:?-{3,}:?$")
_BR_RE = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)


def _clean_breaks(value: Any) -> str:
    """Turn literal ``<br>`` tags (often emitted by models) into real newlines."""
    return _BR_RE.sub("\n", str(value if value is not None else ""))


def build_xlsx(
    sheets: dict[str, list[list[Any]]] | Sequence[dict[str, Any]],
) -> bytes:
    """Build a workbook from plain rows or a structured sheet specification.

    A rich spec looks like::

        {"sheets": [{"name": "Sales", "rows": [["A", "B"], [1, "=A2"]],
                     "freeze": "A2", "formats": {"B2:B9": "#,##0.00"},
                     "merges": ["A1:B1"], "widths": {"A": 24},
                     "styles": {"A1:B1": {"bold": true, "fill": "DDEBF7"}}}]}
    """
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.remove(workbook.active)
    specs = _xlsx_specs(sheets) or [{"name": "Sheet1", "rows": []}]
    for spec in specs:
        sheet = workbook.create_sheet(title=_safe_sheet_name(spec.get("name") or "Sheet"))
        rows = spec.get("rows") or []
        for row in rows:
            sheet.append([_xlsx_value(value) for value in row])
        _format_sheet(sheet, spec, rows)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _xlsx_specs(
    sheets: dict[str, list[list[Any]]] | Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    if isinstance(sheets, dict):
        return [{"name": str(name), "rows": rows or []} for name, rows in sheets.items()]
    return [dict(item) for item in sheets if isinstance(item, dict)]


def _xlsx_value(value: Any) -> Any:
    """Coerce text cells to numbers while preserving leading-zero identifiers."""
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, ensure_ascii=False)
    text = _clean_breaks(value)
    if text.startswith("="):
        return text
    stripped = text.strip()
    if re.fullmatch(r"-?\d+", stripped):
        digits = stripped.lstrip("-")
        if len(digits) > 1 and digits.startswith("0"):
            return text
        return int(stripped)
    if re.fullmatch(r"-?\d+\.\d+", stripped):
        return float(stripped)
    return text


def _format_sheet(sheet: Any, spec: dict[str, Any], rows: list[Any]) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.utils.cell import range_boundaries

    header = bool(spec.get("header", True)) and bool(rows)
    if header:
        fill = str(spec.get("header_fill") or "DDEBF7").lstrip("#")
        for cell in sheet[1]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor=fill)
            cell.alignment = Alignment(horizontal="center", vertical="center")

    for column in sheet.columns:
        letter = get_column_letter(column[0].column)
        longest = max(
            (len(str(cell.value)) for cell in column if cell.value is not None), default=8
        )
        sheet.column_dimensions[letter].width = min(max(10, longest + 2), 60)
    widths = spec.get("widths") or {}
    if isinstance(widths, dict):
        for letter, width in widths.items():
            try:
                sheet.column_dimensions[str(letter)].width = float(width)
            except (TypeError, ValueError):
                logger.warning("Ignoring invalid column width for %s", letter)

    for ref, fmt in (spec.get("formats") or {}).items():
        try:
            min_col, min_row, max_col, max_row = range_boundaries(str(ref))
            for row in sheet.iter_rows(
                min_row=min_row, max_row=max_row, min_col=min_col, max_col=max_col
            ):
                for cell in row:
                    cell.number_format = str(fmt)
        except Exception:  # noqa: BLE001 - invalid range must not kill generation
            logger.warning("Ignoring invalid number format range %s", ref)

    for merge in spec.get("merges") or []:
        try:
            sheet.merge_cells(str(merge))
        except Exception:  # noqa: BLE001
            logger.warning("Ignoring invalid merge range %s", merge)

    for ref, style in (spec.get("styles") or {}).items():
        if not isinstance(style, dict):
            continue
        try:
            min_col, min_row, max_col, max_row = range_boundaries(str(ref))
            for row in sheet.iter_rows(
                min_row=min_row, max_row=max_row, min_col=min_col, max_col=max_col
            ):
                for cell in row:
                    if style.get("bold"):
                        cell.font = Font(bold=True)
                    if style.get("fill"):
                        cell.fill = PatternFill(
                            "solid", fgColor=str(style["fill"]).lstrip("#")
                        )
                    if style.get("align"):
                        cell.alignment = Alignment(horizontal=str(style["align"]))
                    if style.get("number_format"):
                        cell.number_format = str(style["number_format"])
        except Exception:  # noqa: BLE001
            logger.warning("Ignoring invalid style range %s", ref)

    if rows:
        freeze = spec.get("freeze")
        if freeze is None and header and sheet.max_row > 1:
            freeze = "A2"
        if freeze:
            sheet.freeze_panes = str(freeze)
        if (
            spec.get("auto_filter", True)
            and sheet.max_row > 1
            and sheet.max_column > 1
        ):
            sheet.auto_filter.ref = (
                f"A1:{get_column_letter(sheet.max_column)}{sheet.max_row}"
            )


def _safe_sheet_name(name: str) -> str:
    cleaned = re.sub(r"[\[\]:*?/\\]", "_", str(name or "Sheet"))[:31].strip() or "Sheet"
    return cleaned


def _cell(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return _clean_breaks(value) if isinstance(value, str) else value
    if isinstance(value, (list, tuple, dict)):
        import json

        return json.dumps(value, ensure_ascii=False)
    return _clean_breaks(value)


def build_csv(rows: Sequence[Sequence[Any]]) -> bytes:
    import csv

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for row in rows or []:
        writer.writerow([_cell(value) for value in row])
    return buffer.getvalue().encode("utf-8")


def _inline_parts(text: str) -> list[tuple[str, str | None]]:
    """Split a line into (text, style) chunks for bold/italic/code runs."""
    parts: list[tuple[str, str | None]] = []
    position = 0
    for match in _INLINE.finditer(text or ""):
        if match.start() > position:
            parts.append((text[position : match.start()], None))
        token = match.group(0)
        if token.startswith("**") or token.startswith("__"):
            parts.append((token[2:-2], "bold"))
        elif token.startswith("`"):
            parts.append((token[1:-1], "code"))
        else:
            parts.append((token[1:-1], "italic"))
        position = match.end()
    if position < len(text or ""):
        parts.append((text[position:], None))
    return parts or [("", None)]


def _style_exists(document: Any, name: str) -> bool:
    return any(style.name == name for style in document.styles)


def _add_inline(paragraph: Any, text: str, *, bold: bool = False) -> None:
    from docx.shared import Pt

    lines = _clean_breaks(text).split("\n")
    for line_index, line in enumerate(lines):
        if line_index:
            paragraph.add_run().add_break()
        for chunk, style in _inline_parts(line):
            run = paragraph.add_run(chunk)
            if bold or style == "bold":
                run.bold = True
            if style == "italic":
                run.italic = True
            if style == "code":
                run.font.name = "Courier New"
                run.font.size = Pt(9)


def _table_cells(line: str) -> list[str] | None:
    text = line.strip()
    if "|" not in text:
        return None
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def _is_table_separator(line: str) -> bool:
    cells = _table_cells(line)
    return bool(cells) and all(_TABLE_SEPARATOR.fullmatch(cell or "") for cell in cells)


def _add_docx_table(document: Any, header: list[str], rows: list[list[str]]) -> None:
    table = document.add_table(rows=1, cols=max(1, len(header)))
    try:
        table.style = "Table Grid"
    except KeyError:
        logger.debug("Table Grid style is not available in this document")
    for index, text in enumerate(header[: len(table.columns)]):
        _add_inline(table.rows[0].cells[index].paragraphs[0], text, bold=True)
    for row in rows:
        cells = table.add_row().cells
        for index in range(len(table.columns)):
            value = row[index] if index < len(row) else ""
            _add_inline(cells[index].paragraphs[0], value)


def _add_code_block(document: Any, code: str) -> None:
    from docx.shared import Pt

    paragraph = document.add_paragraph()
    if _style_exists(document, "No Spacing"):
        paragraph.style = document.styles["No Spacing"]
    run = paragraph.add_run(code)
    run.font.name = "Courier New"
    run.font.size = Pt(9)


def _docx_blocks(text: str) -> list[Any] | None:
    spec = parse_docx_text(text)
    return [block.as_block() for block in spec.blocks] if spec is not None else None


def _render_docx_markdown(document: Any, text: str) -> None:
    from docx.shared import Pt

    lines = (text or "").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            index += 1
            code: list[str] = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            _add_code_block(document, "\n".join(code))
            continue
        if (
            "|" in stripped
            and index + 1 < len(lines)
            and _is_table_separator(lines[index + 1])
        ):
            header = _table_cells(stripped) or []
            rows: list[list[str]] = []
            index += 2
            while index < len(lines) and lines[index].strip() and "|" in lines[index]:
                cells = _table_cells(lines[index]) or []
                if cells:
                    rows.append(cells)
                index += 1
            if header:
                _add_docx_table(document, header, rows)
            continue
        heading = _MD_HEADING.match(stripped)
        if heading:
            document.add_heading(
                _clean_breaks(heading.group(2)).replace("\n", " "),
                level=min(len(heading.group(1)) + 1, 9),
            )
            index += 1
            continue
        bullet = _MD_BULLET.match(line)
        if bullet:
            style = (
                document.styles["List Bullet"]
                if _style_exists(document, "List Bullet")
                else None
            )
            paragraph = (
                document.add_paragraph(style=style)
                if style
                else document.add_paragraph()
            )
            _add_inline(paragraph, bullet.group(1))
            index += 1
            continue
        numbered = _MD_NUMBERED.match(line)
        if numbered:
            style = (
                document.styles["List Number"]
                if _style_exists(document, "List Number")
                else None
            )
            paragraph = (
                document.add_paragraph(style=style)
                if style
                else document.add_paragraph()
            )
            _add_inline(paragraph, numbered.group(1))
            index += 1
            continue
        if stripped.startswith(">"):
            style = (
                document.styles["Quote"] if _style_exists(document, "Quote") else None
            )
            paragraph = (
                document.add_paragraph(style=style)
                if style
                else document.add_paragraph()
            )
            _add_inline(paragraph, stripped[1:].strip())
            index += 1
            continue
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.space_after = Pt(4)
        _add_inline(paragraph, line)
        index += 1


def _render_docx_blocks(document: Any, blocks: list[Any]) -> None:
    from docx.shared import Mm, Pt

    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "paragraph").lower()
        if kind == "heading":
            value = _clean_breaks(block.get("text") or "").replace("\n", " ")
            if value:
                try:
                    level = int(block.get("level") or 1)
                except (TypeError, ValueError):
                    level = 1
                document.add_heading(value, level=max(1, min(level, 9)))
        elif kind == "table":
            header = [str(cell) for cell in block.get("header") or []]
            rows = [
                [str(cell) for cell in row]
                for row in block.get("rows") or []
                if isinstance(row, (list, tuple))
            ]
            if header:
                _add_docx_table(document, header, rows)
        elif kind == "code":
            _add_code_block(document, str(block.get("text") or ""))
        elif kind == "quote":
            style = (
                document.styles["Quote"] if _style_exists(document, "Quote") else None
            )
            paragraph = (
                document.add_paragraph(style=style)
                if style
                else document.add_paragraph()
            )
            _add_inline(paragraph, str(block.get("text") or ""))
        elif kind == "list":
            style_name = "List Number" if block.get("ordered") else "List Bullet"
            style = (
                document.styles[style_name]
                if _style_exists(document, style_name)
                else None
            )
            for item in block.get("items") or []:
                paragraph = (
                    document.add_paragraph(style=style)
                    if style
                    else document.add_paragraph()
                )
                _add_inline(paragraph, str(item))
        elif kind == "pagebreak":
            document.add_page_break()
        elif kind == "image":
            encoded = str(block.get("data") or block.get("base64") or "")
            if not encoded:
                continue
            try:
                raw = base64.b64decode(encoded, validate=True)
            except Exception:  # noqa: BLE001
                logger.warning("Skipping invalid base64 image block")
                continue
            if len(raw) > 8 * 1024 * 1024:
                logger.warning("Skipping image block larger than 8 MB")
                continue
            kwargs: dict[str, Any] = {}
            try:
                if block.get("width_mm"):
                    kwargs["width"] = Mm(int(block["width_mm"]))
            except (TypeError, ValueError):
                kwargs = {}
            try:
                document.add_paragraph().add_run().add_picture(io.BytesIO(raw), **kwargs)
            except Exception:  # noqa: BLE001
                logger.warning("Could not insert an image block")
        else:
            value = str(block.get("text") or "")
            if value:
                paragraph = document.add_paragraph()
                paragraph.paragraph_format.space_after = Pt(4)
                _add_inline(paragraph, value)


def build_docx(text: str, *, title: str | None = None) -> bytes:
    import docx

    document = docx.Document()
    if title:
        document.add_heading(title, level=0)
        document.core_properties.title = title
    blocks = _docx_blocks(text)
    if blocks is not None:
        _render_docx_blocks(document, blocks)
    else:
        _render_docx_markdown(document, text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _register_pdf_fonts() -> tuple[str, str]:
    """Register a Unicode TTF so Cyrillic/CJK PDFs render (DejaVu ships in the image)."""
    from pathlib import Path

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
        ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
        ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
    ]
    for regular, bold in candidates:
        if not Path(regular).exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont("AppSans", regular))
            bold_name = "AppSans"
            if Path(bold).exists():
                pdfmetrics.registerFont(TTFont("AppSans-Bold", bold))
                bold_name = "AppSans-Bold"
            return "AppSans", bold_name
        except Exception:  # noqa: BLE001 - corrupt font, try the next one
            continue
    return "Helvetica", "Helvetica-Bold"


def build_pdf(text: str, *, title: str | None = None) -> bytes:
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    from xml.sax.saxutils import escape

    body_font, bold_font = _register_pdf_fonts()
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=title or "Element Workspace document",
    )
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "BodyUnicode",
        parent=styles["BodyText"],
        fontName=body_font,
        fontSize=10.5,
        leading=15,
        alignment=TA_LEFT,
    )
    heading = ParagraphStyle(
        "HeadingUnicode",
        parent=styles["Heading2"],
        fontName=bold_font,
        fontSize=14,
        leading=18,
    )
    story: list[Any] = []
    if title:
        story.append(Paragraph(escape(title), heading))
        story.append(Spacer(1, 6 * mm))
    for raw_line in _clean_breaks(text).splitlines():
        line = raw_line.strip()
        if not line:
            story.append(Spacer(1, 3 * mm))
            continue
        match = _MD_HEADING.match(line)
        if match:
            story.append(Paragraph(escape(match.group(2)), heading))
        else:
            story.append(Paragraph(escape(line), body))
    document.build(story)
    return buffer.getvalue()


def pdf_page_count(data: bytes) -> int:
    from pypdf import PdfReader

    return len(PdfReader(io.BytesIO(data)).pages)


def build_pdf_parts(
    data: bytes, parts: Sequence[tuple[str, Sequence[int]]]
) -> list[tuple[str, bytes]]:
    """Copy page ranges into new PDFs.

    Pages are copied as objects, so embedded images, scans, vector graphics and
    the original layout are preserved - unlike :func:`build_pdf`, which
    re-typesets plain text and therefore drops everything visual.
    ``parts`` contains 1-based page numbers.
    """
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        raise ValueError("the PDF is password-protected")
    total = len(reader.pages)
    metadata = reader.metadata
    output: list[tuple[str, bytes]] = []
    for name, pages in parts:
        writer = PdfWriter()
        for number in pages:
            if number < 1 or number > total:
                raise ValueError(f"page {number} is outside 1..{total}")
            writer.add_page(reader.pages[number - 1])
        if metadata:
            try:
                writer.add_metadata(
                    {str(key): str(value) for key, value in metadata.items()}
                )
            except Exception:  # noqa: BLE001 - metadata is best effort
                logger.debug("Could not copy PDF metadata", exc_info=True)
        buffer = io.BytesIO()
        writer.write(buffer)
        output.append((name, buffer.getvalue()))
    return output


def build_pdf_page_edit(
    target: bytes,
    sources: dict[str, bytes],
    pages: Sequence[dict[str, Any]],
) -> bytes:
    """Rebuild a PDF from an explicit page list (reorder / rotate / delete / merge).

    Each item is ``{"node_id": None|"<other pdf id>", "page": 1-based, "rotate": 0|90|180|270}``.
    ``node_id=None`` references the target document; other ids must be supplied in
    ``sources``. Pages are copied as objects, so images and layout survive.
    """
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import NameObject, NumberObject

    readers: dict[str, PdfReader] = {"": PdfReader(io.BytesIO(target))}
    try:
        for node_id, data in sources.items():
            readers[str(node_id)] = PdfReader(io.BytesIO(data))
        writer = PdfWriter()
        for item in pages:
            source = str(item.get("node_id") or "")
            reader = readers.get(source)
            if reader is None:
                raise ValueError(f"Unknown source PDF: {source or 'target'}")
            try:
                index = int(item.get("page") or 0) - 1
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid page number") from exc
            if index < 0 or index >= len(reader.pages):
                raise ValueError(f"Page {index + 1} is outside 1..{len(reader.pages)}")
            page = reader.pages[index]
            angle = int(item.get("rotate") or 0) % 360
            if angle and angle % 90:
                raise ValueError("Rotation must be a multiple of 90 degrees")
            writer.add_page(page)
            if angle:
                # Rotate the copy inside the writer: mutating the source page would
                # leak the rotation into later references to the same source page.
                written = writer.pages[-1]
                current = int(written.get("/Rotate", 0) or 0)
                written[NameObject("/Rotate")] = NumberObject((current + angle) % 360)
        if len(writer.pages) == 0:
            raise ValueError("The result would have no pages")
        metadata = readers[""].metadata
        if metadata:
            try:
                writer.add_metadata({str(key): str(value) for key, value in metadata.items()})
            except Exception:  # noqa: BLE001 - metadata is best effort
                logger.debug("Could not copy PDF metadata", exc_info=True)
        buffer = io.BytesIO()
        writer.write(buffer)
        return buffer.getvalue()
    finally:
        for reader in readers.values():
            try:
                reader.close()
            except Exception:  # noqa: BLE001
                pass


def apply_sheet_edits(data: bytes, cells: Sequence[dict[str, Any]]) -> bytes:
    """Write cell values into an .xlsx workbook and return the new bytes.

    ``cells`` = [{"sheet": "Sheet1", "cell": "B3", "value": "text"}]. Formulas are
    kept when the value starts with ``=``; numeric strings become numbers.
    """
    from openpyxl import load_workbook
    from openpyxl.utils.cell import coordinate_from_string

    workbook = load_workbook(io.BytesIO(data))
    try:
        for item in cells:
            sheet_name = str(item.get("sheet") or "").strip()
            cell_ref = str(item.get("cell") or "").strip().upper()
            if not sheet_name or not cell_ref:
                raise ValueError("sheet and cell are required")
            try:
                coordinate_from_string(cell_ref)
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"Invalid cell reference: {cell_ref}") from exc
            worksheet = (
                workbook[sheet_name]
                if sheet_name in workbook.sheetnames
                else workbook.create_sheet(title=sheet_name[:31])
            )
            worksheet[cell_ref] = _coerce_cell(item.get("value"))
        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()
    finally:
        workbook.close()


def _coerce_cell(value: Any) -> Any:
    if value is None:
        return None
    text = _clean_breaks(value)
    if not text.strip():
        return None
    if text.startswith("="):
        return text
    if text.isdigit() and len(text) > 1 and text.startswith("0"):
        # Keep identifiers such as 007 or 00123 as text.
        return text
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def build_text_document(name: str, text: str) -> bytes:
    """Generate bytes for a file name, choosing the right generator by extension."""
    from pathlib import Path

    ext = Path(name).suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        spec = parse_xlsx_text(text)
        if spec is not None:
            return build_xlsx(spec.model_dump()["sheets"])
        return build_xlsx({"Sheet1": _rows_from_text(text)})
    if ext == ".docx":
        return build_docx(text)
    if ext == ".pdf":
        if settings.office_conversion_available:
            try:
                from app.services import office_convert

                docx_bytes = build_docx(text)
                return office_convert.convert_document(
                    docx_bytes, "docx", target="pdf", title=Path(name).stem
                )
            except Exception as exc:  # noqa: BLE001 - fall back to reportlab
                logger.warning(
                    "OnlyOffice PDF conversion failed, using reportlab: %s", exc
                )
        return build_pdf(text)
    if ext == ".csv":
        return build_csv(_rows_from_text(text))
    return (text or "").encode("utf-8")


def _xlsx_json(text: str) -> Any | None:
    stripped = (text or "").lstrip()
    if not stripped.startswith("{"):
        return None
    try:
        payload = json.loads(stripped)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    sheets = payload.get("sheets")
    if isinstance(sheets, (list, dict)):
        return sheets
    if "rows" in payload:
        return [payload]
    return None


def _rows_from_text(text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        if "\t" in line:
            rows.append(line.split("\t"))
        elif "|" in line:
            rows.append([cell.strip() for cell in line.strip("|").split("|")])
        elif "," in line:
            rows.append([cell.strip() for cell in line.split(",")])
        else:
            rows.append([line.strip()])
    return rows
