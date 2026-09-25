"""One evening's dialling in the flat, replayed digit for digit through the real rules.

Everything here is driven from ``fixtures/house-20260921-dialling-frames.json`` - the 171 frames
Room1 actually sent between 06:07 and 06:18 on 2026-09-21, recorded by
``research/observe_keys.py`` running beside the service while somebody stood at the box and
pressed. It is the first recording of this house made while the window was armed at a key's
RELEASE, and the first of a hand dialling FOUR-DIGIT numbers at a box that was a zone slave
throughout - the case the whole feature is for.

The presses are a person's, not driven through a box's ``/key`` endpoint, and that distinction
matters here: a driven press reports one confirmation outside the zone and none at all once the
box is a slave, so ``burst-as-a-slave-frames.json`` can say nothing about a hand.

**What the hand did is not derived here.** The numbers below are what the service's own journal
recorded at the time, and the holds and gaps are what the box reported; a list this file recovered
with the rules under test could only report that the rules ran.

The recording carries the house as it really is, our own volume fade included, so a rule that only
works on a quiet box fails here.
"""

from __future__ import annotations

import json
from pathlib import Path

from soundtouch_zonemaster.adapters.soundtouch.observer import parse_frame
from soundtouch_zonemaster.domain.dialling import Dialler
from soundtouch_zonemaster.domain.enums import FrameKind
from soundtouch_zonemaster.domain.presses import AskedAtTheFrame, Presses

FIXTURE = Path(__file__).parent / "fixtures" / "house-20260921-dialling-frames.json"

ROOM1 = "192.168.0.31"

AWAKE = AskedAtTheFrame(asleep=False, may_choose_the_channel=True)
"""The box was awake and in the zone throughout, which is the room this feature is for."""

WINDOW_S = 0.6
"""The window the house was carrying that night, calibrated in an earlier run."""

WHAT_THE_HOUSE_DIALLED: tuple[str, ...] = (
    "1",
    "1",
    "1113",
    "1113",
    "111",
    "3",
    "1113",
    "1111",
    "1211",
    "1211",
    "1112",
    "1112",
    "3",
    "3",
)
"""Every number the service completed that evening, from its own journal.

The `111` and the `3` that follow it are ONE four-digit number the person pressed: see
:func:`test_the_only_number_that_broke_is_the_one_pressed_slowly`.
"""

HOW_LONG_THE_KEYS_WERE_HELD: tuple[float, ...] = (0.168, 0.685)
"""The shortest and longest hold of the evening, over the 40 key actions a selection vouches for."""

IDLE_BETWEEN_KEYS: tuple[float, ...] = (0.045, 0.704)
"""The shortest and longest gap from one key coming UP to the next going DOWN, within a number."""


def frames() -> list[tuple[float, str, str]]:
    """The evening, as the observer read it."""
    recorded = json.loads(FIXTURE.read_text())["frames"]
    return [(f["received_at"], f["speaker"], f["frame"]) for f in recorded]


def dialled(*, window_s: float = WINDOW_S) -> list[tuple[float, str]]:
    """Replay the evening through the real rules; every number they complete, and when.

    The two halves are wired exactly as the service wires them: a press is read once the box has
    finished reporting it, the dialling window is armed at the moment the key came back UP, and a
    number may not complete while that box still has a press being worked out.
    """
    presses, dialler = Presses(), Dialler(window_s=window_s)
    completed: list[tuple[float, str]] = []

    def drain(now: float) -> None:
        for device_id, press in presses.due(at=now):
            dialler.digit(device_id, str(press.preset_id), at=press.released_at)
        while (deadline := dialler.deadline(still_pressing=presses.pending)) is not None and deadline <= now:
            for _who, number in dialler.due(at=deadline, still_pressing=presses.pending):
                completed.append((deadline, number))

    recorded = frames()
    for at, speaker, text in recorded:
        drain(at)
        event = parse_frame(speaker, text, at)
        if event.preset_id is not None:
            presses.selection(speaker, event.preset_id, at=at, asked=AWAKE)
        elif event.kind == FrameKind.USER_ACTIVITY_UPDATE:
            presses.user_activity(speaker, at=at)
        drain(at)
    # Well past the last frame, so nothing is left half-read at the end of the recording.
    drain(recorded[-1][0] + 10.0)
    return completed


def key_actions() -> list[tuple[float, float, int]]:
    """Every press of the evening as (key down, key up, preset), from the frames themselves.

    A press is taken only where a SELECTION vouches for it: the touch nearest the selection is the
    key coming up, because the box was a zone slave all evening and a slave reports both touches
    before it names the preset, and the key going down is the touch 0.12 to 1.0 s in front of that.
    Without the selection to anchor it, our own volume fade chains into the answer - it produced
    four "presses" nobody made when this was first read off the touches alone.
    """
    recorded = [(at, text) for at, speaker, text in frames() if speaker == ROOM1]
    touches = [at for at, text in recorded if FrameKind.USER_ACTIVITY_UPDATE in text]
    found: list[tuple[float, float, int]] = []
    for at, text in recorded:
        event = parse_frame(ROOM1, text, at)
        if event.preset_id is None:
            continue
        near = [touch for touch in touches if abs(touch - at) <= 0.30]
        if not near:
            continue
        up = min(near, key=lambda touch: abs(touch - at))
        down = [touch for touch in touches if 0.12 <= up - touch <= 1.0]
        if down:
            found.append((max(down), up, event.preset_id))
    return found


