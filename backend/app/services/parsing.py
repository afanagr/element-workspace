"""Document parsing for indexing and text fallbacks.

Excel -> cell text, Word/PDF -> paragraph text, images -> Tesseract OCR,
plain text/code/Markdown/JSON -> decoded text. Legacy .doc uses antiword/catdoc.
"""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import io
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from app.config import settings
from app.logging_config import get_logger
from app.services.ocr import (
    OcrUnavailable,
    PdfPageOcr,
    ensure_tesseract,
    languages as ocr_languages,
    load_cached,
    store_cached,
)
from app.services.storage import decode_bytes

logger = get_logger(__name__)

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".mdx", ".rst", ".adoc", ".asciidoc", ".org", ".log",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".properties",
    ".env", ".xml", ".html", ".htm", ".css", ".scss", ".sass", ".less", ".js", ".mjs",
    ".cjs", ".ts", ".tsx", ".jsx", ".py", ".rb", ".go", ".rs", ".java", ".kt", ".kts",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".php", ".sh", ".bash", ".zsh", ".fish",
    ".ps1", ".psm1", ".bat", ".cmd", ".sql", ".graphql", ".gql", ".vue", ".svelte",
    ".astro", ".tex", ".bib", ".sty", ".cls", ".lua", ".pl", ".pm", ".r", ".dart",
    ".swift", ".scala", ".clj", ".cljs", ".ex", ".exs", ".erl", ".hrl", ".hs", ".lhs",
    ".ml", ".mli", ".nim", ".zig", ".v", ".sv", ".vhd", ".asm", ".s", ".m", ".mm",
    ".f", ".f90", ".for", ".pas", ".dpr", ".vb", ".vbs", ".groovy", ".gradle", ".proto",
    ".tf", ".tfvars", ".hcl", ".sol", ".tsv", ".diff", ".patch", ".lock", ".gitignore",
    ".gitattributes", ".editorconfig", ".dockerignore", ".npmrc", ".nvmrc", ".prettierrc",
    ".eslintrc", ".babelrc", ".htaccess", ".mailmap",
}
_TEXT_FILENAMES = {
    "dockerfile", "makefile", "gnumakefile", "cmakelists.txt", "readme", "license",
    "changelog", "contributing", "authors", "notice", "procfile", "gemfile", "rakefile",
    "vagrantfile", "jenkinsfile", "justfile", "brewfile", "docker-compose",
}
_TEXT_FILENAME_PREFIXES = (
    "dockerfile", "makefile", "readme", "license", "changelog", "contributing", ".env",
)
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


def _is_text_name(name: str) -> bool:
    lower = (name or "").strip().lower()
    if lower in _TEXT_FILENAMES:
        return True
    return any(lower.startswith(prefix) for prefix in _TEXT_FILENAME_PREFIXES)


def is_text_bytes(data: bytes) -> bool:
    """Heuristic: the bytes look like a text file (no NULs, mostly printable)."""
    sample = data[:8192]
    if b"\x00" in sample:
        return False
    if not sample:
        return True
    try:
        text = decode_bytes(sample)
    except Exception:  # noqa: BLE001 - charset detection failed
        return False
    if not text:
        return False
    printable = sum(1 for char in text if char.isprintable() or char in "\n\r\t")
    return printable / len(text) > 0.9


def is_textual(name: str, data: bytes | None = None) -> bool:
    if extension_of(name) in TEXT_EXTENSIONS or _is_text_name(name):
        return True
    return data is not None and is_text_bytes(data)


def view_kind(name: str, mime: str | None = None) -> str:
    ext = extension_of(name)
    if ext in EXCEL_EXTENSIONS:
        return "excel"
    if ext in WORD_EXTENSIONS:
        return "word"
    if ext in PDF_EXTENSIONS:
        return "pdf"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in TEXT_EXTENSIONS or _is_text_name(name):
        return "text"
    if mime and (
        mime.startswith("text/")
        or mime in ("application/json", "application/xml", "application/x-yaml")
    ):
        return "text"
    return "binary"


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
def _cached_pdf_document(payload: dict[str, Any]) -> ParsedDocument:
    segments = [
        Segment(str(item.get("label") or "page"), str(item.get("text") or ""))
        for item in payload.get("segments") or []
    ]
    return ParsedDocument(
        "pdf",
        str(payload.get("text") or ""),
        segments,
        dict(payload.get("meta") or {}),
    )


def _pdf_cache_payload(document: ParsedDocument) -> dict[str, Any]:
    return {
        "text": document.text,
        "segments": [{"label": segment.label, "text": segment.text} for segment in document.segments],
        "meta": document.meta,
    }


