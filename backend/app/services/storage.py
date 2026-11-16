"""Persistence layer for file contents.

Bytes are stored on disk under opaque random keys; names/paths only exist in
Postgres. That makes path traversal structurally impossible and lets the UI
rename/move nodes without rewriting blobs.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path

import anyio

from app.config import settings
from app.logging_config import get_logger

logger = get_logger(__name__)

_KEY_RE = re.compile(r"^[a-f0-9]{32}$")


class StorageError(Exception):
    pass


class Storage:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or settings.blob_dir)

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (Path(settings.workspace_dir) / "tmp").mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- paths
    def path_for(self, key: str) -> Path:
        if not _KEY_RE.match(key):
            raise StorageError("Invalid storage key")
        return self.root / key[:2] / key

    # ----------------------------------------------------------------- read
    async def read_bytes(self, key: str) -> bytes:
        path = self.path_for(key)
        if not path.exists():
            raise StorageError("File content is missing from storage")
        return await anyio.to_thread.run_sync(path.read_bytes)

    def open_path(self, key: str) -> Path:
        path = self.path_for(key)
        if not path.exists():
            raise StorageError("File content is missing from storage")
        return path

    # ---------------------------------------------------------------- write
    async def save_bytes(self, data: bytes) -> tuple[str, int, str]:
        key = uuid.uuid4().hex
        digest = hashlib.sha256(data).hexdigest()
        target = self.path_for(key)
        target.parent.mkdir(parents=True, exist_ok=True)

        def _write() -> None:
            tmp = target.with_suffix(".part")
            with open(tmp, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)

        await anyio.to_thread.run_sync(_write)
        return key, len(data), digest

    def delete(self, key: str | None) -> None:
        if not key:
            return
        try:
            self.path_for(key).unlink(missing_ok=True)
        except StorageError:
            logger.warning("Refusing to delete invalid storage key %r", key)


def decode_bytes(data: bytes) -> str:
    from charset_normalizer import from_bytes

    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass

    # charset-normalizer is unreliable on very short samples (it often guesses
    # a CJK codec for a handful of Windows-1251 bytes). Prefer cp1251 when the
    # result is overwhelmingly Cyrillic.
    if len(data) < 1024:
        try:
            candidate = data.decode("cp1251")
        except UnicodeDecodeError:
            candidate = ""
        if candidate:
            letters = [char for char in candidate if char.isalpha()]
            cyrillic = [char for char in letters if "\u0400" <= char <= "\u04ff"]
            if letters and len(cyrillic) / len(letters) > 0.6:
                return candidate

    best = from_bytes(data).best()
    if best is not None:
        return str(best)
    return data.decode("utf-8", errors="replace")



storage = Storage()
