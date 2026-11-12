"""Workspace tree operations: create / rename / move / delete / versions / ZIP.

All user supplied names are sanitised. Because paths never reach the filesystem
(blobs are keyed by random ids), `../` style traversal is impossible; the
sanitiser is still applied so directory listings and ZIPs stay clean.
"""

from __future__ import annotations

import io
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.logging_config import get_logger
from app.models import Node, NodeVersion, utcnow
from app.services.parsing import (
    MIME_BY_EXTENSION,
    SUPPORTED_EXTENSIONS,
    detect_mime,
    extension_of,
    is_supported,
)
from app.services.storage import storage

logger = get_logger(__name__)

MAX_VERSIONS = 20
ALLOWED_UPLOAD_EXTENSIONS = SUPPORTED_EXTENSIONS | {
    ".zip", ".pptx", ".ppt", ".odt", ".ods", ".rtf", ".epub",
}
_FORBIDDEN_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_MAGIC_CHECKS: dict[str, tuple[bytes, ...]] = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".bmp": (b"BM",),
    ".pdf": (b"%PDF",),
    ".zip": (b"PK\x03\x04", b"PK\x05\x06"),
    ".docx": (b"PK\x03\x04",),
    ".xlsx": (b"PK\x03\x04",),
    ".xlsm": (b"PK\x03\x04",),
    ".pptx": (b"PK\x03\x04",),
}


class NodeError(Exception):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def sanitize_name(name: str) -> str:
    raw = (name or "").strip().replace("\\", "/")
    raw = raw.split("/")[-1].strip()
    if raw in ("", ".", ".."):
        raise NodeError("Invalid name")
    if len(raw) > 200:
        raise NodeError("Name is longer than 200 characters")
    cleaned = _FORBIDDEN_NAME_CHARS.sub("_", raw).strip(" .")
    if not cleaned:
        raise NodeError("Invalid name")
    return cleaned


def validate_upload(name: str, data: bytes) -> str:
    """Validate extension, size and magic bytes; returns the sanitised name."""
    safe = sanitize_name(name)
    ext = extension_of(safe)
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        raise NodeError(f"Unsupported file type: {ext or 'no extension'}", status_code=415)
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise NodeError(f"File exceeds {settings.max_upload_mb} MB limit", status_code=413)
    signatures = _MAGIC_CHECKS.get(ext)
    if signatures and not any(data.startswith(signature) for signature in signatures):
        raise NodeError(f"File content does not match the .{ext.lstrip('.')} extension", status_code=415)
    return safe


def mime_for(name: str) -> str:
    return MIME_BY_EXTENSION.get(extension_of(name), detect_mime(name))


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------- lookups
async def get_node(db: AsyncSession, node_id: uuid.UUID | str, *, allow_missing: bool = False) -> Node:
    try:
        identifier = uuid.UUID(str(node_id))
    except (ValueError, AttributeError) as exc:
        raise NodeError("Invalid node id", status_code=400) from exc
    node = await db.get(Node, identifier)
    if node is None and not allow_missing:
        raise NodeError("Node not found", status_code=404)
    return node  # type: ignore[return-value]


def ensure_access(node: Node, user: object) -> Node:
    """Raise 404 when a non-admin user reaches a node they do not own.

    A 404 (instead of 403) keeps other users' projects invisible: an attacker
    cannot tell an existing node from a non-existing one.
    """
    if getattr(user, "is_admin", False):
        return node
    if node.owner_id != getattr(user, "id", None):
        raise NodeError("Node not found", status_code=404)
    return node


async def get_node_for(db: AsyncSession, node_id: uuid.UUID | str, user: object) -> Node:
    """Fetch a node and verify that ``user`` may see it."""
    return ensure_access(await get_node(db, node_id), user)


async def get_project_for(
    db: AsyncSession, project_id: uuid.UUID | str | None, user: object
) -> Node | None:
    """Resolve a project node, checking ownership."""
    if not project_id:
        return None
    node = await get_node_for(db, project_id, user)
    if node.kind != "project":
        raise NodeError("Not a project")
    return node


