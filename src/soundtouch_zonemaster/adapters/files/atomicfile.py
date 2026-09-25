"""Writing a file so that nothing can ever read half of it.

Both files this service owns are read after whatever ended the last run, which includes a power cut
in the middle of a write. So neither may be written in place: the document goes to a temporary file
beside the real one, is flushed to the disk, and is then renamed over it. A rename is atomic, so a
reader sees the old document or the new one and never half of either.

It lives in its own module because two files need exactly this and the rule has a part that is easy
to leave out. Writing and fsyncing the temporary file makes its CONTENT durable; the rename that
publishes it is a change to the DIRECTORY, and a power cut can lose that separately. One of the two
callers having the directory fsync and the other not would be invisible until the day it mattered.

The switch file deliberately does NOT go through here. It is one word, rewritten by a person, and
giving it a durable write would say something about it that is not true.
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
