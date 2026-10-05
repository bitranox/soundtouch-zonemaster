"""The old switch file ``zone.switch``, from before the house database.

The service reads the switch from its row in the house database (``house_switch.py``), never from
this file. Its one reader is the installer's ``tools/service_venv.py seed-switch``, which takes
the word of an old file beside a database it has just CREATED as that database's first switch row,
because it is what the operator last said.

**Off only when the file says so.** Missing, empty, unreadable, or holding something nobody
recognises all mean ON - the rule the row follows too, for the same reason: a lost switch cannot
silently stop the house working, and turning the service off stays a deliberate act rather than
an accident. The cost is that a typo means on.

:meth:`Switch.watch` re-opens the path on every poll rather than keeping a descriptor, because
most editors write a new file and rename it over the old one. Nothing in the service calls it:
the service watches the row through ``house_switch.DbSwitch``.

The word and the poll interval are the domain's (``domain/switch.py``); what is here is the
reading of a real file.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ...domain.switch import OFF, POLL_S

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from ...domain.logfn import LogFn

__all__ = ["Switch"]


class Switch:
    """The old switch file: read once by the installer's switch seed, or watched by polling it."""

    def __init__(self, path: Path, *, log: LogFn, poll_s: float = POLL_S) -> None:
        self.path = path
        self.log = log
        self.poll_s = poll_s

    def is_on(self) -> bool:
        """Read the file as it is right now.

        The bytes are decoded as ``utf-8-sig`` rather than ``utf-8``, which is the same codec plus
        one rule: a leading byte order mark belongs to the encoding and not to the word. Windows
        writes that mark when it saves "UTF-8 with BOM", and this file is edited over SMB from
        there, so without the rule three invisible bytes turn a deliberate ``off`` into ``on``.

        A decode failure means the same as a read failure - on. That is not only the module's rule
        applied consistently: this runs inside the polling task, and an exception leaving here
        takes the whole service down with it.
        """
        try:
            written = self.path.read_bytes().decode("utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return True
        return written.strip().casefold() != OFF

    async def watch(self) -> AsyncGenerator[bool, None]:
        """Yield the value now, and again each time it changes. Never yields the same value twice.

        Re-reporting an unchanged value every poll would make a log nobody can read, and would put
        the service through a dissolve or a rejoin once a second.
        """
        last: bool | None = None
        while True:
            current = self.is_on()
            if current != last:
                self.log("switch", f"{self.path}: {'on' if current else 'off'}")
                last = current
                yield current
            await asyncio.sleep(self.poll_s)
