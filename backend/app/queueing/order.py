"""Pure queue-ordering rules (spec 3.2): no database, no clock.

The queue runs in arrival order, which is token order: a check-in or a walk-in
takes the next unused token of its session. A patient's token is internal; the
screens show their position. Moving someone forward only permutes the token
numbers the waiting patients already hold, so tokens stay unique.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Sequence


def next_token(tokens: Iterable[int]) -> int:
    """One past the highest token ever used in the session (a seen patient's
    token is never reused)."""
    return max(tokens, default=0) + 1


def position(waiting_tokens: Sequence[int], token: int) -> int:
    """Place in line: the waiting patients with a smaller token, plus one."""
    return sum(1 for other in waiting_tokens if other < token) + 1


def wait_minutes(ahead: int, in_room: bool, minutes: int) -> int:
    """The estimate: everyone ahead takes a turn, and so does the patient in the room."""
    return (ahead + (1 if in_room else 0)) * minutes


def moved_up[K: Hashable](order: Sequence[K], who: K) -> list[K]:
    """`who` swaps places with the person directly ahead (the first stays first)."""
    items = list(order)
    index = items.index(who)
    if index > 0:
        items[index - 1], items[index] = items[index], items[index - 1]
    return items


def made_next[K: Hashable](order: Sequence[K], who: K) -> list[K]:
    """`who` goes to the front; everyone else keeps their relative order."""
    return [who, *(item for item in order if item != who)]


def reassign[K: Hashable](tokens: Sequence[int], new_order: Sequence[K]) -> dict[K, int]:
    """Give the sorted `tokens` to `new_order`, one each, smallest first."""
    if len(tokens) != len(new_order):
        raise ValueError("one token per person")
    return dict(zip(new_order, sorted(tokens), strict=True))
