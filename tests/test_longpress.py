"""The one gesture rule, and every way the pair of frames it reads can arrive wrong.

A key released inside the threshold is a TAP; a key still down when the threshold passes is a HOLD, and
it is reported THEN rather than when the finger comes up. The rest of these are ways the pair
arrives broken, which is the proportion the module is written in: the service can start with a key
already held, a frame can be lost, and a box can go off the air mid-press. The clock is passed in
on every call, so each boundary is asserted exactly rather than waited out.

Re-recorded 2026-09-20 when the rule changed from "measure the length at the release" to "decide at
the window". What moved is WHEN a hold is known and which call reports it; the refusals are
unchanged, because a broken pair is broken either way.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from soundtouch_zonemaster.domain.longpress import HOLD_CEILING_S, Hold, LongPresses

BOX, OTHER = "192.168.0.31", "192.168.0.32"

THRESHOLD = 0.6
"""The hold threshold these tests run the rule at: 0.6 s, close above this house's recorded taps.

A tight value on purpose, so the margin tests below pin a narrow case; the shipped default is
``HOLD_THRESHOLD_DEFAULT_S``. It is written here rather than imported because these tests are about
the RULE, and a test that moved with the house's own number would stop asserting the boundary it
exists for.
"""

TAPS_MS = (285, 289, 290, 291, 293, 299, 317, 359, 364, 364, 364, 369, 369, 372, 384, 417, 446, 448)
"""Every press-to-release the service's journal holds, 2026-09-20, in milliseconds.

Eighteen taps by one hand on one remote, 285 to 448 ms. The one deliberate hold in the same log
measured 4422 ms. The gap between 448 and the 600 ms threshold is 152 ms - a quarter - and that is
the margin the whole rule rests on, so it is pinned here rather than left to be rediscovered.
"""


class TestTheNumbers:
    def test_the_ceiling_is_far_above_the_longest_hold_measured(self) -> None:
        """The one deliberate long press measured 4422 ms, so a real press never reaches this."""
        assert HOLD_CEILING_S == 10.0
        assert HOLD_CEILING_S > 4.422

    def test_every_tap_this_house_has_recorded_is_inside_the_threshold(self) -> None:
        """The margin, named. If a tap ever reads as a hold, this is the number to raise."""
        assert max(TAPS_MS) == 448
        assert max(TAPS_MS) / 1000 < THRESHOLD
        assert THRESHOLD - max(TAPS_MS) / 1000 == pytest.approx(0.152)

    def test_a_hold_is_frozen(self) -> None:
        """Like ``presses.Press``: what a speaker did is a record of the past, not a variable."""
        hold = Hold(key="NEXT_TRACK", seconds=0.3, long=False)
        with pytest.raises(FrozenInstanceError):
            hold.seconds = 9.0  # type: ignore[misc]


class TestOneRuleForEveryKey:
    """The gesture rule the whole service reads, decided within the hold threshold of the press.

    The threshold is its own number (user, 2026-09-24), measured by the same calibration as the
    dialling window but on how long each key was down, so a slower hand gets a longer hold.
    """

    def test_a_key_still_down_when_the_threshold_passes_is_due_as_a_hold(self) -> None:
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "NEXT_TRACK", at=100.0)

        due = holds.due(at=100.0 + THRESHOLD)

        assert [(box, hold.key, hold.long) for box, hold in due] == [(BOX, "NEXT_TRACK", True)]
        assert due[0][1].seconds == pytest.approx(THRESHOLD)

    def test_the_threshold_itself_counts_as_held(self) -> None:
        """The boundary is inclusive, and one millisecond under it is not: the pair that
        separates >= from >."""
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "NEXT_TRACK", at=100.0)

        assert holds.due(at=100.0 + THRESHOLD - 0.001) == ()
        assert holds.due(at=100.0 + THRESHOLD) != ()

    def test_a_hold_that_fired_swallows_the_release_that_follows_it(self) -> None:
        """The action happened at the threshold; the release ends a gesture already spent."""
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "NEXT_TRACK", at=100.0)
        holds.due(at=100.0 + THRESHOLD)

        assert holds.released(BOX, "NEXT_TRACK", at=104.422) is None

    def test_a_hold_is_due_once_however_long_the_key_stays_down(self) -> None:
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "NEXT_TRACK", at=100.0)
        holds.due(at=100.0 + THRESHOLD)

        assert holds.due(at=103.0) == (), "a key held five times as long is still one gesture"

    def test_a_release_reports_a_tap_or_nothing_at_all_and_never_a_hold(self) -> None:
        """The 4422 ms press is the case: under the old rule this release is where it was
        recognised, and now the threshold recognises it 3.8 s earlier - so by the time the key
        comes up there is nothing left for the release to say."""
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "NEXT_TRACK", at=100.0)

        assert holds.released(BOX, "NEXT_TRACK", at=104.422) is None

        holds.pressed(BOX, "NEXT_TRACK", at=200.0)
        tap = holds.released(BOX, "NEXT_TRACK", at=200.3)
        assert tap is not None and tap.long is False

    def test_the_longest_tap_this_house_has_recorded_is_still_a_tap(self) -> None:
        """448 ms, the longest of the eighteen pairs in the service's own journal."""
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "NEXT_TRACK", at=100.0)

        assert holds.due(at=100.448) == (), "it was let go before the threshold, so nothing is due"
        tap = holds.released(BOX, "NEXT_TRACK", at=100.448)
        assert tap is not None
        assert (tap.key, tap.long) == ("NEXT_TRACK", False)
        assert tap.seconds == pytest.approx(0.448)

    def test_every_recorded_tap_is_answered_as_one(self) -> None:
        """The whole corpus, not only its longest: each one reports a tap and none is ever due."""
        holds = LongPresses(threshold_s=THRESHOLD)
        for index, milliseconds in enumerate(TAPS_MS):
            at = 100.0 * (index + 1)
            holds.pressed(BOX, "NEXT_TRACK", at=at)
            assert holds.due(at=at + milliseconds / 1000) == (), f"{milliseconds} ms read as a hold"
            hold = holds.released(BOX, "NEXT_TRACK", at=at + milliseconds / 1000)
            assert hold is not None and hold.long is False

    def test_a_longer_threshold_keeps_a_slower_hands_presses_taps(self) -> None:
        """A calibration lengthens the threshold for a slower hand: two seconds under a three
        second threshold is a person still pressing, not a person holding."""
        holds = LongPresses(threshold_s=3.0)
        holds.pressed(BOX, "NEXT_TRACK", at=100.0)

        assert holds.due(at=102.0) == ()


