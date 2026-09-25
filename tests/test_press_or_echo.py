"""The press-versus-echo rule, replayed frame by frame over the run that found the defect.

On 2026-09-07 the service held its first zone in the flat and answered its own station change with
another one, 28 times in 45 seconds, because a box reports what it is playing in exactly the frame
a pressed preset produces. Everything below is driven from
``fixtures/live-run-frames.json`` - the 151 frames the four boxes actually sent, through the real
``observer.parse_frame`` and the real ``Presses`` - because a rule about which bytes mean what
proved against invented bytes is proved against the wrong ones.

**What the seven human events were is not derived here.** They are the seven moments somebody in
the flat did something, written down while it was happening
(``docs/measurements/2026-09-07-first-live-run.md``). A test that recovered them with the rule
under test could only report that the rule ran.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from soundtouch_zonemaster.adapters.soundtouch.observer import parse_frame
from soundtouch_zonemaster.domain.enums import FrameKind, SourceName
from soundtouch_zonemaster.domain.membership import Membership
from soundtouch_zonemaster.domain.presses import (
    CONFIRM_BACK_WINDOW_S,
    CONFIRM_WINDOW_S,
    HOLD_CEILING_S,
    HOLD_WHEN_UNSEEN_S,
    AskedAtTheFrame,
    Press,
    Presses,
)

AWAKE = AskedAtTheFrame(asleep=False, may_choose_the_channel=True)
"""What the house answered about a box that was playing: the usual case, and not what is under test here."""

FIXTURE = Path(__file__).parent / "fixtures" / "live-run-frames.json"

ROOM1, ROOM4 = "192.168.0.31", "192.168.0.32"

WHAT_A_PERSON_DID: tuple[tuple[float, str, int, str], ...] = (
    (1788800840.4354403, ROOM1, 1, "19:07:20 POWER on, before the run"),
    (1788800845.709476, ROOM4, 2, "19:07:25 POWER on, before the run"),
    (1788800998.9585383, ROOM1, 1, "19:09:58 POWER on, the run's first box"),
    (1788801004.648861, ROOM4, 2, "19:10:04 POWER on, the run's second box"),
    (1788801211.2694068, ROOM1, 2, "19:13:31 POWER on, after the loop was stopped"),
    (1788801221.2434554, ROOM1, 2, "19:13:41 preset 2 on the REMOTE, already playing preset 2"),
    (1788801496.9233656, ROOM1, 3, "19:18:16 preset 3 at the BOX'S OWN buttons, from standby"),
)
"""Every human touch of the run, in the order it happened, from the log kept at the time.

The last two are the hard ones and are why the run measured both. 19:13:41 is a press of the
preset that was ALREADY playing, so its frame is byte for byte an echo; 19:18:16 was made at the
speaker rather than with the remote, which a rule measured only on the remote would have ignored
for every press made in the room.
"""


def frames_of(fixture: Path) -> list[tuple[float, str, str]]:
    """One run, as (arrival, speaker, frame) in the order the observer read them."""
    recorded = json.loads(fixture.read_text())["frames"]
    return [(f["received_at"], f["speaker"], f["frame"]) for f in recorded]


def frames() -> list[tuple[float, str, str]]:
    """The first run, which is the one most of this file is written about."""
    return frames_of(FIXTURE)


def replay_run(
    recorded: list[tuple[float, str, str]],
    *,
    window_s: float = CONFIRM_WINDOW_S,
    back_window_s: float = CONFIRM_BACK_WINDOW_S,
) -> list[tuple[str, Press]]:
    """A whole run through the real parser and the real rule; every press it reported.

    Neither frame decides anything on its own any more. Both sides are collected and the pairing
    happens once the box has said everything it is going to say, which is what a burst from a
    slave forced: it arrives as a block of selections and a block of confirmations, so a rule that
    answers at the frame has to displace three of every four presses.

    ``due`` is drained on BOTH sides of recording the frame, and the order is the point. Drained
    first, a group whose deadline has passed is reported before the new frame can join it; drained
    afterwards, a group the new frame completed is reported at once, which is what keeps an
    ordinary press exactly as quick as it was.
    """
    presses = Presses(window_s=window_s, back_window_s=back_window_s)
    said: list[tuple[str, Press]] = []
    for at, speaker, text in recorded:
        said.extend(presses.due(at=at))
        event = parse_frame(speaker, text, at)
        if event.preset_id is not None:
            presses.selection(speaker, event.preset_id, at=event.received_at, asked=AWAKE)
        elif event.kind == FrameKind.USER_ACTIVITY_UPDATE:
            presses.user_activity(speaker, at=event.received_at)
        said.extend(presses.due(at=at))
    said.extend(presses.due(at=math.inf))
    return said


def replay(*, window_s: float = CONFIRM_WINDOW_S) -> list[tuple[str, Press]]:
    """The first run, replayed."""
    return replay_run(frames(), window_s=window_s)


def selections(recorded: list[tuple[float, str, str]] | None = None) -> list[tuple[float, str, int]]:
    """Every frame in a run that names a preset, human or not."""
    found: list[tuple[float, str, int]] = []
    for at, speaker, text in recorded if recorded is not None else frames():
        event = parse_frame(speaker, text, at)
        if event.preset_id is not None:
            found.append((at, speaker, event.preset_id))
    return found


def test_the_run_is_what_it_is_said_to_be() -> None:
    """The fixture holds the defect: 47 selections, of which only seven were anybody's doing."""
    assert len(frames()) == 151, "the frames of the run, minus the four that carry listening history"
    assert len(selections()) == 47
    assert len(WHAT_A_PERSON_DID) == 7
    assert sum(1 for _, _, text in frames() if FrameKind.USER_ACTIVITY_UPDATE in text) == 17


