"""Document parsing for indexing and text fallbacks.

Excel -> cell text, Word/PDF -> paragraph text, images -> Tesseract OCR,
plain text/code/Markdown/JSON -> decoded text. Legacy .doc uses antiword/catdoc.
"""

from __future__ import annotations

import csv
import dataclasses
import io
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from app.config import settings
from app.logging_config import get_logger
from app.services.storage import decode_bytes

logger = get_logger(__name__)

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".json", ".jsonl", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".env", ".xml", ".html", ".htm", ".css", ".scss", ".js", ".mjs",
    ".cjs", ".ts", ".tsx", ".jsx", ".py", ".rb", ".go", ".rs", ".java", ".kt", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".php", ".sh", ".bash", ".ps1", ".sql", ".graphql", ".vue", ".svelte",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".tiff", ".tif", ".bmp", ".gif"}
EXCEL_EXTENSIONS = {".xlsx", ".xlsm", ".xls", ".csv"}
WORD_EXTENSIONS = {".docx", ".doc"}
PDF_EXTENSIONS = {".pdf"}
SUPPORTED_EXTENSIONS = (
    TEXT_EXTENSIONS | IMAGE_EXTENSIONS | EXCEL_EXTENSIONS | WORD_EXTENSIONS | PDF_EXTENSIONS
)

MIME_BY_EXTENSION: dict[str, str] = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".json": "application/json",
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".xls": "application/vnd.ms-excel",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".bmp": "image/bmp",
    ".zip": "application/zip",
    ".html": "text/html",
    ".htm": "text/html",
    ".xml": "application/xml",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
    ".py": "text/x-python",
    ".js": "text/javascript",
    ".ts": "text/typescript",
    ".sql": "application/sql",
}


class UnsupportedFormat(Exception):
    """Raised when a file cannot be parsed into text."""


class OcrUnavailable(Exception):
    """Raised when Tesseract is missing from the image."""


@dataclasses.dataclass(slots=True)
class Segment:
    label: str
    text: str


@dataclasses.dataclass(slots=True)
class ParsedDocument:
    kind: str
    text: str
    segments: list[Segment]
    meta: dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


def extension_of(name: str) -> str:
    return Path(name).suffix.lower()


def detect_mime(name: str) -> str:
    return MIME_BY_EXTENSION.get(extension_of(name), "application/octet-stream")


def view_kind(name: str) -> str:
    ext = extension_of(name)
    if ext in EXCEL_EXTENSIONS:
        return "excel"
    if ext in WORD_EXTENSIONS:
        return "word"
    if ext in PDF_EXTENSIONS:
        return "pdf"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in TEXT_EXTENSIONS:
        return "text"
    return "binary"


def is_supported(name: str) -> bool:
    return extension_of(name) in SUPPORTED_EXTENSIONS


def _clean(value: Any) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    if len(text) > 500:
        text = text[:500] + "…"
    return text


def _format_rows(rows: Any, max_rows: int = 5000, max_cols: int = 80) -> list[str]:
    from openpyxl.utils import get_column_letter

    lines: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        if row_index > max_rows:
            lines.append(f"… truncated after {max_rows} rows")
            break
        cells: list[str] = []
        values = row if isinstance(row, (list, tuple)) else list(row)
        for col_index, value in enumerate(values[:max_cols]):
            if value is None or value == "":
                continue
            cells.append(f"{get_column_letter(col_index + 1)}{row_index}={_clean(value)}")
        if cells:
            lines.append(" | ".join(cells))
    return lines


# --------------------------------------------------------------------- excel
def _parse_xlsx(data: bytes) -> ParsedDocument:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    segments: list[Segment] = []
    try:
        for sheet in workbook.worksheets:
            lines = _format_rows(sheet.iter_rows(values_only=True))
            text = "\n".join(lines)
            if text.strip():
                segments.append(Segment(label=f"sheet:{sheet.title}", text=text))
        title_list = [sheet.title for sheet in workbook.worksheets]
    finally:
        workbook.close()
    text = "\n\n".join(segment.text for segment in segments)
    return ParsedDocument("excel", text, segments, {"sheets": title_list})


def _parse_xls(data: bytes) -> ParsedDocument:
    import pandas as pd

    frames = pd.read_excel(io.BytesIO(data), sheet_name=None, engine="xlrd", header=None)
    segments: list[Segment] = []
    for sheet_name, frame in frames.items():
        lines = _format_rows(frame.itertuples(index=False, name=None))
        text = "\n".join(lines)
        if text.strip():
            segments.append(Segment(label=f"sheet:{sheet_name}", text=text))
    text = "\n\n".join(segment.text for segment in segments)
    return ParsedDocument("excel", text, segments, {"sheets": list(frames.keys()), "legacy": True})


def _parse_csv(data: bytes) -> ParsedDocument:
    text = decode_bytes(data)
    try:
        dialect: Any = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = csv.reader(io.StringIO(text), dialect)
    lines = _format_rows(rows)
    body = "\n".join(lines)
    return ParsedDocument("excel", body, [Segment("csv", body)], {"delimiter": getattr(dialect, "delimiter", ",")})


