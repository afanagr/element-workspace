"""Celery tasks: document indexing and full re-index."""

from __future__ import annotations

import asyncio
from typing import Any

from app.worker.celery_app import celery_app

_loop: asyncio.AbstractEventLoop | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    """One long-lived loop per worker process (keeps DB/HTTP clients warm)."""
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop


@celery_app.task(name="app.worker.tasks.index_nodes", bind=True, max_retries=2)
def index_nodes(self, node_ids: list[str]) -> dict[str, Any]:
    from app.services.indexer import index_nodes as _index_nodes

    async def runner() -> dict[str, Any]:
        return await _index_nodes(node_ids)

    try:
        return _get_loop().run_until_complete(runner())
    except Exception as exc:  # noqa: BLE001
        raise self.retry(exc=exc, countdown=10)


@celery_app.task(name="app.worker.tasks.reindex_all", bind=True)
def reindex_all(self) -> dict[str, Any]:
    from app.services.indexer import reindex_all as _reindex_all

    async def runner() -> dict[str, Any]:
        return await _reindex_all()

    return _get_loop().run_until_complete(runner())


@celery_app.task(name="app.worker.tasks.ping")
def ping() -> str:
    return "pong"
