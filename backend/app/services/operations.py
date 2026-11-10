"""Undo/rollback of AI operations recorded by tools."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_config import get_logger
from app.models import Node, Operation, utcnow
from app.services import nodes as node_service
from app.services.indexer import enqueue_index
from app.services.storage import StorageError, storage

logger = get_logger(__name__)


class OperationError(Exception):
    pass


async def get_operation(db: AsyncSession, operation_id: uuid.UUID | str) -> Operation:
    try:
        identifier = uuid.UUID(str(operation_id))
    except (ValueError, AttributeError) as exc:
        raise OperationError("Invalid operation id") from exc
    operation = await db.get(Operation, identifier)
    if operation is None:
        raise OperationError("Operation not found")
    return operation


async def list_session_operations(db: AsyncSession, session_id: uuid.UUID) -> list[Operation]:
    result = await db.execute(
        select(Operation)
        .where(Operation.session_id == session_id)
        .order_by(Operation.created_at.desc())
        .limit(100)
    )
    return list(result.scalars().all())


async def _node(db: AsyncSession, node_id: Any) -> Node:
    try:
        node = await db.get(Node, uuid.UUID(str(node_id)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise OperationError("Invalid node reference in operation") from exc
    if node is None:
        raise OperationError("The file no longer exists, cannot undo")
    return node


async def revert_operation(db: AsyncSession, operation: Operation) -> str:
    if operation.reverted:
        raise OperationError("This action has already been reverted")
    undo = operation.undo or {}
    action = undo.get("action")

    if action == "delete_node":
        node = await db.get(Node, uuid.UUID(str(undo.get("node_id")))) if undo.get("node_id") else None
        if node is not None:
            snapshot = await node_service.delete_node(db, node)
            logger.info("Undo: removed %s node(s)", len(snapshot.get("nodes", [])))
    elif action == "restore_subtree":
        restored = await node_service.restore_snapshot(db, undo)
        if restored is None:
            raise OperationError("Could not restore the deleted items")
        enqueue_index(
            [item["id"] for item in undo.get("nodes", []) if item.get("kind") == "file"]
        )
    elif action == "restore_file":
        node = await _node(db, undo.get("node_id"))
        try:
            data = await storage.read_bytes(str(undo.get("storage_key") or ""))
        except StorageError as exc:
            raise OperationError("Previous file content is no longer available") from exc
        await node_service.write_file(db, node, data, created_by="undo", note="undo AI change")
        enqueue_index([node.id])
    elif action == "rename":
        node = await _node(db, undo.get("node_id"))
        await node_service.rename_node(db, node, str(undo.get("old_name") or node.name))
        if node.kind == "file":
            enqueue_index([node.id])
    elif action == "move":
        node = await _node(db, undo.get("node_id"))
        new_parent = undo.get("old_parent_id")
        if not new_parent:
            raise OperationError("Nothing to undo for this move")
        await node_service.move_node(db, node, str(new_parent))
    else:
        raise OperationError("This operation cannot be reverted")

    operation.reverted = True
    operation.reverted_at = utcnow()
    await db.flush()
    return operation.description
