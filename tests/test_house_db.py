"""The house database: where it is, how it opens, who may write, and which schema it holds."""

from __future__ import annotations

import importlib.util
from typing import TYPE_CHECKING, cast

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, func, insert, inspect, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import QueuePool

from soundtouch_zonemaster.adapters.files.house_db import (
    MIGRATIONS,
    AdvisoryLock,
    FileLock,
    HouseDatabase,
    database_url,
)
from soundtouch_zonemaster.adapters.files.house_schema import MEMBER, METADATA
from soundtouch_zonemaster.application.errors import StoreBusyError, StoreError

if TYPE_CHECKING:
    from pathlib import Path

HEAD = ScriptDirectory(str(MIGRATIONS)).get_current_head()


class _ExplodingLock:
    """A writer lock stub whose release() fails the way a dead PostgreSQL session does."""

    def acquire(self) -> None:
        return

    def release(self) -> None:
        message = "boom"
        raise RuntimeError(message)


def _opened(setting: str, *, exclusive: bool = False, busy_timeout_s: float = 5.0) -> HouseDatabase:
    database = HouseDatabase(setting, busy_timeout_s=busy_timeout_s)
    database.open(exclusive=exclusive)
    return database


def test_a_new_database_holds_every_table_at_the_newest_schema(house_database: str) -> None:
    database = _opened(house_database)
    with database.reading() as connection:
        tables = set(inspect(connection).get_table_names())
        revision = MigrationContext.configure(connection).get_current_revision()
    database.close()
    assert tables == {table.name for table in METADATA.sorted_tables} | {"alembic_version"}
    assert revision == HEAD


def test_the_migrations_build_exactly_the_schema_the_rows_are_written_through(house_database: str) -> None:
    database = _opened(house_database)
    with database.reading() as connection:
        differences = compare_metadata(MigrationContext.configure(connection), METADATA)
    database.close()
    assert differences == []


def test_every_sqlite_table_is_strict(tmp_path: Path) -> None:
    database = _opened(str(tmp_path / "house.sqlite"))
    with database.reading() as connection:
        rows = connection.execute(text("SELECT name, sql FROM sqlite_master WHERE type = 'table'")).all()
    database.close()
    ddl = {str(name): str(sql) for name, sql in rows}
    assert {table.name for table in METADATA.sorted_tables} <= set(ddl)
    assert all(ddl[table.name].rstrip().endswith("STRICT") for table in METADATA.sorted_tables)


def test_opening_twice_keeps_what_the_first_wrote(house_database: str) -> None:
    first = _opened(house_database)
    with first.writing() as connection:
        connection.execute(insert(MEMBER).values(device_id="AABBCC0000A1", position=0))
    first.close()
    second = _opened(house_database)
    with second.reading() as connection:
        members = list(connection.scalars(select(MEMBER.c.device_id)))
    second.close()
    assert members == ["AABBCC0000A1"]


def test_a_file_that_is_not_a_database_is_refused_by_name(tmp_path: Path) -> None:
    database = tmp_path / "house.sqlite"
    database.write_bytes(b"this is not a database, it is a sentence " * 100)
    with pytest.raises(StoreError, match=str(database)):
        _opened(str(database))


def test_a_database_from_a_newer_version_is_refused(house_database: str) -> None:
    database = _opened(house_database)
    with database.writing() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = 'ffffffffffff'"))
    database.close()
    with pytest.raises(StoreError, match="newer version"):
        _opened(house_database)


def test_a_directory_that_does_not_exist_is_refused_by_name(tmp_path: Path) -> None:
    database = tmp_path / "missing" / "house.sqlite"
    with pytest.raises(StoreError, match=str(database)):
        _opened(str(database))


def test_a_failed_write_leaves_nothing_behind(house_database: str) -> None:
    database = _opened(house_database)
    with pytest.raises(RuntimeError), database.writing() as connection:
        connection.execute(insert(MEMBER).values(device_id="AABBCC0000A1", position=0))
        raise RuntimeError
    with database.reading() as connection:
        count = connection.scalar(select(func.count()).select_from(MEMBER))
    database.close()
    assert count == 0


