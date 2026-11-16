"""Document generation: xlsx / docx / pdf / csv from AI tool calls."""

from __future__ import annotations

import io
import re
from typing import Any, Sequence

from app.logging_config import get_logger

logger = get_logger(__name__)

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_MD_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")


def build_xlsx(sheets: dict[str, list[list[Any]]]) -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    default = workbook.active
    workbook.remove(default)
    if not sheets:
        sheets = {"Sheet1": []}
    for name, rows in sheets.items():
        sheet = workbook.create_sheet(title=_safe_sheet_name(name))
        for row in rows or []:
            sheet.append([_cell(value) for value in row])
        if rows:
            for column_cells in sheet.columns:
                width = max((len(str(cell.value)) for cell in column_cells if cell.value is not None), default=8)
                sheet.column_dimensions[column_cells[0].column_letter].width = min(max(10, width + 2), 60)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _safe_sheet_name(name: str) -> str:
    cleaned = re.sub(r"[\[\]:*?/\\]", "_", str(name or "Sheet"))[:31].strip() or "Sheet"
    return cleaned


def _cell(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple, dict)):
        import json

        return json.dumps(value, ensure_ascii=False)
    return str(value)


def build_csv(rows: Sequence[Sequence[Any]]) -> bytes:
    import csv

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for row in rows or []:
        writer.writerow([_cell(value) for value in row])
    return buffer.getvalue().encode("utf-8")


def build_docx(text: str, *, title: str | None = None) -> bytes:
    import docx
    from docx.shared import Pt

    document = docx.Document()
    if title:
        document.add_heading(title, level=0)
    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        heading = _MD_HEADING.match(line.strip())
        if heading:
            document.add_heading(heading.group(2), level=min(len(heading.group(1)) + 1, 9))
            continue
        bullet = _MD_BULLET.match(line)
        if bullet:
            document.add_paragraph(bullet.group(1), style="List Bullet")
            continue
        numbered = _MD_NUMBERED.match(line)
        if numbered:
            document.add_paragraph(numbered.group(1), style="List Number")
            continue
        paragraph = document.add_paragraph(line)
        paragraph.paragraph_format.space_after = Pt(4)
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
    for raw_line in (text or "").splitlines():
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
    text = str(value)
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
        return build_xlsx({"Sheet1": _rows_from_text(text)})
    if ext == ".docx":
        return build_docx(text)
    if ext == ".pdf":
        return build_pdf(text)
    if ext == ".csv":
        return build_csv(_rows_from_text(text))
    return (text or "").encode("utf-8")


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
