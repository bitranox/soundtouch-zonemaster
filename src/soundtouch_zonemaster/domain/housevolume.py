"""The house-volume rule: which volume report steps the whole house, and how far each box goes.

Decided by the user 2026-09-24 (OPEN-WORK rank 191). A person at one box taps a thumb once (up or
down; a hold was the trigger until 2026-09-25, when the hold became multiroom alone) and then uses
volume up or down; for as long as they keep doing that, the change applies to every box in the
house. The other boxes move by the SAME STEP, each from its own level, so a room that was
quieter than Room1 stays quieter. A box that is off hears nothing, so it is OWED the step and
takes it when it next joins.

**It is one key after the other because two at once cannot be sent.** Measured the same evening
(``docs/measurements/2026-09-24-thumb-and-volume.md``): with thumbs up held on the IR remote, the
volume key never reached the box, which reported nothing at all. And a volume key is never
forwarded to the master in any case - the box changes its own volume and REPORTS the result - so the
thumb opens a window and the box's reports inside it are the steps.

**Only the box that opened the window steps the house.** Every other box's report is either our own
write coming back or somebody else's hand, and reading either as a step would send it round again.

The clock is passed in on every call, as in ``longpress`` and ``dialling``: this module owns no
timer, does no I/O and logs nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

__all__ = ["MAX_VOLUME", "OPEN_S", "RENEW_S", "HouseVolume", "owe", "stepped"]

MAX_VOLUME = 100
"""The top of a SoundTouch's volume scale, as ``/volume`` reports and accepts it."""

OPEN_S = 4.0
"""How long after the thumb the first volume report may arrive.

Measured 2026-09-24: the hold fires at the threshold while the thumb is still down, the thumb came
up 0.5 to 1.8 s later, and the first report followed 0.22 to 0.55 s after that. Four seconds covers
that with room for a hand slower than the one measured, and is short enough that a volume change
somebody makes a minute later is their own room's again.
"""

RENEW_S = 3.0
"""How long each report keeps the window open for the next one.

A held volume key reports about every 300 ms, and a person tapping pauses to hear what they did;
three seconds covers the pause and closes soon after they stop."""


def stepped(level: int, step: int) -> int:
    """A level moved by a step, stopped at zero and at the top of the scale."""
    return max(0, min(MAX_VOLUME, level + step))


def owe(owed: Mapping[str, int], device_ids: Iterable[str], step: int) -> dict[str, int]:
    """The steps each box that could not hear it is owed, with this one added.

    A box whose owed steps cancel out is dropped rather than kept at zero, so the state file only
    names boxes that still have something to take when they join.
    """
    after = dict(owed)
    for device_id in device_ids:
        after[device_id] = after.get(device_id, 0) + step
        if after[device_id] == 0:
            del after[device_id]
    return after


class HouseVolume:
    """The last level each box reported, and which box's window is open until when."""

    def __init__(self) -> None:
        self._levels: dict[str, int] = {}
        self._open_until: dict[str, float] = {}

    def open(self, device_id: str, *, at: float) -> None:
        """A thumb was tapped once at this box: its next reports step the house."""
        self._open_until[device_id] = at + OPEN_S

    def close(self, device_id: str) -> None:
        """The tap that opened the window became a double tap: the volume keys are the room's again.

        A single tap opens the window AT ONCE rather than after waiting to see whether a second tap
        follows, because the first volume report lands 0.22 to 0.55 s after the thumb comes up
        (measured 2026-09-24) - inside any dialling window. So the second tap has to take it back.
        """
        self._open_until.pop(device_id, None)

    def level_of(self, device_id: str) -> int | None:
        """The level a box last reported, or nothing while it has reported none."""
        return self._levels.get(device_id)

    def reported(self, device_id: str, level: int, *, at: float) -> int | None:
        """Write down a box's level, and answer the house step when it is one.

        A step is a CHANGE reported by the box whose window is open, measured from its previous
        report. A box never heard before has nothing to measure from, so its first report is only
        written down, and the next one counts.
        """
        previous = self._levels.get(device_id)
        self._levels[device_id] = level
        until = self._open_until.get(device_id)
        if until is None or at > until:
            self._open_until.pop(device_id, None)
            return None
        self._open_until[device_id] = max(until, at + RENEW_S)
        if previous is None or previous == level:
            return None
        return level - previous
