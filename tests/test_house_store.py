"""The house store end to end on a real database: opening, the import of the old files, the lock."""

from __future__ import annotations

import re
import sqlite3
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.exc import OperationalError

from soundtouch_zonemaster.adapters.files.channel_file import save_channels
from soundtouch_zonemaster.adapters.files.house_store import SqlHouseStore
from soundtouch_zonemaster.adapters.files.state_file import save_state
from soundtouch_zonemaster.application.errors import StoreBusyError, StoreError
from soundtouch_zonemaster.application.options import LegacyFiles
from soundtouch_zonemaster.domain.channellist import Channel, ChannelList
from soundtouch_zonemaster.domain.enums import ChannelKind
from soundtouch_zonemaster.domain.state import ZoneState

if TYPE_CHECKING:
    from pathlib import Path

LIST = ChannelList(channels=(Channel(number="1", name="One", kind=ChannelKind.RADIO, url="http://radio.example/1"),))
STATE = ZoneState(channel="1", members=("AABBCC0000A1",), dial_window_s=0.7)


def _quiet(_kind: str, _text: str) -> None:
    return None


def _store(database: str, lines: list[str] | None = None) -> SqlHouseStore:
    said = lines if lines is not None else []
    return SqlHouseStore(database, log=lambda kind, text: said.append(f"{kind}: {text}"))


def _legacy(tmp_path: Path) -> LegacyFiles:
    return LegacyFiles(
        state_file=tmp_path / "zone-state.json",
        channel_file=tmp_path / "channels.json",
        switch_file=tmp_path / "zone.switch",
    )


def _require(path: Path | None) -> Path:
    """Narrow ``LegacyFiles``'s optional fields for a test that just built them itself.

    ``LegacyFiles`` declares each field ``Path | None`` because the store may be asked to import
    fewer than three files; a test that constructs one with every path filled in knows better than
    the type does, and this is the one place that says so instead of every call site guessing.
    """
    assert path is not None
    return path


def test_a_first_start_imports_all_three_files_and_sets_them_aside(house_database: str, tmp_path: Path) -> None:
    legacy = _legacy(tmp_path)
    save_state(_require(legacy.state_file), STATE)
    save_channels(_require(legacy.channel_file), LIST)
    _require(legacy.switch_file).write_text("off\n", encoding="utf-8")
    store = _store(house_database)
    store.open(exclusive=True)
    store.import_legacy(legacy)
    assert (store.load_state(), store.load_channels(), store.is_on()) == (STATE, LIST, False)
    store.close()
    for path in (legacy.state_file, legacy.channel_file, legacy.switch_file):
        real = _require(path)
        assert not real.exists()
        assert real.with_name(real.name + ".imported").exists()


def test_a_part_already_held_is_not_overwritten_and_its_file_is_named(house_database: str, tmp_path: Path) -> None:
    lines: list[str] = []
    store = _store(house_database, lines)
    store.open(exclusive=True)
    store.set_switch(on=True)
    legacy = LegacyFiles(switch_file=tmp_path / "zone.switch")
    switch_file = _require(legacy.switch_file)
    switch_file.write_text("off\n", encoding="utf-8")
    store.import_legacy(legacy)
    assert store.is_on() is True
    assert switch_file.exists()
    assert any("not imported" in line and "zone.switch" in line for line in lines), lines
    store.close()


def test_an_unusable_channel_file_refuses_the_start_and_imports_nothing(house_database: str, tmp_path: Path) -> None:
    legacy = _legacy(tmp_path)
    state_file = _require(legacy.state_file)
    save_state(state_file, STATE)
    channel_file = _require(legacy.channel_file)
    channel_file.write_text('{"channels": [{"number": "x"}]}', encoding="utf-8")
    store = _store(house_database)
    store.open(exclusive=True)
    with pytest.raises(StoreError, match=r"channels\.json"):
        store.import_legacy(legacy)
    assert store.load_state() == ZoneState()
    assert state_file.exists()
    assert channel_file.exists()
    store.close()


