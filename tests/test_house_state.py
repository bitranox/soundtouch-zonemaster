"""The state and the switch, as the house database holds them."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from soundtouch_zonemaster.adapters.files.house_db import HouseDatabase
from soundtouch_zonemaster.adapters.files.house_state import read_state, write_state
from soundtouch_zonemaster.adapters.files.house_switch import DbSwitch, read_switch, write_switch
from soundtouch_zonemaster.domain.state import Place, ZoneState

if TYPE_CHECKING:
    from collections.abc import Iterator

EVERY_FIELD = ZoneState(
    channel="11",
    members=("AABBCC0000A1", "AABBCC0000A3"),
    muted={"AABBCC0000A2": 17},
    out_of_multiroom=("AABBCC0000A4", "AABBCC0000A5"),
    positions={"11": Place(track=4, seconds=93.5, file="Book/05.mp3"), "12": Place(track=0, seconds=0.0)},
    owed_volume={"AABBCC0000A5": 3},
)


@pytest.fixture
def database(house_database: str) -> Iterator[HouseDatabase]:
    """Opened once per test and closed in teardown, so a failing assertion cannot leak it -
    unlike calling a bare ``_opened()`` helper and closing by hand at the end of the test, which a
    failure before that line skips entirely (OPEN-WORK rank 205)."""
    opened = HouseDatabase(house_database)
    opened.open(exclusive=False)
    try:
        yield opened
    finally:
        opened.close()


def test_an_empty_database_has_no_state_rather_than_an_empty_one(database: HouseDatabase) -> None:
    with database.reading() as connection:
        assert read_state(connection) is None


def test_every_field_comes_back_as_it_was_written(database: HouseDatabase) -> None:
    with database.writing() as connection:
        write_state(connection, EVERY_FIELD)
    with database.reading() as connection:
        assert read_state(connection) == EVERY_FIELD


def test_a_second_write_replaces_the_first_entirely(database: HouseDatabase) -> None:
    with database.writing() as connection:
        write_state(connection, EVERY_FIELD)
    with database.writing() as connection:
        write_state(connection, ZoneState(members=("AABBCC0000A2",)))
    with database.reading() as connection:
        assert read_state(connection) == ZoneState(members=("AABBCC0000A2",))


def test_the_member_order_is_the_order_it_was_written_in(database: HouseDatabase) -> None:
    with database.writing() as connection:
        write_state(connection, ZoneState(members=("AABBCC0000A3", "AABBCC0000A1", "AABBCC0000A2")))
    with database.reading() as connection:
        assert read_state(connection) == ZoneState(members=("AABBCC0000A3", "AABBCC0000A1", "AABBCC0000A2"))


def test_a_switch_never_set_reads_as_unset_and_is_on(database: HouseDatabase) -> None:
    with database.reading() as connection:
        assert read_switch(connection) is None


def test_off_is_off_and_on_is_on(database: HouseDatabase) -> None:
    with database.writing() as connection:
        assert write_switch(connection, on=False) is True
    with database.reading() as connection:
        assert read_switch(connection) is False
    with database.writing() as connection:
        assert write_switch(connection, on=True) is True
    with database.reading() as connection:
        assert read_switch(connection) is True


def test_switching_on_a_switch_never_set_is_no_change(database: HouseDatabase) -> None:
    with database.writing() as connection:
        assert write_switch(connection, on=True) is False


def test_the_watch_reports_each_change_once() -> None:
    state = {"on": True}
    lines: list[str] = []

    async def is_on() -> bool:
        return state["on"]

    switch = DbSwitch(
        is_on,
        where="house.sqlite",
        log=lambda kind, text: lines.append(f"{kind}: {text}"),
        poll_s=0.01,
    )

    async def three_values() -> list[bool]:
        seen: list[bool] = []
        async for value in switch.watch():
            seen.append(value)
            if len(seen) == 1:
                state["on"] = False
            elif len(seen) == 2:
                state["on"] = True
            else:
                return seen
        return seen

    assert asyncio.run(asyncio.wait_for(three_values(), timeout=5.0)) == [True, False, True]
    # One line per change and none per poll: the watch polls every 10 ms, so a line per read would
    # be dozens here.
    assert lines == ["switch: house.sqlite: on", "switch: house.sqlite: off", "switch: house.sqlite: on"]
