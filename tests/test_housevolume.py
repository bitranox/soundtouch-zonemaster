"""The house-volume rule: which report is a step for the whole house, and how far each box goes.

Decided by the user 2026-09-24 (OPEN-WORK rank 191): a thumb at a box in the zone (held then, tapped
once since 2026-09-25), then volume. The remote cannot send two keys at once - measured the same
evening, the volume key never reached the box while the thumb was down - so the thumb opens a
window and the box's own volume REPORTS inside it are the steps. The other boxes move by the same
step, each from its own level, which keeps a quiet room quiet. The clock is passed in on every
call, so each edge of the window is asserted exactly.
"""

from __future__ import annotations

from soundtouch_zonemaster.domain.housevolume import (
    MAX_VOLUME,
    OPEN_S,
    RENEW_S,
    HouseVolume,
    owe,
    stepped,
)

ROOM1, ROOM4, ROOM2 = "AABBCC0000A1", "AABBCC0000A2", "AABBCC0000A4"


def test_a_step_moves_a_level_by_that_much() -> None:
    assert stepped(24, +5) == 29
    assert stepped(24, -10) == 14


def test_a_step_stops_at_zero_and_at_the_top() -> None:
    assert stepped(3, -5) == 0
    assert stepped(98, +5) == MAX_VOLUME


def test_a_report_with_no_window_open_is_no_step() -> None:
    house = HouseVolume()
    house.reported(ROOM1, 11, at=0.0)

    assert house.reported(ROOM1, 12, at=1.0) is None


def test_a_report_inside_the_window_is_the_difference_from_the_last_one() -> None:
    house = HouseVolume()
    house.reported(ROOM1, 11, at=0.0)
    house.open(ROOM1, at=10.0)

    assert house.reported(ROOM1, 12, at=10.5) == +1
    assert house.reported(ROOM1, 14, at=10.8) == +2


def test_a_first_report_from_a_box_never_heard_is_remembered_but_is_no_step() -> None:
    """Nothing to take the difference from: the level is written down and the next one counts."""
    house = HouseVolume()
    house.open(ROOM1, at=0.0)

    assert house.reported(ROOM1, 12, at=0.5) is None
    assert house.level_of(ROOM1) == 12
    assert house.reported(ROOM1, 13, at=0.8) == +1


def test_the_window_closes_when_nothing_arrives_in_time() -> None:
    house = HouseVolume()
    house.reported(ROOM1, 11, at=0.0)
    house.open(ROOM1, at=10.0)

    assert house.reported(ROOM1, 12, at=10.0 + OPEN_S + 0.01) is None


def test_every_report_keeps_the_window_open_for_the_next_one() -> None:
    """A held volume key ramps about every 300 ms and a person tapping pauses to listen."""
    house = HouseVolume()
    house.reported(ROOM1, 11, at=0.0)
    house.open(ROOM1, at=10.0)
    first = 10.0 + OPEN_S - 0.1

    assert house.reported(ROOM1, 12, at=first) == +1
    assert house.reported(ROOM1, 13, at=first + RENEW_S - 0.1) == +1
    assert house.reported(ROOM1, 14, at=first + 2 * RENEW_S) is None


def test_only_the_box_that_opened_the_window_steps_the_house() -> None:
    """The other boxes' reports are our own writes coming back, and must never step again."""
    house = HouseVolume()
    house.reported(ROOM1, 11, at=0.0)
    house.reported(ROOM4, 20, at=0.0)
    house.open(ROOM1, at=10.0)

    assert house.reported(ROOM4, 21, at=10.5) is None
    assert house.level_of(ROOM4) == 21


def test_a_closed_window_steps_nothing_although_its_time_is_not_up() -> None:
    """A double tap began as a single tap, which opened the window: the second tap closes it again,
    so a volume change right after a rotation change is that room's own."""
    house = HouseVolume()
    house.reported(ROOM1, 11, at=0.0)
    house.open(ROOM1, at=10.0)
    house.close(ROOM1)

    assert house.reported(ROOM1, 12, at=10.5) is None


def test_closing_a_window_that_is_not_open_is_not_an_error() -> None:
    house = HouseVolume()
    house.close(ROOM1)
    house.reported(ROOM1, 11, at=0.0)

    assert house.reported(ROOM1, 12, at=0.5) is None


def test_a_report_of_the_same_level_is_no_step() -> None:
    house = HouseVolume()
    house.reported(ROOM1, 11, at=0.0)
    house.open(ROOM1, at=10.0)

    assert house.reported(ROOM1, 11, at=10.5) is None


def test_a_box_that_is_off_is_owed_every_step_it_missed() -> None:
    owed = owe({}, [ROOM4, ROOM2], +5)
    owed = owe(owed, [ROOM4], -10)

    assert owed == {ROOM4: -5, ROOM2: +5}


def test_steps_that_cancel_out_owe_nothing() -> None:
    assert owe(owe({}, [ROOM4], +5), [ROOM4], -5) == {}
