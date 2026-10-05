"""What a box's display says while a directory channel plays (OPEN-WORK rank 235).

The user's rule, 2026-10-01: not only the directory's name, but the number and the title of the
piece playing, changing with every next and previous; and with several directories
``<Directory Title> <dir number>/<file Number> <name of the File (without the Number if any)>``.

The numbers are POSITIONS in the queue the house plays, never numbers read out of a file name: a
book whose files are named 01, 02 and 10 shows 1, 2 and 3, which is what a person counting the
pieces they have heard expects. A directory is counted by its STRETCH - an unbroken run of one
parent - the same way a held next or previous steps (``playorder.directory_jump``), so a stored
playlist that comes back to a directory it left numbers that second visit as its own.

How a box shows a changed ``<track>`` mid-stream is not measured yet; this decides only the text.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .playorder import run_starts

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["piece_title", "showing"]

_LEADING_NUMBER = re.compile(r"^\d+[\s._-]+")
"""A track number at the start of a name, with whatever separates it from the title."""


def piece_title(file: str) -> str:
    """A file's name without its directory, its extension, or a leading track number.

    A name that is nothing BUT a number keeps it, because an empty title tells a listener less than
    the number does.
    """
    name = file.rpartition("/")[2]
    stem, dot, extension = name.rpartition(".")
    base = stem if dot and stem and extension else name
    title = _LEADING_NUMBER.sub("", base, count=1)
    return title if title.strip() else base


def showing(queue: Sequence[str], *, current: int | None, channel_name: str = "") -> str | None:
    """The display text for the entry playing, or ``None`` when no entry is.

    ``None`` leaves the caller on the channel's own name, which is what a box shows today. A file at
    the top of the collection has no directory of its own, so the channel's name stands in for it.
    """
    if current is None or not 0 <= current < len(queue):
        return None
    starts = run_starts(queue)
    stretch = max(index for index, start in enumerate(starts) if start <= current)
    number = current - starts[stretch] + 1
    file = queue[current]
    directory = file.rpartition("/")[0].rpartition("/")[2] or channel_name
    title = piece_title(file)
    if len(starts) == 1:
        return f"{directory} {number} {title}".strip()
    return f"{directory} {stretch + 1}/{number} {title}".strip()
