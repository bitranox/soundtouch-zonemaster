"""The house database: where it is, how it opens, who may write, and which schema it holds."""

from __future__ import annotations

import importlib.util
import io
import re
import tokenize
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, func, insert, inspect, select, text, update
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import QueuePool

from soundtouch_zonemaster.adapters.config.settings_map import SETTINGS
from soundtouch_zonemaster.adapters.files import house_db
from soundtouch_zonemaster.adapters.files.house_db import (
    MIGRATIONS,
    AdvisoryLock,
    FileLock,
    HouseDatabase,
    database_url,
)
from soundtouch_zonemaster.adapters.files.house_schema import MEMBER, METADATA
from soundtouch_zonemaster.adapters.files.house_store import SqlHouseStore
from soundtouch_zonemaster.application.errors import StoreBusyError, StoreError, StoreMissingError
from soundtouch_zonemaster.domain.database_url import masked
from soundtouch_zonemaster.domain.secret import Secret

if TYPE_CHECKING:
    import sqlite3

    from conftest import PostgresLogin

HEAD = ScriptDirectory(str(MIGRATIONS)).get_current_head()


def _quiet(_kind: str, _text: str) -> None:
    return None


def _always_held(_key: int) -> int:
    """Stands in for ``pg_try_advisory_lock`` on a SQLite engine: always grants the lock."""
    return 1


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


@pytest.mark.parametrize("spelled", ["path", "url"])
def test_a_database_asked_not_to_be_created_is_refused_as_missing_and_nothing_is_written(
    tmp_path: Path, spelled: str
) -> None:
    """``create=False`` is for a caller that only reads - ``config`` - and must not leave an empty
    database behind at a mistyped path. Both spellings of a SQLite file are checked: the URL form
    reaches the same file through SQLAlchemy's own parse, not through the plain-path branch."""
    missing = tmp_path / "house.sqlite"
    setting = str(missing) if spelled == "path" else f"sqlite:///{missing}"
    store = SqlHouseStore(setting, log=_quiet)
    with pytest.raises(StoreMissingError, match=f"{re.escape(setting)}: does not exist"):
        store.open(exclusive=False, create=False)
    assert list(tmp_path.iterdir()) == []


def test_a_database_asked_not_to_be_created_opens_when_it_is_there(house_database: str) -> None:
    """The control: ``create=False`` refuses a missing file, not an existing one."""
    _opened(house_database).close()
    store = SqlHouseStore(house_database, log=_quiet)
    store.open(exclusive=False, create=False)
    try:
        assert store.load_preferences() == ()
    finally:
        store.close()


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


def test_a_url_carrying_sslpassword_in_the_query_is_refused_without_repeating_it() -> None:
    with pytest.raises(StoreError) as caught:
        database_url("postgresql+psycopg://zonemaster@db.example/zonemaster?sslpassword=s3cret")
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


def test_where_is_the_domain_mask_of_a_url_setting() -> None:
    """One display rule for a setting, the domain's: typed as it was, only secrets masked."""
    setting = "postgresql+psycopg://zonemaster@db.example/zonemaster?passfile=/x&options=-c%20x%3Dy"
    database = HouseDatabase(setting)
    assert database.where == masked(setting) == setting


def test_every_store_message_names_the_database_without_a_password_sqlalchemy_would_misplace() -> None:
    """SQLAlchemy ends a userinfo password at its FIRST ``@``, so its own rendering shows the rest
    of a password holding a raw ``@`` as the host. Every message the store writes before it has a
    parsed URL - "used before open()" here - names the database through the domain's mask."""
    store = SqlHouseStore("postgresql+psycopg://zonemaster:TOP@SECRET@db.example/zonemaster", log=_quiet)
    with pytest.raises(StoreError) as caught:
        store.load_state()
    assert "SECRET" not in str(caught.value)
    assert make_url(store.database).render_as_string(hide_password=True).count("SECRET") == 1, (
        "the control: SQLAlchemy's own rendering does show the rest of that password"
    )


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


def test_advisory_lock_release_survives_an_unlock_that_fails_on_a_session_already_gone() -> None:
    """The PostgreSQL counterpart of a dropped ``flock``: a server restart or a killed session
    drops the advisory lock without notice, and the unlock statement then fails against a session
    that no longer holds it. That failure must not replace whatever the caller was already doing
    to end the run - a clean SIGINT stop, above all - so ``release`` must swallow it and still
    give the connection back.

    ``pg_try_advisory_lock`` is stood in for on this SQLite engine (a real acquire needs a real
    PostgreSQL server); ``pg_advisory_unlock`` is deliberately left undefined, so the unlock
    statement fails with ``OperationalError`` exactly the way it would against a session PostgreSQL
    has already dropped the lock from.
    """
    engine = create_engine("sqlite://", poolclass=QueuePool)

    @event.listens_for(engine, "connect")
    def _stand_in_for_try_lock(dbapi_connection: sqlite3.Connection, _record: object) -> None:
        dbapi_connection.create_function("pg_try_advisory_lock", 1, _always_held)

    lock = AdvisoryLock(engine, where="test")
    lock.acquire()
    pool = cast("QueuePool", engine.pool)
    assert pool.checkedout() == 1, "the lock holds its own connection while acquired"

    lock.release()

    assert pool.checkedout() == 0, "the connection is given back although the unlock itself failed"
    engine.dispose()


