"""Generated Office files must not contain literal HTML ``<br>`` noise."""

from __future__ import annotations

import io

from docx import Document
from openpyxl import load_workbook

from app.services.documents import build_docx, build_xlsx


def test_docx_turns_br_into_real_break() -> None:
    data = build_docx("Первая строка<br>Вторая строка")
    document = Document(io.BytesIO(data))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "<br" not in text.lower()
    assert "Первая строка" in text
    assert "Вторая строка" in text


def test_xlsx_turns_br_into_cell_line_break() -> None:
    data = build_xlsx({"Sheet1": [["A<br>B"]]})
    workbook = load_workbook(io.BytesIO(data))
    try:
        assert workbook.active["A1"].value == "A\nB"
    finally:
        workbook.close()