def test_a_second_writer_is_refused_while_the_first_holds_the_lock_and_admitted_after(house_database: str) -> None:
    first = _opened(house_database, exclusive=True)
    second = HouseDatabase(house_database)
    with pytest.raises(StoreBusyError):
        second.open(exclusive=True)
    first.close()
    second.open(exclusive=True)
    second.close()


def test_a_reader_opens_while_the_writer_lock_is_held(house_database: str) -> None:
    writer = _opened(house_database, exclusive=True)
    reader = _opened(house_database)
    with reader.reading() as connection:
        assert connection.scalar(select(func.count()).select_from(MEMBER)) == 0
    reader.close()
    writer.close()


def test_a_database_behind_the_schema_is_not_migrated_while_another_process_holds_the_lock(tmp_path: Path) -> None:
    path = tmp_path / "house.sqlite"
    lock = FileLock(path)
    lock.acquire()
    try:
        with pytest.raises(StoreBusyError):
            _opened(str(path))
    finally:
        lock.release()


def test_a_read_does_not_block_a_writer_but_a_write_does(tmp_path: Path) -> None:
    setting = str(tmp_path / "house.sqlite")
    first = _opened(setting, busy_timeout_s=0.2)
    second = _opened(setting, busy_timeout_s=0.2)
    with first.reading() as reading:
        reading.execute(select(MEMBER.c.device_id)).all()
        with second.writing() as connection:
            connection.execute(insert(MEMBER).values(device_id="AABBCC0000A1", position=0))
    with first.writing() as writing:
        writing.execute(select(MEMBER.c.device_id)).all()
        with pytest.raises(OperationalError, match="locked"), second.writing() as connection:
            connection.execute(update(MEMBER).values(position=1))
    first.close()
    second.close()


def test_a_url_carrying_a_password_is_refused_without_repeating_it() -> None:
    with pytest.raises(StoreError) as caught:
        database_url("postgresql+psycopg://zonemaster:s3cret@db.example/zonemaster")
    assert "s3cret" not in str(caught.value)
    assert "pgpass" in str(caught.value)


def test_an_unsupported_backend_is_refused_by_name() -> None:
    with pytest.raises(StoreError, match="mysql"):
        database_url("mysql://zonemaster@db.example/zonemaster")


def test_a_plain_path_is_a_sqlite_file(tmp_path: Path) -> None:
    url = database_url(str(tmp_path / "house.sqlite"))
    assert url.get_backend_name() == "sqlite"
    assert url.database == str(tmp_path / "house.sqlite")


def test_an_in_memory_sqlite_database_is_refused() -> None:
    with pytest.raises(StoreError, match="in-memory"):
        database_url("sqlite://")


def test_releasing_a_lock_never_taken_is_harmless(tmp_path: Path) -> None:
    FileLock(tmp_path / "house.sqlite").release()


def test_a_lock_in_a_directory_that_does_not_exist_is_refused_by_name(tmp_path: Path) -> None:
    lock = FileLock(tmp_path / "missing" / "house.sqlite")
    with pytest.raises(StoreError, match="missing"):
        lock.acquire()


def test_a_url_carrying_a_password_in_the_query_is_refused_without_repeating_it() -> None:
    with pytest.raises(StoreError) as caught:
        database_url("postgresql+psycopg://zonemaster@db.example/zonemaster?password=s3cret")
    assert "s3cret" not in str(caught.value)


def test_a_url_carrying_a_passfile_in_the_query_is_accepted() -> None:
    url = database_url("postgresql+psycopg://zonemaster@db.example/zonemaster?passfile=/etc/pgpass")
    assert url.get_backend_name() == "postgresql"


