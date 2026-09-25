"""Measuring the person who dials, rather than asking somebody to type a number.

Three things are asserted here that the design could not settle and the M0 run could:

The statistic reproduces the number the house already carries. The eleven gaps measured on
2026-09-06 have a largest of 398 ms, and the rule returns exactly the 500 ms floor that was
chosen by hand from those same eleven. A rule that returned anything else would be claiming the
floor was wrong.

The gesture that starts a calibration cannot be pressed by accident. Four presses of one key are
not it, four alternating presses spread over more than the gesture's window are not it, and the
window is the gesture's OWN - not the dialling window, because a person who needs a calibration
needs it precisely because that window is too short for them.

Nothing here reads a clock. Every call takes the moment it happened, which is what lets the
boundary be asserted exactly rather than waited out.
"""

from __future__ import annotations

import pytest

from soundtouch_zonemaster.domain.calibration import (
    CAP_S,
    GESTURE_WINDOW_S,
    QUIET_S,
    Calibration,
    Gesture,
    hold_from_durations,
    margin_for,
    window_from_gaps,
)
from soundtouch_zonemaster.domain.dialling import WINDOW_CEILING_S, WINDOW_FLOOR_S
from soundtouch_zonemaster.domain.enums import KeyName
from soundtouch_zonemaster.domain.longpress import HOLD_THRESHOLD_CEILING_S, HOLD_THRESHOLD_FLOOR_S

MEASURED = (0.214, 0.343, 0.398, 0.320, 0.354, 0.348, 0.390, 0.211, 0.216, 0.229, 0.235)
"""The eleven gaps of docs/measurements/2026-09-06-key-family.md, question 4."""


class TestTheStatistic:
    def test_the_eleven_measured_gaps_give_the_window_the_house_already_carries(self) -> None:
        """398 ms is the largest of them, and the rule returns the floor picked by hand from them."""
        assert window_from_gaps(MEASURED) == pytest.approx(WINDOW_FLOOR_S)

    def test_the_margin_is_never_less_than_a_tenth_of_a_second(self) -> None:
        assert margin_for(0.2) == pytest.approx(0.1)

    def test_the_margin_grows_with_a_hand_that_presses_slowly(self) -> None:
        """A slower hand scatters more in absolute terms, so a fixed 100 ms would shrink with it."""
        assert margin_for(0.9) == pytest.approx(0.225)

    def test_a_slow_hand_gets_a_window_above_the_longest_gap_it_produced(self) -> None:
        assert window_from_gaps((0.7, 0.9, 0.8)) == pytest.approx(1.2)

    def test_a_hand_quicker_than_the_floor_never_talks_the_window_below_it(self) -> None:
        """A person quicker than the floor loses nothing by it: the window only has to OUTLAST
        their gap, and the same hand is slower when it is dark, cold or holding something."""
        assert window_from_gaps((0.1, 0.12, 0.11)) == pytest.approx(WINDOW_FLOOR_S)

    def test_a_hand_slower_than_the_ceiling_is_capped_at_it(self) -> None:
        assert window_from_gaps((1.9, 1.95, 1.8)) == pytest.approx(WINDOW_CEILING_S)

    def test_a_pause_between_two_bursts_is_not_a_sample(self) -> None:
        """Pressing in bursts is what a person does, and the pause between them is not a gap."""
        assert window_from_gaps((0.3, 0.32, 9.0, 0.31)) == pytest.approx(WINDOW_FLOOR_S)

    def test_fewer_than_three_usable_gaps_says_nothing_rather_than_guessing(self) -> None:
        assert window_from_gaps((0.3, 0.32)) is None


KEY_DOWN_S = (0.168, 0.23, 0.29, 0.364, 0.447, 0.685)
"""How long this house's hand held a key down on ordinary taps, 2026-09-20 and 2026-09-21.

The longest, 685 ms, is above the 0.6 s the dialling window was calibrated to - which is the whole
of OPEN-WORK rank 188: with one number for both, a slow tap was already read as a hold."""


