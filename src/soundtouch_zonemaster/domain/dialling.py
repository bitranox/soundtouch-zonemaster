"""One digit buffer per speaker, and the one wait time above all of them.

A channel number is dialled a digit at a time on the preset keys, and nothing separates one number
from the next except a pause. So this decides exactly one thing: when a buffer has stopped growing
long enough to be read as a number.

**The window's floor is measured and is 500 ms.** Eleven deliberately fast pairs on two boxes
landed 211 to 398 ms apart, median 320, so a 300 ms window would have cut SIX of the eleven in two
and turned one two-digit number into two channel changes. At 400 ms none of them splits; 500 ms is
that with room for a hand that is slower on another day. The failure the old derived floor was
guarding against - two presses arriving so close that one reads as two - never appeared once
(``docs/measurements/2026-09-06-key-family.md``, question 4).

**A digit is never de-duplicated.** On 2026-09-07 five presses of 1, 1, 2, 2, 1 produced five
frames, so two identical digits inside the window are the number 11 and not one press read twice.
That is the opposite of the KEY path - next, previous and the thumbs, where every press arrives
twice as ``press`` and ``release`` 365 to 444 ms apart. Getting the two the wrong way round reads
every dialled number as doubled, or every next as two nexts.

The clock is passed in on every call rather than read here. That is what makes the boundary
assertable instead of waited out, and it means this module owns no timer: it reports a
:meth:`Dialler.deadline` and the service sleeps to it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from dataclasses import dataclass

from .channellist import dialable

__all__ = ["WINDOW_CEILING_S", "WINDOW_DEFAULT_S", "WINDOW_FLOOR_S", "Dialler", "DigitIgnored"]

WINDOW_FLOOR_S = 0.5
"""Measured, not derived. Below this a two-digit number starts splitting into two."""

WINDOW_DEFAULT_S = 0.8
"""What the house gets until somebody calibrates it against the person who will use it.

800 ms is the user's number (2026-09-07): twice the largest gap M0 measured, which leaves a hand
that is slower than the one measured room before a number starts splitting."""

WINDOW_CEILING_S = 2.0
"""Above this the wait between pressing and hearing stops reading as a wait and starts reading as
a fault."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DigitIgnored:
    """A digit no preset key can press, and the line the caller writes about it.

    The text is carried rather than the digit, because this module no longer knows how a line
    is worded once it leaves: it is the same shape ``CalibrationResult.said`` has, and it keeps
    the wording measured against the old service byte for byte while nothing in ``domain``
    narrates anything itself.
    """

    said: str


def _nobody(device_id: str) -> bool:
    """Nobody is mid-press: what a caller that does not track presses answers."""
    del device_id
    return False


