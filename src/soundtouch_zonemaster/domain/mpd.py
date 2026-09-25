"""What MPD said about itself, in the vocabulary both sides of the port may name.

It is here rather than beside the client because the SERVICE reads a position from it once
channels remember where they were, and ``application`` may not import an adapter. A record that
lived in the adapter could not be the return type of the port that hands it over.

The keys a stopped MPD omits are ``None`` here and never zero. Measured 2026-09-10: a stopped
``status`` carries no ``elapsed``, ``song`` or ``duration`` key AT ALL, and reading an absent key
as 0.0 would write a position of zero over a real one the next time the house changed channel,
with nothing to report - the channel would simply start from the beginning next time.
"""

from __future__ import annotations

from dataclasses import dataclass

from .enums import MpdState

__all__ = ["MpdStatus", "entry_after"]


@dataclass(frozen=True, slots=True, kw_only=True)
class MpdStatus:
    """What ``status`` said, with the keys a stopped MPD omits kept as None rather than zeroed."""

    state: MpdState
    song: int | None = None
    """Which entry of the queue is current, counting from zero, or nothing while stopped."""

    elapsed: float | None = None
    """Seconds into that entry, or nothing while stopped. This is what a channel remembers."""

    duration: float | None = None
    playlist_length: int = 0

    def ran_out(self) -> bool:
        """Stopped with a queue still loaded, which is what the natural end of a queue leaves.

        The loaded queue is the half that matters. A restarted MPD that lost its queue is stopped
        too, and reading THAT as an end would throw away a real place in a book with nothing to
        report.
        """
        return self.state is MpdState.STOP and self.playlist_length > 0


def entry_after(*, current: int | None, length: int, steps: int) -> int | None:
    """The queue entry ``steps`` presses away from ``current``, wrapping at both ends.

    Worked out here rather than left to MPD's ``next`` and ``previous``, because those stop at the
    end of the queue unless ``repeat`` is on, and a press past the end wraps on EVERY channel (user,
    2026-09-24) - a ``stop`` channel's ``repeat`` is off. With no current entry, which is a queue
    that ran out, next starts from the first entry and previous from the last. An empty queue has
    nowhere to go.
    """
    if length <= 0:
        return None
    start = current if current is not None else (-1 if steps > 0 else length)
    return (start + steps) % length
