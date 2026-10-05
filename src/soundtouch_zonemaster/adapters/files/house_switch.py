"""The switch, as one row: off only when the row says so.

A missing row, or a read that fails, means ON, so a lost database cannot silently stop the house
working, and turning the service off stays a deliberate act. The word and the poll interval are
still the domain's (``domain/switch.py``).

Every write is an UPSERT on the fixed row id, for the reason ``house_preferences.py`` gives:
delete-then-insert under READ COMMITTED lets two writers collide on PostgreSQL (OPEN-WORK rank
204), and ``switch`` is the one house-store write a running service does not serialise - it opens
the store WITHOUT the writer lock precisely so a person can turn the house off while the service
runs, which means two ``switch`` invocations really can land at the same moment.

**Every write locks the switch before it reads it** (:func:`hold_the_switch`). The answer a write
gives, whether the house changed, is what a deploy decides by whether to turn the house back on
later, and under READ COMMITTED a person's ``switch off`` committing between a writer's read and
its write would make that answer a change the writer never made.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite

from ...domain.switch import OFF
from .house_schema import SWITCH

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from sqlalchemy.engine import Connection

    from ...domain.logfn import LogFn

__all__ = ["ON", "DbSwitch", "hold_the_switch", "read_switch", "write_switch"]

ON = "on"

_LOCK_THE_SWITCH = "LOCK TABLE switch IN SHARE ROW EXCLUSIVE MODE"
"""The lock a switch write takes on PostgreSQL. SHARE ROW EXCLUSIVE conflicts with itself and with
every INSERT, UPDATE and DELETE, and with no plain SELECT, so the service's poll never waits on it."""


def read_switch(connection: Connection) -> bool | None:
    """True for on, False for off, nothing when nobody has ever set it."""
    word = connection.scalar(select(SWITCH.c.word).where(SWITCH.c.id == 1))
    return None if word is None else str(word) != OFF


def hold_the_switch(connection: Connection) -> None:
    """Take the switch for the rest of the caller's transaction, before anything in it reads it.

    On PostgreSQL this is a lock on the TABLE rather than ``SELECT ... FOR UPDATE`` on the row, for
    two cases a row lock misses. A switch nobody ever set has no row to lock, so two first writes
    would still both read "never set". And the 0.5.2 service's ``switch`` deletes the row and
    inserts a new one; a ``FOR UPDATE`` that waited on the deleted row finds nothing once it may
    go on, and reads the switch as never set. The table lock waits for every other writer's
    transaction to end, and the read after it, a new statement under READ COMMITTED, sees what
    that writer committed. On SQLite there is nothing to take: the write transaction began with
    ``BEGIN IMMEDIATE``, which already holds the database's one write lock.

    ``tools/service_venv.py`` takes the same lock itself, because it runs against whichever
    release is installed, and 0.5.2 has no such function.
    """
    if connection.dialect.name == "postgresql":
        connection.exec_driver_sql(_LOCK_THE_SWITCH)


def write_switch(connection: Connection, *, on: bool) -> bool:
    """Set it, and say whether what the service READS changed. The caller holds the transaction.

    A switch never set already reads as on, so setting it on is no change even though a row
    appears: the answer is about the house, not about the table.

    One statement for the write, on both backends, after :func:`hold_the_switch`: two ``switch``
    invocations landing at the same instant queue on the lock, so each reads what the other wrote
    and each still leaves the row holding exactly what it asked for.
    """
    hold_the_switch(connection)
    before = read_switch(connection)
    row = {"id": 1, "word": ON if on else OFF, "changed_at": datetime.now(UTC).isoformat()}
    replaced = {"word": row["word"], "changed_at": row["changed_at"]}
    if connection.dialect.name == "postgresql":
        connection.execute(
            postgresql.insert(SWITCH).values(**row).on_conflict_do_update(index_elements=["id"], set_=replaced)
        )
    else:
        connection.execute(
            sqlite.insert(SWITCH).values(**row).on_conflict_do_update(index_elements=["id"], set_=replaced)
        )
    return (True if before is None else before) != on


class DbSwitch:
    """The switch as the service reads it: now, or watched for as long as it runs.

    ``is_on`` is awaited, because the read it stands for runs off the service's event loop
    (``store_worker.py``): the poll comes round every second, and on PostgreSQL each one is a
    network round trip the zone's clock would otherwise wait out.
    """

    def __init__(
        self,
        is_on: Callable[[], Awaitable[bool]],
        *,
        where: str,
        log: LogFn,
        poll_s: float,
    ) -> None:
        self._is_on = is_on
        self.where = where
        self.log = log
        self.poll_s = poll_s

    async def is_on(self) -> bool:
        return await self._is_on()

    async def watch(self) -> AsyncGenerator[bool, None]:
        """Yield the value now, and again each time it changes. Never yields the same value twice."""
        last: bool | None = None
        while True:
            current = await self.is_on()
            if current != last:
                self.log("switch", f"{self.where}: {'on' if current else 'off'}")
                last = current
                yield current
            await asyncio.sleep(self.poll_s)
