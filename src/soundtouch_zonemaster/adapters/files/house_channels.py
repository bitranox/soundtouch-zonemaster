"""The house's channel list, as rows, checked by the same rules the channel file is.

Every row read goes back through :class:`ChannelListDocument`, the model that parses the channel
file, so a number no key can press or a URL the master cannot fetch is refused here exactly as it
is refused in a file, with the same problem count. The rules therefore live in one place and a
row written by hand with the ``sqlite3`` shell cannot get past them.

Unusable is REFUSED, never read as empty, for the reason ``channel_file`` gives: this is the only
copy of something a person built, and an empty start would let the next save overwrite it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ValidationError

from ...application.errors import StoreError
from .channel_file import ChannelDocument, ChannelListDocument

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from ...domain.channellist import Channel, ChannelList

__all__ = ["channel_count", "read_channels", "write_channels"]

_SELECT = "SELECT number, name, kind, url, mpd_entry, mpd_directory, in_rotation, at_end FROM channel ORDER BY rowid"
_INSERT = (
    "INSERT INTO channel (number, name, kind, url, mpd_entry, mpd_directory, in_rotation, at_end)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)


def channel_count(connection: sqlite3.Connection) -> int:
    """How many channels the database holds; zero is the part the importer may fill."""
    return int(connection.execute("SELECT COUNT(*) FROM channel").fetchone()[0])


def read_channels(connection: sqlite3.Connection, *, database: Path) -> ChannelList:
    """The channel list, or a :class:`StoreError` naming the file and how many problems it has."""
    documents = [_document(row) for row in connection.execute(_SELECT)]
    try:
        parsed = ChannelListDocument.model_validate({"channels": documents})
    except ValidationError as exc:
        message = f"{database}: the channel list is unusable ({exc.error_count()} problem(s))"
        raise StoreError(message) from exc
    return parsed.to_channel_list()


def write_channels(connection: sqlite3.Connection, channels: ChannelList) -> None:
    """Replace the whole list, keeping its order. The caller holds the transaction."""
    connection.execute("DELETE FROM channel")
    connection.executemany(_INSERT, [_row(one) for one in channels.channels])


def _document(row: sqlite3.Row) -> dict[str, object]:
    """One row as the channel document names its fields (``at_end`` is ``end``, a SQL keyword)."""
    return {
        "number": row["number"],
        "name": row["name"],
        "kind": row["kind"],
        "url": row["url"],
        "mpd_entry": row["mpd_entry"],
        "mpd_directory": row["mpd_directory"],
        "in_rotation": bool(row["in_rotation"]),
        "end": row["at_end"],
    }


def _row(channel: Channel) -> tuple[str, str, str, str, str, str, int, str]:
    """One channel as plain values: the document's JSON form, so enums are their wire strings."""
    dumped = ChannelDocument.of(channel).model_dump(mode="json")
    return (
        str(dumped["number"]),
        str(dumped["name"]),
        str(dumped["kind"]),
        str(dumped["url"]),
        str(dumped["mpd_entry"]),
        str(dumped["mpd_directory"]),
        int(bool(dumped["in_rotation"])),
        str(dumped["end"]),
    )
