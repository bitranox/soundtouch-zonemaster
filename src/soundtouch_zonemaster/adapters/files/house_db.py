"""The house database: where it is, how a connection to it behaves, and who may write it.

One database rather than three files, because a second writer is coming (the web app of
OPEN-WORK rank 16) and three whole-file JSON rewrites race each other with no way to say which one
won. The setting names it as a URL, or as a plain path meaning a SQLite file, so a house that
never runs a database server writes a path and nothing else.

**SQLite** is opened in WAL mode with ``synchronous = FULL``: the service is read back after
whatever ended the last run, including a power cut, and FULL is what makes a committed
transaction survive one in WAL mode. A WRITE transaction begins ``BEGIN IMMEDIATE``, so a writer
waits at the start rather than half-way and the legacy import's check-then-write cannot
interleave with another writer; a READ transaction begins plain ``BEGIN``, so a reader never
blocks the service. The driver's own transaction handling is switched off for that, because it
would emit its own BEGIN at a moment of its choosing.

**PostgreSQL** gets bounded connect and statement timeouts: the service calls the store on its
event loop, and a server that stopped answering must cost it seconds, not the zone. Its password
is the ``database.password`` setting, handed to the driver as a connect argument and never put in
the URL; without one nothing is passed, and libpq finds its own in ``~/.pgpass``, the file
``PGPASSFILE`` names, or ``PGPASSWORD``.

**One writer.** The service holds the writer lock for its whole run, and so does ``channels
import``. On SQLite it is an exclusive ``flock`` on ``<database>.lock`` beside the file, on
PostgreSQL a session advisory lock; both are released by the kernel or the server the moment
their holder dies, which a row saying "busy" would not be.

**The schema** is Alembic's (``migrations/``), and a database behind its head is brought up to it
only while the writer lock is held, so two processes never migrate at once. A database at a
revision this version does not know was written by a newer one, and is refused rather than read
with a schema that does not describe it.
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, DBAPIError, NoSuchModuleError, SQLAlchemyError

from ...application.errors import StoreBusyError, StoreError
from ...domain.database_url import carries_a_password, is_a_password_key, masked

if TYPE_CHECKING:
    from collections.abc import Generator
    from typing import NoReturn

    from sqlalchemy.engine import Connection, Engine
    from sqlalchemy.pool import ConnectionPoolEntry

    from ...domain.secret import Secret

__all__ = [
    "LOCK_SUFFIX",
    "MIGRATIONS",
    "MIN_SQLITE",
    "SUPPORTED",
    "AdvisoryLock",
    "FileLock",
    "HouseDatabase",
    "database_url",
    "reason_for",
]

LOCK_SUFFIX = ".lock"
MIN_SQLITE = (3, 37, 0)
SUPPORTED = ("sqlite", "postgresql")
MIGRATIONS = Path(__file__).resolve().parent / "migrations"

_ADVISORY_KEY = 1515147845
"""The house's advisory lock on a shared PostgreSQL server: "ZONE" as four bytes, fixed forever."""
_POSTGRES_TIMEOUT_S = 5
_ELSEWHERE = "give it as database.password (or keep it in ~/.pgpass) instead"
"""Where a password belongs, for a refusal of one found in the URL: the setting the boundary reads."""
_WRITE = "house_write"
"""The execution option that marks a WRITE transaction, read by the SQLite ``begin`` listener."""


