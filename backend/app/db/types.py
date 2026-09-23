"""Shared column types and identifier generation.

UUIDv7 is generated in the application (TDD 3.1). Python 3.13 has no stdlib
uuid7, so it is implemented here rather than pulling a dependency for 20 lines.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, date, datetime, timedelta, timezone

# Pakistan Standard Time. A fixed offset rather than `ZoneInfo("Asia/Karachi")`:
# Karachi has observed no DST since 2009, and Windows dev machines ship no IANA
# database without the extra `tzdata` package.
PKT = timezone(timedelta(hours=5), "PKT")


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


def today_pk() -> date:
    """Today's calendar date in Pakistan — the reference for age and for
    "not in the future" date checks. Call it as `types.today_pk()` (module
    attribute access) so tests can freeze it in one place."""
    return datetime.now(PKT).date()