def test_an_unusable_state_file_refuses_the_start_and_imports_nothing(house_database: str, tmp_path: Path) -> None:
    """A bad zone-state.json must refuse the WHOLE import, even with a good channel file and a
    good switch file sitting right beside it - proving the transaction rolls back rather than
    importing the two good parts and only refusing the bad one."""
    legacy = _legacy(tmp_path)
    state_file = _require(legacy.state_file)
    state_file.write_text('{"dial_window_s": "nope"}', encoding="utf-8")
    channel_file = _require(legacy.channel_file)
    save_channels(channel_file, LIST)
    switch_file = _require(legacy.switch_file)
    switch_file.write_text("off\n", encoding="utf-8")
    store = _store(house_database)
    store.open(exclusive=True)
    with pytest.raises(StoreError, match=r"zone-state\.json"):
        store.import_legacy(legacy)
    assert store.load_state() == ZoneState()
    assert store.load_channels() == ChannelList()
    assert store.is_on() is True
    for path in (state_file, channel_file, switch_file):
        assert path.exists()
        assert not path.with_name(path.name + ".imported").exists()
    store.close()


def test_an_unreadable_state_file_refuses_the_start_and_imports_nothing(house_database: str, tmp_path: Path) -> None:
    """A directory at the state file's path cannot be read as text on any platform, unlike a
    permission bit, which a root session ignores - so this is the portable way to force an
    unreadable file."""
    legacy = _legacy(tmp_path)
    state_file = _require(legacy.state_file)
    state_file.mkdir()
    store = _store(house_database)
    store.open(exclusive=True)
    with pytest.raises(StoreError, match=r"zone-state\.json"):
        store.import_legacy(legacy)
    assert store.load_state() == ZoneState()
    assert state_file.exists()
    store.close()


def test_an_unusable_state_file_already_held_is_not_even_read(house_database: str, tmp_path: Path) -> None:
    """The state part is checked BEFORE the file is parsed: a garbage zone-state.json must not stop
    a start that was never going to read it anyway."""
    lines: list[str] = []
    store = _store(house_database, lines)
    store.open(exclusive=True)
    held = ZoneState(channel="2")
    store.save_state(held)
    legacy = LegacyFiles(state_file=tmp_path / "zone-state.json")
    state_file = _require(legacy.state_file)
    state_file.write_text('{"dial_window_s": "nope"}', encoding="utf-8")
    store.import_legacy(legacy)
    assert store.load_state() == held
    assert state_file.exists()
    assert any("not imported" in line and "zone-state.json" in line for line in lines), lines
    store.close()


def test_a_duplicate_channel_number_in_the_legacy_file_refuses_and_imports_nothing(tmp_path: Path) -> None:
    """A hand-edited legacy channels.json can hold a duplicate number; the import refuses like any
    other unusable channel file (house_channels.write_channels), naming the database and nothing
    imported - not even the state that WOULD have gone in, because the whole import is one
    transaction.

    Fixed to a SQLite file rather than the ``house_database`` fixture: the refusal names the
    database, and the assertion below pins that name to ``zonemaster.sqlite``, which a PostgreSQL
    URL would never match.
    """
    legacy = _legacy(tmp_path)
    state_file = _require(legacy.state_file)
    save_state(state_file, STATE)
    channel_file = _require(legacy.channel_file)
    channel_file.write_text(
        '{"channels": ['
        '{"number": "1", "name": "One", "kind": "radio", "url": "http://radio.example/1"},'
        '{"number": "1", "name": "One again", "kind": "radio", "url": "http://radio.example/1b"}'
        "]}",
        encoding="utf-8",
    )
    store = _store(str(tmp_path / "zonemaster.sqlite"))
    store.open(exclusive=True)
    with pytest.raises(StoreError, match=r"zonemaster\.sqlite.*duplicate number"):
        store.import_legacy(legacy)
    assert store.load_state() == ZoneState()
    assert store.load_channels() == ChannelList()
    assert state_file.exists()
    assert channel_file.exists()
    store.close()


def test_an_unusable_channel_file_is_not_even_read_when_the_part_is_already_held(
    house_database: str, tmp_path: Path
) -> None:
    """The channel part is checked BEFORE the file is parsed: a garbage channels.json must not stop
    a start that was never going to read it anyway."""
    lines: list[str] = []
    store = _store(house_database, lines)
    store.open(exclusive=True)
    store.save_channels(LIST)
    legacy = LegacyFiles(channel_file=tmp_path / "channels.json")
    channel_file = _require(legacy.channel_file)
    channel_file.write_text('{"channels": [{"number": "x"}]}', encoding="utf-8")
    store.import_legacy(legacy)
    assert store.load_channels() == LIST
    assert channel_file.exists()
    assert any("not imported" in line and "channels.json" in line for line in lines), lines
    store.close()