def database_url(setting: str) -> URL:
    """The database a setting names: a URL, or a plain path meaning a SQLite file. Raises :class:`StoreError`.

    A password is refused rather than accepted, because a URL is written into config files,
    ``--json`` envelopes and logs; the password is its own setting, which none of those show, or
    libpq reads it from ``~/.pgpass``.
    Which shapes carry one is :func:`~soundtouch_zonemaster.domain.database_url.carries_a_password`'s
    rule (the URL's own userinfo, or a password query key); ``passfile`` names a file
    rather than a secret and is accepted. That rule reads text, so it is checked together with
    SQLAlchemy's own reading of the URL - its ``password`` and its query keys, which are what the
    driver receives - and either one refuses: the refusal is never looser than the parser that
    consumes the URL. A setting that fails to parse is never echoed back: it can be the very thing
    carrying the password that made it malformed.
    """
    is_url = "://" in setting
    try:
        url = make_url(setting) if is_url else URL.create("sqlite", database=setting)
    except (ArgumentError, ValueError) as exc:
        if is_url:
            message = 'the database setting is not a database URL (it has "://" but SQLAlchemy cannot parse it)'
        else:
            message = "the database setting could not be read as a SQLite path"
        raise StoreError(message) from exc
    # The domain's mask, never SQLAlchemy's rendering of the URL: SQLAlchemy ends a password at
    # its first "@" and would render the rest of it as the host.
    shown = masked(setting)
    if carries_a_password(setting):
        message = f"{shown}: carries a password; {_ELSEWHERE}"
        raise StoreError(message)
    if _sqlalchemy_reads_a_password(url):
        # A backstop: the domain rule is held no looser than this reading, and when it is not,
        # the mask shows the setting as typed, so this refusal names no part of it.
        message = f"the database URL carries a password; {_ELSEWHERE}"
        raise StoreError(message)
    backend = url.get_backend_name()
    if backend not in SUPPORTED:
        message = f"{shown}: {backend} is not supported (only {', '.join(SUPPORTED)})"
        raise StoreError(message)
    if backend == "sqlite" and url.database in (None, "", ":memory:"):
        message = f"{shown}: an in-memory SQLite database keeps nothing; name a file"
        raise StoreError(message)
    return url


def _sqlalchemy_reads_a_password(url: URL) -> bool:
    """Whether the parsed URL hands the driver a password: its userinfo, or a password query key."""
    return url.password is not None or any(is_a_password_key(key) for key in url.query)


class _WriterLock(Protocol):
    def acquire(self) -> None: ...

    def release(self) -> None: ...


class FileLock:
    """The one-writer rule on SQLite, as a lock the kernel drops the moment its holder dies."""

    def __init__(self, database: Path) -> None:
        self.path = database.with_name(database.name + LOCK_SUFFIX)
        self._fd: int | None = None

    def acquire(self) -> None:
        """Take the lock or refuse at once. A service that waited would look like one that hung."""
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as exc:
            message = f"{self.path}: could not be opened ({type(exc).__name__})"
            raise StoreError(message) from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            message = f"{self.path}: held by another process (is the service running?)"
            raise StoreBusyError(message) from exc
        self._fd = fd

    def release(self) -> None:
        """Give the lock back. Closing the descriptor is what releases a flock."""
        if self._fd is None:
            return
        os.close(self._fd)
        self._fd = None


class AdvisoryLock:
    """The one-writer rule on PostgreSQL: a session advisory lock on a connection of its own.

    AUTOCOMMIT, so the connection holding it is never "idle in transaction" for the length of a
    run. Released explicitly before the connection goes back to the pool, because a pooled
    connection keeps its session - and with it the lock - after ``close()``. The lock lives on
    that ONE server session: a server restart or a killed session drops it without notice, and
    ``pool_pre_ping`` never checks a connection that stays checked out, so nothing here would see
    it happen. That is the PostgreSQL counterpart of a ``flock`` dropped when its holder dies, and
    it is accepted for the same reason.
    """

    def __init__(self, engine: Engine, *, where: str) -> None:
        self._engine = engine
        self.where = where
        self._connection: Connection | None = None

    def acquire(self) -> None:
        connection = self._engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            held = connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": _ADVISORY_KEY}).scalar_one()
        except SQLAlchemyError:
            connection.close()
            raise
        if not held:
            connection.close()
            message = f"{self.where}: held by another process (is the service running?)"
            raise StoreBusyError(message)
        self._connection = connection

    def release(self) -> None:
        """Give the lock back. A stop must still close the connection when the server already
        dropped the session (a restart, a killed session): the unlock statement then fails on a
        connection that no longer holds anything to unlock, and that failure must not replace
        whatever the caller was already doing to end the run - a clean SIGINT stop, above all.
        """
        if self._connection is None:
            return
        connection = self._connection
        try:
            with suppress(SQLAlchemyError):
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _ADVISORY_KEY})
            connection.close()
        finally:
            self._connection = None