class TestTheHoldThreshold:
    """The second number a calibration yields: how long a key must stay down to be HELD (user,
    2026-09-24). It is measured on the same presses as the window, so a slower hand still gets both
    longer from one measurement - but from how long each key was DOWN, which is the question a hold
    asks, rather than from the pause between two keys."""

    def test_every_tap_this_house_has_made_gives_the_floor(self) -> None:
        """685 ms plus its margin is 0.86 s, and the floor the user chose lies above it."""
        assert hold_from_durations(KEY_DOWN_S) == pytest.approx(HOLD_THRESHOLD_FLOOR_S)

    def test_the_floor_is_the_users_second(self) -> None:
        assert HOLD_THRESHOLD_FLOOR_S == 1.0

    def test_a_hand_that_rests_on_its_keys_gets_a_threshold_above_its_longest_tap(self) -> None:
        """0.95 s plus a quarter of it is 1.19, rounded UP to a tenth like the window is."""
        assert hold_from_durations((0.8, 0.95, 0.9)) == pytest.approx(1.2)

    def test_it_is_capped_so_a_hold_never_takes_longer_than_the_ceiling(self) -> None:
        assert hold_from_durations((1.7, 1.8, 1.75)) == pytest.approx(HOLD_THRESHOLD_CEILING_S)

    def test_a_key_held_past_the_ceiling_is_a_hold_rather_than_a_sample(self) -> None:
        """Somebody leaning on one key during the measurement did not tap it."""
        assert hold_from_durations((0.3, 0.35, 4.4, 0.32)) == pytest.approx(HOLD_THRESHOLD_FLOOR_S)

    def test_fewer_than_three_usable_durations_says_nothing_rather_than_guessing(self) -> None:
        assert hold_from_durations((0.3, 0.32)) is None

    def test_a_calibration_reports_both_numbers_from_the_same_presses(self) -> None:
        """Held 0.9 s with 0.3 s of thinking between them: the pause gives the window's floor,
        and the keys' own length gives a hold threshold above it - two answers, one measurement."""
        calibration = Calibration()
        calibration.begin("box", at=100.0)
        for n in range(4):
            calibration.press("box", pressed_at=100.0 + n * 1.2, released_at=100.9 + n * 1.2)
        result = calibration.finish(at=104.5 + QUIET_S)

        assert result.window_s == pytest.approx(WINDOW_FLOOR_S)
        assert result.hold_s == pytest.approx(1.2)
        assert "the hold becomes 1.2 s" in result.said, "the log names the second number too"

    def test_a_calibration_that_measured_nothing_changes_neither_number(self) -> None:
        calibration = Calibration()
        calibration.begin("box", at=100.0)
        calibration.press("box", pressed_at=100.5, released_at=100.8)
        result = calibration.finish(at=200.0)
        assert result.window_s is None
        assert result.hold_s is None


class TestTheGesture:
    def test_next_previous_next_previous_inside_the_window_is_the_gesture(self) -> None:
        gesture = Gesture()
        keys = (KeyName.NEXT_TRACK, KeyName.PREV_TRACK, KeyName.NEXT_TRACK, KeyName.PREV_TRACK)
        seen = [gesture.saw("box", key, at=100.0 + n * 0.4) for n, key in enumerate(keys)]
        assert seen == [False, False, False, True]

    def test_four_presses_of_the_same_key_are_not_the_gesture(self) -> None:
        gesture = Gesture()
        seen = [gesture.saw("box", KeyName.NEXT_TRACK, at=100.0 + n * 0.4) for n in range(4)]
        assert seen == [False] * 4

    def test_four_alternating_presses_too_far_apart_are_not_the_gesture(self) -> None:
        gesture = Gesture()
        keys = (KeyName.NEXT_TRACK, KeyName.PREV_TRACK, KeyName.NEXT_TRACK, KeyName.PREV_TRACK)
        spread = GESTURE_WINDOW_S / 2
        assert [gesture.saw("box", key, at=100.0 + n * spread) for n, key in enumerate(keys)] == [False] * 4

    def test_two_boxes_do_not_add_up_to_one_gesture(self) -> None:
        gesture = Gesture()
        gesture.saw("one", KeyName.NEXT_TRACK, at=100.0)
        gesture.saw("two", KeyName.PREV_TRACK, at=100.2)
        gesture.saw("one", KeyName.PREV_TRACK, at=100.4)
        assert gesture.saw("two", KeyName.NEXT_TRACK, at=100.6) is False

    def test_the_gesture_is_consumed_so_eight_presses_are_not_two_overlapping_ones(self) -> None:
        gesture = Gesture()
        keys = (KeyName.NEXT_TRACK, KeyName.PREV_TRACK) * 3
        seen = [gesture.saw("box", key, at=100.0 + n * 0.3) for n, key in enumerate(keys)]
        assert seen == [False, False, False, True, False, False]

    def test_anything_else_pressed_in_between_breaks_the_run(self) -> None:
        """Measured against a real sequence: next, thumbs down, previous, next. Without this the
        previous is read as the second press of a gesture and never steps, which is a key that
        did nothing for a person who pressed it deliberately."""
        gesture = Gesture()
        gesture.saw("box", KeyName.NEXT_TRACK, at=100.0)
        gesture.forget("box")
        assert gesture.saw("box", KeyName.PREV_TRACK, at=100.2) is False
        assert gesture.saw("box", KeyName.NEXT_TRACK, at=100.4) is False
        assert gesture.saw("box", KeyName.PREV_TRACK, at=100.6) is False

    def test_forgetting_a_speaker_that_pressed_nothing_is_not_an_error(self) -> None:
        Gesture().forget("box")