class TestWhetherAKeyIsStillUndecided:
    """Whether a key is down with its gesture not yet decided - what a pending step waits on."""

    def test_a_key_just_pressed_is_undecided(self) -> None:
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "NEXT_TRACK", at=100.0)
        assert holds.undecided(BOX, "NEXT_TRACK") is True

    def test_a_key_let_go_is_decided(self) -> None:
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "NEXT_TRACK", at=100.0)
        holds.released(BOX, "NEXT_TRACK", at=100.3)
        assert holds.undecided(BOX, "NEXT_TRACK") is False

    def test_a_key_whose_hold_fired_is_decided_although_still_down(self) -> None:
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "NEXT_TRACK", at=100.0)
        holds.due(at=100.0 + THRESHOLD)
        assert holds.undecided(BOX, "NEXT_TRACK") is False

    def test_it_is_per_key_and_per_speaker(self) -> None:
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "NEXT_TRACK", at=100.0)
        assert holds.undecided(BOX, "PREV_TRACK") is False
        assert holds.undecided(OTHER, "NEXT_TRACK") is False


class TestTheDeadline:
    def test_nothing_down_has_no_deadline(self) -> None:
        assert LongPresses(threshold_s=THRESHOLD).deadline() is None

    def test_it_is_when_the_earliest_key_still_down_becomes_a_hold(self) -> None:
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "NEXT_TRACK", at=100.0)
        holds.pressed(OTHER, "THUMBS_UP", at=100.2)

        assert holds.deadline() == 100.0 + THRESHOLD, "the earlier press decides it"

    def test_a_key_let_go_before_the_threshold_leaves_no_deadline_behind(self) -> None:
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "NEXT_TRACK", at=100.0)
        holds.released(BOX, "NEXT_TRACK", at=100.3)

        assert holds.deadline() is None

    def test_a_key_whose_hold_has_fired_leaves_no_deadline_behind(self) -> None:
        """Otherwise the loop would wake for it again and again while the finger stayed on it."""
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "NEXT_TRACK", at=100.0)
        holds.due(at=100.0 + THRESHOLD)

        assert holds.deadline() is None


