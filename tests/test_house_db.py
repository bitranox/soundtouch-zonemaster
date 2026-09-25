"""The house database file: what opening it guarantees, and what it refuses."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from soundtouch_zonemaster.adapters.files.house_db import SCHEMA_VERSION, WriterLock, connect, transaction
from soundtouch_zonemaster.application.errors import StoreBusyError, StoreError

if TYPE_CHECKING:
    from pathlib import Path


def test_a_new_file_is_created_with_every_table_and_the_schema_version(tmp_path: Path) -> None:
    connection = connect(tmp_path / "house.sqlite")
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
    connection.close()
    assert tables == {"zone", "member", "muted", "out_of_multiroom", "place", "owed_volume", "channel", "switch"}
    assert version == SCHEMA_VERSION
    assert journal == "wal"


def test_opening_twice_keeps_what_the_first_wrote(tmp_path: Path) -> None:
    database = tmp_path / "house.sqlite"
    first = connect(database)
    with transaction(first):
        first.execute("INSERT INTO out_of_multiroom (device_id) VALUES ('AABBCC0000A1')")
    first.close()
    second = connect(database)
    kept = [row[0] for row in second.execute("SELECT device_id FROM out_of_multiroom")]
    second.close()
    assert kept == ["AABBCC0000A1"]


def test_a_file_that_is_not_a_database_is_refused_by_name(tmp_path: Path) -> None:
    database = tmp_path / "house.sqlite"
    database.write_bytes(b"this was never a database\n" * 200)
    with pytest.raises(StoreError, match=str(database)):
        connect(database)


def test_a_database_from_a_newer_version_is_refused(tmp_path: Path) -> None:
    database = tmp_path / "house.sqlite"
    raw = sqlite3.connect(database)
    raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    raw.close()
    with pytest.raises(StoreError, match="newer"):
        connect(database)


def test_a_directory_that_does_not_exist_is_refused_by_name(tmp_path: Path) -> None:
    database = tmp_path / "nope" / "house.sqlite"
    with pytest.raises(StoreError, match=str(database)):
        connect(database)


def test_a_failed_transaction_leaves_nothing_behind(tmp_path: Path) -> None:
    connection = connect(tmp_path / "house.sqlite")
    with pytest.raises(RuntimeError), transaction(connection):
        connection.execute("INSERT INTO out_of_multiroom (device_id) VALUES ('AABBCC0000A1')")
        raise RuntimeError("the second half failed")
    left = connection.execute("SELECT COUNT(*) FROM out_of_multiroom").fetchone()[0]
    connection.close()
    assert left == 0


def test_a_second_writer_is_refused_while_the_first_holds_the_lock(tmp_path: Path) -> None:
    database = tmp_path / "house.sqlite"
    first, second = WriterLock(database), WriterLock(database)
    first.acquire()
    try:
        with pytest.raises(StoreBusyError, match="held by another process"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_releasing_a_lock_never_taken_is_harmless(tmp_path: Path) -> None:
    WriterLock(tmp_path / "house.sqlite").release()


def test_a_lock_in_a_directory_that_does_not_exist_is_refused_by_name(tmp_path: Path) -> None:
    lock = WriterLock(tmp_path / "nope" / "house.sqlite")
    with pytest.raises(StoreError, match=str(lock.path)) as caught:
        lock.acquire()
    assert not isinstance(caught.value, StoreBusyError)
