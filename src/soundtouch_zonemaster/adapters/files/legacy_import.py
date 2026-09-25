"""The three files the service kept before, read once each into an empty part of the database.

Everything is READ first and written in ONE transaction, and only then are the files renamed. So
a channel file that is unusable refuses the start with nothing imported and nothing renamed, which
is how it refused before there was a database, and a crash between the commit and the rename
leaves files that the next start names as "not read" instead of importing them a second time.

Only into an EMPTY part. A database that already holds a state, a list or a switch is newer than
any of these files, whoever wrote it, and an import over it would undo that without a word.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...application.errors import StoreError
from .channel_file import ChannelFileError, load_channels
from .house_channels import channel_count, write_channels
from .house_db import transaction
from .house_state import read_state, write_state
from .house_switch import read_switch, write_switch
from .state_file import load_state
from .switch_file import Switch

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from ...application.options import LegacyFiles
    from ...domain.channellist import ChannelList
    from ...domain.logfn import LogFn
    from ...domain.state import ZoneState

__all__ = ["IMPORTED_SUFFIX", "import_legacy"]

IMPORTED_SUFFIX = ".imported"


def import_legacy(connection: sqlite3.Connection, files: LegacyFiles, *, database: Path, log: LogFn) -> None:
    """Import every old file that exists into its empty part, then set the imported ones aside."""
    channels = _read_channels(files.channel_file, log=log)
    state = _read_state(files.state_file, log=log)
    switch = _read_switch(files.switch_file, log=log)
    with transaction(connection):
        taken = [
            _take_channels(connection, files.channel_file, channels, database=database, log=log),
            _take_state(connection, files.state_file, state, database=database, log=log),
            _take_switch(connection, files.switch_file, on=switch, database=database, log=log),
        ]
    for path in taken:
        if path is not None:
            _set_aside(path, log=log)


def _exists(path: Path | None) -> bool:
    return path is not None and path.exists()


def _read_channels(path: Path | None, *, log: LogFn) -> ChannelList | None:
    if path is None or not path.exists():
        return None
    try:
        return load_channels(path, log=log)
    except ChannelFileError as exc:
        raise StoreError(str(exc)) from exc


def _read_state(path: Path | None, *, log: LogFn) -> ZoneState | None:
    return load_state(path, log=log) if path is not None and _exists(path) else None


def _read_switch(path: Path | None, *, log: LogFn) -> bool | None:
    return Switch(path, log=log).is_on() if path is not None and _exists(path) else None


def _take_channels(
    connection: sqlite3.Connection, path: Path | None, channels: ChannelList | None, *, database: Path, log: LogFn
) -> Path | None:
    if path is None or channels is None:
        return None
    if channel_count(connection) > 0:
        _not_read(path, what="a channel list", database=database, log=log)
        return None
    write_channels(connection, channels)
    log("store", f"{path}: imported {len(channels.channels)} channel(s) into {database}")
    return path


def _take_state(
    connection: sqlite3.Connection, path: Path | None, state: ZoneState | None, *, database: Path, log: LogFn
) -> Path | None:
    if path is None or state is None:
        return None
    if read_state(connection) is not None:
        _not_read(path, what="a state", database=database, log=log)
        return None
    write_state(connection, state)
    log("store", f"{path}: imported the state into {database}")
    return path


def _take_switch(
    connection: sqlite3.Connection, path: Path | None, *, on: bool | None, database: Path, log: LogFn
) -> Path | None:
    if path is None or on is None:
        return None
    if read_switch(connection) is not None:
        _not_read(path, what="the switch", database=database, log=log)
        return None
    write_switch(connection, on=on)
    log("store", f"{path}: imported the switch ({'on' if on else 'off'}) into {database}")
    return path


def _not_read(path: Path, *, what: str, database: Path, log: LogFn) -> None:
    log("store", f"{path}: not imported, {database} already holds {what}; this file is not read")


def _set_aside(path: Path, *, log: LogFn) -> None:
    """Rename an imported file, so nobody mistakes it for the one that is read."""
    target = path.with_name(path.name + IMPORTED_SUFFIX)
    try:
        path.replace(target)
    except OSError as exc:
        log("store", f"{path}: imported, but could not be renamed ({type(exc).__name__}); it is not read again")
        return
    log("store", f"{path}: renamed to {target.name}")
