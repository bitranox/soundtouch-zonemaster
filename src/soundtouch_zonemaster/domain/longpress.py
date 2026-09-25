"""The one gesture rule: was that a tap, or is the key being held?

Every key the house forwards has to be classified before anything can act on it, and this is the
only place that decides. A key released inside the hold threshold was a TAP, and joins whatever
the dialling window is collecting; a key still down when the threshold passes is a HOLD, and acts
THEN. What a tap or a hold MEANS is the caller's business - nothing here knows what a key is for.

**The threshold is its own number, measured on the same presses as the dialling window** (user,
2026-09-24, OPEN-WORK rank 188). Until then one number governed both, and that was right while the
window still contained the time a key was down. Once the window was armed at the RELEASE it measured
the pause BETWEEN two keys, while a hold asks how long ONE key stays down - and on 2026-09-21 this
house's hand held ordinary taps down for up to 685 ms against a window calibrated to 0.6 s, so a
slow tap was already read as a hold. A calibration now yields both, each from what it governs, so a
slower hand still gets both longer from one measurement.

**The decision is made at the threshold, not at the release.** A SoundTouch sends nothing at all while
a key is down - its firmware's ``KeyState`` enum carries ``repeat``, ``pnh`` and ``xpnh`` and a box
sends none of them, measured over a 4.42 s hold - so a hold cannot be announced. But it does not
have to be: a key that has not come back up by the threshold is being held, and that is knowable
without another frame. Waiting for the release instead would have acted 4.4 s after the person
meant it.

**A key that reports no release can only ever be a tap.** A preset press arrives as a selection
frame with no second half, so its length is not observable and a long ``1`` is a short ``1``
(user, 2026-09-20). Such keys never reach this module at all.

**A pair is easier to break than to make.** The service can start with a key already held, a frame
can be lost, and a box can go off the air mid-press. So every refusal answers ``None`` rather than
a gesture, because the action a refusal costs is one a person simply makes again.

The clock is passed in on every call rather than read here, the way ``dialling`` and ``presses``
take theirs. That is what makes each boundary assertable exactly instead of waited out, and it
means this module owns no timer, does no I/O and logs nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "HOLD_CEILING_S",
    "HOLD_THRESHOLD_CEILING_S",
    "HOLD_THRESHOLD_DEFAULT_S",
    "HOLD_THRESHOLD_FLOOR_S",
    "Hold",
    "LongPresses",
]

HOLD_THRESHOLD_FLOOR_S = 1.0
"""The shortest a key may be held and still be a hold: the user's own second (2026-09-20, chosen
again 2026-09-24). It sits 315 ms above the slowest ordinary tap measured in this house (685 ms), so
a calibration on a quick hand lands here rather than close under the taps it has to tell apart."""

HOLD_THRESHOLD_DEFAULT_S = HOLD_THRESHOLD_FLOOR_S
"""What the house gets until a calibration measures the hand that presses."""

HOLD_THRESHOLD_CEILING_S = 2.0
"""The longest a threshold may be. Above it a held key acts so late that the person has already
let go and pressed again, reading the first press as having done nothing."""

HOLD_CEILING_S = 10.0
"""How long a key that has already fired its hold is kept waiting for its release.

It is bookkeeping rather than a rule anybody can feel: the hold has acted, and what is left is a
finger nobody can see lifting. A box that goes off the air mid-press would otherwise leave that key
down in the table for the rest of the run, and the release that finally arrives - minutes later -
would be answered as though the gesture were still live.

It sits well above the 4422 ms of the longest hold ever measured, so no real press reaches it.
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class Hold:
    """One gesture a key finished: which key, how long it was down, and which of the two it is.

    ``seconds`` is carried beside the verdict rather than folded into it because the caller logs
    it. A press just under the threshold is exactly the case somebody will complain about, and the
    duration is what tells them by how much they missed rather than that nothing happened.
    """

    key: str
    seconds: float
    long: bool