def test_the_frame_alone_cannot_say_who_caused_it() -> None:
    """The press at 19:13:41 and the echoes around it are the SAME BYTES.

    This is the reason the rule cannot read the frame harder - no location, preset id or item name
    tells the two apart, because a preset press and our announcing that preset are the same event
    as far as the box is concerned.
    """
    by_time = {at: text for at, speaker, text in frames() if speaker == ROOM1}
    press = by_time[1788801221.2434554]
    echo = by_time[1788801044.7991238]
    assert press == echo
    assert "192.168.0.190:8000" in press, "and the echo names OUR url, so that cannot separate them either"


def test_every_press_a_person_made_is_read_as_one() -> None:
    """All seven, at the moment of the selection and with the number that was pressed."""
    said = replay()
    assert [(speaker, press.preset_id, press.named_at) for speaker, press in said] == [
        (speaker, preset, at) for at, speaker, preset, _ in WHAT_A_PERSON_DID
    ]


def test_nothing_the_master_caused_is_read_as_a_press() -> None:
    """The forty echoes, which is the defect: not one of them reaches the dialler."""
    reported = {press.named_at for _, press in replay()}
    human = {at for at, _, _, _ in WHAT_A_PERSON_DID}
    echoes = [at for at, _, _ in selections() if at not in human]
    assert len(echoes) == 40
    assert not [at for at in echoes if at in reported]


def test_a_press_is_timed_at_the_press_and_not_at_its_confirmation() -> None:
    """The dialling window is armed from this, so it has to be the moment the key went down."""
    at_the_box = [press for _, press in replay() if press.preset_id == 3]
    assert len(at_the_box) == 1
    assert at_the_box[0].named_at == 1788801496.9233656, "the selection, 42 ms before the box confirmed it"


@pytest.mark.parametrize("window_s", [0.2, CONFIRM_WINDOW_S, 10.0, 60.0])
def test_the_forward_window_is_a_bound_and_not_a_knob(window_s: float) -> None:
    """Every forward window from 1.5 s to a minute reads all three runs identically.

    Since a press identifies itself as a whole KEY ACTION - two touches with a hold between them,
    next to the selection - rather than as a lone touch, this window decides nothing about a real
    press at all: the largest gap one has ever shown in this direction is 64 ms, and an echo has no
    key action anywhere near it for a wider window to reach. What the number governs is how long a
    selection nobody confirmed keeps its box waiting.

    Only the two runs of real human presses are replayed here. The third was DRIVEN through a
    box's own /key endpoint and its slave half carries no human touch at all, which the tests at
    the end of this file measure.
    """
    for recorded, expected in (
        (frames(), WHAT_A_PERSON_DID),
        (frames_2(), WHAT_A_PERSON_DID_IN_THE_SECOND_RUN),
    ):
        said = replay_run(recorded, window_s=window_s)
        assert [(s, p.preset_id, p.named_at) for s, p in said] == [(sp, pr, at) for at, sp, pr, _ in expected]


