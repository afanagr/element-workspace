"""Template field detection, replacements and rendering (no database needed)."""

from __future__ import annotations

import io

from docx import Document
from openpyxl import Workbook, load_workbook

from app.services.templates import (
    TemplateError,
    _apply_docx_replacements,
    _apply_xlsx_replacements,
    _render_docx,
    _render_xlsx,
    detect_fields,
)
from app.services.tools import TOOL_NAMES, _HANDLERS


def test_docx_fields_include_scalars_and_loops() -> None:
    document = Document()
    document.add_paragraph("Договор с {{ tenant }} № {{ contract_number }}")
    document.add_paragraph("{% for item in items %}{{ item.name }}: {{ item.amount }}; {% endfor %}")
    buffer = io.BytesIO()
    document.save(buffer)

    fields, config = detect_fields("docx", buffer.getvalue())
    names = {field["name"] for field in fields}
    assert {"tenant", "contract_number", "items"} <= names
    assert "name" not in names and "amount" not in names
    assert config["engine"] == "docxtpl"
    assert "items" in config["loops"]


def test_docx_replacements_and_render() -> None:
    document = Document()
    document.add_paragraph("Клиент: ООО Ромашка, сумма 100")
    buffer = io.BytesIO()
    document.save(buffer)
    prepared = _apply_docx_replacements(
        buffer.getvalue(),
        [{"find": "ООО Ромашка", "field": "tenant"}, {"find": "100", "field": "amount"}],
    )
    fields, _ = detect_fields("docx", prepared)
    assert {field["name"] for field in fields} == {"tenant", "amount"}

    rendered = _render_docx(None, prepared, {"tenant": "ООО Лютик", "amount": "250"})
    result = Document(io.BytesIO(rendered))
    text = "\n".join(paragraph.text for paragraph in result.paragraphs)
    assert "ООО Лютик" in text
    assert "250" in text
    assert "Ромашка" not in text


def test_xlsx_fields_render_and_preserve_formula() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = "Клиент"
    sheet["B1"] = "{{ tenant }}"
    sheet["A2"] = "Итого"
    sheet["B2"] = "=1+2"
    source = io.BytesIO()
    workbook.save(source)

    fields, config = detect_fields("xlsx", source.getvalue())
    assert [field["name"] for field in fields] == ["tenant"]
    assert config["engine"] == "jinja-cells"

    rendered = _render_xlsx(None, source.getvalue(), {"tenant": "ООО Ромашка"})
    result = load_workbook(io.BytesIO(rendered), data_only=False)
    assert result.active["B1"].value == "ООО Ромашка"
    assert result.active["B2"].value == "=1+2"


def test_xlsx_replacements() -> None:
    workbook = Workbook()
    workbook.active["A1"] = "ООО Ромашка"
    source = io.BytesIO()
    workbook.save(source)
    prepared = _apply_xlsx_replacements(
        source.getvalue(), [{"find": "ООО Ромашка", "field": "tenant"}]
    )
    fields, _ = detect_fields("xlsx", prepared)
    assert [field["name"] for field in fields] == ["tenant"]


def test_xlsx_row_loops_repeat_and_remove_markers() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = "Позиция"
    sheet["B1"] = "Количество"
    sheet["A2"] = "{% for item in items %}"
    sheet["B2"] = "{{ item.name }}"
    sheet["C2"] = "{{ item.qty }}"
    sheet["D2"] = "{% endfor %}"
    sheet["A4"] = "После таблицы"
    source = io.BytesIO()
    workbook.save(source)

    fields, config = detect_fields("xlsx", source.getvalue())
    assert [field["name"] for field in fields] == ["items"]
    assert "items" in config["loops"]

    rendered = _render_xlsx(
        None,
        source.getvalue(),
        {"items": [{"name": "Болт", "qty": 2}, {"name": "Гайка", "qty": 5}, {"name": "Шайба", "qty": 8}]},
    )
    result = load_workbook(io.BytesIO(rendered), data_only=False)
    active = result.active
    assert active["A1"].value == "Позиция"
    assert active["A2"].value in (None, "")
    assert active["B2"].value == "Болт"
    assert active["C2"].value == "2"
    assert active["B3"].value == "Гайка"
    assert active["C4"].value == "8"
    assert active["A6"].value == "После таблицы"
    assert "{%" not in str(active["A2"].value or "")
    result.close()


def test_pdf_is_not_a_template_kind() -> None:
    try:
        detect_fields("pdf", b"%PDF-1.7")
    except TemplateError as exc:
        assert "DOCX and XLSX" in exc.message
    else:  # pragma: no cover - explicit failure is clearer
        raise AssertionError("PDF must not be accepted as a template")


def test_template_tools_are_advertised_and_dispatched() -> None:
    assert {"create_template_from_file", "fill_template"} <= TOOL_NAMES
    assert {"create_template_from_file", "fill_template"} <= set(_HANDLERS)
