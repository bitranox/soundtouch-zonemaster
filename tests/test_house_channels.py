"""The channel list, as the house database holds it, with the channel file's own rules."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import insert

from soundtouch_zonemaster.adapters.files.channel_file import channels_json, save_channels
from soundtouch_zonemaster.adapters.files.house_channels import channel_count, read_channels, write_channels
from soundtouch_zonemaster.adapters.files.house_db import HouseDatabase
from soundtouch_zonemaster.adapters.files.house_schema import CHANNEL
from soundtouch_zonemaster.application.errors import StoreError
from soundtouch_zonemaster.domain.channellist import Channel, ChannelList
from soundtouch_zonemaster.domain.enums import ChannelEnd, ChannelKind

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

HOUSE = ChannelList(
    channels=(
        Channel(number="3", name="Station three", kind=ChannelKind.RADIO, url="http://radio.example/3"),
        Channel(
            number="1", name="Station one", kind=ChannelKind.RADIO, url="http://radio.example/1", in_rotation=False
        ),
        Channel(
            number="11",
            name="A book",
            kind=ChannelKind.MPD,
            url="http://127.0.0.1:8001/",
            mpd_directory="audiobooks/A book",
            end=ChannelEnd.STOP,
        ),
    )
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


def _radio(number: str) -> Channel:
    return Channel(
        number=number, name=f"Station {number}", kind=ChannelKind.RADIO, url=f"http://radio.example/{number}"
    )


def test_the_list_comes_back_in_the_order_it_was_written(house_database: str, database: HouseDatabase) -> None:
    with database.writing() as connection:
        write_channels(connection, HOUSE, where=house_database)
    with database.reading() as connection:
        assert read_channels(connection, where=house_database) == HOUSE
        assert channel_count(connection) == 3


def test_an_empty_database_holds_an_empty_list(house_database: str, database: HouseDatabase) -> None:
    with database.reading() as connection:
        assert read_channels(connection, where=house_database) == ChannelList()
        assert channel_count(connection) == 0


def test_a_row_the_channel_rules_refuse_is_refused_with_its_count(house_database: str, database: HouseDatabase) -> None:
    with database.writing() as connection:
        connection.execute(
            insert(CHANNEL).values(
                position=0,
                number="7x",
                name="broken",
                kind="radio",
                url="not a url",
                mpd_entry="",
                mpd_directory="",
                in_rotation=1,
                at_end="wrap",
            )
        )
    with (
        database.reading() as connection,
        pytest.raises(
            StoreError, match=rf"{re.escape(house_database)}: the channel list is unusable \(2 problem\(s\)\)"
        ),
    ):
        read_channels(connection, where=house_database)


def test_the_export_is_the_channel_file_byte_for_byte(tmp_path: Path) -> None:
    written = tmp_path / "channels.json"
    save_channels(written, HOUSE)
    assert channels_json(HOUSE) == written.read_text(encoding="utf-8")


def test_a_duplicate_channel_number_is_a_named_refusal(house_database: str, database: HouseDatabase) -> None:
    with database.writing() as connection:
        write_channels(connection, HOUSE, where=house_database)
    duplicated = ChannelList(
        channels=(
            Channel(number="1", name="Station one", kind=ChannelKind.RADIO, url="http://radio.example/1"),
            Channel(number="1", name="Station one again", kind=ChannelKind.RADIO, url="http://radio.example/1b"),
        )
    )
    with (
        pytest.raises(StoreError, match=rf"{re.escape(house_database)}: the channel list has a duplicate number \(1\)"),
        database.writing() as connection,
    ):
        write_channels(connection, duplicated, where=house_database)
    # The failed write must not have overwritten the list that was there before it.
    with database.reading() as connection:
        assert read_channels(connection, where=house_database) == HOUSE
        assert channel_count(connection) == 3


def test_the_list_order_is_kept_even_when_the_numbers_sort_otherwise(
    house_database: str, database: HouseDatabase
) -> None:
    # "10" is not a channel number a preset key can press (the alphabet is 1-6, no 0), so "16" is
    # used instead: it still sorts as text ("1" < "16" < "2") ahead of where it stands here (last).
    channels = ChannelList(channels=tuple(_radio(number) for number in ("2", "16", "1")))
    with database.writing() as connection:
        write_channels(connection, channels, where=house_database)
    with database.reading() as connection:
        numbers = [one.number for one in read_channels(connection, where=house_database).channels]
    assert numbers == ["2", "16", "1"]
