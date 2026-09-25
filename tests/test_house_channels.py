"""The channel list, as the house database holds it, with the channel file's own rules."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from soundtouch_zonemaster.adapters.files.channel_file import channels_json, save_channels
from soundtouch_zonemaster.adapters.files.house_channels import channel_count, read_channels, write_channels
from soundtouch_zonemaster.adapters.files.house_db import connect, transaction
from soundtouch_zonemaster.application.errors import StoreError
from soundtouch_zonemaster.domain.channellist import Channel, ChannelList
from soundtouch_zonemaster.domain.enums import ChannelEnd, ChannelKind

if TYPE_CHECKING:
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


def test_the_list_comes_back_in_the_order_it_was_written(tmp_path: Path) -> None:
    database = tmp_path / "house.sqlite"
    connection = connect(database)
    with transaction(connection):
        write_channels(connection, HOUSE)
    assert read_channels(connection, database=database) == HOUSE
    assert channel_count(connection) == 3


def test_an_empty_database_holds_an_empty_list(tmp_path: Path) -> None:
    database = tmp_path / "house.sqlite"
    connection = connect(database)
    assert read_channels(connection, database=database) == ChannelList()
    assert channel_count(connection) == 0


def test_a_row_the_channel_rules_refuse_is_refused_with_its_count(tmp_path: Path) -> None:
    database = tmp_path / "house.sqlite"
    connection = connect(database)
    with transaction(connection):
        connection.execute(
            "INSERT INTO channel (number, name, kind, url, mpd_entry, mpd_directory, in_rotation, at_end)"
            " VALUES ('7x', 'broken', 'radio', 'not a url', '', '', 1, 'wrap')"
        )
    with pytest.raises(StoreError, match=rf"{database}: the channel list is unusable \(2 problem\(s\)\)"):
        read_channels(connection, database=database)


def test_the_export_is_the_channel_file_byte_for_byte(tmp_path: Path) -> None:
    written = tmp_path / "channels.json"
    save_channels(written, HOUSE)
    assert channels_json(HOUSE) == written.read_text(encoding="utf-8")


def test_a_duplicate_channel_number_is_a_named_refusal(tmp_path: Path) -> None:
    database = tmp_path / "house.sqlite"
    connection = connect(database)
    with transaction(connection):
        write_channels(connection, HOUSE)
    duplicated = ChannelList(
        channels=(
            Channel(number="1", name="Station one", kind=ChannelKind.RADIO, url="http://radio.example/1"),
            Channel(number="1", name="Station one again", kind=ChannelKind.RADIO, url="http://radio.example/1b"),
        )
    )
    with (
        pytest.raises(StoreError, match=rf"{database}: the channel list has a duplicate number \(1\)"),
        transaction(connection),
    ):
        write_channels(connection, duplicated)
    # The failed write must not have overwritten the list that was there before it.
    assert read_channels(connection, database=database) == HOUSE
    assert channel_count(connection) == 3
