"""Object-store interface.

Everything that grows with market activity rather than user activity lives here
rather than in PostgreSQL: raw uploads, L2 histories, tick tapes, historical
option chains, Monte Carlo paths, large job results.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    size_bytes: int
    sha256: str
    content_type: str


class ObjectStore(ABC):
    @abstractmethod
    async def put(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> StoredObject: ...

    @abstractmethod
    async def get(self, key: str) -> bytes: ...

    @abstractmethod
    async def get_range(self, key: str, length: int) -> bytes:
        """Read the first ``length`` bytes.

        Upload previews use this so a 50 MB file is never fully loaded to show
        the user the first 50 rows.
        """

    @abstractmethod
    async def exists(self, key: str) -> bool: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    @abstractmethod
    def iter_keys(self, prefix: str) -> AsyncIterator[str]: ...

    async def health(self) -> bool:
        return True