def test_a_selection_nobody_confirmed_goes_stale_rather_than_waiting_for_ever() -> None:
    """What the window is actually for, and the run contains no case of it.

    A box may name a preset and then say nothing else for hours - the last echo of the loop did,
    for two and a half minutes. The window is what stops the next person who touches that box from
    confirming it. The boundary is inclusive: a confirmation exactly one window behind still
    belongs to its selection.
    """
    presses = Presses()
    presses.selection("box", 2, at=100.0, asked=AWAKE)
    presses.user_activity("box", at=100.0 + CONFIRM_WINDOW_S)
    assert [press.preset_id for _, press in presses.due(at=math.inf)] == [2]

    presses.selection("box", 2, at=200.0, asked=AWAKE)
    presses.user_activity("box", at=200.0 + CONFIRM_WINDOW_S + 0.001)
    assert presses.due(at=math.inf) == ()


def test_a_selection_is_dropped_at_its_own_deadline_and_not_at_the_next_frame() -> None:
    """When an unconfirmed selection stops being able to become a press, in wall-clock terms.

    The deferred rule has to name that moment itself: nothing displaces a selection any more, so
    without a deadline an echo would sit in the table until the box happened to speak again - two
    and a half minutes, in the first run. The service sleeps to this the way it sleeps to the
    dialler's and the gesture's.
    """
    presses = Presses()
    assert presses.deadline() is None, "nothing pending, so the service sleeps on the event instead"
    presses.selection("box", 2, at=100.0, asked=AWAKE)
    assert presses.deadline() == 100.0 + CONFIRM_WINDOW_S
    assert presses.due(at=100.0 + CONFIRM_WINDOW_S - 0.001) == (), "a touch could still arrive"
    assert presses.due(at=100.0 + CONFIRM_WINDOW_S) == (), "and at the deadline it is dropped, not reported"
    assert presses.deadline() is None, "the table is empty again"


def test_a_press_is_reported_the_moment_nothing_is_left_over() -> None:
    """Why a press costs no more than the key being held, although the rule defers.

    A whole key action is a selection and TWO touches. Once all three have arrived nothing a later
    frame could change remains, so the press is reported at the last of them rather than at a
    deadline - which is the moment the key came back up, and the moment the next key's window is
    armed at anyway.
    """
    presses = Presses()
    presses.selection("box", 4, at=100.0, asked=AWAKE)
    assert presses.due(at=100.0) == (), "the selection alone says nothing about who caused it"
    presses.user_activity("box", at=100.04)
    assert presses.due(at=100.04) == (), "and the key is still down"

    presses.user_activity("box", at=100.34)
    ((_device, press),) = presses.due(at=100.34)
    assert (press.preset_id, press.pressed_at, press.released_at) == (4, 100.04, 100.34)


def test_a_confirmation_that_belongs_to_one_selection_cannot_confirm_a_second() -> None:
    """What keeps a mistake to one wrong station change instead of a run of them."""
    presses = Presses()
    presses.selection("box", 4, at=100.0, asked=AWAKE)
    presses.selection("box", 5, at=100.02, asked=AWAKE)
    presses.user_activity("box", at=100.05)
    assert [press.preset_id for _, press in presses.due(at=math.inf)] == [5], "the nearer of the two, and only it"


def test_an_echo_just_in_front_of_a_press_does_not_eat_its_confirmation() -> None:
    """Why the pairing is by DISTANCE and not oldest-first, on the shape that rules oldest-first out.

    An echo arrives, and a fraction of a second later somebody presses a key. Handing the touch to
    the OLDEST waiting selection gives it to the echo, so the house changes to the echoed channel
    and the press is lost. The nearest selection is the pressed one, by a factor of ten.
    """
    presses = Presses()
    presses.selection("box", 2, at=100.0, asked=AWAKE)
    presses.selection("box", 5, at=100.5, asked=AWAKE)
    presses.user_activity("box", at=100.55)
    assert [press.preset_id for _, press in presses.due(at=math.inf)] == [5]


def test_an_echo_a_second_behind_a_press_does_not_eat_its_confirmation() -> None:
    """The same argument from the other side, which is what rules newest-first out.

    A slave confirms a press BEFORE it names the preset, and our own answer to that press comes
    back about a second later looking exactly like another selection. Handing the touch to the
    NEWEST selection gives it to that echo. Again the nearest is the real one.
    """
    presses = Presses()
    presses.user_activity("box", at=100.0)
    presses.selection("box", 4, at=100.05, asked=AWAKE)
    presses.selection("box", 4, at=101.0, asked=AWAKE)
    assert [press.named_at for _, press in presses.due(at=math.inf)] == [100.05]