# ---------------------------------------------------------------------- word
def _parse_docx(data: bytes) -> ParsedDocument:
    import docx

    document = docx.Document(io.BytesIO(data))
    blocks: list[str] = []
    for paragraph in document.paragraphs:
        if paragraph.text.strip():
            blocks.append(paragraph.text.strip())
    for table in document.tables:
        for row in table.rows:
            cells = [_clean(cell.text) for cell in row.cells if cell.text.strip()]
            if cells:
                blocks.append(" | ".join(cells))
    text = "\n".join(blocks)
    return ParsedDocument(
        "word",
        text,
        [Segment("document", text)] if text.strip() else [],
        {"paragraphs": len(document.paragraphs), "tables": len(document.tables)},
    )


def _parse_doc(data: bytes) -> ParsedDocument:
    """Legacy binary .doc via antiword/catdoc (installed in the backend image)."""
    extractors: list[tuple[str, list[str]]] = [
        ("antiword", ["antiword"]),
        ("catdoc", ["catdoc", "-w"]),
    ]
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".doc")
    path = handle.name
    try:
        handle.write(data)
        handle.close()
        for binary, command in extractors:
            if shutil.which(binary) is None:
                continue
            try:
                completed = subprocess.run(
                    [*command, path], capture_output=True, timeout=90, check=False
                )
            except (subprocess.SubprocessError, OSError) as exc:
                logger.warning("legacy .doc extraction with %s failed: %s", binary, exc)
                continue
            if completed.returncode == 0 and completed.stdout.strip():
                text = decode_bytes(completed.stdout)
                return ParsedDocument(
                    "word",
                    text,
                    [Segment("document", text)],
                    {"legacy": True, "extractor": binary},
                )
    finally:
        Path(path).unlink(missing_ok=True)
    raise UnsupportedFormat(
        "Cannot extract text from this legacy .doc file. Re-save it as .docx or PDF."
    )


# ----------------------------------------------------------------------- pdf
def _parse_pdf(data: bytes) -> ParsedDocument:
    import pdfplumber

    segments: list[Segment] = []
    pages = 0
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = len(pdf.pages)
        for index, page in enumerate(pdf.pages, start=1):
            page_text = page.extract_text() or ""
            if page_text.strip():
                segments.append(Segment(label=f"page:{index}", text=page_text))
    text = "\n\n".join(segment.text for segment in segments)
    return ParsedDocument(
        "pdf", text, segments, {"pages": pages, "scanned": not segments, "needs_ocr": not segments}
    )


# --------------------------------------------------------------------- image
def _parse_image(data: bytes) -> ParsedDocument:
    from PIL import Image

    try:
        import pytesseract
    except ImportError as exc:  # pragma: no cover - image always installs it
        raise OcrUnavailable("pytesseract is not installed") from exc

    binary = settings.tesseract_cmd or "tesseract"
    if settings.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd
    if shutil.which(binary) is None:
        raise OcrUnavailable("Tesseract OCR binary is not available in the container")

    with Image.open(io.BytesIO(data)) as image:
        image.load()
        width, height = image.size
        mode = image.mode
        try:
            text = pytesseract.image_to_string(image, lang=settings.tesseract_lang)
        except pytesseract.TesseractError:
            logger.warning("OCR with lang=%s failed, retrying with eng", settings.tesseract_lang)
            text = pytesseract.image_to_string(image, lang="eng")
    text = text.strip()
    return ParsedDocument(
        "image",
        text,
        [Segment("ocr", text)] if text else [],
        {"width": width, "height": height, "mode": mode, "ocr": True},
    )


# ---------------------------------------------------------------------- text
def _parse_text(data: bytes) -> ParsedDocument:
    if b"\x00" in data[:4096]:
        raise UnsupportedFormat("File looks like binary data, not text")
    text = decode_bytes(data)
    lines = text.splitlines()
    segments: list[Segment] = []
    if len(lines) > 400:
        block: list[str] = []
        for line in lines:
            block.append(line)
            if len(block) >= 200:
                segments.append(Segment(f"lines:{len(segments) * 200 + 1}+", "\n".join(block)))
                block = []
        if block:
            segments.append(Segment("tail", "\n".join(block)))
    else:
        segments.append(Segment("document", text))
    return ParsedDocument("text", text, segments, {"lines": len(lines), "chars": len(text)})


# --------------------------------------------------------------------- main
def parse_document(name: str, data: bytes) -> ParsedDocument:
    """Parse a file into text segments. Blocking - call from a worker thread."""
    ext = extension_of(name)
    try:
        if ext in (".xlsx", ".xlsm"):
            return _parse_xlsx(data)
        if ext == ".xls":
            return _parse_xls(data)
        if ext == ".csv":
            return _parse_csv(data)
        if ext == ".docx":
            return _parse_docx(data)
        if ext == ".doc":
            return _parse_doc(data)
        if ext == ".pdf":
            return _parse_pdf(data)
        if ext in IMAGE_EXTENSIONS:
            return _parse_image(data)
        if ext in TEXT_EXTENSIONS:
            return _parse_text(data)
    except (UnsupportedFormat, OcrUnavailable):
        raise
    except Exception as exc:  # noqa: BLE001 - corrupt archives / codec errors etc.
        logger.warning("Parsing %s failed: %s", name, exc)
        raise UnsupportedFormat(f"Could not parse {name}: {exc}") from exc
    raise UnsupportedFormat(f"Unsupported file type: {ext or 'unknown'}")


_tesseract_available: bool | None = None


def tesseract_available() -> bool:
    global _tesseract_available
    if _tesseract_available is None:
        _tesseract_available = shutil.which(settings.tesseract_cmd or "tesseract") is not None
    return _tesseract_available


