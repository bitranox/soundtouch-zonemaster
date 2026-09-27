"""What survives a restart, as rows: one table per field of :class:`ZoneState` that is a collection.

The channel sits in the one-row ``zone`` table. That row is also what tells a database that
has never held a state from one holding an EMPTY state: the legacy importer (``legacy_import``)
may fill the first and must not overwrite the second.

Every write REPLACES the whole state inside the caller's transaction, which is what the service
has always done with the file: the record is small, the writer is one, and a partial update
would be a second way of being right that nothing tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, insert, select

from ...domain.state import Place, ZoneState
from .house_schema import MEMBER, MUTED, OUT_OF_MULTIROOM, OWED_VOLUME, PLACE, ZONE

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.engine import Connection

__all__ = ["read_state", "write_state"]

_REPLACED = (ZONE, MEMBER, MUTED, OUT_OF_MULTIROOM, PLACE, OWED_VOLUME)


def read_state(connection: Connection) -> ZoneState | None:
    """The state, or nothing when no state was ever written."""
    zone = connection.execute(select(ZONE.c.channel).where(ZONE.c.id == 1)).one_or_none()
    if zone is None:
        return None
    (channel,) = zone
    return ZoneState(
        channel=None if channel is None else str(channel),
        members=tuple(str(one) for one in connection.scalars(select(MEMBER.c.device_id).order_by(MEMBER.c.position))),
        muted={
            str(device): int(volume)
            for device, volume in connection.execute(
                select(MUTED.c.device_id, MUTED.c.volume).order_by(MUTED.c.position)
            )
        },
        out_of_multiroom=tuple(
            str(one)
            for one in connection.scalars(select(OUT_OF_MULTIROOM.c.device_id).order_by(OUT_OF_MULTIROOM.c.position))
        ),
        positions={
            str(number): Place(track=int(track), seconds=float(seconds), file=None if file is None else str(file))
            for number, track, seconds, file in connection.execute(
                select(PLACE.c.channel, PLACE.c.track, PLACE.c.seconds, PLACE.c.file).order_by(PLACE.c.position)
            )
        },
        owed_volume={
            str(device): int(steps)
            for device, steps in connection.execute(
                select(OWED_VOLUME.c.device_id, OWED_VOLUME.c.steps).order_by(OWED_VOLUME.c.position)
            )
        },
    )


def write_state(connection: Connection, state: ZoneState) -> None:
    """Replace the whole state. The caller holds the transaction."""
    for table in _REPLACED:
        connection.execute(delete(table))
    connection.execute(insert(ZONE).values(id=1, channel=state.channel))
    _in_order(connection, MEMBER, [{"device_id": one} for one in state.members])
    _in_order(connection, MUTED, [{"device_id": device, "volume": volume} for device, volume in state.muted.items()])
    _in_order(connection, OUT_OF_MULTIROOM, [{"device_id": one} for one in state.out_of_multiroom])
    _in_order(
        connection,
        PLACE,
        [
            {"channel": number, "track": place.track, "seconds": place.seconds, "file": place.file}
            for number, place in state.positions.items()
        ],
    )
    _in_order(
        connection, OWED_VOLUME, [{"device_id": device, "steps": steps} for device, steps in state.owed_volume.items()]
    )


def _in_order(connection: Connection, table: Table, rows: list[dict[str, Any]]) -> None:
    """Insert rows numbered by where they stand. An empty list inserts nothing (executemany refuses [])."""
    if rows:
        connection.execute(insert(table), [{"position": index, **row} for index, row in enumerate(rows)])
