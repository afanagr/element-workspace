"""Intent detection, citation validation and history windowing."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.agent_prompt import (
    build_history,
    claims_file_action,
    is_edit_request,
    is_pdf_split_request,
    validate_citations,
)


def test_infinitive_edit_requests_are_detected() -> None:
    assert is_edit_request("Можешь добавить колонку в отчёт.xlsx?")
    assert is_edit_request("нужно создать файл отчёт.docx")
    assert is_edit_request("создать папку Проекты")
    assert is_edit_request("add a column to budget.xlsx")


def test_questions_are_not_edit_requests() -> None:
    assert not is_edit_request("как добавить колонку в excel?")
    assert not is_edit_request("что такое PDF?")
    assert not is_edit_request("почему файл не открывается")


def test_success_claims_are_detected() -> None:
    assert claims_file_action("Готово, я создал файл отчёт.docx")
    assert claims_file_action("The file was saved as report.pdf")
    assert not claims_file_action("Не удалось создать файл отчёт.docx")
    assert not claims_file_action("Файл отчёт.docx лежит в папке")


def test_pdf_split_detection() -> None:
    assert is_pdf_split_request("разбей PDF по страницам")
    assert is_pdf_split_request("split report.pdf into pages")
    assert not is_pdf_split_request("что такое pdf?")


def test_validate_citations_drops_unknown_markers() -> None:
    text, valid = validate_citations("Итог [1] и [4].", 2)
    assert valid is False
    assert "[1]" in text
    assert "[4]" not in text


def test_validate_citations_keeps_valid_markers() -> None:
    text, valid = validate_citations("Итог [1] [2].", 2)
    assert valid is True
    assert text == "Итог [1] [2]."


def test_build_history_respects_the_token_budget() -> None:
    records = [
        SimpleNamespace(role="user", content="a" * 400),
        SimpleNamespace(role="assistant", content=""),
        SimpleNamespace(role="assistant", content="b" * 40),
    ]
    history = build_history(records, token_budget=20)
    assert [message.role for message in history] == ["system", "assistant"]
    assert history[1].content.startswith("b")
    assert "omitted" in (history[0].content or "")


def test_build_history_keeps_everything_when_it_fits() -> None:
    records = [
        SimpleNamespace(role="user", content="hello"),
        SimpleNamespace(role="assistant", content="hi"),
    ]
    history = build_history(records, token_budget=1000)
    assert [message.content for message in history] == ["hello", "hi"]
