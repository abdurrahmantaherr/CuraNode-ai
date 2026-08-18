"""Ephemeral key/value store with TTL — the Redis role from TDD 1.2.

Holds refresh-token families, login lockouts, and rate-limit counters. The
interface is deliberately the small subset of Redis the application uses, so a
`RedisCache` can be dropped in for pilot without touching call sites.

Nothing here is a source of truth: every key is TTL-bounded and losing the
store only forces re-login (PLAN 9, rollback step 3).
"""

from __future__ import annotations

import time
from typing import Any, Protocol


class Cache(Protocol):
    async def get(self, key: str) -> Any | None: ...
    async def set(self, key: str, value: Any, ttl_s: int) -> None: ...
    async def delete(self, *keys: str) -> None: ...
    async def incr(self, key: str, ttl_s: int) -> int: ...
    async def add_to_set(self, key: str, member: str, ttl_s: int) -> None: ...
    async def members(self, key: str) -> set[str]: ...


class InMemoryCache:
    """Single-process cache. Correct for a single-worker pilot deployment.

    Running more than one worker would give each its own copy, so a shared
    Redis is required before horizontal scaling (TDD 10.8).
    """

    def __init__(self) -> None:
        self._data: dict[str, tuple[Any, float]] = {}

    def _live(self, key: str) -> tuple[Any, float] | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        if entry[1] <= time.monotonic():
            self._data.pop(key, None)
            return None
        return entry

    async def get(self, key: str) -> Any | None:
        entry = self._live(key)
        return None if entry is None else entry[0]

    async def set(self, key: str, value: Any, ttl_s: int) -> None:
        self._data[key] = (value, time.monotonic() + ttl_s)

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self._data.pop(key, None)

    async def incr(self, key: str, ttl_s: int) -> int:
        entry = self._live(key)
        if entry is None:
            self._data[key] = (1, time.monotonic() + ttl_s)
            return 1
        count = int(entry[0]) + 1
        # Preserve the original expiry so a fixed window cannot be extended by
        # continued hammering.
        self._data[key] = (count, entry[1])
        return count

    async def add_to_set(self, key: str, member: str, ttl_s: int) -> None:
        entry = self._live(key)
        current: set[str] = set(entry[0]) if entry else set()
        current.add(member)
        expiry = entry[1] if entry else time.monotonic() + ttl_s
        self._data[key] = (current, expiry)

    async def members(self, key: str) -> set[str]:
        entry = self._live(key)
        return set(entry[0]) if entry else set()

    def clear(self) -> None:
        self._data.clear()


cache: Cache = InMemoryCache()


# ── Keyspace (SPEC 7) ────────────────────────────────────────────────────
def refresh_key(token_hash: str) -> str:
    return f"refresh:{token_hash}"


def refresh_family_key(family_id: str) -> str:
    return f"refresh_family:{family_id}"


def ratelimit_key(scope: str, identifier: str) -> str:
    return f"ratelimit:{scope}:{identifier}"


def lockout_key(user_id: str) -> str:
    return f"lockout:{user_id}"