def test_a_touch_nobody_selected_anything_near_is_dropped() -> None:
    """The surplus on the other side, which the third run is full of.

    Our own volume fade makes a box report that a person touched it (OPEN-WORK rank 185), so a
    touch with no selection near it is not evidence of anything and is discarded rather than kept
    for whatever the box says next.
    """
    presses = Presses()
    presses.user_activity("box", at=100.0)
    assert presses.deadline() == 100.0 + HOLD_CEILING_S, "long enough to have been a press, not just a confirmation"
    assert presses.due(at=math.inf) == ()


def test_a_selection_is_still_from_a_sleeping_box_when_the_confirmation_arrives() -> None:
    """The wake rule rests on this ordering, so the run is asked whether it holds.

    A woken box names its preset BEFORE it says it has left standby, so the service can still tell
    a wake from a press on a box that is already playing at the moment it has to decide. Were the
    two the other way round, every wake would read as an ordinary press.

    The rule answers LATER than the frame now, which is why the answer is asked at the frame and
    CARRIED: asking again at the moment the pair completes would ask a house that has since heard
    the box leave standby, and every wake would read as an ordinary press after all. What is
    asserted below is the carried answer, not a fresh one.
    """
    policy = Membership(master_device_id="5EB0CE000001", now=lambda: 1788801500.0)
    presses = Presses()
    reported: list[tuple[float, bool]] = []
    for at, speaker, text in frames():
        reported.extend((press.named_at, press.asked.asleep) for _, press in presses.due(at=at))
        event = parse_frame(speaker, text, at)
        if event.preset_id is not None:
            asked = AskedAtTheFrame(asleep=policy.is_asleep(event.device_id), may_choose_the_channel=True)
            presses.selection(speaker, event.preset_id, at=at, asked=asked)
        elif event.kind == FrameKind.USER_ACTIVITY_UPDATE:
            presses.user_activity(speaker, at=at)
        reported.extend((press.named_at, press.asked.asleep) for _, press in presses.due(at=at))
        policy.observe(event)
    reported.extend((press.named_at, press.asked.asleep) for _, press in presses.due(at=math.inf))

    woke_up = {at for at, was_asleep in reported if was_asleep}
    assert 1788801211.2694068 in woke_up, "19:13:31 POWER on, the box was in standby"
    assert 1788801496.9233656 in woke_up, "19:18:16 preset 3 pressed at a box in standby"
    assert 1788801221.2434554 not in woke_up, "19:13:41 the box was playing, so this is a press"


def test_the_run_records_a_box_going_into_standby_before_each_wake() -> None:
    """The control for the test above: the standby the wake comes out of is in the frames.

    Without it every box would read as awake for the whole run and the assertion above would hold
    for a reason that has nothing to do with the rule.
    """
    standby = [
        at
        for at, speaker, text in frames()
        if speaker == ROOM1 and f'source="{SourceName.STANDBY}"' in text and "nowPlayingUpdated" in text
    ]
    assert standby, "Room1 reported standby at least once"
    assert min(standby) < 1788801211.2694068, "and did so before the POWER-on at 19:13:31"


# --- the two moments of one key action ------------------------------------------------------------


def moments(recorded: list[tuple[float, str, str]]) -> dict[float, tuple[float, float]]:
    """Every press of a run, as the moment the key went down and the moment it came back up."""
    return {press.named_at: (press.pressed_at, press.released_at) for _, press in replay_run(recorded)}


def test_a_preset_press_reports_both_of_its_moments() -> None:
    """A press is two frames, not one, and the window for the next key starts at the SECOND.

    A box says a person touched it TWICE for one preset - once as the key goes down and once as it
    comes back up - and the two are 0.23 to 0.447 s apart over the twenty presses of the first two
    runs. Until 2026-09-21 the rule spent one of them and threw the other away, so the dialling
    window was armed at the press and charged a person for however long their thumb rested on the
    key: a measured 0.645 s between two presses in the flat is only about 0.31 s of idle time, and
    the number split at 0.6 s all the same.
    """
    first = moments(frames())
    pressed_at, released_at = first[1788800840.4354403]
    assert round(released_at - pressed_at, 3) == 0.363, "the key was down for 363 ms"
    assert round(pressed_at - 1788800840.4354403, 3) == 0.004, "and the box named the preset first"


