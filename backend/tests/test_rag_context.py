"""Retrieval result formatting used for the prompt context block."""

from __future__ import annotations

from app.services.rag import Citation, RetrievalResult


def _citation(index: int = 1, text: str = "hello") -> Citation:
    return Citation(
        index=index,
        node_id="node-1",
        name="a.pdf",
        path="Project/a.pdf",
        label="page:1",
        text=text,
        score=0.87,
    )


def test_context_block_numbers_fragments() -> None:
    block = RetrievalResult(citations=[_citation()]).context_block(max_chars_per_item=100)
    assert block.startswith("[1] Project/a.pdf (page:1)")
    assert "hello" in block


def test_context_block_truncates_long_fragments() -> None:
    block = RetrievalResult(citations=[_citation(text="x" * 500)]).context_block(
        max_chars_per_item=10
    )
    assert "x" * 10 in block
    assert "x" * 11 not in block


def test_empty_result_produces_no_block() -> None:
    assert RetrievalResult().context_block() == ""


def test_citations_serialise_scores() -> None:
    payload = _citation().to_dict()
    assert payload["score"] == 0.87
    assert payload["node_id"] == "node-1"
