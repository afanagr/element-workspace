"""ZIP download and archive upload/extraction (zip-slip safe, size limited)."""

from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable

import anyio
from sqlalchemy import select
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from app.config import settings
from app.logging_config import get_logger
from app.models import Node
from app.services.nodes import NodeError, find_child, sanitize_name
from app.services.storage import storage

logger = get_logger(__name__)

MAX_ZIP_ENTRIES = 20000


async def _collect(
    db, node: Node, arcname: str, entries: list[tuple[str, str | None]], depth: int = 0
) -> None:
    if len(entries) >= MAX_ZIP_ENTRIES:
        return
    if node.kind == "file":
        entries.append((arcname, node.storage_key))
        return
    entries.append((arcname.rstrip("/") + "/", None))
    if depth >= 20:
        return
    result = await db.execute(select(Node).where(Node.parent_id == node.id))
    for child in result.scalars().all():
        await _collect(db, child, f"{arcname}/{sanitize_name(child.name)}", entries, depth + 1)


async def build_zip(nodes: Iterable[Node], db) -> Path:
    """Create a temporary ZIP for the given nodes (folders/projects recursively)."""
    entries: list[tuple[str, str | None]] = []
    for root in nodes:
        await _collect(db, root, sanitize_name(root.name), entries)

    tmp_dir = Path(settings.temp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".zip", dir=tmp_dir)
    handle.close()
    target = Path(handle.name)

    def _write() -> None:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for arcname, storage_key in entries:
                if storage_key is None:
                    archive.writestr(arcname, b"")
                    continue
                try:
                    archive.write(storage.path_for(storage_key), arcname=arcname)
                except FileNotFoundError:
                    logger.warning("Blob missing for %s; skipping", arcname)

    await anyio.to_thread.run_sync(_write)
    return target


def zip_response(path: Path, filename: str) -> FileResponse:
    def _cleanup() -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            logger.warning("Could not remove temporary zip %s", path)

    if not filename.lower().endswith(".zip"):
        filename = f"{filename}.zip"
    return FileResponse(
        path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(_cleanup),
    )


async def extract_archive(
    db,
    parent: Node,
    data: bytes,
    *,
    created_by: str = "user",
) -> list[Node]:
    """Extract a ZIP into a folder. Guards zip-slip, entry count and total size."""
    from app.services import nodes as node_service
    from app.services.indexer import enqueue_index

    if parent.kind == "file":
        raise NodeError("Archives can only be extracted inside a folder")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise NodeError("Uploaded file is not a valid ZIP archive", status_code=415)

    created: list[Node] = []
    total_bytes = 0
    folders: dict[PurePosixPath, Node] = {}

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        if len(members) > settings.max_extract_files:
            raise NodeError(
                f"Archive contains more than {settings.max_extract_files} entries", status_code=413
            )
        for member in members:
            raw_name = member.filename.replace("\\", "/")
            if not raw_name or raw_name.startswith("__MACOSX"):
                continue
            pure = PurePosixPath(raw_name)
            if pure.is_absolute() or ".." in pure.parts:
                logger.warning("Skipping unsafe archive entry %r", raw_name)
                continue
            parts = [part for part in pure.parts if part not in ("", ".", "/")]
            if not parts:
                continue
            is_dir = member.is_dir() or raw_name.endswith("/")
            cleaned = [sanitize_name(part) for part in parts]

            current = parent
            prefix = PurePosixPath()
            for part in cleaned[:-1]:
                prefix = prefix / part
                folder = folders.get(prefix)
                if folder is None:
                    folder = await find_child(db, current.id, part)
                    if folder is None or folder.kind == "file":
                        folder = await node_service.create_folder(db, current.id, part, created_by)
                        created.append(folder)
                    folders[prefix] = folder
                current = folder

            if is_dir:
                prefix = prefix / cleaned[-1]
                folder = folders.get(prefix)
                if folder is None:
                    folder = await find_child(db, current.id, cleaned[-1])
                    if folder is None or folder.kind == "file":
                        folder = await node_service.create_folder(db, current.id, cleaned[-1], created_by)
                        created.append(folder)
                folders[prefix] = folder
                continue

            total_bytes += member.file_size
            if total_bytes > settings.max_extract_mb * 1024 * 1024:
                raise NodeError(
                    f"Archive expands beyond the {settings.max_extract_mb} MB limit", status_code=413
                )
            name = cleaned[-1]
            payload = archive.read(member)
            if len(payload) > settings.max_upload_mb * 1024 * 1024:
                raise NodeError(f"{name} exceeds the per-file size limit", status_code=413)
            node, _replaced = await node_service.create_file(
                db, current.id, name, payload, created_by=created_by, note="extracted from archive"
            )
            created.append(node)

    await db.flush()
    file_ids = [node.id for node in created if node.kind == "file"]
    if file_ids:
        enqueue_index(file_ids)
    return created

