"""Indexing pipeline: parse -> chunk -> embed -> Qdrant, with status tracking.

Runs inside Celery workers, but every call is idempotent so the API can fall
back to in-process background indexing when the broker is unavailable.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Iterable

import anyio
from sqlalchemy import select

from app.config import settings
from app.db import session_scope
from app.llm.factory import registry
from app.llm.schemas import NoProviderAvailable, ProviderError
from app.logging_config import get_logger
from app.models import Node, utcnow
from app.services.chunking import chunk_segments
from app.services.embeddings import EmbeddingService
from app.services.nodes import node_path
from app.services.parsing import OcrUnavailable, UnsupportedFormat, parse_document
from app.services.storage import storage
from app.services.vectorstore import VectorStoreError, vector_store

logger = get_logger(__name__)


async def _mark(db, node: Node, status: str, error: str | None = None, chunks: int = 0) -> None:
    node.index_status = status
    node.index_error = (error or "")[:2000] or None
    if chunks:
        node.chunk_count = chunks
    if status == "indexed":
        node.indexed_at = utcnow()
    await db.flush()


async def index_node(node_id: str | uuid.UUID) -> str:
    """Index a single file node. Returns the final status."""
    identifier = uuid.UUID(str(node_id))
    async with session_scope() as db:
        node = await db.get(Node, identifier)
        if node is None or node.kind != "file" or not node.storage_key:
            return "skipped"
        if node.index_status == "indexed" and node.chunk_count > 0:
            # Already indexed by another process (watchdog / duplicate enqueue).
            return "indexed"

        await _mark(db, node, "indexing")
        await db.commit()

        try:
            data = await storage.read_bytes(node.storage_key)
            parsed = await anyio.to_thread.run_sync(parse_document, node.name, data)
            chunks = chunk_segments(
                parsed.segments, size=settings.chunk_size, overlap=settings.chunk_overlap
            )
            await vector_store.delete_node(str(node.id))

            if not chunks:
                reason = "No extractable text (scanned PDF or empty document)"
                await _mark(db, node, "indexed", reason, 0)
                await db.commit()
                logger.info("Indexed %s with 0 chunks (%s)", node.name, reason)
                return "indexed"

            resolved = await registry.resolve_embeddings(db)
            service = EmbeddingService(resolved.provider, resolved.model)
            vectors = await service.embed_documents([chunk.text for chunk in chunks])
            path = await node_path(db, node)
            await vector_store.upsert_chunks(
                node_id=str(node.id),
                project_id=str(node.project_id) if node.project_id else None,
                name=node.name,
                path=path,
                chunks=chunks,
                vectors=vectors,
            )
            await _mark(db, node, "indexed", None, len(chunks))
            await db.commit()
            logger.info(
                "Indexed %s: %s chunks via %s/%s",
                path,
                len(chunks),
                resolved.provider_name,
                resolved.model,
            )
            return "indexed"

        except UnsupportedFormat as exc:
            await _mark(db, node, "unsupported", str(exc))
            await db.commit()
            logger.warning("Unsupported file %s: %s", node.name, exc)
            return "unsupported"
        except OcrUnavailable as exc:
            await _mark(db, node, "failed", f"OCR unavailable: {exc}")
            await db.commit()
            return "failed"
        except NoProviderAvailable as exc:
            await _mark(db, node, "failed", f"Embedding provider unavailable: {exc}")
            await db.commit()
            logger.warning("Cannot index %s: %s", node.name, exc)
            return "failed"
        except (ProviderError, VectorStoreError) as exc:
            await _mark(db, node, "failed", f"{type(exc).__name__}: {exc}")
            await db.commit()
            logger.warning("Indexing failed for %s: %s", node.name, exc)
            return "failed"
        except Exception as exc:  # noqa: BLE001
            await _mark(db, node, "failed", f"{type(exc).__name__}: {exc}")
            await db.commit()
            logger.exception("Unexpected indexing failure for %s", node.name)
            return "failed"


async def index_nodes(node_ids: Iterable[str]) -> dict[str, str]:
    results: dict[str, str] = {}
    for node_id in node_ids:
        try:
            results[str(node_id)] = await index_node(node_id)
        except Exception as exc:  # noqa: BLE001 - keep going through the batch
            results[str(node_id)] = f"error: {exc}"
    return results


async def _inline_index(node_ids: list[str]) -> None:
    try:
        await index_nodes(node_ids)
    except Exception:  # noqa: BLE001 - background task must never crash the loop
        logger.exception("Inline indexing failed")


def enqueue_index(node_ids: Iterable[str | uuid.UUID]) -> None:
    """Queue indexing on Celery, with an inline fallback and a watchdog.

    The watchdog guarantees "works out of the box" even if the worker container
    is not running: if a node is still pending after a grace period we index it
    in the API process.
    """
    ids = [str(node_id) for node_id in node_ids]
    if not ids:
        return
    dispatched = False
    try:
        from app.worker.celery_app import celery_app

        celery_app.send_task("app.worker.tasks.index_nodes", args=[ids], retry=False)
        dispatched = True
    except Exception as exc:  # noqa: BLE001 - broker may be down in dev setups
        logger.warning("Celery dispatch failed (%s); indexing inline", exc)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # no running loop (sync context)
        if not dispatched:
            asyncio.run(_inline_index(ids))
        return

    if dispatched:
        loop.create_task(_watchdog(ids))
    else:
        loop.create_task(_inline_index(ids))


async def _watchdog(node_ids: list[str], delay: float = 25.0) -> None:
    await asyncio.sleep(delay)
    try:
        ids = [uuid.UUID(item) for item in node_ids]
        async with session_scope() as db:
            result = await db.execute(
                select(Node.id).where(
                    Node.id.in_(ids), Node.index_status.in_(("pending", "indexing"))
                )
            )
            stale = [str(row) for row in result.scalars().all()]
        if stale:
            logger.info("Watchdog: indexing %s node(s) inline (worker idle?)", len(stale))
            await index_nodes(stale)
    except Exception:  # noqa: BLE001
        logger.debug("Index watchdog failed", exc_info=True)


async def reindex_all() -> dict[str, str]:
    async with session_scope() as db:
        result = await db.execute(select(Node.id).where(Node.kind == "file"))
        ids = [str(row) for row in result.scalars().all()]
    return await index_nodes(ids)

