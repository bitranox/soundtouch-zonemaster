"""The order a directory channel plays its files in, and where a held key lands in it.

The order is the user's (OPEN-WORK rank 11, decision C, 2026-09-24): files before subdirectories,
each branch played to its end before the next, numbers compared as numbers, and no locale - so
the same collection gives the same order on every machine. It is ours rather than MPD's database
order, which is why the service builds the queue one file at a time.

The fixture names are chosen to be mean rather than typical: "Kapitel 2" beside "Kapitel 10", case
that differs, umlauts, a file named exactly like the directory beside it, and a directory that
holds only directories.
"""

from __future__ import annotations

import pytest

from soundtouch_zonemaster.domain.playorder import directory_jump, play_order


class TestPlayOrder:
    def test_the_files_of_a_directory_come_before_its_subdirectories(self) -> None:
        files = ["Buch/Bonus/01.mp3", "Buch/02.mp3", "Buch/01.mp3"]

        assert play_order(files) == ("Buch/01.mp3", "Buch/02.mp3", "Buch/Bonus/01.mp3")

    def test_each_branch_is_played_to_its_end_before_the_next_one_starts(self) -> None:
        """Depth-first: ``A/B/x`` sits inside A, so it comes before anything in C, however deep."""
        files = ["C/1.mp3", "A/B/D/x.mp3", "A/2.mp3", "A/B/y.mp3"]

        assert play_order(files) == ("A/2.mp3", "A/B/y.mp3", "A/B/D/x.mp3", "C/1.mp3")

    def test_numbers_are_compared_as_numbers(self) -> None:
        files = ["Kapitel 10.mp3", "Kapitel 2.mp3", "Kapitel 1.mp3"]

        assert play_order(files) == ("Kapitel 1.mp3", "Kapitel 2.mp3", "Kapitel 10.mp3")

    def test_numbered_directories_are_compared_as_numbers_too(self) -> None:
        files = ["CD 10/01.mp3", "CD 9/01.mp3"]

        assert play_order(files) == ("CD 9/01.mp3", "CD 10/01.mp3")

    def test_case_does_not_decide_the_order(self) -> None:
        files = ["b.mp3", "A.mp3", "c.mp3"]

        assert play_order(files) == ("A.mp3", "b.mp3", "c.mp3")

    def test_an_umlaut_sorts_with_its_base_letter_and_not_after_z(self) -> None:
        """A fixed normalisation, not the environment's collation: an a with two dots is an a."""
        files = ["Zebra.mp3", "Ärger.mp3", "Affe.mp3"]

        assert play_order(files) == ("Affe.mp3", "Ärger.mp3", "Zebra.mp3")

    def test_the_same_name_composed_and_decomposed_sorts_the_same(self) -> None:
        """A file copied off a Mac arrives decomposed; it must not land somewhere else for that."""
        composed, decomposed = "Ärger.mp3", "Ärger.mp3"

        assert play_order([decomposed, "Zebra.mp3", "Affe.mp3"]) == ("Affe.mp3", decomposed, "Zebra.mp3")
        assert play_order([composed, "Zebra.mp3", "Affe.mp3"]) == ("Affe.mp3", composed, "Zebra.mp3")

    def test_a_file_named_like_the_directory_beside_it_comes_first(self) -> None:
        files = ["Bonus/01.mp3", "Bonus", "Anhang.mp3"]

        assert play_order(files) == ("Anhang.mp3", "Bonus", "Bonus/01.mp3")

    def test_a_directory_holding_only_directories_is_walked_through(self) -> None:
        files = ["Serie/Staffel 2/01.mp3", "Serie/Staffel 1/01.mp3", "Anfang.mp3"]

        assert play_order(files) == ("Anfang.mp3", "Serie/Staffel 1/01.mp3", "Serie/Staffel 2/01.mp3")

    def test_names_equal_under_the_rule_still_come_out_in_one_order(self) -> None:
        """``a.mp3`` and ``A.mp3`` are both legal on Linux; either input order gives one answer."""
        assert play_order(["a.mp3", "A.mp3"]) == play_order(["A.mp3", "a.mp3"])

    def test_two_directories_equal_under_the_rule_stay_two_branches(self) -> None:
        """A tie broken on the whole path would interleave ``a/`` and ``A/`` file by file."""
        files = ["a/1.mp3", "A/2.mp3", "a/3.mp3"]

        assert play_order(files) == ("A/2.mp3", "a/1.mp3", "a/3.mp3")

    def test_leading_zeros_do_not_change_the_number(self) -> None:
        files = ["10.mp3", "02.mp3", "1.mp3"]

        assert play_order(files) == ("1.mp3", "02.mp3", "10.mp3")

    @pytest.mark.parametrize("files", [[], ["only.mp3"]])
    def test_nothing_and_one_file_are_themselves(self, files: list[str]) -> None:
        assert play_order(files) == tuple(files)


