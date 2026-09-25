"""The real narrator: one line, stamped, on whichever stream this run is using.

The shape every part of the program is handed is ``domain.logfn.LogFn`` and nothing more. The
CALLABLE is here, at the edge, because where a line goes is a property of the run rather than of
the line: normally stdout, where the ``sync`` line is already read from, and all of it on stderr
in a machine-readable mode so that stdout carries the envelope and nothing else.

Two programs share it, so that somebody reading their output is reading the same thing.
"""

from __future__ import annotations

import sys
import time
from typing import ClassVar

from ...domain.logfn import ERROR_KIND

__all__ = ["LogRouting", "log"]


class LogRouting:
    """Whether this run's narration goes to stderr instead of stdout.

    Class state rather than an argument because ``log`` is passed as a bare callable to every part
    of the program, and threading a stream through all of them would be a parameter nothing but the
    two entry points ever sets.
    """

    to_stderr: ClassVar[bool] = False


def log(kind: str, text: str) -> None:
    """One narrated line, stamped with the time it was written.

    Written to the stream rather than printed, so that a rule against stray prints keeps meaning
    something everywhere else: this is the one place output is the job.
    """
    now = time.time()
    stamp = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}"
    stream = sys.stderr if (kind == ERROR_KIND or LogRouting.to_stderr) else sys.stdout
    stream.write(f"{stamp} {kind:<12} {text}\n")
    stream.flush()
