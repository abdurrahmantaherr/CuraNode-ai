"""Engine pool settings.

Supabase's connection pooler closes connections that sit idle. Without a
liveness check the next request reuses a dead one and fails with
"connection was closed in the middle of operation" (seen on sign-in).
"""

from __future__ import annotations

from app.db.session import engine


def test_pool_checks_a_connection_is_alive_before_using_it():
    assert engine.sync_engine.pool._pre_ping is True


def test_pool_replaces_connections_before_the_pooler_drops_them():
    assert engine.sync_engine.pool._recycle == 300