def test_the_two_moments_are_read_from_either_side_of_the_selection() -> None:
    """Which side they arrive on is the box's STATE, and both shapes are in the second run.

    Outside the zone a box names the preset and the two touches follow it. As a SLAVE both touches
    arrive BEFORE the selection, the release 21 to 37 ms in front of it - so a rule that looked
    only forwards would find no release at all in the one room the feature exists for.
    """
    second = moments(frames_2())

    outside = second[1788806143.2642496]
    assert outside[0] > 1788806143.2642496, "the press follows the selection"

    as_a_slave = second[1788806567.8134596]
    assert as_a_slave[1] < 1788806567.8134596, "the release precedes it"
    assert round(as_a_slave[1] - as_a_slave[0], 3) == 0.372


def test_a_press_whose_release_never_arrives_still_gets_one() -> None:
    """The fallback, and it is not an edge case: a press DRIVEN at a box reports one touch only.

    Measured 2026-09-21: every press made by a person on the remote or at the box's own buttons
    reports two touches, and every press driven through the box's own ``/key`` endpoint reports
    one. A digit with no release would otherwise arm no window at all, so the longest hold this
    house has recorded stands in for it - long rather than short, because a window that closes
    early splits a number and one that closes late only delays it.
    """
    presses = Presses()
    presses.selection("box", 4, at=100.0, asked=AWAKE)
    presses.user_activity("box", at=100.05)
    assert presses.due(at=100.05) == (), "it waits to see whether the key comes back up"

    ((_device, press),) = presses.due(at=100.05 + HOLD_CEILING_S)
    assert press.pressed_at == 100.05
    assert press.released_at == 100.05 + HOLD_WHEN_UNSEEN_S


def test_a_box_with_something_undecided_says_so() -> None:
    """What stops a number completing while one of its digits is still being worked out.

    The dialling window is armed at a RELEASE, so a press that has not been paired yet has no
    moment the dialler can use. Rather than re-timing the digits to paper over it, the number is
    simply held while the box still has something pending - the user's rule of 2026-09-21, that
    nothing may be acted on until it is known what was pressed.
    """
    presses = Presses()
    assert not presses.pending("box")
    presses.user_activity("box", at=100.0)
    assert presses.pending("box"), "a bare touch is the first frame of a press by a slave"
    presses.due(at=100.0 + CONFIRM_WINDOW_S + HOLD_CEILING_S)
    assert not presses.pending("box")


# --- the second run: the same rule, from inside the zone ----------------------------------------

FIXTURE_2 = Path(__file__).parent / "fixtures" / "live-run-2-frames.json"

ROOM3, ROOM2 = "192.168.0.33", "192.168.0.34"

PRESSED_AS_A_SLAVE: tuple[float, ...] = (
    1788806567.8134596,
    1788806574.6464121,
    1788806628.7234116,
    1788806632.4332025,
)
"""The four presses the first run could not record, and the reason this fixture exists.

A box only forwards a key while it is a member, and the loop the first run found made the switch
unusable within seconds, so nobody had ever pressed a preset on a box that was a SLAVE and lived to
record it. Three of these were made on the remote and the last at the speaker's own buttons.
"""

