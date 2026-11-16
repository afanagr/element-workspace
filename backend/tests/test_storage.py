"""Storage: key validation, streaming writes and size limits."""

from __future__ import annotations

import io

import pytest

from app.services.storage import Storage, StorageError


@pytest.fixture()
def store(tmp_path) -> Storage:
    return Storage(tmp_path / "blobs")


def test_invalid_keys_are_rejected(store: Storage) -> None:
    for key in ("../etc/passwd", "", "ABCDEF", "g" * 32, "a" * 31):
        with pytest.raises(StorageError):
            store.path_for(key)


async def test_save_bytes_roundtrip(store: Storage) -> None:
    key, size, digest = await store.save_bytes(b"hello")
    assert size == 5
    assert len(digest) == 64
    assert await store.read_bytes(key) == b"hello"
    assert await store.read_head(key, 2) == b"he"


async def test_save_stream_from_sync_file_object(store: Storage) -> None:
    key, size, _ = await store.save_stream(io.BytesIO(b"streamed"), max_bytes=1024)
    assert size == 8
    assert await store.read_bytes(key) == b"streamed"


async def test_save_stream_enforces_the_limit(store: Storage) -> None:
    with pytest.raises(StorageError):
        await store.save_stream(io.BytesIO(b"x" * 4096), max_bytes=1024)
    assert list((store.root).rglob("*.part")) == []


def test_delete_ignores_unknown_keys(store: Storage) -> None:
    store.delete(None)
    store.delete("not-a-valid-key")  # must not raise