BOOK = (
    "Buch/01.mp3",  # 0
    "Buch/02.mp3",  # 1
    "Buch/Bonus/01.mp3",  # 2
    "Buch/Bonus/02.mp3",  # 3
    "Zweites/01.mp3",  # 4
    "Zweites/02.mp3",  # 5
    "Zweites/03.mp3",  # 6
)
"""Three runs of one parent each: Buch at 0, Buch/Bonus at 2, Zweites at 4."""


class TestDirectoryJump:
    """A held next or previous: whole directories rather than files (decisions D, E, G, H).

    It reads nothing but the PATH of each queue entry, so it holds for a stored playlist exactly as
    for a directory channel - a playlist's entries can come from several directories too.
    """

    @pytest.mark.parametrize(("current", "expected"), [(0, 2), (1, 2), (2, 4), (3, 4), (5, 0), (6, 0)])
    def test_held_next_goes_to_the_first_file_of_the_next_directory_and_wraps(
        self, current: int, expected: int
    ) -> None:
        """D: the next entry whose parent differs, which is a subdirectory of the current one when
        it comes next - so forward never skips something that was about to play. E: past the end
        it wraps, on every channel."""
        assert directory_jump(BOOK, current=current, steps=1) == expected

    @pytest.mark.parametrize(("current", "expected"), [(1, 0), (3, 2), (5, 4), (6, 4)])
    def test_held_previous_from_inside_a_directory_goes_to_its_own_first_file(
        self, current: int, expected: int
    ) -> None:
        """G: the CD player's back key - the start of this one first, the one before only from there."""
        assert directory_jump(BOOK, current=current, steps=-1) == expected

    @pytest.mark.parametrize(("current", "expected"), [(2, 0), (4, 2), (0, 4)])
    def test_held_previous_from_a_first_file_goes_to_the_directory_before_and_wraps(
        self, current: int, expected: int
    ) -> None:
        assert directory_jump(BOOK, current=current, steps=-1) == expected

    def test_two_holds_are_two_directories(self) -> None:
        """H: two directories either way; backwards the first of them is the one the file is in."""
        assert directory_jump(BOOK, current=0, steps=2) == 4
        assert directory_jump(BOOK, current=5, steps=-2) == 2
        assert directory_jump(BOOK, current=4, steps=-2) == 0

    def test_a_directory_that_comes_back_later_in_a_playlist_is_two_runs(self) -> None:
        """A stored playlist may return to a directory; each unbroken stretch is one stop."""
        queue = ("A/1.mp3", "B/1.mp3", "A/2.mp3")

        assert directory_jump(queue, current=0, steps=1) == 1
        assert directory_jump(queue, current=1, steps=1) == 2

    def test_with_no_current_entry_next_starts_at_the_first_and_previous_at_the_last(self) -> None:
        """A queue that ran out has no current entry, which is what ``entry_after`` does too."""
        assert directory_jump(BOOK, current=None, steps=1) == 0
        assert directory_jump(BOOK, current=None, steps=-1) == 4

    def test_one_directory_only_goes_back_to_its_first_file(self) -> None:
        queue = ("A/1.mp3", "A/2.mp3", "A/3.mp3")

        assert directory_jump(queue, current=2, steps=1) == 0
        assert directory_jump(queue, current=2, steps=-1) == 0

    def test_an_empty_queue_has_nowhere_to_go(self) -> None:
        assert directory_jump((), current=None, steps=1) is None

    def test_a_current_entry_past_the_queue_counts_as_none(self) -> None:
        """A status read and a queue read are two exchanges; the queue can be shorter in between."""
        assert directory_jump(BOOK, current=7, steps=1) == 0
