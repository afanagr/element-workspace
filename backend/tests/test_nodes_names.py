"""Node names and upload validation (pure functions, no database)."""

from __future__ import annotations

import pytest

from app.services.nodes import NodeError, sanitize_name, validate_upload


def test_sanitize_strips_directories() -> None:
    assert sanitize_name("../../etc/passwd") == "passwd"
    assert sanitize_name("a<b>.txt") == "a_b_.txt"


def test_sanitize_rejects_empty_names() -> None:
    for value in ("", "   ", ".."):
        with pytest.raises(NodeError):
            sanitize_name(value)


def test_upload_rejects_a_mismatched_magic_number() -> None:
    with pytest.raises(NodeError):
        validate_upload("report.pdf", b"not a pdf at all")


def test_upload_accepts_a_pdf() -> None:
    assert validate_upload("report.pdf", b"%PDF-1.7\n...") == "report.pdf"


def test_upload_checks_the_declared_size() -> None:
    with pytest.raises(NodeError):
        validate_upload("notes.txt", b"hi", size=10**9)