WHAT_A_PERSON_DID_IN_THE_SECOND_RUN: tuple[tuple[float, str, int, str], ...] = (
    (1788806132.1635623, ROOM1, 1, "20:35:32 preset 1 at a box in standby, the run's first box"),
    (1788806143.2642496, ROOM4, 2, "20:35:43 preset 2 at a box in standby, the second"),
    (1788806567.8134596, ROOM3, 4, "20:42:47 preset 4 on the REMOTE, the box a slave in the zone"),
    (1788806574.6464121, ROOM3, 4, "20:42:54 preset 4 again, still a slave"),
    (1788806628.7234116, ROOM3, 4, "20:43:48 preset 4 a third time"),
    (1788806632.4332025, ROOM3, 5, "20:43:52 preset 5 at the BOX'S OWN buttons, still a slave"),
    (1788807423.9986613, ROOM3, 1, "20:57:03 the calibration, digit 1 of 6"),
    (1788807424.4527276, ROOM3, 2, "20:57:04 digit 2"),
    (1788807424.917815, ROOM3, 3, "20:57:04 digit 3"),
    (1788807425.3860888, ROOM3, 4, "20:57:05 digit 4"),
    (1788807425.823185, ROOM3, 5, "20:57:05 digit 5"),
    (1788807426.2500818, ROOM3, 6, "20:57:06 digit 6, the one the old rule lost"),
    (1788807627.6027582, ROOM1, 1, "21:00:27 POWER on again after a POWER off"),
    (1788807653.2788427, ROOM2, 2, "21:00:53 POWER on"),
    (1788807809.1506662, ROOM3, 0, "21:03:29 the source key, out of standby onto BLUETOOTH"),
    (1788807825.337592, ROOM3, 0, "21:03:45 the source key again"),
    (1788807831.277869, ROOM3, 0, "21:03:51 and again"),
    (1788807834.8835304, ROOM3, 0, "21:03:54 and again"),
    (1788807837.327987, ROOM3, 1, "21:03:57 the source key back onto the radio"),
)
"""Every human touch of the second run that named a preset, from the log kept while it happened.

Written down from the record rather than recovered by the rule, for the reason the first run's list
is: a list the rule produced could only report that the rule ran. The four with preset 0 are the
source key cycling through BLUETOOTH, which announces itself as a preset like any other selection;
they are presses, and it is the dialler that refuses the digit afterwards.
"""


def frames_2() -> list[tuple[float, str, str]]:
    """The second run, as the observer read it."""
    return frames_of(FIXTURE_2)


def test_the_second_run_is_what_it_is_said_to_be() -> None:
    """The fixture holds what the evening measured, echoes and all."""
    assert len(frames_2()) == 311, "every frame minus the two that carry listening history"
    assert len(selections(frames_2())) == 41
    assert len(WHAT_A_PERSON_DID_IN_THE_SECOND_RUN) == 19
    assert len(PRESSED_AS_A_SLAVE) == 4


def test_a_slave_confirms_a_press_before_it_names_the_preset() -> None:
    """The measurement the rule had to be changed for, asserted on the frames themselves.

    A box outside the zone names its preset and then says a person touched it. A box that is a
    SLAVE does the opposite: the touch arrives first, and nothing follows the selection at all. A
    rule that pairs only forwards therefore hears every room except the ones that are playing.
    """
    for at in PRESSED_AS_A_SLAVE:
        touches = [
            when for when, speaker, text in frames_2() if speaker == ROOM3 and FrameKind.USER_ACTIVITY_UPDATE in text
        ]
        before = [when for when in touches if 0 < at - when <= 0.25]
        after = [when for when in touches if 0 < when - at <= CONFIRM_WINDOW_S]
        assert before, f"the press at {at} was confirmed before it was named"
        assert max(before) >= at - 0.05, "and the confirmation is tens of milliseconds away, not hundreds"
        assert not after, f"nothing follows the selection at {at}, which is why the old rule dropped it"


def test_every_press_a_person_made_in_the_second_run_is_read_as_one() -> None:
    """All nineteen, including the four made from inside the zone and the sixth calibration digit."""
    said = replay_run(frames_2())
    assert [(speaker, press.preset_id, press.named_at) for speaker, press in said] == [
        (speaker, preset, at) for at, speaker, preset, _ in WHAT_A_PERSON_DID_IN_THE_SECOND_RUN
    ]


def test_nothing_the_master_caused_in_the_second_run_is_read_as_a_press() -> None:
    """The twenty-two echoes: seven station changes, each one reported back by every member."""
    reported = {press.named_at for _, press in replay_run(frames_2())}
    human = {at for at, _, _, _ in WHAT_A_PERSON_DID_IN_THE_SECOND_RUN}
    echoes = [at for at, _, _ in selections(frames_2()) if at not in human]
    assert len(echoes) == 22
    assert not [at for at in echoes if at in reported]


def test_the_last_press_of_a_burst_is_not_lost() -> None:
    """What the old rule got wrong in a way that read as working.

    Six presets were pressed in a row during the calibration and the old rule reported five: each
    selection was confirmed by the activity of the NEXT press, so only the last one had nothing
    behind it. A burst of one - an ordinary single press - is the same case, which is why every
    lone press by a slave vanished.
    """
    said = replay_run(frames_2())
    burst = [
        press.preset_id for speaker, press in said if speaker == ROOM3 and 1788807423 < press.named_at < 1788807427
    ]
    assert burst == [1, 2, 3, 4, 5, 6]