class TestOneCalibration:
    def test_it_takes_no_samples_from_another_speaker(self) -> None:
        """A channel change in the middle of a measurement would confuse both rooms, and the
        measurement is over in seconds."""
        calibration = Calibration()
        calibration.begin("box", at=100.0)
        assert calibration.press("box", pressed_at=100.3, released_at=100.6) is True
        assert calibration.press("other", pressed_at=100.4, released_at=100.7) is False

    def test_a_press_before_anything_began_is_not_a_sample(self) -> None:
        assert Calibration().press("box", pressed_at=100.0, released_at=100.3) is False

    def test_it_ends_when_the_pressing_stops(self) -> None:
        calibration = Calibration()
        calibration.begin("box", at=100.0)
        # A key held 0.3 s with 0.3 s of thinking between one coming up and the next going down.
        for n in range(5):
            calibration.press("box", pressed_at=100.5 + n * 0.6, released_at=100.8 + n * 0.6)
        last = 100.8 + 4 * 0.6
        assert calibration.deadline() == pytest.approx(last + QUIET_S), "the quiet runs from the RELEASE"
        result = calibration.finish(at=last + QUIET_S)
        assert result.window_s == pytest.approx(WINDOW_FLOOR_S)
        assert calibration.is_running() is False

    def test_it_ends_at_the_cap_when_nobody_ever_presses(self) -> None:
        calibration = Calibration()
        calibration.begin("box", at=100.0)
        assert calibration.deadline() == pytest.approx(100.0 + CAP_S)
        assert calibration.finish(at=100.0 + CAP_S).window_s is None

    def test_a_refusal_says_what_it_had_rather_than_only_that_it_refused(self) -> None:
        calibration = Calibration()
        calibration.begin("box", at=100.0)
        calibration.press("box", pressed_at=100.5, released_at=100.8)
        assert "1" in calibration.finish(at=200.0).said

    def test_a_second_gesture_while_one_runs_does_not_start_another(self) -> None:
        calibration = Calibration()
        assert calibration.begin("box", at=100.0) is True
        assert calibration.begin("other", at=100.5) is False

    def test_nothing_is_running_before_a_gesture(self) -> None:
        calibration = Calibration()
        assert calibration.is_running() is False
        assert calibration.deadline() is None

    def test_the_house_is_told_once_at_the_start_and_not_again(self) -> None:
        calibration = Calibration()
        calibration.begin("box", at=100.0)
        assert calibration.needs_announcing() is True
        calibration.announced()
        assert calibration.needs_announcing() is False

    def test_the_success_line_names_the_gap_the_window_was_built_from(self) -> None:
        """A pause is dropped as a sample, so it must not be reported as the gap that decided.

        The line is what somebody reads in the log to understand the number the house now has.
        Naming the pause put two figures in one sentence that cannot both be true of the same
        rule: a longest gap of 9.4 s and a window of 0.5 s.
        """
        calibration = Calibration()
        calibration.begin("box", at=100.0)
        # Two bursts with a pause between them, timed as the rule now reads them: the gap runs
        # from one key coming UP to the next going DOWN, so each press below is held 0.3 s and the
        # figures in the assertions are the idle times 0.300, 0.320, 9.400 and 0.310.
        for pressed_at in (100.0, 100.6, 101.22, 110.92, 111.53):
            calibration.press("box", pressed_at=pressed_at, released_at=pressed_at + 0.3)
        result = calibration.finish(at=111.83 + QUIET_S)

        assert result.window_s == pytest.approx(WINDOW_FLOOR_S)
        assert "9." not in result.said, "the pause between the two bursts decided nothing"
        assert "0.320" in result.said, "the longest gap that COUNTED is what built the window"
