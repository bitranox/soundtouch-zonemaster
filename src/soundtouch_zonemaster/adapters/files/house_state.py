"""What survives a restart, as rows: one table per field of :class:`ZoneState` that is a collection.

The scalar fields sit in the one-row ``zone`` table. That row is also what tells a database that
has never held a state from one holding an EMPTY state: the legacy importer (``legacy_import``)
may fill the first and must not overwrite the second.

Every write REPLACES the whole state inside the caller's transaction, which is what the service
has always done with the file: the record is small, the writer is one, and a partial update
would be a second way of being right that nothing tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...domain.state import Place, ZoneState

if TYPE_CHECKING:
    import sqlite3

__all__ = ["read_state", "write_state"]

_CLEAR = (
    "DELETE FROM member",
    "DELETE FROM muted",
    "DELETE FROM out_of_multiroom",
    "DELETE FROM place",
    "DELETE FROM owed_volume",
)


def read_state(connection: sqlite3.Connection) -> ZoneState | None:
    """The state, or nothing when no state was ever written."""
    zone = connection.execute("SELECT channel, dial_window_s, hold_threshold_s FROM zone WHERE id = 1").fetchone()
    if zone is None:
        return None
    channel: str | None = zone["channel"]
    dial_window_s: float | None = zone["dial_window_s"]
    hold_threshold_s: float | None = zone["hold_threshold_s"]
    return ZoneState(
        channel=None if channel is None else str(channel),
        members=tuple(str(row[0]) for row in connection.execute("SELECT device_id FROM member ORDER BY rowid")),
        muted={
            str(row[0]): int(row[1]) for row in connection.execute("SELECT device_id, volume FROM muted ORDER BY rowid")
        },
        out_of_multiroom=tuple(
            str(row[0]) for row in connection.execute("SELECT device_id FROM out_of_multiroom ORDER BY rowid")
        ),
        positions={
            str(row[0]): Place(track=int(row[1]), seconds=float(row[2]), file=None if row[3] is None else str(row[3]))
            for row in connection.execute("SELECT channel, track, seconds, file FROM place ORDER BY rowid")
        },
        dial_window_s=None if dial_window_s is None else float(dial_window_s),
        hold_threshold_s=None if hold_threshold_s is None else float(hold_threshold_s),
        owed_volume={
            str(row[0]): int(row[1])
            for row in connection.execute("SELECT device_id, steps FROM owed_volume ORDER BY rowid")
        },
    )


def write_state(connection: sqlite3.Connection, state: ZoneState) -> None:
    """Replace the whole state. The caller holds the transaction."""
    connection.execute(
        "INSERT INTO zone (id, channel, dial_window_s, hold_threshold_s) VALUES (1, ?, ?, ?)"
        " ON CONFLICT (id) DO UPDATE SET channel = excluded.channel,"
        " dial_window_s = excluded.dial_window_s, hold_threshold_s = excluded.hold_threshold_s",
        (state.channel, state.dial_window_s, state.hold_threshold_s),
    )
    for statement in _CLEAR:
        connection.execute(statement)
    connection.executemany("INSERT INTO member (device_id) VALUES (?)", [(one,) for one in state.members])
    connection.executemany("INSERT INTO muted (device_id, volume) VALUES (?, ?)", list(state.muted.items()))
    connection.executemany(
        "INSERT INTO out_of_multiroom (device_id) VALUES (?)", [(one,) for one in state.out_of_multiroom]
    )
    connection.executemany(
        "INSERT INTO place (channel, track, seconds, file) VALUES (?, ?, ?, ?)",
        [(number, place.track, place.seconds, place.file) for number, place in state.positions.items()],
    )
    connection.executemany("INSERT INTO owed_volume (device_id, steps) VALUES (?, ?)", list(state.owed_volume.items()))
