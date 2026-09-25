"""The house database: one SQLite file for the state, the channel list and the switch.

One file rather than three, because a second writer is coming (the web app of OPEN-WORK rank 16)
and three whole-file JSON rewrites race each other with no way to say which one won. SQLite gives
each write a transaction and each reader a consistent view.

Opened in WAL mode with ``synchronous = FULL``: the service is read back after whatever ended the
last run, including a power cut, and FULL is what makes a committed transaction survive one in WAL
mode. The tables are STRICT, so a value of the wrong type is refused by the file itself rather than
discovered by whoever reads it next; that needs SQLite 3.37, which is checked before anything
opens. ``PRAGMA user_version`` carries the schema version, and a file written by a NEWER version
is refused rather than read with a schema that does not describe it.

**One writer.** The service holds an exclusive ``flock`` on ``<database>.lock`` for its whole run,
and so does ``channels import``. The lock sits beside the database rather than inside it, because
a lock the kernel releases when the process dies cannot be left behind by a crash, and a row
saying "busy" can.
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
from contextlib import contextmanager
from typing import TYPE_CHECKING

from ...application.errors import StoreBusyError, StoreError

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

__all__ = ["LOCK_SUFFIX", "MIN_SQLITE", "SCHEMA_VERSION", "WriterLock", "connect", "transaction"]

SCHEMA_VERSION = 1
MIN_SQLITE = (3, 37, 0)
LOCK_SUFFIX = ".lock"

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS zone (id INTEGER PRIMARY KEY CHECK (id = 1), channel TEXT,"
    " dial_window_s REAL, hold_threshold_s REAL) STRICT",
    "CREATE TABLE IF NOT EXISTS member (device_id TEXT PRIMARY KEY) STRICT",
    "CREATE TABLE IF NOT EXISTS muted (device_id TEXT PRIMARY KEY, volume INTEGER NOT NULL) STRICT",
    "CREATE TABLE IF NOT EXISTS out_of_multiroom (device_id TEXT PRIMARY KEY) STRICT",
    "CREATE TABLE IF NOT EXISTS place (channel TEXT PRIMARY KEY, track INTEGER NOT NULL,"
    " seconds REAL NOT NULL, file TEXT) STRICT",
    "CREATE TABLE IF NOT EXISTS owed_volume (device_id TEXT PRIMARY KEY, steps INTEGER NOT NULL) STRICT",
    "CREATE TABLE IF NOT EXISTS channel (number TEXT NOT NULL UNIQUE, name TEXT NOT NULL, kind TEXT NOT NULL,"
    " url TEXT NOT NULL, mpd_entry TEXT NOT NULL, mpd_directory TEXT NOT NULL,"
    " in_rotation INTEGER NOT NULL CHECK (in_rotation IN (0, 1)), at_end TEXT NOT NULL) STRICT",
    "CREATE TABLE IF NOT EXISTS switch (id INTEGER PRIMARY KEY CHECK (id = 1),"
    " word TEXT NOT NULL CHECK (word IN ('on', 'off')), changed_at TEXT NOT NULL) STRICT",
)
"""IF NOT EXISTS because two processes can both find version 0 and both try to create the
schema; the second one's BEGIN IMMEDIATE waits for the first, and must then find nothing to do.
Rows keep their insertion order through ``rowid``, which is what the ordered lists read back by."""


def connect(database: Path) -> sqlite3.Connection:
    """Open the house database, creating the schema on a new file. Raises :class:`StoreError`."""
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        message = f"{database}: SQLite {sqlite3.sqlite_version} is older than 3.37.0, which STRICT tables need"
        raise StoreError(message)
    try:
        connection = sqlite3.connect(database, isolation_level=None)
    except sqlite3.Error as exc:
        message = f"{database}: could not be opened ({exc})"
        raise StoreError(message) from exc
    try:
        _prepare(connection, database)
    except sqlite3.DatabaseError as exc:
        connection.close()
        message = f"{database}: not a house database ({exc})"
        raise StoreError(message) from exc
    except StoreError:
        connection.close()
        raise
    except BaseException:
        connection.close()
        raise
    return connection


def _prepare(connection: sqlite3.Connection, database: Path) -> None:
    """Set the pragmas every connection needs, then bring a new file up to the schema."""
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        message = f"{database}: written by a newer version (schema {version}, this one reads {SCHEMA_VERSION})"
        raise StoreError(message)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    if version < SCHEMA_VERSION:
        with transaction(connection):
            for statement in _SCHEMA:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


@contextmanager
def transaction(connection: sqlite3.Connection) -> Generator[sqlite3.Connection]:
    """All of it or none of it. IMMEDIATE, so a writer waits at the start rather than half-way."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


class WriterLock:
    """The one-writer rule, as a lock the kernel drops the moment its holder dies."""

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