def test_advisory_lock_release_forgets_the_connection_even_when_close_itself_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``release`` must reset its own state in a ``finally`` around the close, not only around the
    unlock statement: a connection whose ``close()`` itself raises (the pool reports the session
    already gone a different way than the unlock statement does) must still leave the lock ready
    to ``acquire()`` again, rather than stuck thinking it still holds a connection nothing can
    reach any more.
    """
    engine = create_engine("sqlite://", poolclass=QueuePool)

    @event.listens_for(engine, "connect")
    def _stand_in_for_try_lock(dbapi_connection: sqlite3.Connection, _record: object) -> None:
        dbapi_connection.create_function("pg_try_advisory_lock", 1, _always_held)

    lock = AdvisoryLock(engine, where="test")
    lock.acquire()
    connection = lock._connection  # pyright: ignore[reportPrivateUsage]
    assert connection is not None

    def _close_fails() -> None:
        message = "simulated: the connection could not be closed"
        raise RuntimeError(message)

    monkeypatch.setattr(connection, "close", _close_fails)

    with pytest.raises(RuntimeError):
        lock.release()

    held = lock._connection  # pyright: ignore[reportPrivateUsage]
    assert held is None, "the state is reset even though close() itself raised"
    engine.dispose()


FAKE_PASSWORD = "TOPSECRET"


class _StopBeforeConnectingError(Exception):
    """Raised by the ``do_connect`` listener once it has seen what the driver would be handed."""


def _driver_arguments(setting: str, *, password: Secret | None) -> dict[str, object]:
    """What the driver's ``connect()`` would receive when the store opens ``setting``.

    Read at SQLAlchemy's own ``do_connect`` event, the last point before the driver is called,
    registered on the ``Engine`` class for the length of the call because the engine is the
    store's own; the listener stops the connect there, so no server is needed.
    """
    seen: dict[str, object] = {}

    def capture(_dialect: object, _record: object, _args: object, params: dict[str, object]) -> None:
        seen.update(params)
        raise _StopBeforeConnectingError

    event.listen(Engine, "do_connect", capture)
    try:
        with pytest.raises(_StopBeforeConnectingError):
            HouseDatabase(setting, password=password).open(exclusive=False)
    finally:
        event.remove(Engine, "do_connect", capture)
    return seen


def test_the_password_reaches_the_driver_as_a_connect_argument_and_never_the_url() -> None:
    setting = "postgresql+psycopg://zonemaster@db.example/zonemaster"
    house = HouseDatabase(setting, password=Secret(FAKE_PASSWORD))
    assert house.url.password is None
    assert FAKE_PASSWORD not in house.url.render_as_string(hide_password=False)
    assert FAKE_PASSWORD not in house.where

    params = _driver_arguments(setting, password=Secret(FAKE_PASSWORD))
    assert params["password"] == FAKE_PASSWORD
    assert params["user"] == "zonemaster", "the control: the URL's own parts arrive beside it"


def test_no_password_passes_none_to_the_driver_so_libpq_finds_its_own() -> None:
    params = _driver_arguments("postgresql+psycopg://zonemaster@db.example/zonemaster", password=None)
    assert params["user"] == "zonemaster", "the control: the listener saw this connect"
    assert "password" not in params


@pytest.mark.parametrize("setting", ["house.sqlite", "sqlite:///house.sqlite"], ids=["path", "url"])
def test_a_password_for_a_sqlite_database_is_refused_by_name_without_its_value(tmp_path: Path, setting: str) -> None:
    """SQLite has no password; one given for it would otherwise be ignored without a word."""
    where = str(tmp_path / setting) if "://" not in setting else f"sqlite:///{tmp_path / 'house.sqlite'}"
    with pytest.raises(StoreError) as caught:
        HouseDatabase(where, password=Secret(FAKE_PASSWORD))
    message = str(caught.value)
    assert "database.password" in message
    assert "SQLite" in message
    assert FAKE_PASSWORD not in message


def test_the_password_refusal_for_a_url_says_where_the_password_goes() -> None:
    with pytest.raises(StoreError) as caught:
        database_url("postgresql+psycopg://zonemaster:s3cret@db.example/zonemaster")
    assert "database.password" in str(caught.value)


def _without_libpq_s_own_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """Take away every other way libpq finds a password, so only the connect argument can log in."""
    monkeypatch.delenv("PGPASSWORD", raising=False)
    monkeypatch.setenv("PGPASSFILE", "/dev/null")


def test_the_password_setting_logs_in_on_postgresql(
    monkeypatch: pytest.MonkeyPatch, postgres_login: PostgresLogin
) -> None:
    _without_libpq_s_own_password(monkeypatch)
    database = HouseDatabase(postgres_login.url, password=postgres_login.password)
    database.open(exclusive=True)
    try:
        with database.reading() as connection:
            assert connection.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        database.close()


def test_without_the_password_setting_postgresql_refuses_the_login(
    monkeypatch: pytest.MonkeyPatch, postgres_login: PostgresLogin
) -> None:
    """The control for the test above: with libpq's own sources gone, no connect argument means no
    login - so the one above logged in through the setting and nothing else."""
    _without_libpq_s_own_password(monkeypatch)
    with pytest.raises(StoreError) as caught:
        HouseDatabase(postgres_login.url, password=None).open(exclusive=False)
    assert "password" in str(caught.value), "refused for the login, not for anything else"


def test_a_wrong_password_on_postgresql_is_refused_without_repeating_it(
    monkeypatch: pytest.MonkeyPatch, postgres_login: PostgresLogin
) -> None:
    _without_libpq_s_own_password(monkeypatch)
    wrong = "WRONG-TOPSECRET"
    with pytest.raises(StoreError) as caught:
        HouseDatabase(postgres_login.url, password=Secret(wrong)).open(exclusive=False)
    message = str(caught.value)
    assert "password" in message, "refused for the login, not for anything else"
    assert wrong not in message
    leaked = postgres_login.password.reveal() in message
    assert not leaked, "the real password appears in the refusal"


_TEXT_TOKENS = frozenset({tokenize.STRING, tokenize.FSTRING_MIDDLE})
"""Where a setting's name can be written in a module: a string, a docstring, or the text of an
f-string, which tokenizes into pieces of its own since Python 3.12."""

_A_DATABASE_SETTING = re.compile(r"\bdatabase\.\w+")


def _database_settings_named_in(source: str) -> list[str]:
    """Every ``database.<key>`` written inside a string of ``source``, code outside strings aside."""
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    return [
        found for token in tokens if token.type in _TEXT_TOKENS for found in _A_DATABASE_SETTING.findall(token.string)
    ]


def test_every_database_setting_the_store_names_is_one_the_settings_map_reads() -> None:
    """``adapters/files`` may not import the config adapter, so the store spells the settings out in
    its refusals and its docstring. Each such spelling is read from the module's own text and held
    to the config paths the settings map actually reads, so a misspelling fails while a correct
    mention of another setting of the section (``database.url``) passes, and one added later is
    covered without this test being edited."""
    named = _database_settings_named_in(Path(house_db.__file__).read_text(encoding="utf-8"))
    assert named, "the control: the store names a setting somewhere"
    assert set(named) <= set(SETTINGS), f"not a setting the service reads: {sorted(set(named) - set(SETTINGS))}"


_ZoneRow = tuple[str | None, float | None, float | None]
"""The 0001 zone row's channel, calibrated window and calibrated hold."""


