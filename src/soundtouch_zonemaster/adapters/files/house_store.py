"""The house store: the one object the service and the CLI reach the database through.

It opens nothing until :meth:`open`, so building one costs nothing and a service can be
constructed in a test without touching a disk. Every other method refuses with ``StoreError``
before ``open`` and after ``close``, because a silent no-op there would let a test pass while the
thing it meant to exercise was never written.

Nothing the database library raises leaves this class: every error becomes a ``StoreError``
naming the database, which is the one type the service and the CLI are written to refuse on. The
switch is the exception by design - a read that fails is ON, as it was for the switch file.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from ...application.errors import StoreError
from ...domain.database_url import masked
from ...domain.state import ZoneState
from .channel_file import ChannelFileError, channels_json, load_channels
from .house_channels import read_channels, write_channels
from .house_db import HouseDatabase, reason_for
from .house_state import read_state, write_state
from .house_switch import DbSwitch, read_switch, write_switch
from .legacy_import import import_legacy

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from ...application.options import LegacyFiles
    from ...domain.channellist import ChannelList
    from ...domain.logfn import LogFn

__all__ = ["SqlHouseStore"]


class SqlHouseStore:
    """The state, the channel list and the switch, in one SQLite file or PostgreSQL database."""

    def __init__(self, database: str, *, log: LogFn) -> None:
        self.database = database
        self.where = masked(database)
        """The database, safe to put in any message: the domain's mask of the setting, which can carry a password."""
        self.log = log
        self._house: HouseDatabase | None = None
        self._exclusive = False

    def open(self, *, exclusive: bool) -> None:
        """Connect, taking the writer lock first when asked. Nothing is left held on a refusal.

        Refuses by name when the store is already open, rather than silently replacing the
        connection it holds: every method after it would quietly talk to a different one.
        """
        if self._house is not None:
            message = f"{self.where}: the house store is already open"
            raise StoreError(message)
        house = HouseDatabase(self.database)
        house.open(exclusive=exclusive)
        self._house = house
        self._exclusive = exclusive

    def close(self) -> None:
        """Close the database, resetting this store's own state even when that raises.

        The reset happens in ``finally`` rather than after a plain call: ``HouseDatabase.close()``
        already swallows a lock release that fails on a session already gone (a dead PostgreSQL
        connection), but a caller may still reach this from elsewhere while a genuine close error
        propagates for another reason, and without the ``finally`` a store left thinking it was
        still open would refuse the next ``open()`` by name as "already open" - unable to reconnect
        at all until the process restarts.
        """
        if self._house is None:
            return
        house = self._house
        try:
            with self._guarded():
                house.close()
        finally:
            self._house = None
            self._exclusive = False

    def import_legacy(self, files: LegacyFiles) -> None:
        self._require_exclusive(what="importing the old files")
        with self._guarded():
            import_legacy(self._db, files, log=self.log)

    def load_state(self) -> ZoneState:
        with self._guarded(), self._db.reading() as connection:
            return read_state(connection) or ZoneState()

    def save_state(self, state: ZoneState) -> None:
        with self._guarded(), self._db.writing() as connection:
            write_state(connection, state)

    def load_channels(self) -> ChannelList:
        with self._guarded(), self._db.reading() as connection:
            channels = read_channels(connection, where=self.where)
        self.log("channels", f"{self.where}: {len(channels.channels)} channel(s)")
        return channels

    def save_channels(self, channels: ChannelList) -> None:
        with self._guarded(), self._db.writing() as connection:
            write_channels(connection, channels, where=self.where)

    def export_channels(self) -> str:
        with self._guarded(), self._db.reading() as connection:
            return channels_json(read_channels(connection, where=self.where))

    def import_channels(self, path: Path) -> ChannelList:
        """Replace the list with a file's, under the writer lock, or refuse naming what is wrong."""
        self._require_exclusive(what="an import")
        try:
            channels = load_channels(path, log=self.log)
        except ChannelFileError as exc:
            raise StoreError(str(exc)) from exc
        self.save_channels(channels)
        return channels

    def is_on(self) -> bool:
        """Off only when the database says so; a read that fails is ON, as it was for the file."""
        house = self._db
        try:
            with house.reading() as connection:
                held = read_switch(connection)
        except SQLAlchemyError:
            return True
        return True if held is None else held

    def set_switch(self, *, on: bool) -> bool:
        with self._guarded(), self._db.writing() as connection:
            return write_switch(connection, on=on)

    def switch(self, *, poll_s: float, ignored_file: Path | None) -> DbSwitch:
        return DbSwitch(self.is_on, where=self.where, log=self.log, poll_s=poll_s, ignored_file=ignored_file)

    def _require_exclusive(self, *, what: str) -> None:
        """Refuse an action that writes over what an unrelated reader might be reading right now."""
        if not self._exclusive:
            message = f"{self.where}: {what} needs the store opened exclusive"
            raise StoreError(message)

    @contextmanager
    def _guarded(self) -> Generator[None]:
        """Turn anything the database library raises into the one refusal type, naming the database."""
        try:
            yield
        except SQLAlchemyError as exc:
            message = f"{self.where}: {reason_for(exc)}"
            raise StoreError(message) from exc

    @property
    def _db(self) -> HouseDatabase:
        if self._house is None:
            message = f"{self.where}: the house store was used before open()"
            raise StoreError(message)
        return self._house
