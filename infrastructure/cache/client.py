"""Redis-backed cache with an in-memory fallback.

The fallback is not a toy: it keeps tests and single-process development free of
a Redis dependency while exercising the same ``Cache`` interface the application
uses in production.

Cache keys embed a data version (a market-state id, a dataset digest) so
invalidation is implicit. There is deliberately no ``flush``-style API: if a
result can go stale, its key was built wrong.
"""

from __future__ import annotations

import json
import time
from typing import Any

from infrastructure.settings import Settings, get_settings


class Cache:
    async def get(self, key: str) -> Any | None:  # pragma: no cover - interface
        raise NotImplementedError

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        raise NotImplementedError  # pragma: no cover - interface

    async def delete(self, key: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def incr(self, key: str, ttl_seconds: int) -> int:  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:
        return None


class InMemoryCache(Cache):
    def __init__(self) -> None:
        self._data: dict[str, tuple[float | None, Any]] = {}

    def _live(self, key: str) -> tuple[float | None, Any] | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at, _ = entry
        if expires_at is not None and expires_at < time.monotonic():
            self._data.pop(key, None)
            return None
        return entry

    async def get(self, key: str) -> Any | None:
        entry = self._live(key)
        return None if entry is None else entry[1]

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        expires_at = None if ttl_seconds is None else time.monotonic() + ttl_seconds
        self._data[key] = (expires_at, value)

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def incr(self, key: str, ttl_seconds: int) -> int:
        entry = self._live(key)
        current = 0 if entry is None else int(entry[1])
        current += 1
        expires_at = entry[0] if entry is not None else time.monotonic() + ttl_seconds
        self._data[key] = (expires_at, current)
        return current


class RedisCache(Cache):
    def __init__(self, url: str) -> None:
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(url, decode_responses=True)

    async def get(self, key: str) -> Any | None:
        raw = await self._redis.get(key)
        return None if raw is None else json.loads(raw)

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        payload = json.dumps(value)
        if ttl_seconds is None:
            await self._redis.set(key, payload)
        else:
            await self._redis.set(key, payload, ex=ttl_seconds)

    async def delete(self, key: str) -> None:
        await self._redis.delete(key)

    async def incr(self, key: str, ttl_seconds: int) -> int:
        pipe = self._redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, ttl_seconds, nx=True)
        value, _ = await pipe.execute()
        return int(value)

    async def ping(self) -> bool:
        return bool(await self._redis.ping())

    async def close(self) -> None:
        await self._redis.aclose()


_cache: Cache | None = None


def get_cache(settings: Settings | None = None) -> Cache:
    global _cache
    if _cache is None:
        settings = settings or get_settings()
        if settings.redis_url:
            try:
                _cache = RedisCache(settings.redis_url)
            except Exception:  # redis-py missing or URL unusable
                _cache = InMemoryCache()
        else:
            _cache = InMemoryCache()
    return _cache


def override_cache(cache: Cache | None) -> None:
    global _cache
    _cache = cache