@pytest.mark.parametrize("back_window_s", [0.05, CONFIRM_BACK_WINDOW_S, 0.5, 5.0])
def test_the_backward_window_reads_every_real_press_across_its_measured_range(back_window_s: float) -> None:
    """Where the number for pairing backwards comes from: a range, not a single reading.

    The two runs of real human presses - 88 selections, 26 of them somebody's doing - classify
    identically anywhere from 40 ms to five seconds, for the same reason the forward one does: what
    the rule looks for is a whole key action, and an echo has none. The chosen value is about three
    times the largest real gap measured in this direction, 37 ms, and it is kept small because the
    frame it must not reach is our own volume fade (OPEN-WORK rank 185).
    """
    for recorded, expected in (
        (frames(), WHAT_A_PERSON_DID),
        (frames_2(), WHAT_A_PERSON_DID_IN_THE_SECOND_RUN),
    ):
        said = replay_run(recorded, back_window_s=back_window_s)
        assert [(s, p.preset_id, p.named_at) for s, p in said] == [(sp, pr, at) for at, sp, pr, _ in expected]


def test_neither_window_can_let_the_loop_back_in_now_that_a_press_is_a_whole_key_action() -> None:
    """What used to be a ceiling, and why there is no longer one to measure.

    Until 2026-09-21 a touch was spent singly, so a release left lying about could be adopted by
    our own echo arriving 0.7 to 1.4 s later and read as a press of the channel already playing -
    the first step of the loop that ran the flat into 28 station changes in 45 seconds. Widening
    the backward window to 1.9 s used to produce exactly that, and at 5 s both runs did.

    A press now claims its release in the same step that claims its confirmation, so there is
    nothing loose for a late echo to take, and neither run breaks however wide either window is
    opened. This test is the control for that claim: it must fail the day a touch is left loose
    again.
    """
    for wide in (1.9, 5.0, 60.0):
        assert len(replay_run(frames(), back_window_s=wide)) == len(WHAT_A_PERSON_DID), wide
        assert len(replay_run(frames_2(), back_window_s=wide)) == len(WHAT_A_PERSON_DID_IN_THE_SECOND_RUN), wide
        assert len(replay_run(frames(), window_s=wide)) == len(WHAT_A_PERSON_DID), wide
        assert len(replay_run(frames_2(), window_s=wide)) == len(WHAT_A_PERSON_DID_IN_THE_SECOND_RUN), wide


# ------------------------------------------------------------------------------------------------
# The third run: eight presses driven at one box, four of them while it was a zone slave
# ------------------------------------------------------------------------------------------------

FIXTURE_3 = Path(__file__).parent / "fixtures" / "burst-as-a-slave-frames.json"

ROOM1_DRIVEN: tuple[float, ...] = (
    1789953070.4655538,
    1789953070.9506824,
    1789953071.3792074,
    1789953071.8639748,
    1789953076.0099535,
    1789953076.4850292,
    1789953076.955417,
    1789953077.5605247,
)
"""The eight presses of PRESET_2 that were driven at Room1, as the box named them.

Ground truth rather than a reading of the frames: they were sent through the box's own ``/key``
endpoint, four then four, and every press and release was answered HTTP 200. That is what makes
this run worth keeping - the number of presses is KNOWN, so a rule that reports fewer is wrong by
arithmetic rather than by interpretation.

The first four were made while the box was outside the zone; between the bursts the service took
it into the zone and faded its volume up, so the last four were made by a SLAVE.
"""


def frames_3() -> list[tuple[float, str, str]]:
    """The third run, as the observer read it."""
    return frames_of(FIXTURE_3)


def touches_of(recorded: list[tuple[float, str, str]]) -> list[float]:
    """Every moment a box in a run said that a person touched it."""
    return [at for at, _speaker, text in recorded if FrameKind.USER_ACTIVITY_UPDATE in text]


def test_the_third_run_is_what_it_is_said_to_be() -> None:
    """The fixture holds what the night measured, the two selections nobody pressed included."""
    assert len(frames_3()) == 37
    assert len(selections(frames_3())) == 10, "ten selections for eight presses"
    assert [at for at, _, _ in selections(frames_3())][:8] == list(ROOM1_DRIVEN)


