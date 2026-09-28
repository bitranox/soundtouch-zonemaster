"""The switch, as one row: off only when the row says so.

The rule is the one ``switch_file.py`` applies to the old file, for the same reason. A
missing row, or a read that fails, means ON, so a lost database cannot silently stop the house
working, and turning the service off stays a deliberate act. The word and the poll interval are
still the domain's (``domain/switch.py``).

Every write is an UPSERT on the fixed row id, for the reason ``house_preferences.py`` gives:
delete-then-insert under READ COMMITTED lets two writers collide on PostgreSQL (OPEN-WORK rank
204), and ``switch`` is the one house-store write a running service does not serialise - it opens
the store WITHOUT the writer lock precisely so a person can turn the house off while the service
runs, which means two ``switch`` invocations really can land at the same moment.

**The old file is watched too, for one reason.** Anybody who has operated this house writes
``off`` into ``zone.switch``, and after the move nothing reads it any more, so the house keeps
playing with no sign of why. The watch says so once each time that file appears, naming it and
the command that replaced it.
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
    from collections.abc import AsyncGenerator, Callable
    from pathlib import Path

    from sqlalchemy.engine import Connection

    from ...domain.logfn import LogFn

__all__ = ["ON", "DbSwitch", "read_switch", "write_switch"]

ON = "on"


def read_switch(connection: Connection) -> bool | None:
    """True for on, False for off, nothing when nobody has ever set it."""
    word = connection.scalar(select(SWITCH.c.word).where(SWITCH.c.id == 1))
    return None if word is None else str(word) != OFF


def write_switch(connection: Connection, *, on: bool) -> bool:
    """Set it, and say whether what the service READS changed. The caller holds the transaction.

    A switch never set already reads as on, so setting it on is no change even though a row
    appears: the answer is about the house, not about the table.

    One statement, on both backends: two ``switch`` invocations landing at the same instant race
    on the SELECT that decides ``changed``, never on the write itself, and each still leaves the
    row holding exactly what it asked for.
    """
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
    """The switch as the service reads it: now, or watched for as long as it runs."""

    def __init__(
        self, is_on: Callable[[], bool], *, where: str, log: LogFn, poll_s: float, ignored_file: Path | None
    ) -> None:
        self._is_on = is_on
        self.where = where
        self.log = log
        self.poll_s = poll_s
        self.ignored_file = ignored_file

    def is_on(self) -> bool:
        return self._is_on()

    async def watch(self) -> AsyncGenerator[bool, None]:
        """Yield the value now, and again each time it changes. Never yields the same value twice."""
        last: bool | None = None
        noticed = False
        while True:
            current = self.is_on()
            if current != last:
                self.log("switch", f"{self.where}: {'on' if current else 'off'}")
                last = current
                yield current
            noticed = self._notice_the_old_file(noticed=noticed)
            await asyncio.sleep(self.poll_s)

    def _notice_the_old_file(self, *, noticed: bool) -> bool:
        """Say once, each time it appears, that the old switch file is not read any more."""
        present = self.ignored_file is not None and self.ignored_file.exists()
        if present and not noticed:
            self.log("switch", f"{self.ignored_file}: is not read any more; use the service's 'switch' command")
        return present
