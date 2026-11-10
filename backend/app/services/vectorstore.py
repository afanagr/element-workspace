"""Qdrant vector store wrapper (async client, one collection, cosine distance)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Sequence

from qdrant_client import AsyncQdrantClient, models

from app.config import settings
from app.logging_config import get_logger

logger = get_logger(__name__)

_POINT_NAMESPACE = uuid.UUID("0f6c9b28-7fd4-4b0a-9a6b-1b5a2f0d8c11")


class VectorStoreError(Exception):
    pass


def point_id(node_id: str, chunk_index: int) -> str:
    return str(uuid.uuid5(_POINT_NAMESPACE, f"{node_id}:{chunk_index}"))


class VectorStore:
    def __init__(self) -> None:
        self._client: AsyncQdrantClient | None = None
        self._dimension: int | None = None
        self._lock = asyncio.Lock()

    @property
    def collection(self) -> str:
        return settings.qdrant_collection

    def client(self) -> AsyncQdrantClient:
        if self._client is None:
            location = (settings.qdrant_url or "").strip()
            if location in (":memory:", "memory://"):
                # Embedded mode: handy for local dev / single-container setups.
                # Vectors are not persisted across restarts.
                logger.warning("Qdrant is running in embedded in-memory mode")
                self._client = AsyncQdrantClient(location=":memory:")
            else:
                self._client = AsyncQdrantClient(
                    url=location,
                    api_key=settings.qdrant_api_key or None,
                    timeout=30,
                    prefer_grpc=False,
                )
        return self._client

    async def ensure_collection(self, dimension: int) -> None:
        if dimension <= 0:
            raise VectorStoreError("Invalid embedding dimension")
        async with self._lock:
            if self._dimension == dimension:
                return
            client = self.client()
            exists = await client.collection_exists(self.collection)
            if exists:
                info = await client.get_collection(self.collection)
                current = getattr(info.config.params.vectors, "size", None)
                if current and int(current) != dimension:
                    logger.warning(
                        "Embedding dimension changed (%s -> %s); recreating collection %s",
                        current,
                        dimension,
                        self.collection,
                    )
                    await client.delete_collection(self.collection)
                    exists = False
            if not exists:
                await client.create_collection(
                    collection_name=self.collection,
                    vectors_config=models.VectorParams(
                        size=dimension, distance=models.Distance.COSINE
                    ),
                )
            self._dimension = dimension

    async def delete_node(self, node_id: str) -> None:
        client = self.client()
        if not await client.collection_exists(self.collection):
            return
        await client.delete(
            collection_name=self.collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="node_id", match=models.MatchValue(value=str(node_id))
                        )
                    ]
                )
            ),
            wait=True,
        )

    async def upsert_chunks(
        self,
        *,
        node_id: str,
        project_id: str | None,
        name: str,
        path: str,
        chunks: Sequence[Any],
        vectors: Sequence[Sequence[float]],
    ) -> int:
        if len(chunks) != len(vectors):
            raise VectorStoreError("Chunk/vector count mismatch")
        if not chunks:
            return 0
        await self.ensure_collection(len(vectors[0]))
        points: list[models.PointStruct] = []
        for chunk, vector in zip(chunks, vectors):
            points.append(
                models.PointStruct(
                    id=point_id(node_id, chunk.index),
                    vector=[float(value) for value in vector],
                    payload={
                        "node_id": str(node_id),
                        "project_id": str(project_id) if project_id else None,
                        "name": name,
                        "path": path,
                        "label": chunk.label,
                        "chunk_index": chunk.index,
                        "text": chunk.text,
                    },
                )
            )
        client = self.client()
        for start in range(0, len(points), 64):
            await client.upsert(
                collection_name=self.collection, points=points[start : start + 64], wait=True
            )
        return len(points)

    async def search(
        self,
        vector: Sequence[float],
        *,
        limit: int = 6,
        project_id: str | None = None,
        node_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        client = self.client()
        if not await client.collection_exists(self.collection):
            return []
        conditions: list[models.Condition] = []
        if project_id:
            conditions.append(
                models.FieldCondition(key="project_id", match=models.MatchValue(value=str(project_id)))
            )
        if node_ids:
            conditions.append(
                models.FieldCondition(
                    key="node_id", match=models.MatchAny(any=[str(item) for item in node_ids])
                )
            )
        query_filter = models.Filter(must=conditions) if conditions else None
        try:
            response = await client.query_points(
                collection_name=self.collection,
                query=[float(value) for value in vector],
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(f"Qdrant search failed: {exc}") from exc
        hits: list[dict[str, Any]] = []
        for point in getattr(response, "points", []) or []:
            payload = dict(point.payload or {})
            payload["score"] = float(point.score)
            hits.append(payload)
        return hits

    async def count_points(self) -> int:
        client = self.client()
        if not await client.collection_exists(self.collection):
            return 0
        info = await client.get_collection(self.collection)
        return int(getattr(info, "points_count", 0) or 0)

    async def health(self) -> tuple[bool, str]:
        try:
            await self.client().get_collections()
            return True, "ok"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None


vector_store = VectorStore()