def test_a_url_that_fails_to_parse_is_refused_without_echoing_the_setting() -> None:
    setting = "postgres ql://zonemaster:s3cret@db.example/zonemaster"
    with pytest.raises(StoreError) as caught:
        database_url(setting)
    message = str(caught.value)
    assert setting not in message
    assert "s3cret" not in message
    assert '"://"' in message


def test_a_bad_port_in_a_url_is_refused_as_a_store_error() -> None:
    with pytest.raises(StoreError) as caught:
        database_url("postgresql+psycopg://zonemaster@db.example:notaport/zonemaster")
    assert "notaport" not in str(caught.value)


def test_where_is_the_raw_setting_for_a_plain_path(tmp_path: Path) -> None:
    setting = str(tmp_path / "house.sqlite")
    database = HouseDatabase(setting)
    assert database.where == setting


def test_where_is_the_redacted_url_for_a_url_setting() -> None:
    setting = "postgresql+psycopg://zonemaster@db.example/zonemaster?passfile=/x"
    database = HouseDatabase(setting)
    assert database.where == database.url.render_as_string(hide_password=True)
    assert database.where != setting


def test_sqlite_reading_uses_wal_journal_mode_and_full_synchronous(tmp_path: Path) -> None:
    database = _opened(str(tmp_path / "house.sqlite"))
    with database.reading() as connection:
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
        synchronous = connection.exec_driver_sql("PRAGMA synchronous").scalar()
    database.close()
    assert journal_mode == "wal"
    assert synchronous == 2


def test_close_gives_back_the_engine_even_when_releasing_the_lock_raises(tmp_path: Path) -> None:
    database = _opened(str(tmp_path / "house.sqlite"))
    # Reaching into the internal state is the point of this test: it pins the `finally` cleanup
    # in `close()`, which nothing public exposes a seam for.
    database._lock = _ExplodingLock()  # pyright: ignore[reportPrivateUsage]
    database._locked = True  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(RuntimeError):
        database.close()
    assert database._locked is False  # pyright: ignore[reportPrivateUsage]
    assert database._lock is None  # pyright: ignore[reportPrivateUsage]
    assert database._engine is None  # pyright: ignore[reportPrivateUsage]


def test_an_unknown_driver_refuses_as_a_store_error_naming_the_database() -> None:
    """``create_engine`` raises ``NoSuchModuleError`` for a dialect+driver combination it cannot load.

    That is a ``SQLAlchemyError`` subclass, so it is already close to being wrapped - the point of
    this test is that it happens while ``open()`` is building the engine, before the ``try`` that
    used to start only after ``_build_engine()`` returned.
    """
    with pytest.raises(StoreError, match=r"nosuchdriver.*NoSuchModuleError: Can't load plugin"):
        HouseDatabase("postgresql+nosuchdriver://zonemaster@db.example/zonemaster").open(exclusive=False)


def test_a_driver_that_cannot_be_imported_refuses_as_a_store_error() -> None:
    """A dialect naming a DBAPI module this environment never installed raises a plain ``ImportError``.

    ``asyncpg`` is never a project dependency, so it stands in for "driver not installed"; skip if
    something else on the machine happens to have pulled it in, since the point is the import
    failing. ``find_spec`` rather than a bare ``import`` so pyright never has to resolve a module
    this project deliberately never depends on.
    """
    if importlib.util.find_spec("asyncpg") is not None:
        pytest.skip("asyncpg is installed here, so its import cannot be made to fail")
    with pytest.raises(StoreError, match=r"asyncpg.*ModuleNotFoundError: No module named 'asyncpg'"):
        HouseDatabase("postgresql+asyncpg://zonemaster@db.example/zonemaster").open(exclusive=False)


def test_advisory_lock_acquire_closes_the_connection_when_the_query_fails() -> None:
    engine = create_engine("sqlite://", poolclass=QueuePool)
    lock = AdvisoryLock(engine, where="test")
    with pytest.raises(OperationalError):
        lock.acquire()
    pool = cast("QueuePool", engine.pool)
    assert pool.checkedout() == 0
    engine.dispose()
