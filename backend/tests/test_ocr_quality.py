"""OCR quality helpers: preprocessing, skew, multi-PSM selection and cache keys.

Everything here runs without a real Tesseract binary: the engine is stubbed, so
the tests stay fast and deterministic in CI.
"""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from app.config import settings
from app.services import ocr as ocr_module
from app.services.ocr import (
    OcrUnavailable,
    _best_attempt,
    available_languages,
    cache_key,
    estimate_skew,
    otsu_threshold,
    prepare_image,
    recognize_image,
    words_to_text,
)
from app.services.parsing import _text_layer_usable


def _lines_image(angle: float = 0.0, size: tuple[int, int] = (600, 400)) -> Image.Image:
    image = Image.new("L", size, 255)
    draw = ImageDraw.Draw(image)
    for index in range(12):
        top = 20 + index * 30
        draw.rectangle([40, top, 520, top + 10], fill=0)
    if angle:
        image = image.rotate(angle, resample=Image.BILINEAR, fillcolor=255)
    return image


def _data(words: list[str], confidences: list[float]) -> dict[str, list[object]]:
    count = len(words)
    return {
        "text": list(words),
        "conf": list(confidences),
        "block_num": [1] * count,
        "par_num": [1] * count,
        "line_num": [1] * count,
    }


def test_otsu_threshold_splits_bimodal_histogram() -> None:
    image = Image.new("L", (10, 10), 255)
    for x in range(5):
        for y in range(10):
            image.putpixel((x, y), 0)
    threshold = otsu_threshold(image)
    assert 0 <= threshold < 255


def test_prepare_image_binarizes_and_adds_a_border() -> None:
    source = _lines_image()
    prepared, info = prepare_image(source)
    try:
        assert prepared.mode == "L"
        assert info["border"] == settings.ocr_border_px
        # Small pages are upscaled, so only the lower bound is guaranteed.
        assert prepared.width >= source.width + 2 * settings.ocr_border_px
        assert info["size"] == [prepared.width, prepared.height]
        assert set(prepared.getdata()) <= {0, 255}
    finally:
        prepared.close()
        source.close()


def test_estimate_skew_finds_the_correction_angle() -> None:
    skewed = _lines_image(angle=-4.0)
    try:
        angle = estimate_skew(skewed, 6.0)
    finally:
        skewed.close()
    assert angle == pytest.approx(4.0, abs=1.5)


def test_estimate_skew_is_disabled_by_zero() -> None:
    image = _lines_image()
    try:
        assert estimate_skew(image, 0.0) == 0.0
    finally:
        image.close()


def test_words_to_text_rebuilds_lines_and_confidence() -> None:
    data = {
        "text": ["Hello", "world", "", "second", "line"],
        "conf": [90, 80, -1, 70, 60],
        "block_num": [1, 1, 1, 1, 1],
        "par_num": [1, 1, 1, 1, 1],
        "line_num": [1, 1, 1, 2, 2],
    }
    text, confidence, words = words_to_text(data)
    assert text == "Hello world\nsecond line"
    assert confidence == pytest.approx(75.0)
    assert words == 4


def test_best_attempt_prefers_confidence_then_length() -> None:
    best = _best_attempt(
        [
            {"text": "a " * 50, "confidence": 40.0, "psm": 3},
            {"text": "short", "confidence": 90.0, "psm": 6},
        ]
    )
    assert best is not None and best["psm"] == 6


class _FakeTesseract:
    class Output:  # noqa: D106 - mimics pytesseract.Output
        DICT = "dict"

    def __init__(self, primary: dict, english: dict | None = None) -> None:
        self.primary = primary
        self.english = english or {}
        self.calls: list[tuple[int, str]] = []

    def image_to_osd(self, image, output_type=None):  # noqa: ANN001, ARG002
        return {"rotate": 0, "orientation_conf": 0}

    def image_to_data(  # noqa: ANN001
        self, image, lang=None, config=None, output_type=None, timeout=None
    ):
        psm = int(str(config).split("--psm ")[1].split()[0])
        self.calls.append((psm, str(lang)))
        table = self.english if lang == "eng" else self.primary
        return table[psm]


def _stub_engine(monkeypatch, engine: _FakeTesseract) -> None:
    monkeypatch.setattr(settings, "ocr_deskew", False)
    monkeypatch.setattr(settings, "ocr_deskew_angle", 0.0)
    monkeypatch.setattr(ocr_module, "ensure_tesseract", lambda: engine)


def test_recognize_image_picks_the_most_confident_psm(monkeypatch) -> None:
    engine = _FakeTesseract(
        {
            3: _data(["bad", "text"], [30, 40]),
            6: _data(["good", "text"], [95, 90]),
        }
    )
    _stub_engine(monkeypatch, engine)
    monkeypatch.setattr(settings, "ocr_psm", "3,6")
    image = _lines_image()
    try:
        text, info = recognize_image(image)
    finally:
        image.close()
    assert text == "good text"
    assert info["psm"] == 6
    assert info["confidence"] == pytest.approx(92.5)
    assert [attempt["psm"] for attempt in info["attempts"]] == [3, 6]


def test_recognize_image_retries_with_english_when_confidence_is_low(monkeypatch) -> None:
    engine = _FakeTesseract(
        {3: _data(["ocr", "noise"], [30, 20])},
        english={3: _data(["clean", "english"], [90, 85])},
    )
    _stub_engine(monkeypatch, engine)
    monkeypatch.setattr(settings, "ocr_psm", "3")
    monkeypatch.setattr(settings, "ocr_min_confidence", 55.0)
    image = _lines_image()
    try:
        text, info = recognize_image(image, langs="rus")
    finally:
        image.close()
    assert text == "clean english"
    assert info["language"] == "eng"


def test_cache_key_depends_on_the_settings(monkeypatch) -> None:
    digest = "a" * 64
    first = cache_key(digest)
    assert first is not None
    monkeypatch.setattr(settings, "ocr_dpi", 400)
    assert cache_key(digest) != first
    assert cache_key("not-a-hash") is None


def test_available_languages_is_empty_without_tesseract(monkeypatch) -> None:
    def _raise() -> None:
        raise OcrUnavailable("missing")

    monkeypatch.setattr(ocr_module, "ensure_tesseract", _raise)
    assert available_languages() == []


def test_text_layer_usable_rejects_junk() -> None:
    assert _text_layer_usable("") is False
    assert _text_layer_usable("ok") is False
    assert _text_layer_usable("Hello world, this is a real page. " * 5) is True
    assert _text_layer_usable("?" * 200) is False
    assert _text_layer_usable("\ufffd" * 200) is False
