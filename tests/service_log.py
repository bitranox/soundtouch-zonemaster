"""The log sink every test that runs the service hands it.

A test asserts on the lines it collected, so the list stays. What the list alone cannot do is
explain a test that FAILED: a timeout in ``eventually`` says what never happened, and the lines
that would say why stayed in a list nobody printed. Six timeouts in ``_both_wake`` (OPEN-WORK rank
176) left exactly that - a traceback and nothing of what the service did. So every line is printed
as well; pytest captures a test's stdout and shows it only when that test fails, which costs a
passing run nothing.

Each printed line starts with ``time.monotonic()``, the clock an asyncio event loop's ``time()``
reads on the platforms this runs on, so a line can be placed against the loop time a timed-out
``eventually`` shows in its assertion.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from soundtouch_zonemaster.domain.logfn import LogFn

__all__ = ["recording_into"]


def recording_into(logs: list[str]) -> LogFn:
    """A log callable that appends ``"<kind>: <text>"`` to ``logs`` and prints it with its time."""

    def log(kind: str, text: str) -> None:
        line = f"{kind}: {text}"
        logs.append(line)
        print(f"{time.monotonic()!r} {line}")

    return log