def test_a_channel_list_already_held_is_not_overwritten_and_its_file_is_named(
    house_database: str, tmp_path: Path
) -> None:
    lines: list[str] = []
    store = _store(house_database, lines)
    store.open(exclusive=True)
    store.save_channels(LIST)
    legacy = LegacyFiles(channel_file=tmp_path / "channels.json")
    channel_file = _require(legacy.channel_file)
    other = ChannelList(
        channels=(Channel(number="2", name="Other", kind=ChannelKind.RADIO, url="http://radio.example/2"),)
    )
    save_channels(channel_file, other)
    store.import_legacy(legacy)
    assert store.load_channels() == LIST
    assert channel_file.exists()
    assert any("not imported" in line and "channels.json" in line for line in lines), lines
    store.close()


def test_a_state_already_held_is_not_overwritten_and_its_file_is_named(house_database: str, tmp_path: Path) -> None:
    lines: list[str] = []
    store = _store(house_database, lines)
    store.open(exclusive=True)
    held = ZoneState(channel="2")
    store.save_state(held)
    legacy = LegacyFiles(state_file=tmp_path / "zone-state.json")
    state_file = _require(legacy.state_file)
    save_state(state_file, STATE)
    store.import_legacy(legacy)
    assert store.load_state() == held
    assert state_file.exists()
    assert any("not imported" in line and "zone-state.json" in line for line in lines), lines
    store.close()


def test_missing_old_files_are_nothing_to_import(house_database: str, tmp_path: Path) -> None:
    store = _store(house_database)
    store.open(exclusive=True)
    store.import_legacy(_legacy(tmp_path))
    assert (store.load_state(), store.load_channels(), store.is_on()) == (ZoneState(), ChannelList(), True)
    store.close()


def test_state_and_channels_survive_a_reopen(house_database: str) -> None:
    store = _store(house_database)
    store.open(exclusive=True)
    store.save_state(STATE)
    store.save_channels(LIST)
    store.close()
    again = _store(house_database)
    again.open(exclusive=False)
    assert (again.load_state(), again.load_channels()) == (STATE, LIST)
    again.close()


def test_a_second_exclusive_open_is_refused_but_a_reader_is_not(house_database: str) -> None:
    holder = _store(house_database)
    holder.open(exclusive=True)
    with pytest.raises(StoreBusyError):
        _store(house_database).open(exclusive=True)
    reader = _store(house_database)
    reader.open(exclusive=False)
    assert reader.set_switch(on=False) is True
    assert holder.is_on() is False
    reader.close()
    holder.close()


def test_an_import_of_channels_needs_the_writer_lock(house_database: str, tmp_path: Path) -> None:
    source = tmp_path / "edited.json"
    save_channels(source, LIST)
    reader = _store(house_database)
    reader.open(exclusive=False)
    with pytest.raises(StoreError, match="exclusive"):
        reader.import_channels(source)
    reader.close()
    writer = _store(house_database)
    writer.open(exclusive=True)
    assert writer.import_channels(source) == LIST
    assert writer.load_channels() == LIST
    writer.close()


def test_the_export_is_what_the_import_reads(house_database: str, tmp_path: Path) -> None:
    store = _store(house_database)
    store.open(exclusive=True)
    store.save_channels(LIST)
    exported = tmp_path / "out.json"
    exported.write_text(store.export_channels(), encoding="utf-8")
    store.save_channels(ChannelList())
    assert store.import_channels(exported) == LIST
    store.close()


def test_using_a_store_before_opening_it_is_refused(house_database: str) -> None:
    with pytest.raises(StoreError, match="before open"):
        _store(house_database).load_state()


def test_using_a_store_before_opening_it_never_names_a_password_in_the_setting() -> None:
    """M2: the refusal used to embed the raw setting, which can carry a password nothing else does."""
    store = SqlHouseStore("postgresql+psycopg://zonemaster:s3cret@db.example/zonemaster", log=_quiet)
    with pytest.raises(StoreError) as caught:
        store.load_state()
    assert "s3cret" not in str(caught.value)


