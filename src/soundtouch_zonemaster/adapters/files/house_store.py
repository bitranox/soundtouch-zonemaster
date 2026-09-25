"""The house store: the one object the service and the CLI reach the database through.

It opens nothing until :meth:`open`, so building one costs nothing and a service can be
constructed in a test without touching a disk. Every other method refuses with ``StoreError``
before ``open`` and after ``close``, because a silent no-op there would let a test pass while the
thing it meant to exercise was never written.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from ...application.errors import StoreError
from ...domain.state import ZoneState
from .channel_file import ChannelFileError, channels_json, load_channels
from .house_channels import read_channels, write_channels
from .house_db import WriterLock, connect, transaction
from .house_state import read_state, write_state
from .house_switch import DbSwitch, read_switch, write_switch
from .legacy_import import import_legacy

if TYPE_CHECKING:
    from pathlib import Path

    from ...application.options import LegacyFiles
    from ...domain.channellist import ChannelList
    from ...domain.logfn import LogFn

__all__ = ["SqliteHouseStore"]


class SqliteHouseStore:
    """The state, the channel list and the switch, in one SQLite file."""

    def __init__(self, database: Path, *, log: LogFn) -> None:
        self.database = database
        self.log = log
        self._lock = WriterLock(database)
        self._connection: sqlite3.Connection | None = None
        self._exclusive = False

    def open(self, *, exclusive: bool) -> None:
        """Connect, taking the writer lock first when asked. Nothing is left held on a refusal."""
        if exclusive:
            self._lock.acquire()
        try:
            self._connection = connect(self.database)
        except StoreError:
            self._lock.release()
            raise
        self._exclusive = exclusive

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        self._lock.release()
        self._exclusive = False

    def import_legacy(self, files: LegacyFiles) -> None:
        import_legacy(self._db, files, database=self.database, log=self.log)

    def load_state(self) -> ZoneState:
        return read_state(self._db) or ZoneState()

    def save_state(self, state: ZoneState) -> None:
        with transaction(self._db) as connection:
            write_state(connection, state)

    def load_channels(self) -> ChannelList:
        channels = read_channels(self._db, database=self.database)
        self.log("channels", f"{self.database}: {len(channels.channels)} channel(s)")
        return channels

    def save_channels(self, channels: ChannelList) -> None:
        with transaction(self._db) as connection:
            write_channels(connection, channels)

    def export_channels(self) -> str:
        return channels_json(read_channels(self._db, database=self.database))

    def import_channels(self, path: Path) -> ChannelList:
        """Replace the list with a file's, under the writer lock, or refuse naming what is wrong."""
        if not self._exclusive:
            message = f"{self.database}: an import needs the store opened exclusive"
            raise StoreError(message)
        try:
            channels = load_channels(path, log=self.log)
        except ChannelFileError as exc:
            raise StoreError(str(exc)) from exc
        self.save_channels(channels)
        return channels

    def is_on(self) -> bool:
        """Off only when the database says so; a read that fails is ON, as it was for the file."""
        try:
            held = read_switch(self._db)
        except sqlite3.Error:
            return True
        return True if held is None else held

    def set_switch(self, *, on: bool) -> bool:
        with transaction(self._db) as connection:
            return write_switch(connection, on=on)

    def switch(self, *, poll_s: float, ignored_file: Path | None) -> DbSwitch:
        return DbSwitch(self.is_on, where=str(self.database), log=self.log, poll_s=poll_s, ignored_file=ignored_file)

    @property
    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            message = f"{self.database}: the house store was used before open()"
            raise StoreError(message)
        return self._connection