def _database_at_0001(setting: str, *, zone: _ZoneRow | None) -> None:
    """A database exactly as rank 19 part 1 left it: migrated to 0001, with ``zone`` as its one row.

    ``zone`` is that row, or nothing for a database never given a state. The
    engine is a plain one, not the store's: the store migrates to head as it opens, and the point
    is a database that has not seen 0002 yet."""
    engine = create_engine(database_url(setting))
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0001")
        if zone is not None:
            channel, window, hold = zone
            connection.execute(
                text("INSERT INTO zone (id, channel, dial_window_s, hold_threshold_s) VALUES (1, :c, :w, :h)"),
                {"c": channel, "w": window, "h": hold},
            )
    engine.dispose()


def _after_the_migration(setting: str) -> tuple[list[tuple[str, str, str, str]], str | None, set[str]]:
    """Open the store as the service does, which migrates: the rows, the channel and the zone's columns."""
    store = SqlHouseStore(setting, log=_quiet)
    store.open(exclusive=True)
    try:
        rows = [(row.name, row.text, row.source, row.changed_at) for row in store.load_preferences()]
        channel = store.load_state().channel
    finally:
        store.close()
    database = _opened(setting)
    with database.reading() as connection:
        columns = {str(column["name"]) for column in inspect(connection).get_columns("zone")}
    database.close()
    return rows, channel, columns


