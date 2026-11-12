"""Retrieval augmented generation: embed query -> Qdrant -> citations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.llm.factory import registry
from app.llm.schemas import NoProviderAvailable, ProviderError
from app.logging_config import get_logger
from app.models import Node
from app.services.embeddings import EmbeddingService
from app.services.vectorstore import VectorStoreError, vector_store

logger = get_logger(__name__)


@dataclass(slots=True)
class Citation:
    index: int
    node_id: str
    name: str
    path: str
    label: str
    text: str
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "node_id": self.node_id,
            "name": self.name,
            "path": self.path,
            "label": self.label,
            "text": self.text,
            "score": round(self.score, 4),
        }


@dataclass(slots=True)
class RetrievalResult:
    citations: list[Citation] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "citations": [citation.to_dict() for citation in self.citations],
            "error": self.error,
        }

    def context_block(self, max_chars_per_item: int = 1800) -> str:
        if not self.citations:
            return ""
        blocks: list[str] = []
        for citation in self.citations:
            text = citation.text[:max_chars_per_item]
            blocks.append(
                f"[{citation.index}] {citation.path} ({citation.label})\n{text}"
            )
        return "\n\n".join(blocks)


async def retrieve(
    db: AsyncSession,
    query: str,
    *,
    project_id: str | None = None,
    node_ids: Sequence[str] | None = None,
    top_k: int | None = None,
) -> RetrievalResult:
    """Vector search limited to one project.

    ``project_id`` is mandatory: the workspace never mixes documents of different
    projects into one answer.
    """
    query = (query or "").strip()
    if not query:
        return RetrievalResult(error="Empty query")
    if not project_id:
        return RetrievalResult(error="project_id is required for retrieval")
    try:
        resolved = await registry.resolve_embeddings(db)
    except NoProviderAvailable as exc:
        return RetrievalResult(error=str(exc))
    service = EmbeddingService(resolved.provider, resolved.model)
    try:
        vector = await service.embed_query(query)
    except ProviderError as exc:
        logger.warning("Query embedding failed: %s", exc.message)
        return RetrievalResult(error=f"Embedding failed: {exc.message}")
    try:
        hits = await vector_store.search(
            vector,
            limit=top_k or settings.rag_top_k,
            project_id=project_id,
            node_ids=node_ids,
        )
    except VectorStoreError as exc:
        return RetrievalResult(error=str(exc))

    citations: list[Citation] = []
    seen: set[tuple[str, str]] = set()
    for hit in hits:
        node_id = str(hit.get("node_id") or "")
        label = str(hit.get("label") or "")
        text = str(hit.get("text") or "")
        if not node_id or not text:
            continue
        key = (node_id, text[:120])
        if key in seen:
            continue
        seen.add(key)
        citations.append(
            Citation(
                index=len(citations) + 1,
                node_id=node_id,
                name=str(hit.get("name") or "document"),
                path=str(hit.get("path") or hit.get("name") or "document"),
                label=label,
                text=text,
                score=float(hit.get("score") or 0.0),
            )
        )
    return RetrievalResult(citations=citations)


async def expand_node_ids(db: AsyncSession, node_ids: Sequence[str]) -> list[str]:
    """Expand folders/projects to the ids of the files they contain."""
    from sqlalchemy import select

    import uuid as uuid_module

    if not node_ids:
        return []
    parsed: list[uuid_module.UUID] = []
    for item in node_ids:
        try:
            parsed.append(uuid_module.UUID(str(item)))
        except (ValueError, AttributeError, TypeError):
            continue
    if not parsed:
        return []
    result = await db.execute(select(Node).where(Node.id.in_(parsed)))
    nodes = list(result.scalars().all())
    file_ids: set[str] = set()
    project_ids: set[uuid_module.UUID] = set()
    for node in nodes:
        if node.kind == "file":
            file_ids.add(str(node.id))
        elif node.kind == "project":
            project_ids.add(node.id)
        elif node.project_id:
            project_ids.add(node.project_id)
    if project_ids:
        children = await db.execute(
            select(Node.id).where(Node.project_id.in_(project_ids), Node.kind == "file")
        )
        file_ids.update(str(row) for row in children.scalars().all())
    return sorted(file_ids)

