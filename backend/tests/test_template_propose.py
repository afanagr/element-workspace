"""Automation of template creation: placeholder suggestions and split-run edits."""

from __future__ import annotations

import io

from docx import Document

from app.services.templates import (
    _apply_docx_replacements,
    _document_text,
    detect_fields,
    propose_replacements,
)


def _docx(paragraphs: list[str]) -> bytes:
    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _contract() -> bytes:
    return _docx(
        [
            "Договор поставки № 12/2026 от 01.12.2026",
            "Поставщик: ООО «Ромашка», ИНН 7701234567, КПП 770101001",
            "Сумма: 250 000,00 руб.",
            "Тел: +7 999 123-45-67, e-mail: sales@romashka.ru",
            "Подписант: Иванов И.И.",
        ]
    )


def test_propose_finds_typical_variable_values() -> None:
    result = propose_replacements("docx", _contract())
    found = {item["field"]: item["find"] for item in result["suggestions"]}
    assert found["contract_number"] == "12/2026"
    assert found["date"] == "01.12.2026"
    assert found["organization"] == "ООО «Ромашка»"
    assert found["inn"] == "7701234567"
    assert found["kpp"] == "770101001"
    assert found["amount"] == "250 000,00 руб."
    assert found["phone"] == "+7 999 123-45-67"
    assert found["email"] == "sales@romashka.ru"
    assert found["full_name"] == "Иванов И.И."


def test_propose_skips_existing_placeholders() -> None:
    data = _docx(["Уже шаблон: {{ existing_field }}, дата 02.02.2026"])
    result = propose_replacements("docx", data)
    finds = [item["find"] for item in result["suggestions"]]
    assert "existing_field" not in finds
    assert "02.02.2026" in finds


def test_propose_names_duplicate_values_apart() -> None:
    data = _docx(["Дата 01.12.2026", "Дата 02.12.2026"])
    result = propose_replacements("docx", data)
    assert [item["field"] for item in result["suggestions"]] == ["date", "date_2"]


def test_document_text_separates_paragraphs() -> None:
    text = _document_text("docx", _docx(["Первая строка", "Вторая строка"]))
    assert text == "Первая строка\nВторая строка"


def test_replacements_work_when_word_split_the_phrase() -> None:
    document = Document()
    paragraph = document.add_paragraph()
    paragraph.add_run("Поставщик: ООО Ро")
    paragraph.add_run("машка")
    buffer = io.BytesIO()
    document.save(buffer)

    prepared = _apply_docx_replacements(buffer.getvalue(), [{"find": "ООО Ромашка", "field": "tenant"}])
    fields, _config = detect_fields("docx", prepared)
    assert [field["name"] for field in fields] == ["tenant"]