def test_a_press_driven_at_a_box_reports_one_touch_outside_the_zone_and_none_inside_it() -> None:
    """What this recording actually proves, which is not what it was taken to prove.

    Every press here was DRIVEN through the box's own ``/key`` endpoint rather than made by a
    person, and the two halves of the run answer differently. Outside the zone each press reports
    exactly one touch, 18 to 49 ms behind its selection - one, where a person's press reports two.
    Once the box is a zone SLAVE it reports NONE: the first of those four presses has no touch
    within 0.6 s in either direction and the nearest is more than a second away.

    So the four presses made as a slave carry no evidence that a person caused them, and the rule
    is right to be unable to tell them from an echo. Measured 2026-09-21 while re-deriving the
    press rule; until then they were read as presses by pairing each with a touch our OWN volume
    fade had caused, which is what the next test is about.
    """
    touches = touches_of(frames_3())
    outside = ROOM1_DRIVEN[:4]
    for at in outside:
        behind = [round(touch - at, 3) for touch in touches if 0.0 <= touch - at <= 0.1]
        assert len(behind) == 1, f"exactly one touch behind the press at {at}, not the two a person makes"
        assert 0.018 <= behind[0] <= 0.049

    span = [touch for touch in touches if outside[0] - 0.5 <= touch <= outside[-1] + 1.0]
    assert len(span) == len(outside), "four presses and four touches, so none of them has a release"

    as_a_slave = ROOM1_DRIVEN[4]
    assert not [touch for touch in touches if abs(touch - as_a_slave) <= 0.6], (
        "and the first press made as a slave reports nothing at all"
    )


def test_the_touches_in_the_slave_half_are_our_own_volume_fade() -> None:
    """Named rather than inferred: they arrive beside the volume changes WE sent.

    Eight touches arrive in that span and seven ``volumeUpdated`` frames arrive among them, which
    is the box answering the fade the service runs when it takes a box into the zone
    (OPEN-WORK rank 185). Nothing else is happening at the speaker.
    """
    recorded = frames_3()
    span = (ROOM1_DRIVEN[4] - 0.6, ROOM1_DRIVEN[-1] + 1.5)
    touches = [at for at in touches_of(recorded) if span[0] <= at <= span[1]]
    volume = [at for at, _speaker, text in recorded if "volumeUpdated" in text and span[0] <= at <= span[1]]
    assert len(touches) == 8
    assert len(volume) == 6
    assert min(volume) < min(touches) < max(touches) < max(volume), "the touches sit inside the fade"


def test_a_reading_built_on_that_fade_is_not_stable_and_so_is_not_evidence() -> None:
    """The control that tells an artefact from a measurement, and it is the reason for both.

    A real press is read identically whatever the windows are - the two runs of human presses hold
    from 0.2 s to a minute forwards and 40 ms to five seconds backwards, because what the rule
    looks for is a whole key action and the windows cannot reach one that is not there. The slave
    half of this run is the opposite: how many of its four selections come out as presses depends
    on the window, because nothing there is a confirmation and the answer is decided by which of
    our own fade touches happens to fall inside.

    An assertion that this run yields eight presses would therefore be pinning an artefact, and
    widening a window until it does - which is what the constants were drifting towards - loosens
    the echo defence to accommodate a recording in which nobody pressed anything.
    """
    as_a_slave = list(ROOM1_DRIVEN[4:])
    readings = {
        window_s: tuple(
            press.named_at for _s, press in replay_run(frames_3(), window_s=window_s) if press.named_at in as_a_slave
        )
        for window_s in (0.3, 1.0, 2.0, 2.5)
    }
    assert len(set(readings.values())) > 1, f"it should move with the window, and it does: {readings}"
    assert readings[2.5] == tuple(as_a_slave), "wide enough, every one of them is adopted by the fade"


def test_every_press_driven_outside_the_zone_is_read_as_one() -> None:
    """The half of this run that IS evidence: four driven presses, four read, no more.

    The count is what makes it worth keeping - they were sent through the box's own ``/key``
    endpoint and every press and release was answered HTTP 200, so a rule that reports three or
    five is wrong by arithmetic rather than by interpretation.
    """
    said = replay_run(frames_3(), window_s=1.0)
    assert [press.named_at for _s, press in said][:4] == list(ROOM1_DRIVEN[:4])
