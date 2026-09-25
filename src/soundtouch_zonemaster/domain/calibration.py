"""The dialling window and the hold threshold, measured on the person who will use them.

The wait between two pressed digits is one number for the whole house, and the house it is right
for is whoever set it up. The people who need the longer window are the least likely to go and
change a setting, so the service measures them instead: a gesture starts a calibration, the person
presses number keys as they normally would, and the gaps between those presses become the window.

**The same presses give the hold threshold** (user, 2026-09-24, OPEN-WORK rank 188): how long each
key was DOWN, where the window is the pause between two of them. They are two numbers because they
answer two questions - a slow tap can be down longer than the pause after it - and one measurement
because a slower hand needs both longer. The threshold uses the window's statistic over the
key-down times, floored at the user's second. A digit's two touches pair only up to
``presses.HOLD_CEILING_S`` apart, so a digit held longer than that is no sample at all.

**Starting it is a gesture and not a number** (user, 2026-09-07, over three other shapes). Next,
previous, next, previous in quick succession, collected in a window of its OWN. That last part is
the whole reason it beat a reserved channel number: a person needs a calibration because the
dialling window is too short for them, so a start that goes THROUGH that window cannot be relied on
by the very person the feature exists for.

**The gesture is inert at the pace a person actually presses.** Alternating steps sum to zero, and
four presses at the 320 ms M0 measured all land inside one dialling window, so the jump they make
is zero and is dropped. Pressed slowly enough that the window closes between two of them, the zone
steps a channel and steps back: the listener ends where they started, having heard two restarts.
Suppressing that was tried and taken out again - the rule that could see a half-finished gesture
could not tell it from somebody pressing previous and then next on purpose, and it swallowed the
second key. A key that does nothing for a person who pressed it deliberately is worse than a
restart they can hear.

**The gap it measures is the IDLE time between two keys** - from the moment one comes back up to
the moment the next goes down - because that is what the dialling window governs (user,
2026-09-21: "exactly - its the time between two keypresses"). Measured from press to press instead,
it would charge the hold twice: the window is armed at a release, so a number built from
press-to-press gaps is longer than the hand it was measured on by however long that hand holds a
key, which is 0.23 to 0.447 s in this house.

**The statistic is the largest gap plus a margin of at least 100 ms**, a quarter of the gap where
that is more, rounded up and clamped. It reproduces the number the house already carries: the
eleven gaps M0 measured have a largest of 398 ms, and this returns exactly the 500 ms floor that
was chosen by hand from those same eleven. The two risks are not symmetric - a window that is too
long costs patience between the last digit and the change, one that is too short splits a
two-digit number into two audible channel changes - which is why the margin errs long.

Nothing here reads a clock. Every call takes the moment it happened, the way ``dialling`` does, so
the whole state machine is assertable rather than waited out.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .dialling import WINDOW_CEILING_S, WINDOW_FLOOR_S
from .enums import KeyName
from .longpress import HOLD_THRESHOLD_CEILING_S, HOLD_THRESHOLD_FLOOR_S

__all__ = [
    "CAP_S",
    "GESTURE_KEYS",
    "GESTURE_PRESSES",
    "GESTURE_WINDOW_S",
    "MARGIN_FLOOR_S",
    "MARGIN_FRACTION",
    "MIN_GAPS",
    "QUIET_S",
    "Calibration",
    "CalibrationResult",
    "Gesture",
    "hold_from_durations",
    "margin_for",
    "usable_gaps",
    "window_from_gaps",
]

GESTURE_KEYS: tuple[str, ...] = (KeyName.NEXT_TRACK, KeyName.PREV_TRACK)
"""The two keys the gesture alternates between. They are the ones that sum to zero."""

GESTURE_PRESSES = 4
"""How many alternating presses start a calibration. Two would happen by accident."""

GESTURE_WINDOW_S = 4.0
"""How long the four presses may take together.

Its own window rather than the dialling one, which is the point of this shape. Four seconds is
long enough for a hand that presses at half the speed M0 measured and short enough that four
alternating presses spread over a minute are not read as one gesture.
"""

QUIET_S = 3.0
"""How long the pressing has to stop before what was collected is read as the answer."""

CAP_S = 30.0
"""The whole calibration, so that somebody who walks away does not leave one open for ever."""

MIN_GAPS = 3
"""Fewer than this says nothing rather than guessing: one hurried pair is not a person's pace."""

