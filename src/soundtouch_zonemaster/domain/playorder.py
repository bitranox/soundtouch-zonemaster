"""The order a directory channel plays its files in.

The user's rule (OPEN-WORK rank 11, decision C, 2026-09-24): on every level the files of THAT level
first, then its subdirectories, each played to its end before the next one begins - the order a
person opening the folders one after another would read them in. A book's chapters usually sit in
its directory and a bonus directory beside them, and the bonus belongs after the book rather than
in the middle of it.

Names are compared NATURALLY and without the environment: a run of digits is a number, so chapter 2
comes before chapter 10; case does not decide; and an umlaut is its base letter, by a fixed Unicode
normalisation rather than a locale's collation. That last part is what makes two machines holding
the same collection agree on the order, which matters because a remembered place names a position
in it.

The order is this module's and not MPD's database order, which is why the service builds the queue
one ``add`` per file rather than adding the directory.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = ["directory_jump", "play_order"]

_DIGITS = re.compile(r"(\d+)")

_NO_NUMBER = -1
"""What a name's last text run is paired with: less than any number, so ``a`` sorts before ``a0``."""

_FILE, _DIRECTORY = 0, 1
"""At one level a file sorts before a directory, which is the whole of files-before-subdirectories."""

type _NaturalKey = tuple[tuple[str, int], ...]
type _ComponentKey = tuple[int, _NaturalKey, str]


def _natural(name: str) -> _NaturalKey:
    """A name as (text, number) pairs, so digits compare as numbers and text never meets a number.

    Pairing rather than a flat mixed list is what keeps every comparison between two values of one
    type: position N is always a text run followed by the number after it.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    plain = "".join(character for character in decomposed if not unicodedata.combining(character)).casefold()
    runs = _DIGITS.split(plain)
    texts, numbers = runs[0::2], [int(run) for run in runs[1::2]]
    return tuple(zip(texts, [*numbers, _NO_NUMBER], strict=True))


def _component(name: str, *, kind: int) -> _ComponentKey:
    """One level of a path. The raw name last, so two names equal under the rule stay two.

    That tie is broken per LEVEL and not on the whole path: broken on the path, ``a/`` and ``A/``
    would be one branch as far as the order is concerned, and their files would interleave.
    """
    return (kind, _natural(name), name)


def _path_key(path: str) -> tuple[_ComponentKey, ...]:
    *directories, file = path.split("/")
    return (*(_component(one, kind=_DIRECTORY) for one in directories), _component(file, kind=_FILE))


def play_order(files: Iterable[str]) -> tuple[str, ...]:
    """Every file, in the order a directory channel plays them. Paths are ``/``-separated.

    Comparing paths level by level with a file ranked before a directory at each level IS the
    depth-first walk: everything under ``A/`` shares its first component, so it stays together
    and comes out before ``C/``, however deep it goes.
    """
    return tuple(sorted(files, key=_path_key))


def _run_starts(queue: Sequence[str]) -> tuple[int, ...]:
    """Where each unbroken stretch of one parent directory begins.

    Stretches rather than directories, because a stored playlist can come back to a directory it
    left, and a person stepping through it meets that directory twice.
    """
    parents = [path.rpartition("/")[0] for path in queue]
    return tuple(index for index, parent in enumerate(parents) if index == 0 or parent != parents[index - 1])


def directory_jump(queue: Sequence[str], *, current: int | None, steps: int) -> int | None:
    """The queue entry a held next or previous lands on: the first file of a directory.

    The user's rules (OPEN-WORK rank 11, 2026-09-24). D: the next directory is the next one in PLAY
    order, the next entry whose parent differs, so a subdirectory that comes next is not skipped.
    G: a held previous from inside a directory goes to that directory's first file, and only from
    there to the one before, the way a CD player's back key does. H: two holds are two directories.
    E: past either end it wraps, on every channel.

    Only the PATHS are read, so it holds for a stored playlist the same as for a directory channel.
    With no current entry, which is a queue that ran out or one read shorter than the status that
    named the entry, next starts at the first directory and previous at the last, as
    ``entry_after`` does for files. An empty queue has nowhere to go.
    """
    starts = _run_starts(queue)
    if not starts:
        return None
    if current is None or not 0 <= current < len(queue):
        return starts[(steps - 1) % len(starts)] if steps > 0 else starts[steps % len(starts)]
    run = max(index for index, start in enumerate(starts) if start <= current)
    if steps < 0 and current != starts[run]:
        # The first step back is spent reaching the start of the directory the file is in.
        steps += 1
    return starts[(run + steps) % len(starts)]
