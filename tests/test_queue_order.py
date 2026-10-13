"""P6: the pure queue-ordering rules. No database, no clock."""

from __future__ import annotations

import pytest
from app.queueing import order


def test_the_first_token_is_one():
    assert order.next_token([]) == 1


def test_the_next_token_is_one_past_the_highest_ever_used():
    assert order.next_token([1, 2, 5]) == 6
    assert order.next_token(iter([3])) == 4  # any iterable


def test_position_is_the_number_ahead_plus_one():
    assert order.position([4, 7, 9], 4) == 1
    assert order.position([4, 7, 9], 7) == 2
    assert order.position([4, 7, 9], 9) == 3


def test_position_ignores_the_order_the_tokens_are_listed_in():
    assert order.position([9, 4, 7], 7) == 2


@pytest.mark.parametrize(
    ("ahead", "in_room", "minutes", "expected"),
    [
        (0, False, 15, 0),  # you're next, nobody in the room
        (0, True, 15, 15),  # next, but someone is being seen
        (2, False, 15, 30),
        (2, True, 15, 45),
        (3, True, 30, 120),
    ],
)
def test_the_wait_is_people_ahead_plus_the_room_times_minutes(ahead, in_room, minutes, expected):
    assert order.wait_minutes(ahead, in_room, minutes) == expected


def test_move_up_swaps_with_the_one_ahead():
    assert order.moved_up(["a", "b", "c"], "c") == ["a", "c", "b"]
    assert order.moved_up(["a", "b", "c"], "b") == ["b", "a", "c"]


def test_move_up_of_the_first_changes_nothing():
    assert order.moved_up(["a", "b", "c"], "a") == ["a", "b", "c"]


def test_make_next_puts_them_first_and_keeps_the_rest_in_order():
    assert order.made_next(["a", "b", "c", "d"], "c") == ["c", "a", "b", "d"]
    assert order.made_next(["a", "b"], "a") == ["a", "b"]


def test_inputs_are_not_changed():
    original = ["a", "b", "c"]
    order.moved_up(original, "c")
    order.made_next(original, "c")
    assert original == ["a", "b", "c"]


def test_reassign_gives_the_sorted_tokens_to_the_new_order():
    # a had 2, b had 5, c had 9; c is made next
    new = order.reassign([2, 5, 9], ["c", "a", "b"])
    assert new == {"c": 2, "a": 5, "b": 9}


def test_reassign_keeps_the_set_of_tokens_unique():
    new = order.reassign([7, 3, 5], ["x", "y", "z"])
    assert sorted(new.values()) == [3, 5, 7] and len(set(new.values())) == 3


def test_reassign_needs_one_token_per_person():
    with pytest.raises(ValueError):
        order.reassign([1, 2], ["a", "b", "c"])
