"""The house store end to end on a real database: opening, the channel export and import, the lock."""

from __future__ import annotations

import re
import sqlite3
import threading
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.exc import OperationalError
from switch_race import InThread, a_person_writing, wait_until_a_writer_waits

from soundtouch_zonemaster.adapters.files.channel_file import save_channels
from soundtouch_zonemaster.adapters.files.house_store import SqlHouseStore
from soundtouch_zonemaster.adapters.files.house_switch import write_switch
from soundtouch_zonemaster.application.errors import StoreBusyError, StoreError
from soundtouch_zonemaster.domain.channellist import Channel, ChannelList
from soundtouch_zonemaster.domain.enums import ChannelKind
from soundtouch_zonemaster.domain.state import ZoneState

if TYPE_CHECKING:
    from pathlib import Path

LIST = ChannelList(channels=(Channel(number="1", name="One", kind=ChannelKind.RADIO, url="http://radio.example/1"),))
STATE = ZoneState(channel="1", members=("AABBCC0000A1",))


def _quiet(_kind: str, _text: str) -> None:
    return None


def _store(database: str, lines: list[str] | None = None) -> SqlHouseStore:
    said = lines if lines is not None else []
    return SqlHouseStore(database, log=lambda kind, text: said.append(f"{kind}: {text}"))


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


def test_two_simultaneous_switch_writes_never_collide_on_a_unique_violation(house_database: str) -> None:
    """``switch`` opens the store WITHOUT the writer lock (the whole point: turning the house off
    is done to a running service), so two invocations really can write the row at the same
    instant. A delete-then-insert races under PostgreSQL's READ COMMITTED: one writer's DELETE can
    unblock and find the OTHER writer's just-committed row still there, then its own INSERT
    duplicates that id. SQLite serialises every writer through one file lock and never shows
    this, so this proves it only where it can happen (OPEN-WORK rank 204)."""
    if not house_database.startswith("postgresql"):
        pytest.skip("SQLite's own writer lock already serialises every write; nothing races there")
    a = _store(house_database)
    b = _store(house_database)
    a.open(exclusive=False)
    b.open(exclusive=False)
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _flip(store: SqlHouseStore, *, on: bool) -> None:
        barrier.wait()
        try:
            for _ in range(30):
                store.set_switch(on=on)
        except BaseException as exc:  # noqa: BLE001 - collected and asserted on below, not swallowed
            errors.append(exc)

    first = threading.Thread(target=_flip, args=(a,), kwargs={"on": True})
    second = threading.Thread(target=_flip, args=(b,), kwargs={"on": False})
    first.start()
    second.start()
    first.join()
    second.join()
    a.close()
    b.close()
    assert errors == []


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
    report = store.export_channels(exported)
    assert report.count == len(LIST.channels)
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


@pytest.mark.parametrize(
    ("held", "person", "changed"),
    [
        ("on", "off", False),
        ("never set", "off", False),
        ("on", "on", True),
    ],
)
def test_a_switch_a_person_sets_while_another_writer_waits_is_the_one_that_writer_compares_with(
    house_database: str, held: str, person: str, *, changed: bool
) -> None:
    """``changed`` is what a deploy decides whether to turn the house back on by, so it must be true.

    The person's ``switch`` write is held uncommitted, the store's write starts behind it, and the
    person commits once the store waits. Under READ COMMITTED a writer that reads the switch before
    it locks anything read the word from before the person, then wrote over theirs and said it had
    turned the house off. The switch is locked first, so the read waits and sees the person's word.
    A row nobody ever set is covered too: there is no row to lock then, which is why the lock is
    the table's. The last arm is the control: a person's write that leaves the house on still lets
    the store's off count as the change it is.
    """
    if not house_database.startswith("postgresql"):
        pytest.skip("SQLite's writer takes BEGIN IMMEDIATE before it reads, so no other write can come between")
    store = _store(house_database)
    store.open(exclusive=False)
    try:
        if held == "on":
            store.set_switch(on=True)
        with a_person_writing(house_database) as their:
            write_switch(their, on=person == "on")
            writer = InThread(lambda: store.set_switch(on=False))
            wait_until_a_writer_waits(house_database)
            their.commit()
            assert writer.result() is changed
        assert store.is_on() is False
    finally:
        store.close()
