"""Helpers used by the agent tools: translation windows and write verification."""

from __future__ import annotations

import io

from app.services.tools import _translate_windows, _verification_note, _verify_bytes


def test_short_text_is_one_window() -> None:
    assert _translate_windows("short text", 100) == ["short text"]


def test_windows_are_paragraph_aligned() -> None:
    text = "\n\n".join(["a" * 4000, "b" * 4000, "c" * 4000])
    windows = _translate_windows(text, 5000)
    assert len(windows) >= 3
    assert all(len(window) <= 5000 for window in windows)
    assert "".join(windows).count("a") == 4000


def test_one_huge_paragraph_is_hard_split() -> None:
    windows = _translate_windows("x" * 25, 10)
    assert [len(window) for window in windows] == [10, 10, 5]


def test_verify_reports_everything_for_text() -> None:
    info = _verify_bytes("notes.txt", "one\ntwo".encode())
    assert info["ok"] is True
    assert info["lines"] == 2
    assert info["chars"] == 7
    assert "Verified" in _verification_note(info)


def test_verify_flags_an_empty_file() -> None:
    info = _verify_bytes("notes.txt", b"")
    assert info["ok"] is False
    assert "empty" in str(info["problem"])


def test_verify_counts_sheets_of_a_workbook() -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.active.append(["a", "b"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    info = _verify_bytes("book.xlsx", buffer.getvalue())
    assert info["ok"] is True
    assert info["sheets"]
    assert info["rows"] >= 1
    assert "sheet" in _verification_note(info)
