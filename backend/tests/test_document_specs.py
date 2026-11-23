"""Typed DOCX/XLSX specification validation and rendering."""

from __future__ import annotations

import io
import json

import pytest
from docx import Document
from openpyxl import load_workbook

from app.services.document_specs import (
    DocumentSpecError,
    docx_json_schema,
    parse_docx_text,
    parse_xlsx_text,
)
from app.services.documents import build_docx, build_text_document


def test_valid_docx_spec_renders_real_document() -> None:
    spec = parse_docx_text(
        json.dumps(
            {
                "blocks": [
                    {"type": "heading", "text": "Договор", "level": 1},
                    {"type": "paragraph", "text": "Клиент: ООО Тест"},
                    {"type": "table", "header": ["Товар", "Сумма"], "rows": [["Болт", 100]]},
                ]
            },
            ensure_ascii=False,
        )
    )
    assert spec is not None
    document = Document(io.BytesIO(build_docx(json.dumps({"blocks": [b.as_block() for b in spec.blocks]}, ensure_ascii=False))))
    assert document.paragraphs[0].text == "Договор"
    assert document.tables[0].cell(1, 0).text == "Болт"


def test_invalid_docx_spec_is_rejected_before_rendering() -> None:
    with pytest.raises(DocumentSpecError) as exc:
        build_text_document(
            "bad.docx",
            json.dumps({"blocks": [{"type": "heading"}]}),
        )
    message = str(exc.value)
    assert "Invalid DOCX specification" in message
    assert "heading requires text" in message


def test_invalid_xlsx_spec_reports_a_path() -> None:
    with pytest.raises(DocumentSpecError) as exc:
        parse_xlsx_text(
            json.dumps({"sheets": [{"name": "Лист", "rows": ["not-a-row"]}]})
        )
    message = str(exc.value)
    assert "sheets.0.rows" in message


def test_xlsx_shorthand_spec_and_json_schema() -> None:
    spec = parse_xlsx_text(json.dumps({"Данные": [["A", "B"], [1, 2]]}))
    assert spec is not None
    assert spec.sheets[0].name == "Данные"
    data = build_text_document(
        "table.xlsx",
        json.dumps({"Данные": [["A", "B"], [1, 2]]}, ensure_ascii=False),
    )
    workbook = load_workbook(io.BytesIO(data))
    assert workbook.sheetnames == ["Данные"]
    assert workbook["Данные"]["A2"].value == 1
    schema = docx_json_schema()
    assert schema["properties"]["blocks"]["type"] == "array"


def test_markdown_remains_a_compatible_fallback() -> None:
    data = build_text_document("doc.docx", "# Заголовок\n\nТекст")
    document = Document(io.BytesIO(data))
    assert document.paragraphs[0].text == "Заголовок"
