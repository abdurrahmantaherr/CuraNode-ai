"""Which failures mean "the network or the database server could not be reached", and a bounded
retry for opening a connection.

Learned from a real dev machine on a slow, flaky route to a far-away pooler: DNS lookups that
fail now and then (`gaierror`), TLS handshakes that stall past the connect timeout, and
connections reset by the other end (WinError 10054). Each is gone a moment later, so opening a
connection is worth a second try — but ONLY these. A wrong password, a missing database or a
refused login is not transient, and retrying it would only hammer the server (and risk a lockout).
"""

from __future__ import annotations

import errno
import socket
import time
from collections.abc import Callable

from asyncpg import exceptions as pg

# An `OSError` that is not one of the named subclasses is still a network failure when its code
# says so. On Windows a stalled connection is often a plain `OSError` (WinError 121, "the
# semaphore timeout period has expired") or "network unreachable", which Python does not map to
# `TimeoutError` / `ConnectionError`.
_NETWORK_ERRNOS = frozenset(
    code
    for code in (
        getattr(errno, name, None)
        for name in (
            "ENETUNREACH",
            "EHOSTUNREACH",
            "ENETDOWN",
            "ENETRESET",
            "EHOSTDOWN",
            "ETIMEDOUT",
            "ECONNRESET",
            "ECONNREFUSED",
            "ECONNABORTED",
            "EPIPE",
        )
    )
    if code is not None
)
_NETWORK_WINERRORS = frozenset(
    {
        53,  # the network path was not found
        64,  # the specified network name is no longer available
        121,  # the semaphore timeout period has expired (a connect that stalled)
        1225,  # the remote computer refused the connection
        1226,  # the connection was aborted
        1231,  # the network location cannot be reached
        10050,  # network is down
        10051,  # network is unreachable
        10053,
        10054,  # connection aborted / reset
        10060,  # connection timed out
        10061,  # connection refused
        10064,  # host is down
    }
)


def is_network_error(exc: BaseException) -> bool:
    """A timeout, a refused / reset / aborted connection, a failed DNS lookup, or an `OSError`
    whose code says the network (not a file, not a permission) was the problem."""
    if isinstance(exc, TimeoutError | ConnectionError | socket.gaierror):
        return True
    if isinstance(exc, OSError):
        return exc.errno in _NETWORK_ERRNOS or getattr(exc, "winerror", None) in _NETWORK_WINERRORS
    return False


def is_transient_connect_error(exc: BaseException) -> bool:
    """Worth opening the connection again: a network failure, or the server saying a connection
    could not be made / was lost (SQLSTATE class 08) or that it is not accepting connections yet.
    NOT an authentication or configuration error."""
    return is_network_error(exc) or isinstance(
        exc, pg.PostgresConnectionError | pg.CannotConnectNowError
    )


def connect_with_retry[T](
    connect: Callable[[], T],
    *,
    attempts: int,
    budget_s: float,
    backoff_s: float,
    pause: Callable[[float], None],
    clock: Callable[[], float] = time.monotonic,
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> T:
    """`connect()`, again if it failed in a way that is gone a moment later (see
    `is_transient_connect_error`) — at most `attempts` times, and never once `budget_s` seconds
    would be exceeded (an attempt that timed out has already spent most of a budget; a second
    one is worth it, a fifth is not). `pause` waits `backoff_s * attempt` between tries. Any other
    error is raised at once, and so is the last one when the tries or the budget run out."""
    started = clock()
    attempt = 0
    while True:
        attempt += 1
        try:
            return connect()
        except Exception as exc:
            if attempt >= attempts or not is_transient_connect_error(exc):
                raise
            delay = backoff_s * attempt
            if clock() - started + delay >= budget_s:
                raise
            if on_retry is not None:
                on_retry(attempt, exc)
            pause(delay)
