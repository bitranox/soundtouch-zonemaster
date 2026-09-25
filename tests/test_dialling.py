"""One digit buffer per speaker, and the one wait time above all of them.

The clock is passed in on every call rather than read, which is what lets the window's boundary be
asserted exactly instead of waited out. Nothing here talks to a speaker, owns a timer, or knows
what a channel is: it decides that a number is COMPLETE and what it reads, and the service decides
what a completed number does.

Two rules in here come from measurements and each has a test that says so:

- Every digit re-arms the window. Four digits 0.9 s apart are ONE number, and the plausible wrong
  implementation - arm once from the first digit - passes a two-digit test and turns those four
  into four separate channel changes.
- A digit is never de-duplicated. On 2026-09-07 five presses of 1, 1, 2, 2, 1 produced five
  frames, so two identical digits inside the window are the number 11 and not the number 1 read
  twice. That is the opposite of the KEY path, where every press arrives twice as press and
  release; getting the two the wrong way round reads every number as doubled.
"""

from __future__ import annotations

import pytest

from soundtouch_zonemaster.domain.dialling import WINDOW_CEILING_S, WINDOW_DEFAULT_S, WINDOW_FLOOR_S, Dialler


def _dialler(window_s: float = 1.0) -> Dialler:
    return Dialler(window_s=window_s)


class TestTheBounds:
    def test_the_floor_is_the_measured_five_hundred_milliseconds(self) -> None:
        """Eleven deliberately fast pairs spanned 211 to 398 ms; at 400 ms none of them splits."""
        assert WINDOW_FLOOR_S == 0.5

    def test_the_default_sits_inside_the_bounds(self) -> None:
        assert WINDOW_FLOOR_S <= WINDOW_DEFAULT_S <= WINDOW_CEILING_S

    def test_the_default_is_the_number_the_house_chose(self) -> None:
        """800 ms, the user's call on 2026-09-07: twice the largest gap M0 measured (398 ms).

        Pinned rather than left to read off the constant, because it is a decision about the house
        and not a value somebody may round while tidying.
        """
        assert WINDOW_DEFAULT_S == 0.8


class TestForgettingWhatWasCollected:
    def test_steps_can_be_forgotten_so_a_gesture_does_not_also_move_the_house(self) -> None:
        """The four presses that start a calibration are steps until they turn out to be a
        gesture. They sum to zero, but only if all four land in one jump - at a leisurely pace the
        window closes between two of them, and then the first would have moved the zone."""
        d = _dialler()
        d.step("box", 1, at=100.0)
        d.step("box", -1, at=100.2)
        d.forget_steps("box")
        assert d.steps_due(at=200.0) == ()
        assert d.deadline() is None

    def test_forgetting_a_speaker_that_pressed_nothing_is_not_an_error(self) -> None:
        d = _dialler()
        d.forget_steps("box")
        assert d.deadline() is None

    def test_forgetting_steps_leaves_the_digits_of_a_number_alone(self) -> None:
        """They are two buffers on purpose: pressing 1 and then next is two different actions."""
        d = _dialler()
        d.digit("box", "1", at=100.0)
        d.step("box", 1, at=100.1)
        d.forget_steps("box")
        assert d.due(at=200.0) == (("box", "1"),)


class TestCompletingANumber:
    def test_two_digits_inside_the_window_are_one_number(self) -> None:
        d = _dialler()
        d.digit("box", "1", at=100.0)
        d.digit("box", "2", at=100.3)
        assert d.due(at=100.4) == ()
        assert d.due(at=101.31) == (("box", "12"),)

    def test_two_digits_outside_the_window_are_two_numbers(self) -> None:
        d = _dialler()
        d.digit("box", "1", at=100.0)
        assert d.due(at=101.01) == (("box", "1"),)
        d.digit("box", "2", at=102.0)
        assert d.due(at=103.01) == (("box", "2"),)

    def test_each_digit_re_arms_the_window(self) -> None:
        """The one that catches arming once from the FIRST digit: that version completes "1" at
        101.0 and turns one four-digit number into four channel changes."""
        d = _dialler()
        for at in (100.0, 100.9, 101.8, 102.7):
            d.digit("box", "1", at=at)
        assert d.due(at=103.6) == ()
        assert d.due(at=103.71) == (("box", "1111"),)

    def test_a_digit_is_never_de_duplicated(self) -> None:
        """Measured 2026-09-07: five presses of 1,1,2,2,1 gave five frames, so 1 then 1 is 11."""
        d = _dialler()
        d.digit("box", "1", at=100.0)
        d.digit("box", "1", at=100.3)
        assert d.due(at=101.31) == (("box", "11"),)

    def test_the_window_boundary_is_inclusive_and_asserted_exactly(self) -> None:
        d = _dialler()
        d.digit("box", "1", at=100.0)
        assert d.due(at=100.999) == ()
        assert d.due(at=101.0) == (("box", "1"),)

    def test_a_completed_number_is_reported_once(self) -> None:
        d = _dialler()
        d.digit("box", "1", at=100.0)
        assert d.due(at=101.01) == (("box", "1"),)
        assert d.due(at=101.02) == ()

    def test_a_digit_no_key_can_press_is_dropped_rather_than_buffered(self) -> None:
        """A buffer holding a 7 could never complete into a number anybody can reach."""
        d = Dialler(window_s=1.0)
        ignored = d.digit("box", "7", at=100.0)
        assert d.deadline() is None
        assert d.due(at=200.0) == ()
        assert ignored is not None
        assert "7" in ignored.said


