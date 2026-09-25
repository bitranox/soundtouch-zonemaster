"""The house store end to end on a real file: opening, the import of the old files, the lock."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from soundtouch_zonemaster.adapters.files.channel_file import save_channels
from soundtouch_zonemaster.adapters.files.house_store import SqliteHouseStore
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


def _store(tmp_path: Path, lines: list[str] | None = None) -> SqliteHouseStore:
    said = lines if lines is not None else []
    return SqliteHouseStore(tmp_path / "zonemaster.sqlite", log=lambda kind, text: said.append(f"{kind}: {text}"))


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


def test_a_first_start_imports_all_three_files_and_sets_them_aside(tmp_path: Path) -> None:
    legacy = _legacy(tmp_path)
    save_state(_require(legacy.state_file), STATE)
    save_channels(_require(legacy.channel_file), LIST)
    _require(legacy.switch_file).write_text("off\n", encoding="utf-8")
    store = _store(tmp_path)
    store.open(exclusive=True)
    store.import_legacy(legacy)
    assert (store.load_state(), store.load_channels(), store.is_on()) == (STATE, LIST, False)
    store.close()
    for path in (legacy.state_file, legacy.channel_file, legacy.switch_file):
        real = _require(path)
        assert not real.exists()
        assert real.with_name(real.name + ".imported").exists()


def test_a_part_already_held_is_not_overwritten_and_its_file_is_named(tmp_path: Path) -> None:
    lines: list[str] = []
    store = _store(tmp_path, lines)
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


def test_an_unusable_channel_file_refuses_the_start_and_imports_nothing(tmp_path: Path) -> None:
    legacy = _legacy(tmp_path)
    state_file = _require(legacy.state_file)
    save_state(state_file, STATE)
    channel_file = _require(legacy.channel_file)
    channel_file.write_text('{"channels": [{"number": "x"}]}', encoding="utf-8")
    store = _store(tmp_path)
    store.open(exclusive=True)
    with pytest.raises(StoreError, match=r"channels\.json"):
        store.import_legacy(legacy)
    assert store.load_state() == ZoneState()
    assert state_file.exists()
    assert channel_file.exists()
    store.close()


def test_an_unusable_state_file_refuses_the_start_and_imports_nothing(tmp_path: Path) -> None:
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
    store = _store(tmp_path)
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


def test_an_unreadable_state_file_refuses_the_start_and_imports_nothing(tmp_path: Path) -> None:
    """A directory at the state file's path cannot be read as text on any platform, unlike a
    permission bit, which a root session ignores - so this is the portable way to force an
    unreadable file."""
    legacy = _legacy(tmp_path)
    state_file = _require(legacy.state_file)
    state_file.mkdir()
    store = _store(tmp_path)
    store.open(exclusive=True)
    with pytest.raises(StoreError, match=r"zone-state\.json"):
        store.import_legacy(legacy)
    assert store.load_state() == ZoneState()
    assert state_file.exists()
    store.close()


def test_an_unusable_state_file_already_held_is_not_even_read(tmp_path: Path) -> None:
    """The state part is checked BEFORE the file is parsed: a garbage zone-state.json must not stop
    a start that was never going to read it anyway."""
    lines: list[str] = []
    store = _store(tmp_path, lines)
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
    transaction."""
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
    store = _store(tmp_path)
    store.open(exclusive=True)
    with pytest.raises(StoreError, match=r"zonemaster\.sqlite.*duplicate number"):
        store.import_legacy(legacy)
    assert store.load_state() == ZoneState()
    assert store.load_channels() == ChannelList()
    assert state_file.exists()
    assert channel_file.exists()
    store.close()


def test_an_unusable_channel_file_is_not_even_read_when_the_part_is_already_held(tmp_path: Path) -> None:
    """The channel part is checked BEFORE the file is parsed: a garbage channels.json must not stop
    a start that was never going to read it anyway."""
    lines: list[str] = []
    store = _store(tmp_path, lines)
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


def test_a_channel_list_already_held_is_not_overwritten_and_its_file_is_named(tmp_path: Path) -> None:
    lines: list[str] = []
    store = _store(tmp_path, lines)
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


def test_a_state_already_held_is_not_overwritten_and_its_file_is_named(tmp_path: Path) -> None:
    lines: list[str] = []
    store = _store(tmp_path, lines)
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


def test_missing_old_files_are_nothing_to_import(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.open(exclusive=True)
    store.import_legacy(_legacy(tmp_path))
    assert (store.load_state(), store.load_channels(), store.is_on()) == (ZoneState(), ChannelList(), True)
    store.close()


def test_state_and_channels_survive_a_reopen(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.open(exclusive=True)
    store.save_state(STATE)
    store.save_channels(LIST)
    store.close()
    again = _store(tmp_path)
    again.open(exclusive=False)
    assert (again.load_state(), again.load_channels()) == (STATE, LIST)
    again.close()


def test_a_second_exclusive_open_is_refused_but_a_reader_is_not(tmp_path: Path) -> None:
    holder = _store(tmp_path)
    holder.open(exclusive=True)
    with pytest.raises(StoreBusyError):
        _store(tmp_path).open(exclusive=True)
    reader = _store(tmp_path)
    reader.open(exclusive=False)
    assert reader.set_switch(on=False) is True
    assert holder.is_on() is False
    reader.close()
    holder.close()


def test_an_import_of_channels_needs_the_writer_lock(tmp_path: Path) -> None:
    source = tmp_path / "edited.json"
    save_channels(source, LIST)
    reader = _store(tmp_path)
    reader.open(exclusive=False)
    with pytest.raises(StoreError, match="exclusive"):
        reader.import_channels(source)
    reader.close()
    writer = _store(tmp_path)
    writer.open(exclusive=True)
    assert writer.import_channels(source) == LIST
    assert writer.load_channels() == LIST
    writer.close()


def test_the_export_is_what_the_import_reads(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.open(exclusive=True)
    store.save_channels(LIST)
    exported = tmp_path / "out.json"
    exported.write_text(store.export_channels(), encoding="utf-8")
    store.save_channels(ChannelList())
    assert store.import_channels(exported) == LIST
    store.close()


def test_using_a_store_before_opening_it_is_refused(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="before open"):
        _store(tmp_path).load_state()


def test_close_twice_is_harmless(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.open(exclusive=True)
    store.close()
    store.close()


def test_opening_an_already_open_store_refuses_without_leaking_the_connection(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.open(exclusive=False)
    with pytest.raises(StoreError, match=r"zonemaster\.sqlite.*already open"):
        store.open(exclusive=False)
    # The first connection must still be the one in use, not silently replaced by a leaked second.
    store.save_state(STATE)
    assert store.load_state() == STATE
    store.close()


def test_import_legacy_needs_the_writer_lock(tmp_path: Path) -> None:
    reader = _store(tmp_path)
    reader.open(exclusive=False)
    with pytest.raises(StoreError, match="exclusive"):
        reader.import_legacy(LegacyFiles())
    reader.close()