class HouseDatabase:
    """One house database: an engine that behaves the same on every backend, and its writer lock."""

    def __init__(self, setting: str, *, password: Secret | None = None, busy_timeout_s: float = 5.0) -> None:
        """Read the setting and refuse what cannot be opened; nothing is connected yet.

        A password given for a SQLite database is refused by name: SQLite has none, and a setting
        that is silently ignored reads to its author as one that is in force.
        """
        self.url = database_url(setting)
        self.where = masked(setting)
        """The database, safe to put in any message: the domain's one display rule for a setting."""
        if password is not None and self.url.get_backend_name() == "sqlite":
            message = f"{self.where}: database.password is set, but a SQLite database has no password; remove it"
            raise StoreError(message)
        self._password = password
        self._busy_timeout_s = busy_timeout_s
        self._engine: Engine | None = None
        self._lock: _WriterLock | None = None
        self._locked = False

    def open(self, *, exclusive: bool) -> None:
        """Connect, take the writer lock when asked, and bring the schema to head. Nothing is held on a refusal.

        Building the engine is inside its own guard: an unknown dialect+driver combination raises
        ``NoSuchModuleError`` and a driver module this environment never installed raises a plain
        ``ImportError``, neither of which the caller (the service, a CLI command) is written to
        recognise - both must leave here as the one ``StoreError`` they refuse on. ``ImportError``
        is caught only around that step: a broken Alembic migration module is a programming error,
        not a missing driver, and must reach ``report_crash`` with its traceback rather than being
        folded into the same refusal.
        """
        try:
            self._engine = self._build_engine()
        except (SQLAlchemyError, ImportError) as exc:
            self._refuse(exc)
        except BaseException:
            self.close()
            raise
        try:
            self._lock = self._build_lock(self._engine)
            if exclusive:
                self._lock.acquire()
                self._locked = True
            self._bring_up_to_date(self._lock)
        except SQLAlchemyError as exc:
            self._refuse(exc)
        except BaseException:
            self.close()
            raise

    def _refuse(self, exc: Exception) -> NoReturn:
        """Close whatever ``open()`` managed to build, then raise the one ``StoreError`` callers refuse on."""
        self.close()
        message = f"{self.where}: could not be opened as a house database ({reason_for(exc)})"
        raise StoreError(message) from exc

    def close(self) -> None:
        """Give the lock back, then the connections. Harmless when nothing is open.

        The state is reset and the engine disposed in ``finally``, so a lock whose connection is
        already gone (a dead PostgreSQL session: a server restart, a killed session) still leaves
        nothing held open and nothing stuck thinking the lock is still ours.
        ``AdvisoryLock.release`` itself already swallows a failed ``pg_advisory_unlock`` on such a
        session, so this ``finally`` is defence for a ``FileLock`` release rather than the usual
        case for a dead PostgreSQL one.
        """
        try:
            if self._lock is not None and self._locked:
                self._lock.release()
        finally:
            self._locked = False
            self._lock = None
            if self._engine is not None:
                self._engine.dispose()
            self._engine = None

    @contextmanager
    def reading(self) -> Generator[Connection]:
        """Nothing enforces read-only here.

        A write made through this is rolled back at the end, the same as any other exception.
        Never blocks the writer on SQLite.
        """
        with self._require().connect() as connection:
            yield connection

    @contextmanager
    def writing(self) -> Generator[Connection]:
        """All of it or none of it: committed at the end, rolled back on any exception."""
        with self._require().execution_options(**{_WRITE: True}).begin() as connection:
            yield connection

    def _require(self) -> Engine:
        if self._engine is None:
            message = f"{self.where}: the house database was used before open()"
            raise StoreError(message)
        return self._engine

    def _build_engine(self) -> Engine:
        if self.url.get_backend_name() == "sqlite":
            if sqlite3.sqlite_version_info < MIN_SQLITE:
                message = (
                    f"{self.where}: SQLite {sqlite3.sqlite_version} is older than 3.37.0, which STRICT tables need"
                )
                raise StoreError(message)
            engine = create_engine(self.url, connect_args={"timeout": self._busy_timeout_s})
            event.listen(engine, "connect", _sqlite_connect)
            event.listen(engine, "begin", _sqlite_begin)
            return engine
        connect_args: dict[str, object] = {
            "connect_timeout": _POSTGRES_TIMEOUT_S,
            "options": f"-c statement_timeout={_POSTGRES_TIMEOUT_S * 1000}",
        }
        if self._password is not None:
            # The one place the value is revealed: the driver's own connect argument, which no
            # message, envelope or rendering of the URL ever includes.
            connect_args["password"] = self._password.reveal()
        return create_engine(self.url, pool_pre_ping=True, connect_args=connect_args)

    def _build_lock(self, engine: Engine) -> _WriterLock:
        if self.url.get_backend_name() == "sqlite":
            return FileLock(Path(str(self.url.database)))
        return AdvisoryLock(engine, where=self.where)

    def _bring_up_to_date(self, lock: _WriterLock) -> None:
        """Migrate a database behind head, and only under the writer lock."""
        script = ScriptDirectory(str(MIGRATIONS))
        if self._current(script) == script.get_current_head():
            return
        if self._locked:
            self._upgrade()
            return
        lock.acquire()
        try:
            self._upgrade()
        finally:
            lock.release()

    def _current(self, script: ScriptDirectory) -> str | None:
        with self.reading() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
        if current is None:
            return None
        try:
            script.get_revision(current)
        except CommandError as exc:
            message = (
                f"{self.where}: written by a newer version (schema {current}, "
                f"this one reads up to {script.get_current_head()})"
            )
            raise StoreError(message) from exc
        return current

    def _upgrade(self) -> None:
        config = Config()
        config.set_main_option("script_location", str(MIGRATIONS))
        with self.writing() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")


