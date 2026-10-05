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
from sqlalchemy import delete, insert, select

from ...application.errors import StoreError
from .channel_file import ChannelDocument, ChannelListDocument
from .house_schema import CHANNEL

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, RowMapping

    from ...domain.channellist import Channel, ChannelList

__all__ = ["read_channels", "write_channels"]

_COLUMNS = (
    CHANNEL.c.number,
    CHANNEL.c.name,
    CHANNEL.c.kind,
    CHANNEL.c.url,
    CHANNEL.c.mpd_entry,
    CHANNEL.c.mpd_directory,
    CHANNEL.c.in_rotation,
    CHANNEL.c.at_end,
)


def read_channels(connection: Connection, *, where: str) -> ChannelList:
    """The channel list, or a :class:`StoreError` naming the database and how many problems it has."""
    rows = connection.execute(select(*_COLUMNS).order_by(CHANNEL.c.position)).mappings()
    documents = [_document(row) for row in rows]
    try:
        parsed = ChannelListDocument.model_validate({"channels": documents})
    except ValidationError as exc:
        message = f"{where}: the channel list is unusable ({exc.error_count()} problem(s))"
        raise StoreError(message) from exc
    return parsed.to_channel_list()


def write_channels(connection: Connection, channels: ChannelList, *, where: str) -> None:
    """Replace the whole list, keeping its order. The caller holds the transaction.

    Nothing in the domain refuses two channels sharing a number, and a hand-edited ``channels.json``
    handed to ``channels import`` can hold one. It is refused HERE, by name and before anything is
    written, rather than left to the UNIQUE index: the index's error differs per backend and names
    whichever duplicate it met first, and on PostgreSQL it would also abort the transaction under
    the caller's feet. The index stays, for a row written past this function.
    """
    duplicated = _duplicated_numbers(channels)
    if duplicated:
        message = f"{where}: the channel list has a duplicate number ({', '.join(duplicated)})"
        raise StoreError(message)
    connection.execute(delete(CHANNEL))
    rows = [{"position": index, **_row(one)} for index, one in enumerate(channels.channels)]
    if rows:
        connection.execute(insert(CHANNEL), rows)


def _duplicated_numbers(channels: ChannelList) -> tuple[str, ...]:
    """Every number that names more than one channel, sorted and named once each."""
    seen: set[str] = set()
    duplicated: set[str] = set()
    for channel in channels.channels:
        if channel.number in seen:
            duplicated.add(channel.number)
        seen.add(channel.number)
    return tuple(sorted(duplicated))


def _document(row: RowMapping) -> dict[str, object]:
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


def _row(channel: Channel) -> dict[str, object]:
    """One channel as plain values: the document's JSON form, so enums are their wire strings."""
    dumped = ChannelDocument.of(channel).model_dump(mode="json")
    return {
        "number": str(dumped["number"]),
        "name": str(dumped["name"]),
        "kind": str(dumped["kind"]),
        "url": str(dumped["url"]),
        "mpd_entry": str(dumped["mpd_entry"]),
        "mpd_directory": str(dumped["mpd_directory"]),
        "in_rotation": int(bool(dumped["in_rotation"])),
        "at_end": str(dumped["end"]),
    }