def test_a_database_at_0001_carries_its_calibration_into_the_preference_table(house_database: str) -> None:
    _database_at_0001(house_database, zone=("3", 0.7, None))

    rows, channel, columns = _after_the_migration(house_database)

    assert rows == [("dialling.window_s", "0.7", "calibration", "")]
    assert channel == "3"
    assert columns == {"id", "channel"}


def test_a_database_at_0001_carries_both_calibrated_numbers(house_database: str) -> None:
    _database_at_0001(house_database, zone=(None, 0.6, 1.4))

    rows, _channel, _columns = _after_the_migration(house_database)

    assert rows == [
        ("dialling.hold_threshold_s", "1.4", "calibration", ""),
        ("dialling.window_s", "0.6", "calibration", ""),
    ]


@pytest.mark.parametrize(
    "zone", [None, ("3", None, None)], ids=["never-given-a-state", "a-state-with-nothing-calibrated"]
)
def test_a_database_at_0001_with_nothing_calibrated_migrates_to_an_empty_preference_table(
    house_database: str, zone: _ZoneRow | None
) -> None:
    _database_at_0001(house_database, zone=zone)

    rows, _channel, columns = _after_the_migration(house_database)

    assert rows == []
    assert columns == {"id", "channel"}


def test_a_downgrade_to_0001_puts_the_calibration_back_on_the_zone_row(house_database: str) -> None:
    """The way back: what 0002 moved into the preference table returns to the columns it came from."""
    _database_at_0001(house_database, zone=("3", 0.7, 1.4))
    _after_the_migration(house_database)
    engine = create_engine(database_url(house_database))
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0001")
        zone = connection.execute(text("SELECT channel, dial_window_s, hold_threshold_s FROM zone")).one()
        tables = set(inspect(connection).get_table_names())
    engine.dispose()
    assert tuple(zone) == ("3", 0.7, 1.4)
    assert "preference" not in tables


@pytest.mark.parametrize(
    "bad_value",
    ["not json at all", '"0.7"', "true"],
    ids=["not-json", "json-string", "json-boolean"],
)
def test_a_downgrade_refuses_a_window_row_that_is_not_a_json_number(house_database: str, bad_value: str) -> None:
    """Drives the downgrade the way the alembic CLI does: a plain ``engine.begin()``, the same
    mechanism ``test_a_downgrade_to_0001_puts_the_calibration_back_on_the_zone_row`` uses. A window
    row that is not a JSON number must refuse the downgrade before any DDL runs, leaving the
    database exactly as it was - a valid 0002 database - so a second attempt after fixing the row
    succeeds instead of hitting a duplicate column from a half-applied downgrade."""
    _database_at_0001(house_database, zone=("3", 0.7, 1.4))
    _after_the_migration(house_database)
    engine = create_engine(database_url(house_database))
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    with engine.begin() as connection:
        connection.execute(text("UPDATE preference SET value = :v WHERE name = 'dialling.window_s'"), {"v": bad_value})

    with pytest.raises(Exception), engine.begin() as connection:  # noqa: B017 - json.loads or _number's ValueError
        config.attributes["connection"] = connection
        command.downgrade(config, "0001")

    with engine.begin() as connection:
        columns = {str(column["name"]) for column in inspect(connection).get_columns("zone")}
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert columns == {"id", "channel"}, "the two columns must not have been added by the refused downgrade"
    assert version == "0002"

    with engine.begin() as connection:
        connection.execute(text("UPDATE preference SET value = '0.7' WHERE name = 'dialling.window_s'"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0001")
        zone = connection.execute(text("SELECT channel, dial_window_s, hold_threshold_s FROM zone")).one()
        tables = set(inspect(connection).get_table_names())
    engine.dispose()
    assert tuple(zone) == ("3", 0.7, 1.4)
    assert "preference" not in tables


def test_dropping_the_calibrated_columns_keeps_zone_strict(tmp_path: Path) -> None:
    """SQLite only: STRICT is a SQLite table option. 0002 drops the two columns with SQLite's own
    ``ALTER TABLE ... DROP COLUMN``, which alters ``zone`` in place rather than rebuilding it, so
    this guards that the in-place drop keeps the table STRICT - and catches a future revision that
    swaps it for a rebuild (Alembic's batch mode) which stops carrying STRICT forward."""
    path = str(tmp_path / "house.sqlite")
    _database_at_0001(path, zone=("3", 0.7, None))
    _after_the_migration(path)
    database = _opened(path)
    with database.reading() as connection:
        ddl = str(connection.execute(text("SELECT sql FROM sqlite_master WHERE name = 'zone'")).scalar_one())
    database.close()
    assert ddl.rstrip().endswith("STRICT"), "the rebuilt zone table must still be STRICT"