class Dialler:
    """The digits each speaker has pressed since its last pause, and when that pause is up."""

    def __init__(self, *, window_s: float) -> None:
        self.window_s = window_s
        self._buffers: dict[str, tuple[str, float]] = {}
        """Device id to the digits so far and the moment the window was last re-armed."""
        self._steps: dict[str, tuple[int, float]] = {}
        """The same, for next and previous: a signed count and its own re-arm moment.

        Separate from the digits on purpose. Pressing 1 and then next is two different actions,
        and one buffer holding both would make either unreadable.
        """
        self._thumbs: dict[str, tuple[str, int, float]] = {}
        """Device id to the thumb last tapped, how many taps of it in a row, and when it came up."""

    def thumb(self, device_id: str, key: str, *, pressed_at: float, released_at: float) -> int:
        """Count one tapped thumb: 1 for a single tap, 2 for the second of a double tap.

        A tap continues the count only when it is the SAME thumb pressed within the window of the
        last one coming up. That is the idle time between the two, measured the way the digits'
        window is armed: how long a thumb rests on a key is not a pause (user, 2026-09-21).

        The count is answered at every tap rather than at the window's end, because each answer
        acts at once - a single tap hands the box the house volume before its first volume report
        can arrive, and a double tap moves the box in or out of the group at its second release. Two taps are one
        double tap and the count starts again after them, so a third is a single tap of its own.
        """
        count = 1
        last = self._thumbs.get(device_id)
        if last is not None:
            last_key, last_count, last_up = last
            if last_key == key and last_count == 1 and pressed_at - last_up <= self.window_s:
                count = 2
        self._thumbs[device_id] = (key, count, released_at)
        return count

    def digit(self, device_id: str, digit: str, *, at: float) -> DigitIgnored | None:
        """Append one pressed digit and re-arm the window; answer what was ignored, if any.

        Re-arming on EVERY digit rather than only on the first is the whole rule. Arming once would
        pass a two-digit test and then complete a four-digit number after its first digit, turning
        one number into four channel changes.

        ``at`` is the moment the key came back UP, not the moment it went down (user, 2026-09-21).
        The window is the idle time between two keys, and a window armed at the press charges a
        person for however long they held the last one: measured in the flat, a 0.645 s gap between
        two presses is about 0.31 s of idle time, and a 0.6 s window armed at the press split the
        number all the same. Nothing here knows that - it is simply the moment the caller hands in.
        """
        if not dialable(digit):
            # A buffer holding this could never complete into a number anybody can reach, so it is
            # dropped rather than carried. No preset key produces one; a box reporting it would be
            # telling us something new, which is worth a line in the log.
            return DigitIgnored(said=f"{device_id}: ignoring {digit!r}, which no preset key can press")
        digits, _ = self._buffers.get(device_id, ("", at))
        self._buffers[device_id] = (digits + digit, at)
        return None

    def deadline(
        self, *, still_pressing: Callable[[str], bool] = _nobody, still_stepping: Callable[[str], bool] = _nobody
    ) -> float | None:
        """When the earliest pending number completes, or ``None`` while nothing is being dialled.

        The service sleeps to this instead of polling, and reads it again after every wake: a digit
        arriving during that sleep moves it.

        A box the caller says is STILL PRESSING contributes no number, and one STILL STEPPING no
        jump, for the same reasons they may not complete - and because a deadline already in the
        past that cannot be drained would spin the waiting loop rather than let it sleep.
        """
        pending = [
            armed_at for device_id, (_digits, armed_at) in self._buffers.items() if not still_pressing(device_id)
        ]
        pending += [armed_at for device_id, (_, armed_at) in self._steps.items() if not still_stepping(device_id)]
        if not pending:
            return None
        return min(armed_at + self.window_s for armed_at in pending)

    def step(self, device_id: str, delta: int, *, at: float) -> None:
        """Add one step of next (+1) or previous (-1), and re-arm the window.

        Repeated presses inside the window are collected into ONE jump, because a channel change
        is not cheap here - a new source, a new t0, a new PLAY to every speaker and the placement
        books reset - so three quick nexts must not be three of that in half a second.

        WHICH key means which sign, and the fact that every key arrives twice as press and release
        365 to 444 ms apart, are the caller's business. This module never sees a key.
        """
        steps, _ = self._steps.get(device_id, (0, at))
        self._steps[device_id] = (steps + delta, at)

    def forget_steps(self, device_id: str) -> None:
        """Drop what one speaker has collected in steps, without touching its digits.

        The caller for this is the calibration gesture: its four presses are steps until the
        fourth one turns them into something else, and they only sum to zero if all four land in
        one jump. At a leisurely pace the window closes between two of them, and then the first
        pair would move the zone and the second move it back - two audible restarts in answer to
        something that was never a channel change.
        """
        self._steps.pop(device_id, None)

    def steps_due(self, *, at: float, still_stepping: Callable[[str], bool] = _nobody) -> tuple[tuple[str, int], ...]:
        """Every jump whose window has closed by ``at``, taken out of the buffers as it is read.

        A jump of zero is dropped rather than reported: next then previous inside one window is a
        person changing their mind, and restarting the station to arrive where it already is would
        be an audible answer to nothing.

        **A box whose step key is still down keeps its jump open.** The key may yet become a hold,
        and a hold replaces the tap it began as. The window and the hold threshold are two numbers
        (user, 2026-09-24), so the window can close first; acting then stepped once as a tap and
        again at the hold. The caller answers the question, as it does for ``still_pressing``.
        """
        closed = sorted(
            (device_id, steps)
            for device_id, (steps, armed_at) in self._steps.items()
            if armed_at + self.window_s <= at and not still_stepping(device_id)
        )
        for device_id, _ in closed:
            del self._steps[device_id]
        return tuple((device_id, steps) for device_id, steps in closed if steps)

    def due(self, *, at: float, still_pressing: Callable[[str], bool] = _nobody) -> tuple[tuple[str, str], ...]:
        """Every number whose window has closed by ``at``, taken out of the buffers as it is read.

        Sorted by device id so that two speakers completing in one pass always report in the same
        order; an order that varies makes one event read as two different ones in a log.

        **A box that is STILL PRESSING keeps its number open however long the window has been up.**
        The window is armed at a key's release, and a press is only known once the box has finished
        reporting it - so a digit already on its way would otherwise arrive after its own number
        had completed, and a four-digit channel would be dialled as a three and a one. The caller
        answers the question; nothing here knows what a press is.
        """
        complete = sorted(
            (device_id, digits)
            for device_id, (digits, armed_at) in self._buffers.items()
            if armed_at + self.window_s <= at and not still_pressing(device_id)
        )
        for device_id, _ in complete:
            del self._buffers[device_id]
        return tuple(complete)