def _sqlite_connect(dbapi_connection: sqlite3.Connection, _record: ConnectionPoolEntry) -> None:
    """Hand transaction control to SQLAlchemy, and set what every connection to the file needs."""
    dbapi_connection.isolation_level = None
    dbapi_connection.execute("PRAGMA journal_mode = WAL")
    dbapi_connection.execute("PRAGMA synchronous = FULL")


def _sqlite_begin(connection: Connection) -> None:
    """IMMEDIATE for a write, so it waits at the start rather than half-way; deferred for a read."""
    write = bool(connection.get_execution_options().get(_WRITE, False))
    connection.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")


def reason_for(exc: Exception) -> str:
    """The driver's own words when there are any, the error's name otherwise - never a whole traceback.

    Shared with :class:`~soundtouch_zonemaster.adapters.files.house_store.SqlHouseStore`'s own
    guard, so the two do not repeat the same line: a ``DBAPIError`` speaks for itself through
    ``.orig``; a missing driver module (``ModuleNotFoundError``) or an unknown dialect+driver
    combination (``NoSuchModuleError``) names its type AND carries the library's own message,
    neither of which can hold a password; anything else is named by its type alone.
    """
    if isinstance(exc, DBAPIError):
        return str(exc.orig)
    if isinstance(exc, (ModuleNotFoundError, NoSuchModuleError)):
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__
