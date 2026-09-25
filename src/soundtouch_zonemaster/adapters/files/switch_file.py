"""The switch file: the only way to tell the service to let the house alone.

Off means the zone is dissolved and the speakers are independent again. The Bose zone function
between real speakers is untouched either way, so turning this off does not take multiroom away
from anybody - it takes US out of it.

**Off only when the file says so.** Missing, empty, unreadable, or holding something nobody
recognises all mean ON. One rule, so there is nothing to get subtly wrong at two in the morning:
a lost file cannot silently stop the house working, and turning the service off stays a deliberate
act rather than an accident. The cost is that a typo means on; the reverse default would make a
deleted file look like a working service that has quietly stopped.

**Watched, not read once.** Most editors do not modify a file in place - they write a new one and
rename it over the old - so anything holding on to what it opened at start keeps reporting the
value from then, forever, with nothing about it looking broken. The path is therefore re-opened on
every poll and no descriptor is kept.

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
    """The switch file, read now or watched for as long as the service runs."""

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
