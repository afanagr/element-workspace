"""Page OCR: rasterise PDFs with PDFium and recognise them with Tesseract.

Text quality comes from four things, all tunable through settings:

1. preprocessing - grayscale, upscaling of small scans, median denoise, Otsu
   binarisation and a white border (Tesseract needs margins around the text);
2. orientation - OSD fixes 90/180/270 rotations, a projection-profile pass
   corrects the small skew (1-5 degrees) that OSD cannot see;
3. multi-PSM recognition - several page segmentation modes are tried and the
   result with the highest mean word confidence wins;
4. a settings-aware cache, so tuning the options above re-recognises documents
   instead of silently reusing stale text.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Iterable

from app.config import settings
from app.logging_config import get_logger

logger = get_logger(__name__)

_CACHE_RE = re.compile(r"^[a-f0-9]{64}$")


class OcrUnavailable(Exception):
    """Raised when Tesseract or its language data is missing."""


def languages() -> str:
    return (settings.ocr_lang or settings.tesseract_lang or "eng").strip() or "eng"


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


def available_languages() -> list[str]:
    """Language data installed in the image (reported by /api/health)."""
    try:
        pytesseract = ensure_tesseract()
    except OcrUnavailable:
        return []
    try:
        return sorted(pytesseract.get_languages(config=""))
    except Exception:  # noqa: BLE001 - broken tessdata must not break health
        return []


# --------------------------------------------------------------- preprocessing
def otsu_threshold(image: Any) -> int:
    """Otsu's threshold from the 8-bit histogram (no numpy needed)."""
    histogram = image.histogram()
    total = sum(histogram)
    if total == 0:
        return 127
    sum_all = sum(index * count for index, count in enumerate(histogram))
    weight_background = 0
    sum_background = 0
    best_variance = -1.0
    threshold = 127
    for index, count in enumerate(histogram):
        weight_background += count
        if weight_background == 0:
            continue
        weight_foreground = total - weight_background
        if weight_foreground == 0:
            break
        sum_background += index * count
        mean_background = sum_background / weight_background
        mean_foreground = (sum_all - sum_background) / weight_foreground
        variance = weight_background * weight_foreground * (mean_background - mean_foreground) ** 2
        if variance > best_variance:
            best_variance = variance
            threshold = index
    return threshold


def prepare_image(image: Any) -> tuple[Any, dict[str, Any]]:
    """Clean a page before recognition and return it with the settings used."""
    from PIL import Image, ImageFilter, ImageOps

    if image.mode not in ("L", "RGB"):
        image = image.convert("RGB")
    prepared = ImageOps.autocontrast(image.convert("L"))
    info: dict[str, Any] = {"source_size": list(prepared.size)}

    width, height = prepared.size
    if 0 < width < 1600:
        factor = min(2.0, 2000.0 / width)
        resized = prepared.resize(
            (int(width * factor), int(height * factor)), Image.LANCZOS
        )
        prepared.close()
        prepared = resized
        info["upscale"] = round(factor, 2)

    if settings.ocr_denoise:
        denoised = prepared.filter(ImageFilter.MedianFilter(size=3))
        prepared.close()
        prepared = denoised

    if settings.ocr_binarize:
        threshold = otsu_threshold(prepared)
        binarized = prepared.point(
            lambda value, limit=threshold: 255 if value > limit else 0, mode="L"
        )
        prepared.close()
        prepared = binarized
        info["threshold"] = threshold

    border = max(0, int(settings.ocr_border_px))
    if border:
        canvas = Image.new("L", (prepared.width + 2 * border, prepared.height + 2 * border), 255)
        canvas.paste(prepared, (border, border))
        prepared.close()
        prepared = canvas
        info["border"] = border

    info["size"] = list(prepared.size)
    return prepared, info


