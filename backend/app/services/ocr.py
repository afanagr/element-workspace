"""PDF page OCR: rasterise with PDFium and recognise the page with Tesseract.

Only pages without a usable text layer are rendered, so searchable PDFs keep
their original extraction. OCR results are cached by content hash in the shared
workspace volume: indexing and the manual "recognise" action reuse each other.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from app.config import settings
from app.logging_config import get_logger

logger = get_logger(__name__)

_CACHE_RE = re.compile(r"^[a-f0-9]{64}$")


class OcrUnavailable(Exception):
    """Raised when Tesseract or its language data is missing."""


def languages() -> str:
    return (settings.ocr_lang or settings.tesseract_lang or "eng").strip()


def ensure_tesseract() -> Any:
    """Import pytesseract, point it at the configured binary and check it exists."""
    try:
        import pytesseract
    except ImportError as exc:  # pragma: no cover - the image always installs it
        raise OcrUnavailable("pytesseract is not installed") from exc
    binary = settings.tesseract_cmd or "tesseract"
    if settings.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd
    if shutil.which(binary) is None:
        raise OcrUnavailable("Tesseract OCR binary is not available in the container")
    return pytesseract


def preprocess(image: Any) -> Any:
    """Grayscale, contrast and small-page upscale before recognition."""
    from PIL import Image, ImageOps

    if image.mode not in ("L", "RGB"):
        image = image.convert("RGB")
    prepared = ImageOps.autocontrast(image.convert("L"))
    width, height = prepared.size
    if 0 < width < 1600:
        factor = min(2.0, 2000.0 / width)
        prepared = prepared.resize(
            (int(width * factor), int(height * factor)), Image.LANCZOS
        )
    return prepared


def _deskew(image: Any) -> tuple[Any, int]:
    """Rotate pages that Tesseract's orientation detector considers sideways."""
    if not settings.ocr_deskew:
        return image, 0
    pytesseract = ensure_tesseract()
    try:
        osd = pytesseract.image_to_osd(image, output_type=pytesseract.Output.DICT)
    except Exception:  # noqa: BLE001 - OSD fails on sparse/blank pages
        return image, 0
    try:
        rotation = int(osd.get("rotate") or 0)
        confidence = float(osd.get("orientation_conf") or 0)
    except (TypeError, ValueError):
        return image, 0
    if rotation in (90, 180, 270) and confidence >= settings.ocr_min_orientation_confidence:
        return image.rotate(-rotation, expand=True), rotation
    return image, 0


class PdfPageOcr:
    """Renders selected pages of one PDF document and OCRs them on demand."""

    def __init__(self, data: bytes) -> None:
        try:
            import pypdfium2 as pdfium
        except ImportError as exc:  # pragma: no cover - installed by the image
            raise OcrUnavailable("pypdfium2 is not installed") from exc
        self._document = pdfium.PdfDocument(data)
        self.pages = len(self._document)

    def render(self, index: int) -> Any:
        page = self._document[index]
        try:
            scale = max(1.0, settings.ocr_dpi / 72.0)
            bitmap = page.render(scale=scale)
            try:
                return bitmap.to_pil()
            finally:
                bitmap.close()
        finally:
            page.close()

    def recognize(self, index: int) -> tuple[str, dict[str, Any]]:
        pytesseract = ensure_tesseract()
        image = preprocess(self.render(index))
        image, rotation = _deskew(image)
        started = time.monotonic()
        try:
            text = pytesseract.image_to_string(
                image,
                lang=languages(),
                config="--oem 3 --psm 3",
                timeout=settings.ocr_timeout_seconds,
            )
        except RuntimeError as exc:  # pytesseract raises this on timeout
            logger.warning("OCR timed out on page %s: %s", index + 1, exc)
            return "", {"error": "timeout"}
        finally:
            image.close()
        return text.strip(), {
            "rotation": rotation,
            "seconds": round(time.monotonic() - started, 2),
        }

    def close(self) -> None:
        self._document.close()


# --------------------------------------------------------------------- cache
def cache_path(sha256: str) -> Path | None:
    if not _CACHE_RE.match(sha256 or ""):
        return None
    directory = Path(settings.workspace_dir) / "ocr"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{sha256}.json"


def load_cached(sha256: str) -> dict[str, Any] | None:
    path = cache_path(sha256)
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:  # noqa: BLE001 - a corrupt cache entry must not break indexing
        logger.warning("Ignoring corrupt OCR cache entry %s", path.name)
        return None


def store_cached(sha256: str, payload: dict[str, Any]) -> None:
    path = cache_path(sha256)
    if path is None:
        return
    temp = path.with_suffix(".part")
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        os.replace(temp, path)
    except Exception:  # noqa: BLE001 - caching is best effort
        logger.warning("Could not write OCR cache for %s", sha256[:12])
        temp.unlink(missing_ok=True)
