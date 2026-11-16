"""Convert documents with the OnlyOffice Document Server.

The Document Server fetches the source through a short-lived signed URL served by
this API and returns the converted file (``async: false``). This keeps the heavy
Office rendering engine out of our image and gives PDFs real Word layout.
"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path

import httpx

from app.config import settings
from app.logging_config import get_logger
from app.services import office

logger = get_logger(__name__)

_SUPPORTED = {"docx", "xlsx", "xlsm", "pptx", "odt", "ods", "odp", "csv", "rtf"}
_NAME_RE = re.compile(r"^[a-f0-9]{32}\.(docx|xlsx|xlsm|pptx|odt|ods|odp|csv|rtf)$")
_MAGIC = {
    "pdf": b"%PDF",
    "xlsx": b"PK\x03\x04",
    "docx": b"PK\x03\x04",
    "pptx": b"PK\x03\x04",
}
_CACHE_AGE_SECONDS = 3600


class OfficeConversionError(Exception):
    """Raised when the Document Server cannot convert a document."""


def _directory() -> Path:
    path = Path(settings.workspace_dir) / "tmp" / "convert"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cleanup_stale() -> None:
    cutoff = time.time() - _CACHE_AGE_SECONDS
    for entry in _directory().iterdir():
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not remove stale conversion file %s", entry, exc_info=True)


def stage_source(data: bytes, suffix: str) -> str:
    """Write the source to a temp file and return a signed fetch token."""
    suffix = (suffix or "").lower().lstrip(".")
    if suffix not in _SUPPORTED:
        raise OfficeConversionError(f"Conversion from .{suffix or '?'} is not supported")
    _cleanup_stale()
    name = f"{uuid.uuid4().hex}.{suffix}"
    (_directory() / name).write_bytes(data)
    return office.sign_link(name, "cv", 900)


def source_path(name: str) -> Path:
    """Resolve a staged conversion source, rejecting traversal and stale names."""
    if not _NAME_RE.match(name or ""):
        raise OfficeConversionError("Invalid conversion source")
    directory = _directory().resolve()
    path = (directory / name).resolve()
    if path.parent != directory or not path.exists():
        raise OfficeConversionError("Conversion source has expired")
    return path


def convert_document(
    data: bytes, source_ext: str, *, target: str = "pdf", title: str | None = None
) -> bytes:
    """Convert ``data`` from ``source_ext`` to ``target`` using OnlyOffice."""
    if not settings.office_conversion_available:
        raise OfficeConversionError("OnlyOffice conversion is not configured")
    source = (source_ext or "").lower().lstrip(".")
    target = (target or "pdf").lower().lstrip(".")
    token = stage_source(data, source)
    url = (
        f"{settings.onlyoffice_api_url.rstrip('/')}/api/office/convert-source/{token}"
    )
    body = {
        "async": False,
        "filetype": source,
        "outputtype": target,
        "key": uuid.uuid4().hex,
        "title": title or f"document.{source}",
        "url": url,
    }
    body["token"] = office.encode_jwt(body)
    endpoint = f"{settings.onlyoffice_internal_url.rstrip('/')}/converter"
    try:
        response = httpx.post(endpoint, json=body, timeout=settings.onlyoffice_convert_timeout)
    except httpx.HTTPError as exc:
        raise OfficeConversionError(f"Document Server is unreachable: {exc}") from exc
    if response.status_code != 200:
        raise OfficeConversionError(
            f"Document Server returned HTTP {response.status_code}"
        )

    content = response.content or b""
    magic = _MAGIC.get(target)
    if magic and content.startswith(magic):
        return content

    error: object = None
    payload: object = None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        file_url = payload.get("fileUrl")
        if isinstance(file_url, str) and file_url:
            try:
                converted = httpx.get(
                    file_url, timeout=settings.onlyoffice_convert_timeout
                )
            except httpx.HTTPError as exc:
                raise OfficeConversionError(
                    f"Could not download the converted file: {exc}"
                ) from exc
            if converted.status_code == 200 and (
                not magic or converted.content.startswith(magic)
            ):
                return converted.content
    raise OfficeConversionError(f"OnlyOffice conversion failed (error={error})")