MARGIN_FRACTION = 0.25
"""How much of the largest gap is added to it. A slower hand scatters more in absolute terms."""

MARGIN_FLOOR_S = 0.1
"""The smallest margin, whatever the fraction says: at least this far above the longest gap."""

ROUNDING_S = 0.1
"""What the answer is rounded UP to, so the number in the file reads like a decision."""


def margin_for(longest_gap_s: float) -> float:
    """How far above the longest measured gap the window is set.

    At least :data:`MARGIN_FLOOR_S`, and a quarter of the gap where that is more. Below a gap of
    400 ms the window floor already decides the answer, so the two terms only differ above it -
    the floor is here because the rule that was decided reads "at least 100 ms above the longest",
    and it becomes the deciding term again if the window floor is ever lowered.
    """
    return max(MARGIN_FLOOR_S, longest_gap_s * MARGIN_FRACTION)


def usable_gaps(gaps: tuple[float, ...]) -> tuple[float, ...]:
    """The gaps that count as a pace. A gap longer than the ceiling is a PAUSE, not a sample.

    One definition, shared by the rule and by the line that reports it, so the figure a person
    reads in the log is the figure the window was built from. Reporting the raw longest instead
    put a gap of 9.4 s and a window of 0.5 s in one sentence, which reads as the rule having
    ignored its own measurement rather than as a burst with a break in the middle.
    """
    return tuple(gap for gap in gaps if 0.0 < gap <= WINDOW_CEILING_S)


def window_from_gaps(gaps: tuple[float, ...]) -> float | None:
    """The dialling window these gaps ask for, or ``None`` when they do not ask for one.

    A gap longer than the ceiling is a PAUSE and not a sample, so a person may press in bursts.
    """
    usable = usable_gaps(gaps)
    if len(usable) < MIN_GAPS:
        return None
    return _bounded(max(usable) + margin_for(max(usable)), floor=WINDOW_FLOOR_S, ceiling=WINDOW_CEILING_S)


def hold_from_durations(durations: tuple[float, ...]) -> float | None:
    """The hold threshold these key-down times ask for, or ``None`` when they do not ask for one.

    The same statistic as the window - the longest sample plus its margin, rounded up and clamped -
    over how long each key was DOWN, which is what a hold is decided on. A key held past the
    ceiling was held on purpose rather than tapped, so it is not a sample of the hand's tap.
    """
    usable = tuple(duration for duration in durations if 0.0 < duration <= HOLD_THRESHOLD_CEILING_S)
    if len(usable) < MIN_GAPS:
        return None
    longest = max(usable)
    return _bounded(longest + margin_for(longest), floor=HOLD_THRESHOLD_FLOOR_S, ceiling=HOLD_THRESHOLD_CEILING_S)


def _bounded(wanted: float, *, floor: float, ceiling: float) -> float:
    """``wanted`` rounded UP to a tenth, then clamped, so the number in the file reads like a decision."""
    # Rounded before the ceiling division because 0.5 / 0.1 is 4.999... in binary, which would
    # round a value that lands exactly on a tenth up to the next one.
    stepped = math.ceil(round(wanted / ROUNDING_S, 9)) * ROUNDING_S
    return min(max(stepped, floor), ceiling)


@dataclass(frozen=True, slots=True, kw_only=True)
class CalibrationResult:
    """What one calibration produced: a window and a hold threshold, or nothing and the reason in words."""

    window_s: float | None
    hold_s: float | None
    """How long a key must now stay down to be held; ``None`` leaves the threshold as it is."""
    said: str
    """What to put in the log. It names what was collected, not only that it refused."""