class TestTwoSpeakersAtOnce:
    def test_they_do_not_touch_each_other(self) -> None:
        d = _dialler()
        d.digit("a", "1", at=100.0)
        d.digit("b", "2", at=100.1)
        assert sorted(d.due(at=101.2)) == [("a", "1"), ("b", "2")]

    def test_one_completing_leaves_the_other_dialling(self) -> None:
        d = _dialler()
        d.digit("a", "1", at=100.0)
        d.digit("b", "2", at=100.8)
        assert d.due(at=101.0) == (("a", "1"),)
        d.digit("b", "3", at=101.1)
        assert d.due(at=102.11) == (("b", "23"),)

    def test_the_report_is_in_a_stable_order(self) -> None:
        """Two speakers completing in one pass must not reorder between runs, or a log reads as
        two different events."""
        d = _dialler()
        d.digit("b", "2", at=100.0)
        d.digit("a", "1", at=100.0)
        assert d.due(at=101.0) == (("a", "1"), ("b", "2"))


class TestTheDeadline:
    def test_nothing_pending_has_no_deadline(self) -> None:
        assert _dialler().deadline() is None

    def test_it_is_the_earliest_pending_completion(self) -> None:
        d = _dialler()
        d.digit("a", "1", at=100.0)
        d.digit("b", "2", at=100.5)
        assert d.deadline() == pytest.approx(101.0)

    def test_a_new_digit_moves_the_deadline_out(self) -> None:
        """The worker sleeps to this, so a digit arriving during that sleep has to move it."""
        d = _dialler()
        d.digit("a", "1", at=100.0)
        assert d.deadline() == pytest.approx(101.0)
        d.digit("a", "2", at=100.4)
        assert d.deadline() == pytest.approx(101.4)

    def test_it_is_gone_again_once_everything_has_completed(self) -> None:
        d = _dialler()
        d.digit("a", "1", at=100.0)
        d.due(at=101.0)
        assert d.deadline() is None


class TestStepping:
    """Next and previous, collected into ONE jump by the same window the digits use.

    A file change is not cheap on this architecture - new source, new t0, a new PLAY to every
    speaker, the placement books reset - so three quick nexts must be one jump of three rather than
    three changes in half a second.

    The dialler counts signed steps and nothing else. WHICH key means plus one, and the fact that
    every key arrives twice as press and release, are the service's business: this module never
    sees a key.
    """

    def test_three_quick_steps_are_one_jump(self) -> None:
        d = _dialler()
        for at in (100.0, 100.3, 100.6):
            d.step("box", 1, at=at)
        assert d.steps_due(at=101.0) == ()
        assert d.steps_due(at=101.61) == (("box", 3),)

    def test_a_step_re_arms_the_window_like_a_digit(self) -> None:
        d = _dialler()
        for at in (100.0, 100.9, 101.8):
            d.step("box", 1, at=at)
        assert d.steps_due(at=102.7) == ()
        assert d.steps_due(at=102.81) == (("box", 3),)

    def test_next_and_previous_cancel_out_and_nothing_is_reported(self) -> None:
        """A jump of zero changes nothing, so there is nothing to report and no station to restart."""
        d = _dialler()
        d.step("box", 1, at=100.0)
        d.step("box", -1, at=100.3)
        assert d.steps_due(at=101.31) == ()

    def test_previous_is_a_negative_jump(self) -> None:
        d = _dialler()
        d.step("box", -1, at=100.0)
        d.step("box", -1, at=100.2)
        assert d.steps_due(at=101.21) == (("box", -2),)

    def test_steps_and_digits_are_separate_on_one_box(self) -> None:
        """Pressing 1 and then next is not the number 1 plus a step of one on it: they are two
        different actions, and mixing them into one buffer would make either unreadable."""
        d = _dialler()
        d.digit("box", "1", at=100.0)
        d.step("box", 1, at=100.1)
        assert d.due(at=101.2) == (("box", "1"),)
        assert d.steps_due(at=101.2) == (("box", 1),)

    def test_a_jump_is_reported_once(self) -> None:
        d = _dialler()
        d.step("box", 1, at=100.0)
        assert d.steps_due(at=101.01) == (("box", 1),)
        assert d.steps_due(at=101.02) == ()

    def test_a_step_waits_while_its_key_is_still_down(self) -> None:
        """A key still down may yet become a HOLD, and a hold replaces the tap it began as.

        The window and the hold threshold are two numbers (user, 2026-09-24), so the window can
        close while the key is still down. Acting then would step as a tap and step again when the
        hold is decided - two jumps for one gesture. The caller answers whether a key is down, the
        way it answers whether a press is still being reported for a number.
        """
        d = _dialler()
        d.step("box", 1, at=100.0)
        assert d.steps_due(at=101.2, still_stepping=lambda _box: True) == (), "the key is still down"
        assert d.steps_due(at=101.3, still_stepping=lambda _box: False) == (("box", 1),), "and it came up"

    def test_a_step_held_back_by_its_key_contributes_no_deadline(self) -> None:
        """A deadline already past that cannot be drained would spin the waiting loop instead of
        letting it sleep; the key's own hold deadline is what wakes it."""
        d = _dialler()
        d.step("box", 1, at=100.0)
        assert d.deadline(still_stepping=lambda _box: True) is None
        assert d.deadline(still_stepping=lambda _box: False) == pytest.approx(101.0)

    def test_the_deadline_covers_steps_as_well_as_digits(self) -> None:
        """The service sleeps to ONE deadline, so a pending jump has to be inside it."""
        d = _dialler()
        d.step("box", 1, at=100.0)
        assert d.deadline() == pytest.approx(101.0)
        d.digit("other", "1", at=100.4)
        assert d.deadline() == pytest.approx(101.0), "the earliest of the two, whichever kind it is"