def test_close_twice_is_harmless(house_database: str) -> None:
    store = _store(house_database)
    store.open(exclusive=True)
    store.close()
    store.close()


def test_opening_an_already_open_store_refuses_without_leaking_the_connection(tmp_path: Path) -> None:
    """Fixed to a SQLite file: the assertion pins the database name in the refusal message."""
    store = _store(str(tmp_path / "zonemaster.sqlite"))
    store.open(exclusive=False)
    with pytest.raises(StoreError, match=r"zonemaster\.sqlite.*already open"):
        store.open(exclusive=False)
    # The first connection must still be the one in use, not silently replaced by a leaked second.
    store.save_state(STATE)
    assert store.load_state() == STATE
    store.close()


def test_import_legacy_needs_the_writer_lock(house_database: str) -> None:
    reader = _store(house_database)
    reader.open(exclusive=False)
    with pytest.raises(StoreError, match="exclusive"):
        reader.import_legacy(LegacyFiles())
    reader.close()


def test_a_driver_error_leaves_the_store_as_a_store_error(tmp_path: Path) -> None:
    # Dropping the tables under an open store is what makes the DRIVER fail on the next read.
    # Replacing the file would not: a pooled connection keeps reading the unlinked inode.
    database = tmp_path / "house.sqlite"
    store = SqlHouseStore(str(database), log=_quiet)
    store.open(exclusive=False)
    raw = sqlite3.connect(database)
    raw.execute("DROP TABLE channel")
    raw.execute("DROP TABLE switch")
    raw.commit()
    raw.close()
    with pytest.raises(StoreError, match=re.escape(str(database))):
        store.load_channels()
    assert store.is_on() is True
    store.close()


def test_a_url_carrying_a_password_refuses_the_open_without_repeating_it() -> None:
    store = SqlHouseStore("postgresql+psycopg://zonemaster:s3cret@db.example/zonemaster", log=_quiet)
    with pytest.raises(StoreError) as caught:
        store.open(exclusive=False)
    assert "s3cret" not in str(caught.value)


def test_opening_an_unknown_driver_url_refuses_as_a_store_error() -> None:
    """The root cause was in HouseDatabase.open(); this pins that the store surfaces it the same way."""
    store = SqlHouseStore("postgresql+nosuchdriver://zonemaster@db.example/zonemaster", log=_quiet)
    with pytest.raises(StoreError, match="nosuchdriver"):
        store.open(exclusive=False)


class _ExplodingLock:
    """A writer lock stub whose release() fails the way a dead PostgreSQL session does.

    Raising ``OperationalError`` rather than a plain ``RuntimeError`` matches the real failure this
    finding names: a lock release that fails is always a DBAPI error SQLAlchemy wraps, never a bare
    Python exception. Plugged into the real ``HouseDatabase`` a real, opened store already holds -
    the same technique ``tests/test_house_db.py`` uses for its own close() cleanup test - rather than
    monkeypatching the store or ``HouseDatabase`` themselves.
    """

    def acquire(self) -> None:
        return

    def release(self) -> None:
        raise OperationalError("statement", {}, Exception("boom"))


def test_close_resets_the_store_even_when_the_underlying_close_raises(house_database: str) -> None:
    store = _store(house_database)
    store.open(exclusive=True)
    house = store._house  # pyright: ignore[reportPrivateUsage]
    assert house is not None
    # The real lock is displaced by the stub, so close() never releases it; it is released here
    # instead. On PostgreSQL it is a server session holding the advisory lock, and left alone it
    # would hold it until the connection is garbage-collected, refusing every later writer.
    displaced = house._lock  # pyright: ignore[reportPrivateUsage]
    assert displaced is not None
    house._lock = _ExplodingLock()  # pyright: ignore[reportPrivateUsage]
    house._locked = True  # pyright: ignore[reportPrivateUsage]
    try:
        with pytest.raises(StoreError, match=re.escape(house_database)):
            store.close()
        # A store left thinking it is still open would refuse the SAME store's next open() by name
        # as "already open" - a fresh instance can never see that, so this must reuse `store`.
        store.open(exclusive=False)
        store.close()
    finally:
        displaced.release()