class Gesture:
    """The alternating run of step keys each speaker is in the middle of."""

    def __init__(self) -> None:
        self._runs: dict[str, list[tuple[str, float]]] = {}
        """Device id to the presses still inside the gesture window, newest last."""

    def saw(self, device_id: str, key: str, *, at: float) -> bool:
        """Record one step-key PRESS; ``True`` when it completed the gesture.

        Two of the same key in a row is not an alternation, so the run starts again from that
        press rather than being thrown away - somebody pressing next twice and then alternating
        should not have to pause first.
        """
        run = [(pressed, when) for pressed, when in self._runs.get(device_id, []) if at - when <= GESTURE_WINDOW_S]
        if run and run[-1][0] == key:
            run = []
        run.append((key, at))
        self._runs[device_id] = run
        if len(run) < GESTURE_PRESSES:
            return False
        # Consumed, so eight presses are two gestures and not five overlapping ones.
        self._runs[device_id] = []
        return True

    def forget(self, device_id: str) -> None:
        """Break the run, because something that is not a step key was pressed.

        The gesture is four alternating presses with NOTHING in between. Without this a real
        sequence - next, thumbs down, previous - reads the previous as the second press of a
        gesture and never steps with it, which is a key that did nothing for somebody who pressed
        it deliberately.
        """
        self._runs.pop(device_id, None)


class Calibration:
    """One calibration in progress, or none.

    One at a time for the whole house: the result is one global setting, so two people measuring
    at once would be two answers to a question that has one.
    """

    def __init__(self) -> None:
        self._device_id: str | None = None
        self._started_at = 0.0
        self._announced = False
        self._presses: list[tuple[float, float]] = []
        """One entry per sampled press: the moment the key went down, and the moment it came up."""

    def begin(self, device_id: str, *, at: float) -> bool:
        """Start one; ``False`` when one is already running."""
        if self._device_id is not None:
            return False
        self._device_id = device_id
        self._started_at = at
        self._announced = False
        self._presses = []
        return True

    def is_running(self) -> bool:
        return self._device_id is not None

    def needs_announcing(self) -> bool:
        """Whether the house still has to be told this began. The telling is the caller's."""
        return self._device_id is not None and not self._announced

    def announced(self) -> None:
        self._announced = True

    def press(self, device_id: str, *, pressed_at: float, released_at: float) -> bool:
        """Take one digit press as a sample; ``False`` when it is not this calibration's to take.

        A press from another box is not a sample. It is not dialled either, which is the caller's
        decision and is written down there: a channel change in the middle of a measurement would
        be confusing in both rooms, and the measurement is over in seconds.

        BOTH moments are taken, because the gap this measures runs from one key coming up to the
        next going down and neither moment alone can say that.
        """
        if self._device_id is None or device_id != self._device_id:
            return False
        self._presses.append((pressed_at, released_at))
        return True

    def deadline(self) -> float | None:
        """When this calibration is read, or ``None`` while none is running.

        The quiet after the last press, or the cap when nobody has pressed anything yet - which
        also gives a person the time to hear that it started before having to press.
        """
        if self._device_id is None:
            return None
        if not self._presses:
            return self._started_at + CAP_S
        return min(self._presses[-1][1] + QUIET_S, self._started_at + CAP_S)

    def finish(self, *, at: float) -> CalibrationResult:
        """Read what was collected and end it, whatever it says."""
        # From one key coming UP to the next going DOWN, which is the idle time the window
        # governs. Measured between the two PRESSES it would carry the hold as well, and a window
        # built from it would be 0.23 to 0.447 s longer than the hand it was measured on.
        gaps = tuple(later[0] - earlier[1] for earlier, later in zip(self._presses, self._presses[1:], strict=False))
        durations = tuple(released - pressed for pressed, released in self._presses)
        presses, waited = len(self._presses), at - self._started_at
        self._device_id = None
        self._presses = []
        window_s = window_from_gaps(gaps)
        if window_s is None:
            said = f"{presses} press(es) in {waited:.0f} s is not enough to measure a pace; the window is unchanged"
            return CalibrationResult(window_s=None, hold_s=None, said=said)
        # The gap that COUNTED, not the longest of them: a window_s that is not None means the
        # rule found its samples, and a pause between two bursts is not one of them.
        counted = usable_gaps(gaps)
        said = f"{presses} presses, longest counted gap {max(counted):.3f} s: the window becomes {window_s:.1f} s"
        hold_s = hold_from_durations(durations)
        said += "; the hold is unchanged" if hold_s is None else f"; the hold becomes {hold_s:.1f} s"
        return CalibrationResult(window_s=window_s, hold_s=hold_s, said=said)