def estimate_skew(image: Any, max_angle: float) -> float:
    """Return the rotation in degrees that straightens the text lines.

    Horizontal text produces sharp peaks in the row-ink profile, so the angle
    with the highest squared row sums is the one where the lines are aligned.
    """
    if max_angle <= 0:
        return 0.0
    import numpy as np
    from PIL import Image

    sample = image
    if sample.width > 900:
        ratio = 900 / sample.width
        sample = image.resize((900, max(1, int(sample.height * ratio))), Image.BILINEAR)
    best_angle = 0.0
    best_score = -1.0
    try:
        for step in range(int(-max_angle * 2), int(max_angle * 2) + 1):
            angle = step / 2.0
            rotated = sample.rotate(angle, resample=Image.BILINEAR, fillcolor=255, expand=False)
            try:
                ink = (np.asarray(rotated, dtype=np.uint8) < 128).sum(axis=1).astype(np.float64)
            finally:
                rotated.close()
            score = float((ink * ink).sum())
            if score > best_score:
                best_score, best_angle = score, angle
    finally:
        if sample is not image:
            sample.close()
    return best_angle


def _orient(image: Any) -> tuple[Any, int]:
    """Apply the OSD rotation and the fine skew correction (may return a new image)."""
    from PIL import Image

    rotated = image
    rotation = 0
    if settings.ocr_deskew:
        pytesseract = ensure_tesseract()
        try:
            osd = pytesseract.image_to_osd(image, output_type=pytesseract.Output.DICT)
            angle = int(osd.get("rotate") or 0)
            confidence = float(osd.get("orientation_conf") or 0)
            if angle in (90, 180, 270) and confidence >= settings.ocr_min_orientation_confidence:
                rotated = image.rotate(-angle, expand=True, fillcolor=255)
                rotation = angle
        except Exception:  # noqa: BLE001 - OSD fails on sparse or blank pages
            rotated = image

    if settings.ocr_deskew_angle > 0:
        skew = estimate_skew(rotated, settings.ocr_deskew_angle)
        if abs(skew) >= 0.5:
            corrected = rotated.rotate(skew, resample=Image.BILINEAR, fillcolor=255, expand=True)
            if rotated is not image:
                rotated.close()
            rotated = corrected
    return rotated, rotation


# ----------------------------------------------------------------- recognition
def words_to_text(data: dict[str, list[Any]]) -> tuple[str, float, int]:
    """Rebuild text lines from ``image_to_data`` and average the word confidence."""
    lines: dict[tuple[int, int, int], list[str]] = {}
    confidences: list[float] = []
    texts = data.get("text") or []
    for index, raw in enumerate(texts):
        word = str(raw or "").strip()
        if not word:
            continue
        try:
            confidence = float(data["conf"][index])
            key = (
                int(data["block_num"][index]),
                int(data["par_num"][index]),
                int(data["line_num"][index]),
            )
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if confidence < 0:
            continue
        lines.setdefault(key, []).append(word)
        confidences.append(confidence)
    text = "\n".join(" ".join(words) for _, words in sorted(lines.items()))
    mean = sum(confidences) / len(confidences) if confidences else 0.0
    return text, mean, len(confidences)


def _tesseract_config(psm: int) -> str:
    return f"--oem {settings.ocr_oem} --psm {psm} -c preserve_interword_spaces=1"


def _run_tesseract(pytesseract: Any, image: Any, lang: str, psm: int) -> dict[str, Any] | None:
    try:
        data = pytesseract.image_to_data(
            image,
            lang=lang,
            config=_tesseract_config(psm),
            output_type=pytesseract.Output.DICT,
            timeout=settings.ocr_timeout_seconds,
        )
    except RuntimeError as exc:  # pytesseract raises this on timeout
        logger.warning("OCR timed out (psm %s, lang %s): %s", psm, lang, exc)
        return None
    except Exception as exc:  # noqa: BLE001 - TesseractError on missing language data
        logger.warning("OCR failed (psm %s, lang %s): %s", psm, lang, exc)
        return None
    text, confidence, words = words_to_text(data)
    if not text.strip():
        return None
    return {"text": text.strip(), "confidence": confidence, "psm": psm, "words": words}