class LongPresses:
    """Which keys are down, which have already fired, and nothing else.

    ``threshold_s`` is how long a key must stay down to be held. A calibration measures it on the
    same presses as the dialling window, and the service sets both from one result, so neither can
    be re-measured while the other is left behind.
    """

    def __init__(self, *, threshold_s: float) -> None:
        self.threshold_s = threshold_s
        self._down: dict[tuple[str, str], tuple[float, bool]] = {}
        """Device id and key to the moment that key went down, and whether its hold has fired.

        Keyed by the PAIR, because one hand can be on two buttons and two rooms can be pressing
        the same one. A press is taken out by the release that answers it, and one whose release
        never arrives is dropped at :data:`HOLD_CEILING_S`.
        """

    def pressed(self, device_id: str, key: str, *, at: float) -> None:
        """Record that a key went down, replacing whatever that key was already holding.

        A second press with no release in between means the first one's release was lost. The
        newer press is the live one: timing from the older moment would read a tap as a hold,
        which is the wrong direction to fail in.
        """
        self._down[device_id, key] = (at, False)

    def undecided(self, device_id: str, key: str) -> bool:
        """Whether that key is down and not yet a hold - so it may still become either gesture.

        A key whose hold has fired is decided although the finger is still on it, and a key let
        go is decided by the release.
        """
        down = self._down.get((device_id, key))
        return down is not None and not down[1]

    def deadline(self) -> float | None:
        """When the earliest key still down becomes a hold, or nothing while none can.

        The service sleeps to this instead of polling, and reads it again after every wake: a
        further press moves it, and a release or a fired hold takes one away.
        """
        pending = [down_at + self.threshold_s for down_at, fired in self._down.values() if not fired]
        return min(pending) if pending else None

    def due(self, *, at: float) -> tuple[tuple[str, Hold], ...]:
        """Every key that has now been down longer than the threshold, reported once.

        Reported once and not again, however long the finger stays on the button: the gesture is
        the crossing of the threshold, not the state of being held. The key is left in the table
        rather than taken out, because its release is still to come and has to be swallowed rather
        than read as a tap - and a key that fired and was never released is dropped here too, at
        the ceiling, which is the only sweep this module has.
        """
        self._drop_what_can_no_longer_be_released(at=at)
        crossed = sorted(
            (device_id, key, down_at)
            for (device_id, key), (down_at, fired) in self._down.items()
            # Compared on the same side as ``deadline()`` builds it, rather than as a subtraction:
            # a threshold of 0.6 s is not representable, so ``at - down_at >= 0.6`` is FALSE at
            # exactly the moment the loop was woken for, and the hold would slip a whole tick.
            if not fired and at >= down_at + self.threshold_s
        )
        for device_id, key, down_at in crossed:
            self._down[device_id, key] = (down_at, True)
        return tuple((device_id, Hold(key=key, seconds=at - down_at, long=True)) for device_id, key, down_at in crossed)

    def released(self, device_id: str, key: str, *, at: float) -> Hold | None:
        """The tap that key just finished, or ``None`` when there is nothing to answer.

        The press is spent whichever way this goes. A press too old or too strange to answer THIS
        release is too old to answer a later one either, and leaving it in would only hand a stale
        moment to the next release of the same key.

        Four shapes answer nothing. A key whose hold already FIRED, which is the ordinary end of a
        held key and the reason this returns an option at all. A release with no press behind it,
        which is not hypothetical: the service can start with a key already held. A release that
        arrives BEFORE its press, which is a clock that went backwards and not a gesture of
        negative length. And a press older than :data:`HOLD_CEILING_S`.
        """
        down = self._down.get((device_id, key))
        if down is None:
            return None
        down_at, fired = down
        if not fired and at >= down_at + self.threshold_s:
            # The threshold passed and the loop has not woken yet, so this release arrived first.
            # It is not a tap - the threshold decides the gesture, not which of the two got here -
            # and the press is LEFT so that the hold is still reported by the one place that
            # reports holds. Spending it here would lose the action instead of delaying it.
            return None
        del self._down[device_id, key]
        seconds = at - down_at
        if fired or seconds < 0.0 or seconds > HOLD_CEILING_S:
            return None
        return Hold(key=key, seconds=seconds, long=False)

    def forget(self, device_id: str) -> None:
        """Drop every key one speaker is holding, and no other speaker's.

        For a box that has gone: whatever it had down can never be released now, and a moment kept
        from before would time a gesture from the wrong evening. Clearing the whole table instead
        would drop the key somebody in another room has under their finger right now.
        """
        self._down = {held: down for held, down in self._down.items() if held[0] != device_id}

    def _drop_what_can_no_longer_be_released(self, *, at: float) -> None:
        """Forget a press whose release is never coming, so the table cannot grow without bound."""
        self._down = {held: down for held, down in self._down.items() if at - down[0] <= HOLD_CEILING_S}