def _meaningful_chars(text: str) -> int:
    """Letters/digits only: scans often carry a tiny junk header in the layer."""
    return sum(1 for char in text if char.isalnum())


def _parse_pdf(data: bytes, *, ocr: bool = False) -> ParsedDocument:
    import pdfplumber

    # A cached OCR result is reused by every caller (indexing, previews, agent
    # tools), so a scan does not need to be recognised twice.
    sha256 = hashlib.sha256(data).hexdigest()
    cached = load_cached(sha256)
    if cached is not None:
        return _cached_pdf_document(cached)

    segments: list[Segment] = []
    pages = 0
    ocr_pages = 0
    ocr_skipped = 0
    ocr_started = time.monotonic()
    recognizer: PdfPageOcr | None = None
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = len(pdf.pages)
            for index, page in enumerate(pdf.pages, start=1):
                page_text = page.extract_text() or ""
                used_ocr = False
                if (
                    ocr
                    and _meaningful_chars(page_text) < settings.ocr_min_page_chars
                ):
                    out_of_pages = ocr_pages >= settings.ocr_max_pages
                    out_of_time = (
                        time.monotonic() - ocr_started
                    ) >= settings.ocr_budget_seconds
                    if out_of_pages or out_of_time:
                        ocr_skipped += 1
                    else:
                        try:
                            if recognizer is None:
                                recognizer = PdfPageOcr(data)
                            ocr_text, info = recognizer.recognize(index - 1)
                        except OcrUnavailable:
                            raise
                        except Exception as exc:  # noqa: BLE001 - one bad page must not kill the document
                            logger.warning("OCR failed for page %s of the PDF: %s", index, exc)
                        else:
                            if ocr_text:
                                used_ocr = True
                                ocr_pages += 1
                                page_text = f"{page_text}\n{ocr_text}".strip() if page_text.strip() else ocr_text
                                logger.info(
                                    "OCR page %s/%s: %s chars in %ss",
                                    index,
                                    pages,
                                    len(ocr_text),
                                    info.get("seconds"),
                                )
                if page_text.strip():
                    label = f"page:{index}" + (" (ocr)" if used_ocr else "")
                    segments.append(Segment(label, page_text))
    finally:
        if recognizer is not None:
            recognizer.close()

    if ocr_skipped:
        logger.info(
            "OCR stopped after %s page(s): %s page(s) left for a later run",
            ocr_pages,
            ocr_skipped,
        )

    text = "\n\n".join(segment.text for segment in segments)
    document = ParsedDocument(
        "pdf",
        text,
        segments,
        {
            "pages": pages,
            "scanned": not segments,
            "needs_ocr": not segments,
            "ocr": ocr_pages > 0,
            "ocr_pages": ocr_pages,
            "ocr_skipped_pages": ocr_skipped,
            "ocr_engine": "tesseract" if ocr_pages else None,
            "ocr_lang": ocr_languages() if ocr_pages else None,
        },
    )
    if sha256 and ocr_pages:
        store_cached(sha256, _pdf_cache_payload(document))
    return document


# --------------------------------------------------------------------- image
def _parse_image(data: bytes) -> ParsedDocument:
    from PIL import Image

    pytesseract = ensure_tesseract()

    with Image.open(io.BytesIO(data)) as image:
        image.load()
        width, height = image.size
        mode = image.mode
        try:
            text = pytesseract.image_to_string(image, lang=ocr_languages())
        except pytesseract.TesseractError:
            logger.warning("OCR with lang=%s failed, retrying with eng", ocr_languages())
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
def parse_document(name: str, data: bytes, ocr: bool = False) -> ParsedDocument:
    """Parse a file into text segments. Blocking - call from a worker thread.

    ``ocr=True`` additionally rasterises and recognises PDF pages that have no
    usable text layer; it is intentionally opt-in so preview endpoints stay fast.
    """
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
            return _parse_pdf(data, ocr=ocr)
        if ext in IMAGE_EXTENSIONS:
            return _parse_image(data)
        if ext in TEXT_EXTENSIONS:
            return _parse_text(data)
        if is_text_bytes(data):
            # Any file a text editor can open is indexed as plain text.
            return _parse_text(data)
    except (UnsupportedFormat, OcrUnavailable):
        raise
    except Exception as exc:  # noqa: BLE001 - corrupt archives / codec errors etc.
        logger.warning("Parsing %s failed: %s", name, exc)
        raise UnsupportedFormat(f"Could not parse {name}: {exc}") from exc
    raise UnsupportedFormat(f"Unsupported file type: {ext or 'unknown'}")


