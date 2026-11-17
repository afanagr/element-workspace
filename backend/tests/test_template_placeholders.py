"""Placeholder detection and template creation edge cases."""

from __future__ import annotations

import io

import pytest
from docx import Document

from app.services.templates import TemplateError, _apply_docx_replacements, detect_fields


def _docx(build) -> bytes:
    document = Document()
    build(document)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _names(data: bytes) -> list[str]:
    fields, _config = detect_fields("docx", data)
    return [str(field["name"]) for field in fields]


def test_detects_ascii_placeholders() -> None:
    data = _docx(lambda document: document.add_paragraph("Договор № {{ number }} от {{ date }}"))
    assert sorted(_names(data)) == ["date", "number"]


def test_detects_cyrillic_placeholders() -> None:
    data = _docx(lambda document: document.add_paragraph("{{ номер }} / {{ дата }}"))
    assert sorted(_names(data)) == ["дата", "номер"]


def test_detects_placeholders_split_across_word_runs() -> None:
    def build(document) -> None:
        paragraph = document.add_paragraph("Договор № ")
        paragraph.add_run("{{ nu")
        paragraph.add_run("mber }}")

    assert _names(_docx(build)) == ["number"]


def test_placeholder_with_space_is_rejected() -> None:
    data = _docx(lambda document: document.add_paragraph("{{ full name }}"))
    with pytest.raises(TemplateError) as error:
        detect_fields("docx", data)
    assert "full_name" in error.value.message


def test_single_braces_are_not_placeholders() -> None:
    data = _docx(lambda document: document.add_paragraph("{ number }"))
    assert _names(data) == []


def test_replacements_create_placeholders() -> None:
    data = _docx(lambda document: document.add_paragraph("Договор № 123-А"))
    prepared = _apply_docx_replacements(data, [{"find": "123-А", "field": "number"}])
    assert "number" in _names(prepared)


def test_replacements_report_text_that_is_not_in_the_document() -> None:
    data = _docx(lambda document: document.add_paragraph("Договор № 123-А"))
    with pytest.raises(TemplateError) as error:
        _apply_docx_replacements(data, [{"find": "нет такого текста", "field": "number"}])
    assert "not found" in error.value.message