def _best_attempt(attempts: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for item in attempts:
        if best is None or (item["confidence"], len(item["text"])) > (
            best["confidence"],
            len(best["text"]),
        ):
            best = item
    return best


def recognize_image(
    image: Any,
    *,
    langs: str | None = None,
    psm_list: list[int] | None = None,
) -> tuple[str, dict[str, Any]]:
    """OCR one page/image and return the text plus quality metadata."""
    pytesseract = ensure_tesseract()
    primary = (langs or languages()).strip() or "eng"
    prepared, prep = prepare_image(image)
    started = time.monotonic()
    attempts: list[dict[str, Any]] = []
    rotation = 0
    try:
        oriented, rotation = _orient(prepared)
        try:
            for psm in psm_list or settings.ocr_psm_list:
                result = _run_tesseract(pytesseract, oriented, primary, psm)
                if result is not None:
                    attempts.append(result)
            best = _best_attempt(attempts)
            if best is None:
                return "", {"error": "no text", "preprocess": prep, "rotation": rotation}
            if (
                best["confidence"] < settings.ocr_min_confidence
                and "eng" not in [part.strip() for part in primary.lower().split("+")]
            ):
                retry = _run_tesseract(pytesseract, oriented, "eng", best["psm"])
                if retry is not None and retry["confidence"] > best["confidence"]:
                    retry["language"] = "eng"
                    attempts.append(retry)
                    best = retry
        finally:
            if oriented is not prepared:
                oriented.close()
    finally:
        prepared.close()

    return best["text"], {
        "confidence": round(best["confidence"], 1),
        "psm": best["psm"],
        "words": best["words"],
        "rotation": rotation,
        "language": best.get("language", primary),
        "seconds": round(time.monotonic() - started, 2),
        "preprocess": prep,
        "attempts": [
            {"psm": item["psm"], "confidence": round(item["confidence"], 1)}
            for item in attempts
        ],
    }


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
        image = self.render(index)
        try:
            return recognize_image(image)
        finally:
            image.close()

    def close(self) -> None:
        self._document.close()


# --------------------------------------------------------------------- cache
def cache_key(sha256: str) -> str | None:
    """Cache key of a document; changes whenever an OCR setting changes."""
    if not _CACHE_RE.match(sha256 or ""):
        return None
    fingerprint = "|".join(
        str(part)
        for part in (
            settings.ocr_cache_version,
            settings.ocr_dpi,
            languages(),
            settings.ocr_oem,
            settings.ocr_psm,
            settings.ocr_binarize,
            settings.ocr_denoise,
            settings.ocr_border_px,
            settings.ocr_deskew,
            settings.ocr_min_orientation_confidence,
            settings.ocr_deskew_angle,
            settings.ocr_min_confidence,
        )
    )
    return hashlib.sha256(f"{sha256}:{fingerprint}".encode()).hexdigest()


def cache_path(key: str) -> Path | None:
    if not _CACHE_RE.match(key or ""):
        return None
    directory = Path(settings.workspace_dir) / "ocr"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{key}.json"


def load_cached(key: str) -> dict[str, Any] | None:
    path = cache_path(key)
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:  # noqa: BLE001 - a corrupt cache entry must not break indexing
        logger.warning("Ignoring corrupt OCR cache entry %s", path.name)
        return None


def store_cached(key: str, payload: dict[str, Any]) -> None:
    path = cache_path(key)
    if path is None:
        return
    temp = path.with_suffix(".part")
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        os.replace(temp, path)
    except Exception:  # noqa: BLE001 - caching is best effort
        logger.warning("Could not write OCR cache for %s", key[:12])
        temp.unlink(missing_ok=True)