async def list_children(db: AsyncSession, parent_id: uuid.UUID | None) -> list[Node]:
    stmt = select(Node).where(Node.parent_id == parent_id)
    stmt = stmt.order_by(Node.kind.desc(), func.lower(Node.name))
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def find_child(db: AsyncSession, parent_id: uuid.UUID | None, name: str) -> Node | None:
    stmt = select(Node).where(Node.parent_id == parent_id, func.lower(Node.name) == name.lower())
    result = await db.execute(stmt)
    return result.scalars().first()


async def node_path(db: AsyncSession, node: Node) -> str:
    parts: list[str] = [node.name]
    current = node
    guard = 0
    while current.parent_id is not None and guard < 64:
        parent = await db.get(Node, current.parent_id)
        if parent is None:
            break
        parts.append(parent.name)
        current = parent
        guard += 1
    return "/".join(reversed(parts))


async def project_of(db: AsyncSession, node: Node) -> Node | None:
    current = node
    guard = 0
    while guard < 64:
        if current.kind == "project":
            return current
        if current.parent_id is None:
            return None
        current = await db.get(Node, current.parent_id)  # type: ignore[assignment]
        if current is None:
            return None
        guard += 1
    return None


async def unique_name(
    db: AsyncSession,
    parent_id: uuid.UUID | None,
    name: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> str:
    candidate = name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    for counter in range(2, 500):
        existing = await find_child(db, parent_id, candidate)
        if existing is None or existing.id == exclude_id:
            return candidate
        candidate = f"{stem} ({counter}){'.' + ext if ext else ''}"
    raise NodeError("Too many files with a similar name", status_code=409)


# ------------------------------------------------------------------ creation
async def create_project(
    db: AsyncSession, name: str, created_by: str = "user", owner_id: uuid.UUID | None = None
) -> Node:
    safe = await unique_name(db, None, sanitize_name(name))
    node = Node(name=safe, kind="project", parent_id=None, created_by=created_by, owner_id=owner_id)
    db.add(node)
    await db.flush()
    node.project_id = node.id
    await db.flush()
    return node


async def create_folder(
    db: AsyncSession, parent_id: uuid.UUID | str, name: str, created_by: str = "user"
) -> Node:
    parent = await get_node(db, parent_id)
    if parent.kind == "file":
        raise NodeError("Cannot create a folder inside a file")
    safe = await unique_name(db, parent.id, sanitize_name(name))
    project = await project_of(db, parent)
    node = Node(
        name=safe,
        kind="folder",
        parent_id=parent.id,
        project_id=project.id if project else None,
        owner_id=parent.owner_id,
        created_by=created_by,
    )
    db.add(node)
    await db.flush()
    return node


async def _next_version_no(db: AsyncSession, node_id: uuid.UUID) -> int:
    result = await db.execute(select(func.max(NodeVersion.version_no)).where(NodeVersion.node_id == node_id))
    return int(result.scalar() or 0) + 1


async def _prune_versions(db: AsyncSession, node_id: uuid.UUID) -> None:
    result = await db.execute(
        select(NodeVersion)
        .where(NodeVersion.node_id == node_id)
        .order_by(NodeVersion.version_no.desc())
        .offset(MAX_VERSIONS)
    )
    for old in result.scalars().all():
        if old.storage_key:
            storage.delete(old.storage_key)
        await db.delete(old)


async def write_file(
    db: AsyncSession,
    node: Node,
    data: bytes,
    *,
    created_by: str = "ai",
    note: str | None = None,
) -> Node:
    """Store new bytes for an existing file node and create a version snapshot."""
    key, size, sha = await storage.save_bytes(data)
    node.storage_key = key
    node.size = size
    node.sha256 = sha
    node.mime = mime_for(node.name)
    node.index_status = "pending"
    node.index_error = None
    node.chunk_count = 0
    node.indexed_at = None
    node.updated_at = utcnow()
    version_no = await _next_version_no(db, node.id)
    db.add(
        NodeVersion(
            node_id=node.id,
            version_no=version_no,
            storage_key=key,
            size=size,
            sha256=sha,
            mime=node.mime,
            created_by=created_by,
            note=(note or "")[:255] or None,
        )
    )
    await db.flush()
    await _prune_versions(db, node.id)
    return node


async def create_file(
    db: AsyncSession,
    parent_id: uuid.UUID | str,
    name: str,
    data: bytes,
    *,
    created_by: str = "ai",
    note: str | None = None,
    overwrite: bool = True,
) -> tuple[Node, bool]:
    """Create a file, or write a new version when the name already exists."""
    parent = await get_node(db, parent_id)
    if parent.kind == "file":
        raise NodeError("Cannot create a file inside a file")
    safe = sanitize_name(name)
    ext = extension_of(safe)
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        raise NodeError(f"Unsupported file type: {ext or 'no extension'}", status_code=415)
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise NodeError(f"File exceeds {settings.max_upload_mb} MB limit", status_code=413)

    existing = await find_child(db, parent.id, safe)
    if existing is not None:
        if existing.kind != "file":
            raise NodeError("A folder with this name already exists", status_code=409)
        if not overwrite:
            raise NodeError("A file with this name already exists", status_code=409)
        await write_file(db, existing, data, created_by=created_by, note=note)
        return existing, True

    project = await project_of(db, parent)
    node = Node(
        name=safe,
        kind="file",
        parent_id=parent.id,
        project_id=project.id if project else None,
        owner_id=parent.owner_id,
        mime=mime_for(safe),
        size=len(data),
        storage_key=None,
        created_by=created_by,
    )
    db.add(node)
    await db.flush()
    await write_file(db, node, data, created_by=created_by, note=note)
    return node, False


# ----------------------------------------------------------------- mutation
async def rename_node(db: AsyncSession, node: Node, new_name: str) -> Node:
    safe = sanitize_name(new_name)
    if safe == node.name:
        return node
    safe = await unique_name(db, node.parent_id, safe, exclude_id=node.id)
    node.name = safe
    if node.kind == "file":
        node.mime = mime_for(safe)
    node.updated_at = utcnow()
    await db.flush()
    return node


async def move_node(db: AsyncSession, node: Node, new_parent_id: uuid.UUID | str) -> Node:
    if node.kind == "project":
        raise NodeError("Projects cannot be moved")
    target = await get_node(db, new_parent_id)
    if target.kind == "file":
        raise NodeError("Cannot move a node inside a file")
    cursor: Node | None = target
    guard = 0
    while cursor is not None and guard < 64:
        if cursor.id == node.id:
            raise NodeError("Cannot move a folder into its own subtree")
        cursor = await db.get(Node, cursor.parent_id) if cursor.parent_id else None
        guard += 1
    safe = await unique_name(db, target.id, node.name, exclude_id=node.id)
    project = await project_of(db, target)
    node.parent_id = target.id
    node.name = safe
    node.project_id = project.id if project else None
    node.updated_at = utcnow()
    await db.flush()
    if node.kind == "folder":
        await _reassign_project(db, node, node.project_id)
    return node


async def _reassign_project(db: AsyncSession, node: Node, project_id: uuid.UUID | None) -> None:
    for child in await list_children(db, node.id):
        child.project_id = project_id
        if child.kind == "folder":
            await _reassign_project(db, child, project_id)
    await db.flush()


# -------------------------------------------------------------- snapshots
def _serialize_node(node: Node) -> dict[str, Any]:
    return {
        "id": str(node.id),
        "parent_id": str(node.parent_id) if node.parent_id else None,
        "project_id": str(node.project_id) if node.project_id else None,
        "name": node.name,
        "kind": node.kind,
        "mime": node.mime,
        "size": node.size,
        "storage_key": node.storage_key,
        "sha256": node.sha256,
        "index_status": node.index_status,
        "created_by": node.created_by,
    }


async def _collect_subtree(db: AsyncSession, node: Node) -> list[Node]:
    collected: list[Node] = [node]
    index = 0
    while index < len(collected):
        current = collected[index]
        index += 1
        if current.kind != "file":
            collected.extend(await list_children(db, current.id))
    return collected


async def snapshot_subtree(db: AsyncSession, node: Node) -> dict[str, Any]:
    """Snapshot a node with descendants + versions so delete can be undone."""
    nodes = await _collect_subtree(db, node)
    node_ids = [item.id for item in nodes]
    versions_by_node: dict[uuid.UUID, list[NodeVersion]] = {item.id: [] for item in nodes}
    if node_ids:
        version_rows = await db.execute(
            select(NodeVersion).where(NodeVersion.node_id.in_(node_ids))
        )
        for version in version_rows.scalars().all():
            versions_by_node.setdefault(version.node_id, []).append(version)

    payload: list[dict[str, Any]] = []
    for item in nodes:
        data = _serialize_node(item)
        data["versions"] = [
            {
                "id": str(version.id),
                "version_no": version.version_no,
                "storage_key": version.storage_key,
                "size": version.size,
                "sha256": version.sha256,
                "mime": version.mime,
                "created_by": version.created_by,
                "note": version.note,
                "created_at": version.created_at.isoformat() if version.created_at else None,
            }
            for version in versions_by_node.get(item.id, [])
        ]
        payload.append(data)
    return {"action": "restore_subtree", "nodes": payload, "root_id": str(node.id)}


async def delete_node(db: AsyncSession, node: Node) -> dict[str, Any]:
    """Delete a node (and descendants). Blobs are kept so the action is undoable."""
    snapshot = await snapshot_subtree(db, node)
    await db.delete(node)
    await db.flush()
    return snapshot


async def restore_snapshot(db: AsyncSession, snapshot: dict[str, Any]) -> uuid.UUID | None:
    """Re-create a deleted subtree (used by AI undo and operation rollback)."""
    payload = snapshot.get("nodes") or []
    snapshot_ids = {item["id"] for item in payload}
    restored: set[str] = set()
    remaining = list(payload)
    progress = True
    while remaining and progress:
        progress = False
        next_remaining: list[dict[str, Any]] = []
        for data in remaining:
            parent_id = data.get("parent_id")
            if parent_id and parent_id in snapshot_ids and parent_id not in restored:
                next_remaining.append(data)
                continue
            node_id = uuid.UUID(data["id"])
            if await db.get(Node, node_id) is not None:
                restored.add(data["id"])
                progress = True
                continue
            parent_uuid: uuid.UUID | None = uuid.UUID(parent_id) if parent_id else None
            if parent_uuid is not None and await db.get(Node, parent_uuid) is None:
                parent_uuid = None
            project_uuid = uuid.UUID(data["project_id"]) if data.get("project_id") else None
            node = Node(
                id=node_id,
                parent_id=parent_uuid,
                project_id=project_uuid,
                name=data["name"],
                kind=data["kind"],
                mime=data.get("mime"),
                size=int(data.get("size") or 0),
                storage_key=data.get("storage_key"),
                sha256=data.get("sha256"),
                index_status=data.get("index_status") or "pending",
                created_by=data.get("created_by") or "user",
            )
            db.add(node)
            for version in data.get("versions") or []:
                created_at = version.get("created_at")
                db.add(
                    NodeVersion(
                        id=uuid.UUID(version["id"]),
                        node_id=node_id,
                        version_no=int(version["version_no"]),
                        storage_key=version["storage_key"],
                        size=int(version["size"]),
                        sha256=version["sha256"],
                        mime=version.get("mime"),
                        created_by=version.get("created_by") or "user",
                        note=version.get("note"),
                        created_at=datetime.fromisoformat(created_at) if created_at else utcnow(),
                    )
                )
            restored.add(data["id"])
            progress = True
        await db.flush()
        remaining = next_remaining
    root_id = snapshot.get("root_id")
    return uuid.UUID(root_id) if root_id and root_id in restored else None


async def revert_to_version(
    db: AsyncSession, node: Node, version: NodeVersion, *, created_by: str = "user"
) -> Node:
    data = await storage.read_bytes(version.storage_key)
    return await write_file(
        db, node, data, created_by=created_by, note=f"revert to v{version.version_no}"
    )


# --------------------------------------------------------------------- tree
def node_to_dict(node: Node) -> dict[str, Any]:
    from app.services.parsing import view_kind

    return {
        "id": str(node.id),
        "parent_id": str(node.parent_id) if node.parent_id else None,
        "project_id": str(node.project_id) if node.project_id else None,
        "owner_id": str(node.owner_id) if node.owner_id else None,
        "owner": None,
        "name": node.name,
        "kind": node.kind,
        "mime": node.mime,
        "size": node.size,
        "index_status": node.index_status,
        "index_error": node.index_error,
        "chunk_count": node.chunk_count,
        "indexed_at": node.indexed_at.isoformat() if node.indexed_at else None,
        "created_at": node.created_at.isoformat() if node.created_at else None,
        "updated_at": node.updated_at.isoformat() if node.updated_at else None,
        "view_kind": view_kind(node.name) if node.kind == "file" else "folder",
        "extension": extension_of(node.name),
        "created_by": node.created_by,
    }


async def build_tree(
    db: AsyncSession,
    project_id: uuid.UUID | str | None = None,
    *,
    owner_id: uuid.UUID | str | None = None,
    with_owners: bool = False,
) -> list[dict[str, Any]]:
    """Build the tree for one project, one owner, or the whole workspace.

    ``owner_id`` restricts the listing to a single account (normal users always
    pass their own id); ``with_owners`` adds usernames so an administrator can
    tell whose project is whose.
    """
    from collections import defaultdict

    from sqlalchemy import or_

    stmt = select(Node)
    if project_id:
        try:
            pid = uuid.UUID(str(project_id))
        except ValueError as exc:
            raise NodeError("Invalid project id") from exc
        stmt = stmt.where(or_(Node.id == pid, Node.project_id == pid))
    if owner_id:
        stmt = stmt.where(Node.owner_id == uuid.UUID(str(owner_id)))
    result = await db.execute(stmt)
    nodes = list(result.scalars().all())
    by_parent: dict[uuid.UUID | None, list[Node]] = defaultdict(list)
    for node in nodes:
        by_parent[node.parent_id].append(node)

    budget = 5000

    def build(parent_id: uuid.UUID | None, depth: int) -> list[dict[str, Any]]:
        nonlocal budget
        if depth > 16:
            return []
        output: list[dict[str, Any]] = []
        children = sorted(
            by_parent.get(parent_id, []),
            key=lambda item: (item.kind == "file", item.name.lower()),
        )
        for child in children:
            if budget <= 0:
                break
            budget -= 1
            item = node_to_dict(child)
            item["children"] = build(child.id, depth + 1) if child.kind != "file" else []
            output.append(item)
        return output

    tree = build(None, 0)
    if with_owners:
        await _attach_owner_names(db, tree)
    return tree


async def _attach_owner_names(db: AsyncSession, tree: list[dict[str, Any]]) -> None:
    from app.models import User

    owner_ids = {
        item["owner_id"] for item in tree if item.get("kind") == "project" and item.get("owner_id")
    }
    if not owner_ids:
        return
    result = await db.execute(
        select(User.id, User.username, User.display_name).where(
            User.id.in_([uuid.UUID(value) for value in owner_ids])
        )
    )
    names = {
        str(row[0]): (row[2] or row[1]) for row in result.all()
    }
    for item in tree:
        if item.get("kind") == "project" and item.get("owner_id"):
            item["owner"] = names.get(item["owner_id"])




