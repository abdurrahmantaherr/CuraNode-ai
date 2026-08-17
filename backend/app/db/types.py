"""Shared column types and identifier generation.

UUIDv7 is generated in the application (TDD 3.1). Python 3.13 has no stdlib
uuid7, so it is implemented here rather than pulling a dependency for 20 lines.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime


def uuid7() -> uuid.UUID:
    """Time-ordered UUID (RFC 9562 v7): 48-bit ms timestamp + 74 random bits.

    Time ordering keeps primary-key inserts index-friendly, which is the whole
    reason TDD 3.1 chose v7 over v4.
    """
    ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
    rand = int.from_bytes(os.urandom(10), "big")

    # 48 bits timestamp | 4 bits version | 12 bits rand_a | 2 bits variant | 62 bits rand_b
    rand_a = (rand >> 62) & 0xFFF
    rand_b = rand & 0x3FFFFFFFFFFFFFFF

    value = (ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return uuid.UUID(int=value)


def utcnow() -> datetime:
    """Timezone-aware UTC now. All timestamps are stored in UTC (TDD 3.1)."""
    return datetime.now(UTC)