def test_the_recording_is_what_it_is_said_to_be() -> None:
    """The fixture holds one evening at one box, fade and all."""
    recorded = frames()
    assert len(recorded) == 171
    assert {speaker for _at, speaker, _text in recorded} == {ROOM1}
    assert sum(1 for _at, _speaker, text in recorded if "volumeUpdated" in text) == 16, (
        "our own fade is in here, because it is in the house"
    )
    assert len(key_actions()) == 40


def test_the_house_reads_the_evening_exactly_as_it_did_on_the_night() -> None:
    """Every number, in order. This is the regression test for the whole press-to-number path."""
    assert [number for _when, number in dialled()] == list(WHAT_THE_HOUSE_DIALLED)


def test_every_number_completes_within_the_window_of_the_last_key_coming_up() -> None:
    """The rule, end to end: the house acts a window after the key comes UP, not after it goes down.

    Measured on the night at 0.595 to 0.599 s from the last release to the dial. Armed at the press
    instead, the same evening would have charged a person for the 0.168 to 0.685 s their thumb
    rested on each key.
    """
    actions = key_actions()
    for when, _number in dialled():
        released = [up for _down, up, _preset in actions if up <= when]
        assert released, "every number has a key that came up before it"
        assert 0.0 < when - max(released) <= WINDOW_S + 0.02, f"the number at {when} did not follow a release"


def test_the_only_number_that_broke_is_the_one_pressed_slowly() -> None:
    """The one failure of the evening, and it is the window rather than anything being lost.

    The person pressed 1113 four times. Three came out whole; the fourth was pressed deliberately
    slowly, and the gap from the third `1` coming up to the `3` going down was 0.704 s against a
    0.6 s window - so `111` completed on its own, was not a channel and was discarded, and the `3`
    arrived afterwards as a number of its own and moved the house to the radio.

    Nothing was lost: the box reported all four presses and the service read all four. This test
    pins the defect that remains, and it is the one the channel list can answer - `1113` can only
    be one channel, so it could act on its last key with no window at all, and `111` is no channel
    at all, so it could wait as long as it likes.
    """
    numbers = [number for _when, number in dialled()]
    assert numbers.count("1113") == 3, "three of the four came out whole"
    assert "111" in numbers, "and the fifth split"
    assert numbers[numbers.index("111") + 1] == "3", "its last digit arriving as a number of its own"

    actions = key_actions()
    gaps = [round(actions[i + 1][0] - actions[i][1], 3) for i in range(len(actions) - 1)]
    inside_a_number = [gap for gap in gaps if gap <= 2.0]
    assert max(inside_a_number) == IDLE_BETWEEN_KEYS[1], "0.704 s, the only gap of the evening past the window"
    assert sum(1 for gap in inside_a_number if gap > WINDOW_S) == 1, "the only one, out of 27"


def test_a_window_wide_enough_for_that_hand_reads_the_whole_evening() -> None:
    """What re-calibrating at the box would buy, measured rather than guessed.

    The largest idle gap of the evening is 0.704 s, so a window above it reads every number whole -
    including the slow one. It is not free: every number then waits that much longer before the
    house does anything, which is why it is a question for the user rather than a default.
    """
    numbers = [number for _when, number in dialled(window_s=0.9)]
    assert numbers.count("1113") == 4, "all four, the slow one included"
    assert "111" not in numbers


def test_the_holds_and_the_gaps_are_the_two_different_things_the_rule_needs() -> None:
    """Why the window cannot be armed at the press, in one assertion over real presses.

    The hold and the gap after it are independent: this evening held keys for 0.153 to 0.685 s and
    waited 0.045 to 0.704 s between them. A window armed at the press has to cover the sum, and the
    sum of the two largest is more than twice the window that the gaps alone need.
    """
    actions = key_actions()
    holds = sorted(round(up - down, 3) for down, up, _preset in actions)
    assert (holds[0], holds[-1]) == HOW_LONG_THE_KEYS_WERE_HELD

    gaps = [round(actions[i + 1][0] - actions[i][1], 3) for i in range(len(actions) - 1)]
    inside_a_number = sorted(gap for gap in gaps if gap <= 2.0)
    assert (inside_a_number[0], inside_a_number[-1]) == IDLE_BETWEEN_KEYS
    assert holds[-1] + inside_a_number[-1] > 2 * WINDOW_S, "which is what arming at the press would have to cover"