class TestThumbTaps:
    """A single thumb tap and a double one mean two different things (user, 2026-09-25).

    A single tap hands the box the house volume, a double tap moves the box in or out of the group,
    so that one stray touch can no longer take a channel out of what next and previous walk through. The gap
    that decides it is the IDLE time between the two taps - from the first key coming up to the
    second going down - for the same reason the digits' window is armed at a release: how long a
    thumb rests on a key is not a pause. Nothing is waited out: the count is answered at each tap,
    so a double tap acts at its second release and a single one at its first.
    """

    def test_one_tap_counts_one(self) -> None:
        d = _dialler()
        assert d.thumb("box", "THUMBS_DOWN", pressed_at=100.0, released_at=100.3) == 1

    def test_a_second_tap_pressed_inside_the_window_counts_two(self) -> None:
        d = _dialler()
        d.thumb("box", "THUMBS_DOWN", pressed_at=100.0, released_at=100.3)
        assert d.thumb("box", "THUMBS_DOWN", pressed_at=101.2, released_at=101.6) == 2

    def test_the_gap_is_measured_from_the_release_to_the_next_press(self) -> None:
        """Release to RELEASE would be 1.3 s here and count one; the person paused 0.9 s."""
        d = _dialler()
        d.thumb("box", "THUMBS_DOWN", pressed_at=100.0, released_at=100.3)
        assert d.thumb("box", "THUMBS_DOWN", pressed_at=101.2, released_at=101.7) == 2

    def test_the_window_boundary_is_inclusive_and_asserted_exactly(self) -> None:
        d = _dialler()
        d.thumb("box", "THUMBS_UP", pressed_at=100.0, released_at=100.25)
        assert d.thumb("box", "THUMBS_UP", pressed_at=101.25, released_at=101.5) == 2

    def test_a_second_tap_after_the_window_is_a_single_tap_again(self) -> None:
        d = _dialler()
        d.thumb("box", "THUMBS_UP", pressed_at=100.0, released_at=100.25)
        assert d.thumb("box", "THUMBS_UP", pressed_at=101.26, released_at=101.5) == 1

    def test_the_other_thumb_starts_the_count_again(self) -> None:
        """Up then down is two single taps, never a double tap of either."""
        d = _dialler()
        d.thumb("box", "THUMBS_UP", pressed_at=100.0, released_at=100.3)
        assert d.thumb("box", "THUMBS_DOWN", pressed_at=100.5, released_at=100.8) == 1

    def test_a_third_tap_starts_the_count_again(self) -> None:
        """Two taps are one double tap; what follows it is a tap of its own, not a triple."""
        d = _dialler()
        d.thumb("box", "THUMBS_DOWN", pressed_at=100.0, released_at=100.3)
        d.thumb("box", "THUMBS_DOWN", pressed_at=100.5, released_at=100.8)
        assert d.thumb("box", "THUMBS_DOWN", pressed_at=101.0, released_at=101.3) == 1

    def test_two_boxes_count_their_own_taps(self) -> None:
        d = _dialler()
        d.thumb("a", "THUMBS_DOWN", pressed_at=100.0, released_at=100.3)
        assert d.thumb("b", "THUMBS_DOWN", pressed_at=100.5, released_at=100.8) == 1
        assert d.thumb("a", "THUMBS_DOWN", pressed_at=100.6, released_at=100.9) == 2
