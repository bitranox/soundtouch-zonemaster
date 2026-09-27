"""The three files the service kept before, read once each into an empty part of the database.

Everything is READ first and written in ONE transaction, and only then are the files renamed. So
a channel file OR a state file that is unusable refuses the start with nothing imported and
nothing renamed - the state file is no longer the one that quietly starts empty here, because this
is the only place its bytes are turned into the sole copy the database will ever hold. That is how
the channel file already refused before there was a database, and a crash between the commit and
the rename leaves files that the next start names as "not read" instead of importing them a
second time.

Only into an EMPTY part. A database that already holds a state, a list or a switch is newer than
any of these files, whoever wrote it, and an import over it would undo that without a word - which
is also why an unusable file over an ALREADY-HELD part must not refuse: nothing was ever going to
read it.

An old state file also carries the two numbers a calibration measured. They become preference
rows (``house_preferences``) with ``source = 'calibration'`` and no time, because the file never
recorded when the calibration ran - and, by the same rule, each only where nobody has set that
preference already.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...application.errors import StoreError
from ...domain.preferences import PreferenceSource
from .channel_file import ChannelFileError, load_channels
from .house_channels import channel_count, write_channels
from .house_preferences import preference_is_set, write_preference
from .house_state import read_state, write_state
from .house_switch import read_switch, write_switch
from .state_file import LegacyState, StateFileError, load_state_strict
from .switch_file import Switch

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Connection

    from ...application.options import LegacyFiles
    from ...domain.channellist import ChannelList
    from ...domain.logfn import LogFn
    from .house_db import HouseDatabase

__all__ = ["IMPORTED_SUFFIX", "import_legacy"]

IMPORTED_SUFFIX = ".imported"


def import_legacy(database: HouseDatabase, files: LegacyFiles, *, log: LogFn) -> None:
    """Import every old file that exists into its empty part, then set the imported ones aside.

    The channel file and the state file are the two parts whose parse can RAISE (an unusable
    ``channels.json`` or ``zone-state.json``), so each is read only after ITS OWN part is confirmed
    empty, and only inside the same IMMEDIATE transaction that makes that check and the write
    atomic - a database that already holds a list or a state must never even attempt to parse a
    legacy file it is not going to read, and a database that is missing only one of the two must
    still refuse the whole start rather than half-import. ``load_state_strict`` is the strict
    sibling of ``state_file.load_state``, which never raises and which nothing in the program calls
    any more (the golden corpus replays the old format through it). The switch file never raises
    on a bad read (``Switch.is_on``), so reading it ahead of the transaction changes nothing
    observable and keeps the transaction itself short.
    """
    switch = _read_switch(files.switch_file, log=log)
    with database.writing() as connection:
        taken = [
            _take_channels(connection, files.channel_file, where=database.where, log=log),
            _take_state(connection, files.state_file, where=database.where, log=log),
            _take_switch(connection, files.switch_file, on=switch, where=database.where, log=log),
        ]
    for path in taken:
        if path is not None:
            _set_aside(path, log=log)


def _read_switch(path: Path | None, *, log: LogFn) -> bool | None:
    if path is None or not path.exists():
        return None
    return Switch(path, log=log).is_on()


def _read_channels(path: Path, *, log: LogFn) -> ChannelList:
    try:
        return load_channels(path, log=log)
    except ChannelFileError as exc:
        raise StoreError(str(exc)) from exc


def _read_state(path: Path) -> LegacyState:
    try:
        return load_state_strict(path)
    except StateFileError as exc:
        raise StoreError(str(exc)) from exc


def _take_channels(connection: Connection, path: Path | None, *, where: str, log: LogFn) -> Path | None:
    if path is None or not path.exists():
        return None
    if channel_count(connection) > 0:
        _not_read(path, what="a channel list", where=where, log=log)
        return None
    channels = _read_channels(path, log=log)
    write_channels(connection, channels, where=where)
    log("store", f"{path}: imported {len(channels.channels)} channel(s) into {where}")
    return path


def _take_state(connection: Connection, path: Path | None, *, where: str, log: LogFn) -> Path | None:
    if path is None or not path.exists():
        return None
    if read_state(connection) is not None:
        _not_read(path, what="a state", where=where, log=log)
        return None
    legacy = _read_state(path)
    write_state(connection, legacy.state)
    for name, value in legacy.calibration():
        # Only into an empty place, like every other part of the import: a value somebody set
        # with `prefs set` before this first start is newer than any file.
        if not preference_is_set(connection, name):
            write_preference(connection, name, value, source=PreferenceSource.CALIBRATION, changed_at="")
    log("store", f"{path}: imported the state into {where}")
    return path


def _take_switch(connection: Connection, path: Path | None, *, on: bool | None, where: str, log: LogFn) -> Path | None:
    if path is None or on is None:
        return None
    if read_switch(connection) is not None:
        _not_read(path, what="the switch", where=where, log=log)
        return None
    write_switch(connection, on=on)
    log("store", f"{path}: imported the switch ({'on' if on else 'off'}) into {where}")
    return path


def _not_read(path: Path, *, what: str, where: str, log: LogFn) -> None:
    log("store", f"{path}: not imported, {where} already holds {what}; this file is not read")


def _set_aside(path: Path, *, log: LogFn) -> None:
    """Rename an imported file, so nobody mistakes it for the one that is read."""
    target = path.with_name(path.name + IMPORTED_SUFFIX)
    try:
        path.replace(target)
    except OSError as exc:
        log("store", f"{path}: imported, but could not be renamed ({type(exc).__name__}); it is not read again")
        return
    log("store", f"{path}: renamed to {target.name}")