class TestAPairThatCannotBeTrusted:
    def test_a_release_with_no_press_behind_it_answers_nothing(self) -> None:
        """The service can start with a key already held, and a frame can be lost."""
        holds = LongPresses(threshold_s=THRESHOLD)
        assert holds.released(BOX, "NEXT_TRACK", at=100.4) is None

    def test_a_press_is_spent_by_its_release(self) -> None:
        """A second release with no new press is the same case as no press at all: whatever it
        is, it is not the key going up again, and answering the old press twice would fire the
        action twice."""
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "NEXT_TRACK", at=100.0)
        assert holds.released(BOX, "NEXT_TRACK", at=100.3) is not None
        assert holds.released(BOX, "NEXT_TRACK", at=100.4) is None

    def test_a_press_older_than_the_ceiling_answers_nothing(self) -> None:
        """The lost-release case: a release minutes later must not be read as a gesture at all."""
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "NEXT_TRACK", at=100.0)
        assert holds.released(BOX, "NEXT_TRACK", at=100.0 + HOLD_CEILING_S + 0.001) is None

    def test_a_press_refused_by_the_ceiling_is_spent_too(self) -> None:
        """It cannot answer a later release either, so leaving it would only hand a stale moment
        to the next one."""
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "NEXT_TRACK", at=100.0)
        assert holds.released(BOX, "NEXT_TRACK", at=200.0) is None
        assert holds.released(BOX, "NEXT_TRACK", at=200.1) is None

    def test_a_release_before_its_press_answers_nothing(self) -> None:
        """A clock that went backwards, not a gesture of negative length."""
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "NEXT_TRACK", at=100.0)
        assert holds.released(BOX, "NEXT_TRACK", at=99.0) is None

    def test_a_second_press_replaces_the_moment_the_first_recorded(self) -> None:
        """A press with no release between means the first release was lost; the newer press is
        the live one, because timing from the older moment reads a tap as a hold."""
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "NEXT_TRACK", at=100.0)
        holds.pressed(BOX, "NEXT_TRACK", at=100.5)

        assert holds.due(at=100.9) == (), "0.4 s after the live press is not a hold"
        assert holds.deadline() == 100.5 + THRESHOLD

    def test_a_press_whose_release_never_arrives_is_not_kept_for_ever(self) -> None:
        """It fired at the threshold; what is left is a finger nobody can see lifting.

        Dropped once the ceiling passes, so a box that went off the air mid-press does not leave a
        key down in the table for the rest of the run.
        """
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "NEXT_TRACK", at=100.0)
        holds.due(at=100.0 + THRESHOLD)
        holds.due(at=100.0 + HOLD_CEILING_S + 0.1)

        assert holds.released(BOX, "NEXT_TRACK", at=100.0 + HOLD_CEILING_S + 0.2) is None
        assert holds.deadline() is None


class TestKeysAndSpeakersAreIndependent:
    def test_two_keys_on_one_speaker_do_not_touch_each_other(self) -> None:
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "NEXT_TRACK", at=100.0)
        holds.pressed(BOX, "THUMBS_UP", at=100.1)
        holds.released(BOX, "NEXT_TRACK", at=100.3)

        assert holds.deadline() == 100.1 + THRESHOLD, "the thumb is still down"

    def test_two_speakers_do_not_touch_each_other(self) -> None:
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "NEXT_TRACK", at=100.0)
        holds.pressed(OTHER, "NEXT_TRACK", at=100.0)

        assert [box for box, _hold in holds.due(at=100.0 + THRESHOLD)] == [BOX, OTHER]

    def test_two_keys_on_one_box_become_due_together_in_a_stable_order(self) -> None:
        """One hand on two buttons is not one gesture, and the report is stable in key order."""
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "THUMBS_UP", at=100.0)
        holds.pressed(BOX, "NEXT_TRACK", at=100.0)

        assert [hold.key for _box, hold in holds.due(at=100.0 + THRESHOLD)] == ["NEXT_TRACK", "THUMBS_UP"]


class TestForgetting:
    def test_forgetting_a_speaker_drops_every_key_it_holds(self) -> None:
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "NEXT_TRACK", at=100.0)
        holds.pressed(BOX, "THUMBS_UP", at=100.0)

        holds.forget(BOX)

        assert holds.deadline() is None

    def test_forgetting_one_speaker_leaves_another_alone(self) -> None:
        """Clearing the whole table would drop the key somebody in another room is pressing."""
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "NEXT_TRACK", at=100.0)
        holds.pressed(OTHER, "NEXT_TRACK", at=100.0)

        holds.forget(BOX)

        assert [box for box, _hold in holds.due(at=100.0 + THRESHOLD)] == [OTHER]

    def test_forgetting_a_speaker_that_pressed_nothing_is_not_an_error(self) -> None:
        LongPresses(threshold_s=THRESHOLD).forget(BOX)


class TestWhoeverNoticesFirst:
    """The loop and the release can both be the first to see that the threshold has passed.

    The loop wakes on a deadline and the frames arrive when the speaker sends them, so on a busy
    pass the release of a held key can land before the tick that was going to report it. It must
    not be read as a tap: it is a hold nobody has acted on yet, and the gesture is decided by the
    threshold rather than by which of the two got there first.
    """

    def test_a_release_after_the_threshold_is_not_a_tap(self) -> None:
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "THUMBS_DOWN", at=100.0)

        assert holds.released(BOX, "THUMBS_DOWN", at=100.9) is None

    def test_and_the_hold_it_was_still_becomes_due(self) -> None:
        """Left in the table rather than spent, so the one place that acts still acts."""
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "THUMBS_DOWN", at=100.0)
        holds.released(BOX, "THUMBS_DOWN", at=100.9)

        assert [hold.key for _box, hold in holds.due(at=100.95)] == ["THUMBS_DOWN"]

    def test_a_hold_reported_that_way_is_still_reported_once(self) -> None:
        holds = LongPresses(threshold_s=THRESHOLD)
        holds.pressed(BOX, "THUMBS_DOWN", at=100.0)
        holds.released(BOX, "THUMBS_DOWN", at=100.9)
        holds.due(at=100.95)

        assert holds.due(at=101.5) == ()
