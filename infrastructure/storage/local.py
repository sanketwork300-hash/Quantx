"""Filesystem-backed object store for development, tests and single-host deploys."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

from infrastructure.storage.base import ObjectStore, StoredObject


class LocalObjectStore(ObjectStore):
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Keys are server-generated, but a traversal check costs nothing and
        # removes an entire class of bug if that ever stops being true.
        candidate = (self._root / key).resolve()
        root = self._root.resolve()
        if not str(candidate).startswith(str(root)):
            raise ValueError(f"object key escapes the store root: {key!r}")
        return candidate

    async def put(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> StoredObject:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        (path.parent / (path.name + ".contenttype")).write_text(content_type)
        return StoredObject(
            key=key,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            content_type=content_type,
        )

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise FileNotFoundError(key)
        return path.read_bytes()

    async def get_range(self, key: str, length: int) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise FileNotFoundError(key)
        with path.open("rb") as handle:
            return handle.read(length)

    async def exists(self, key: str) -> bool:
        return self._path(key).exists()

    async def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    async def iter_keys(self, prefix: str) -> AsyncIterator[str]:
        base = self._root.resolve()
        for path in sorted(self._root.rglob("*")):
            if not path.is_file() or path.name.endswith(".contenttype"):
                continue
            key = str(path.resolve().relative_to(base))
            if key.startswith(prefix):
                yield key
