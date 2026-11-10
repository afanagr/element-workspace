"""Text chunking with overlap, preserving per-page/per-sheet labels for citations."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.parsing import Segment

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")


@dataclass(slots=True)
class Chunk:
    index: int
    label: str
    text: str


def _split_long(text: str, size: int, overlap: int) -> list[str]:
    """Split an oversized paragraph on sentence/word boundaries."""
    pieces: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + size, length)
        if end < length:
            window = text[start:end]
            boundary = max(window.rfind(". "), window.rfind("\n"), window.rfind(" "))
            if boundary > size // 2:
                end = start + boundary + 1
        pieces.append(text[start:end].strip())
        if end >= length:
            break
        start = max(end - overlap, start + 1)
    return [piece for piece in pieces if piece]


def chunk_segments(
    segments: list[Segment],
    *,
    size: int = 1200,
    overlap: int = 200,
) -> list[Chunk]:
    if size <= 0:
        size = 1200
    overlap = max(0, min(overlap, size // 2))

    chunks: list[Chunk] = []
    for segment in segments:
        text = (segment.text or "").strip()
        if not text:
            continue
        paragraphs = [paragraph.strip() for paragraph in _PARAGRAPH_SPLIT.split(text) if paragraph.strip()]
        buffer = ""
        for paragraph in paragraphs:
            if len(paragraph) > size:
                if buffer:
                    chunks.append(Chunk(len(chunks), segment.label, buffer))
                    buffer = ""
                for piece in _split_long(paragraph, size, overlap):
                    chunks.append(Chunk(len(chunks), segment.label, piece))
                continue
            if len(buffer) + len(paragraph) + 2 <= size:
                buffer = f"{buffer}\n\n{paragraph}" if buffer else paragraph
            else:
                if buffer:
                    chunks.append(Chunk(len(chunks), segment.label, buffer))
                tail = buffer[-overlap:] if overlap and buffer else ""
                buffer = f"{tail}\n\n{paragraph}".strip() if tail else paragraph
        if buffer:
            chunks.append(Chunk(len(chunks), segment.label, buffer))
    return chunks
