"""What the service must remember across a restart.

The current channel and the membership, and later the position of every file channel. It is small
on purpose: everything else is asked again at start, from the registry and from the speakers
themselves, because a fact that can be re-derived is a fact that cannot go stale here.

The record only. Reading and writing it - including the rule that a document this cannot make
sense of starts the service EMPTY rather than raising - is ``adapters/files/state_file.py``, which
keeps a pydantic model over exactly these fields so the bytes on disk and the problem counts in
the log do not change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["Place", "ZoneState"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Place:
    """Where a channel was left: which file of it, and how far into that file.

    Both halves or neither. Measured in the flat 2026-09-20 at 15:43 with only the seconds kept:
    the house left the fifth file of a channel 1.5 s in, came back, and played the FIRST file 1.5 s
    in. The offset alone is not a place in a list, and the file alone is not a place in a book.

    ``track`` is MPD's ``song``, the position in the queue counting from zero. It is not called
    entry because an ENTRY here is the stored playlist a channel names (``Channel.mpd_entry``),
    and one channel's entry holds many tracks.
    """

    track: int
    seconds: float
    file: str | None = None
    """The file's path as MPD names it, or nothing for a place written before names were kept.

    The name, because the index is a position in an ORDER and a directory's order changes the
    moment somebody adds a file in front of the one the house was in (OPEN-WORK rank 11). The
    index alone would then point at a different chapter with nothing anywhere saying so.
    """

    def found_in(self, files: Sequence[str]) -> Place | None:
        """This place in the files as they are NOW, or nothing when it is no longer anywhere.

        By NAME first, offset kept. A file that is gone falls back to its old index, from the
        start of the file now there: the offset belonged to the file that went, and twenty minutes
        into a different chapter is not a place anybody left. An index past the end, or no files at
        all, is no place, and the channel starts from the beginning.

        A place with no name is trusted by its index, which is all a place written before names
        were kept has to go on.
        """
        if self.file is not None and self.file in files:
            return Place(track=files.index(self.file), seconds=self.seconds, file=self.file)
        if not 0 <= self.track < len(files):
            return None
        if self.file is None:
            return self
        return Place(track=self.track, seconds=0.0, file=files[self.track])

    def resumed(self, *, rewind_s: float) -> Place:
        """Where to actually start when the house comes back here (user, 2026-09-20).

        A little BEFORE the place, so that somebody returning to an audiobook hears their way back
        in rather than landing mid-sentence. The stored place is left exactly where the house
        stopped and only the resume steps back, so the overlap is a setting that can change
        without rewriting anything already recorded.

        It never steps into the PREVIOUS track: an offset shorter than the overlap starts that
        same file again from the beginning. The file before it is a different recording, often a
        different chapter, and a person who stopped ten seconds into one did not ask to hear the
        end of the other.
        """
        return Place(track=self.track, seconds=max(0.0, self.seconds - rewind_s), file=self.file)


@dataclass(frozen=True, slots=True, kw_only=True)
class ZoneState:
    """What survives a restart."""

    channel: str | None = None
    """The channel number as dialled, a digit string over 1 to 6, or nothing playing."""

    members: tuple[str, ...] = ()
    """Device ids, not addresses: an address can change while the service is down."""

    muted: dict[str, int] = field(default_factory=dict[str, int])
    """Boxes this service turned down to zero to hide their own audio while taking them in, and the
    volume each was on before that.

    It is written BEFORE the box is muted and cleared after it is put back, so a service that dies
    in between can still find the level on its next start. Nothing else in this file protects a
    person from a symptom: a speaker silently at zero reads as broken hardware rather than as a
    service that stopped halfway."""

    out_of_multiroom: tuple[str, ...] = ()
    """Boxes somebody switched out of the zone by hand, by holding a thumb down on one of them.

    Device ids, for the same reason as ``members``. It is here rather than derived because it is a
    decision a person made standing in the room and nothing they can see records it: a restart that
    quietly took the box back into the zone would undo that decision without anybody pressing
    anything, and the room would start playing again on its own."""

    positions: dict[str, Place] = field(default_factory=dict[str, Place])
    """Where each MPD channel was left, by channel number, written when the house leaves it.

    MPD keeps none of its own - measured 2026-09-10: coming back to a stored playlist gives
    ``song`` and ``elapsed`` as nothing and ``state`` as stop - so an audiobook that is not
    remembered here is an audiobook that starts again from the beginning after every restart.

    A channel absent from this map has never been left, which is a different thing from a channel
    left at the very beginning, and the two must not be written the same way: a stopped MPD
    reports no position at all, and recording that as 0.0 would put the start of the book over a
    real place in it with nothing to report. The same rule covers the file: a status that names no
    ``song`` is not a status that names the first one."""

    dial_window_s: float | None = None
    """What a calibration measured, or nothing while none has run.

    It is here rather than in the options because it is measured on a person in this house, and a
    number that had to be re-measured after every restart would be worth less than the option it
    replaces. Nothing means the option decides, which is what a first start looks like.
    """

    hold_threshold_s: float | None = None
    """How long a key must stay down to be held, as the same calibration measured it, or nothing.

    Its own field rather than the window's value because the two measure different things (user,
    2026-09-24): the window the pause between two keys, this how long one key is down. A file
    written before it existed carries a window and no threshold, and loads with the option deciding.
    """

    owed_volume: dict[str, int] = field(default_factory=dict[str, int])
    """The house-volume steps each box missed while it was off, by device id, summed.

    A box in standby cannot be told the house was turned up, and writing to a sleeping box might
    wake it - which would read as somebody switching it on. So the step is owed instead, and taken
    when the box next joins: the join already turns it down and fades it back up, and the fade ends
    at its own level plus what it is owed (user, 2026-09-24, OPEN-WORK rank 191). It is in this
    file because the house can be turned up hours before Room1 is next switched on."""
