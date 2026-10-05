"""Writing a file so that nothing can ever read half of it.

A channel-list file is read after whatever ended the run that wrote it, which includes a power cut
in the middle of a write. So it may not be written in place: the document goes to a temporary file
beside the real one, is flushed to the disk, and is then renamed over it. A rename is atomic, so a
reader sees the old document or the new one and never half of either.

It lives in its own module because the rule has a part that is easy to leave out, and more than one
writer needs it: the store's ``channels export`` in the program, and ``channel_file.save_channels``,
which the channel-file corpus and its tests still write through. Writing and fsyncing the temporary
file makes its CONTENT durable; the rename that publishes it is a change to the DIRECTORY, and a
power cut can lose that separately. A writer with the directory fsync and one without would look
the same until the day it mattered.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["TEMP_SUFFIX", "write_atomic"]

TEMP_SUFFIX = ".tmp"
"""Beside the real file rather than in a temporary directory, so the rename cannot cross a
filesystem, which is the one thing that would stop it being atomic."""


def write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` so that no reader can ever see half of it.

    A failure before the rename leaves the old document exactly as it was, which is the property
    the whole arrangement exists for.
    """
    temp = path.with_name(path.name + TEMP_SUFFIX)
    with temp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)
    _fsync_directory(path.parent)


def _fsync_directory(directory: Path) -> None:
    """Make the rename itself survive a power cut, not just the bytes it renamed.

    Skipped rather than fatal where the platform does not allow opening a directory: the rename
    has already happened, and refusing the write because the durability could not be improved
    would be worse than writing without it.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        return
    finally:
        os.close(fd)
