"""The channels the house can dial, and the numbers a person can actually press.

The alphabet is the six preset keys and nothing else, so the numbers that exist at all are 1 to 6,
then 11 to 66, then 111 upwards. That is bijective base 6, and the two functions that walk it in
either direction are inverses, which one test here asserts over five hundred numbers rather than
over the handful somebody thought of.

Two rules in here are measured rather than reasoned, and both have a test that says so. A repeated
digit is dialable, because on 2026-09-07 a box announced a preset press even when it selected what
was already selected; had that gone the other way, every number with two equal digits side by side
would have had to be skipped. And the seventh number handed out is 11 and never 7, which is the
user's rule from 2026-09-06: a channel nobody can type would be worse than no channel, because the
list would say it is there.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from soundtouch_zonemaster.domain.channellist import (
    Channel,
    ChannelList,
    ChannelNumberError,
    PresetStation,
    dialable,
    ladder_index,
    nth_number,
    seed_from_presets,
)
from soundtouch_zonemaster.domain.enums import ChannelEnd, ChannelKind


def _radio(number: str, name: str | None = None) -> Channel:
    """One radio channel with an invented name and URL; only its number matters to most tests."""
    return Channel(
        number=number,
        name=name if name is not None else f"Station {number}",
        kind=ChannelKind.RADIO,
        url=f"http://192.0.2.1/{number}",
    )


class TestTheAlphabet:
    """Which strings are numbers at all."""

    def test_the_six_preset_keys_are_the_whole_alphabet(self) -> None:
        assert dialable("1")
        assert dialable("6")
        assert dialable("66")
        assert dialable("111")

    @pytest.mark.parametrize("bad", ["", "0", "7", "8", "9", "10", "1 2", "a", "1.2", "-1", "1\n"])
    def test_anything_no_key_can_press_is_not_a_number(self, bad: str) -> None:
        assert not dialable(bad)

    def test_a_repeated_digit_is_a_number_like_any_other(self) -> None:
        """Measured 2026-09-07: a box announces the same preset pressed twice, so 11 is dialable."""
        assert dialable("11")
        assert dialable("111")
        assert dialable("666")


class TestTheLadder:
    """Dialling order, which is the order the numbers can be typed in and nothing else."""

    def test_it_counts_in_bijective_base_six(self) -> None:
        assert [nth_number(n) for n in range(1, 8)] == ["1", "2", "3", "4", "5", "6", "11"]
        assert nth_number(12) == "16"
        assert nth_number(13) == "21"
        assert nth_number(42) == "66"
        assert nth_number(43) == "111"

    def test_a_repeated_digit_sits_where_counting_puts_it(self) -> None:
        assert ladder_index("11") == 7
        assert ladder_index("66") == 42

    def test_the_two_directions_are_inverses(self) -> None:
        for n in range(1, 500):
            assert ladder_index(nth_number(n)) == n

    def test_there_is_no_zeroth_number(self) -> None:
        with pytest.raises(ChannelNumberError):
            nth_number(0)

    def test_a_number_no_key_can_press_has_no_place_in_the_ladder(self) -> None:
        with pytest.raises(ChannelNumberError) as caught:
            ladder_index("7")
        assert "7" in str(caught.value)


class TestTheList:
    """Looking a channel up, ordering the list, and handing out the next free number."""

    def test_a_channel_is_found_by_its_number(self) -> None:
        have = ChannelList(channels=(_radio("1", "Superfly"), _radio("11", "Technikum")))
        found = have.by_number("11")
        assert found is not None
        assert found.name == "Technikum"

    def test_a_number_that_is_not_there_comes_back_empty_rather_than_raising(self) -> None:
        """The design's rule: an undefined number does nothing at all, which is not an error."""
        assert ChannelList(channels=(_radio("1"),)).by_number("66") is None

    def test_numbers_in_order_is_dialling_order_not_alphabetical(self) -> None:
        """Sorted as strings these read 1, 11, 111, 2 - so the obvious sorted() is wrong."""
        have = ChannelList(channels=(_radio("11"), _radio("2"), _radio("111"), _radio("1")))
        assert have.numbers_in_order() == ("1", "2", "11", "111")

    def test_the_seventh_number_handed_out_is_eleven_and_never_seven(self) -> None:
        taken = ChannelList(channels=tuple(_radio(nth_number(n)) for n in range(1, 7)))
        assert taken.lowest_free_number() == "11"

    def test_the_lowest_free_number_fills_a_hole_before_going_on(self) -> None:
        have = ChannelList(channels=(_radio("1"), _radio("3")))
        assert have.lowest_free_number() == "2"

    def test_an_empty_list_hands_out_the_first_number(self) -> None:
        assert ChannelList().lowest_free_number() == "1"

    def test_adding_a_channel_leaves_the_original_alone(self) -> None:
        """The record is frozen, so a caller that kept a reference still sees what it had."""
        before = ChannelList(channels=(_radio("1"),))
        after = before.with_channel(_radio("2"))
        assert before.numbers_in_order() == ("1",)
        assert after.numbers_in_order() == ("1", "2")

    def test_adding_a_number_that_is_taken_replaces_it_rather_than_duplicating(self) -> None:
        before = ChannelList(channels=(_radio("1", "old"),))
        after = before.with_channel(_radio("1", "new"))
        assert after.numbers_in_order() == ("1",)
        found = after.by_number("1")
        assert found is not None
        assert found.name == "new"

    def test_removing_a_number_that_is_not_there_changes_nothing(self) -> None:
        before = ChannelList(channels=(_radio("1"),))
        assert before.without_number("66") == before

    def test_removing_a_number_takes_exactly_that_one(self) -> None:
        before = ChannelList(channels=(_radio("1"), _radio("2")))
        assert before.without_number("1").numbers_in_order() == ("2",)


class TestTheRotation:
    """What next and previous walk, once a thumbs down has taken something out of it.

    Decided by the user on 2026-09-07 over two other shapes for the thumbs. The mark is read on
    every step rather than never, which is the whole reason this one was chosen: a mark nothing
    reads is a mark that can be wrong for years without anybody noticing.
    """

    def test_rotation_numbers_leaves_out_what_was_taken_out(self) -> None:
        have = ChannelList(channels=(_radio("1"), _radio("2"), _radio("3"))).with_rotation("2", in_rotation=False)
        assert have.numbers_in_order() == ("1", "2", "3")
        assert have.rotation_numbers() == ("1", "3")

    def test_taking_a_number_out_that_is_not_in_the_list_changes_nothing(self) -> None:
        before = ChannelList(channels=(_radio("1"),))
        assert before.with_rotation("66", in_rotation=False) == before

    def test_next_skips_the_channel_that_was_taken_out(self) -> None:
        have = ChannelList(channels=(_radio("1"), _radio("2"), _radio("3"))).with_rotation("2", in_rotation=False)
        assert have.step("1", 1) == "3"

    def test_next_from_a_channel_that_is_out_of_the_rotation_lands_on_the_one_after_it(self) -> None:
        """The case a thumbs down creates: the listener is ON the channel they just took out."""
        have = ChannelList(channels=(_radio("1"), _radio("2"), _radio("3"))).with_rotation("2", in_rotation=False)
        assert have.step("2", 1) == "3"

    def test_previous_from_a_channel_that_is_out_of_the_rotation_lands_on_the_one_before_it(self) -> None:
        have = ChannelList(channels=(_radio("1"), _radio("2"), _radio("3"))).with_rotation("2", in_rotation=False)
        assert have.step("2", -1) == "1"

    def test_a_jump_of_three_counts_only_channels_in_the_rotation(self) -> None:
        have = ChannelList(channels=tuple(_radio(n) for n in ("1", "2", "3", "4"))).with_rotation(
            "2", in_rotation=False
        )
        assert have.step("1", 3) == "1"

    def test_stepping_wraps_at_the_end(self) -> None:
        have = ChannelList(channels=(_radio("1"), _radio("2")))
        assert have.step("2", 1) == "1"

    def test_stepping_when_nothing_is_in_the_rotation_has_nowhere_to_go(self) -> None:
        have = ChannelList(channels=(_radio("1"),)).with_rotation("1", in_rotation=False)
        assert have.step("1", 1) is None

    def test_stepping_from_a_number_that_is_not_in_the_list_starts_at_the_beginning(self) -> None:
        """What a hand-edited file leaves behind: the remembered channel is gone from the list."""
        have = ChannelList(channels=(_radio("1"), _radio("2")))
        assert have.step("66", 1) == "1"


class TestSeeding:
    """Places 1 to 6, taken from the presets of ONE named speaker at first start.

    The rule an implementer gets wrong by being helpful is the hole: a box with presets 1 and 3 set
    seeds channels 1 and 3, never 1 and 2. The key on the box IS the number in the room, which is
    the entire reason the design seeds from presets rather than from a list somebody types.
    """

    def test_six_presets_become_channels_one_to_six(self) -> None:
        have = seed_from_presets({n: PresetStation(name=f"S{n}", url=f"http://x/{n}") for n in range(1, 7)})
        assert have.seeded.numbers_in_order() == ("1", "2", "3", "4", "5", "6")

    def test_an_unset_preset_leaves_a_hole_rather_than_shifting_the_others(self) -> None:
        have = seed_from_presets(
            {1: PresetStation(name="A", url="http://x/a"), 2: None, 3: PresetStation(name="C", url="http://x/c")}
        )
        assert have.seeded.numbers_in_order() == ("1", "3")
        found = have.seeded.by_number("3")
        assert found is not None
        assert found.name == "C"

    def test_the_name_and_url_come_from_the_preset(self) -> None:
        have = seed_from_presets({1: PresetStation(name="Superfly", url="http://x/sf")})
        found = have.seeded.by_number("1")
        assert found is not None
        assert (found.name, found.url, found.kind) == ("Superfly", "http://x/sf", ChannelKind.RADIO)

    def test_seeding_nothing_is_an_empty_list_and_says_so(self) -> None:
        have = seed_from_presets(dict.fromkeys(range(1, 7)))
        assert have.seeded.channels == ()
        assert any("no preset" in said for said in have.said)

    def test_a_preset_that_is_not_set_is_named_in_the_log(self) -> None:
        """A hole is normal, not an error - but somebody looking for channel 2 needs to see why."""
        report = seed_from_presets({1: PresetStation(name="A", url="http://x/a"), 2: None})
        assert any("2" in said for said in report.said)


class TestTheChannelRecord:
    """What a channel refuses to be."""

    def test_a_channel_cannot_carry_a_number_no_key_can_press(self) -> None:
        with pytest.raises(ValueError, match="7"):
            Channel(number="7", name="nope", kind=ChannelKind.RADIO, url="http://192.0.2.1/")

    def test_a_channel_is_frozen(self) -> None:
        channel = _radio("1")
        with pytest.raises(FrozenInstanceError):
            channel.number = "2"  # type: ignore[misc]

    def test_a_channel_is_in_the_rotation_until_something_takes_it_out(self) -> None:
        assert _radio("1").in_rotation is True

    @pytest.mark.parametrize("bad", ["/srv/music/a.mp3", "file:///srv/music/a.mp3", "a.mp3", "ftp://x/y"])
    def test_a_url_the_master_cannot_fetch_is_refused(self, bad: str) -> None:
        """A bare path reads like something that could work, which is why it is named here.

        The refusal used to sit on the service's --station-url and moved with the channel list,
        because this is where a URL somebody can type now lives.
        """
        with pytest.raises(ValueError, match="http"):
            Channel(number="1", name="local", kind=ChannelKind.RADIO, url=bad)


class TestAnMpdChannel:
    """The kind and the stored playlist it names are ONE fact, so a mismatch cannot be built.

    Both halves matter and only one of them is obvious. An MPD channel with no playlist dials to
    silence with nothing in the log saying why. A radio channel CARRYING a playlist plays correctly
    and lies to the next reader about where its sound comes from, which is the half that would
    survive a review.
    """

    def test_an_mpd_channel_names_the_stored_playlist_it_plays(self) -> None:
        """The url is MPD's httpd endpoint, the same for every MPD channel; the entry is not."""
        channel = Channel(
            number="3",
            name="Hoerbuecher",
            kind=ChannelKind.MPD,
            url="http://127.0.0.1:8000",
            mpd_entry="hoerbuecher",
        )
        assert channel.mpd_entry == "hoerbuecher"

    def test_an_mpd_channel_without_an_entry_is_refused(self) -> None:
        """A channel MPD cannot be asked to load would dial to silence with nothing saying why."""
        with pytest.raises(ChannelNumberError, match="names no playlist"):
            Channel(number="3", name="leer", kind=ChannelKind.MPD, url="http://127.0.0.1:8000")

    def test_a_radio_channel_carrying_an_entry_is_refused(self) -> None:
        """A radio channel's url is the whole answer, so an entry beside it can only mislead."""
        with pytest.raises(ChannelNumberError, match="carries an MPD playlist"):
            Channel(
                number="1",
                name="OE3",
                kind=ChannelKind.RADIO,
                url="http://example.invalid/oe3",
                mpd_entry="something",
            )

    def test_a_radio_channel_is_unaffected_by_the_new_field(self) -> None:
        """The control: every channel that existed before this field omits it and must still build."""
        assert _radio("1").mpd_entry == ""


class TestAnMpdDirectoryChannel:
    """A channel that plays a DIRECTORY rather than a stored playlist (OPEN-WORK rank 11).

    The path is relative to MPD's ``music_directory``, the way a playlist name is relative to its
    ``playlist_directory``: MPD knows one tree, and how that tree is filled is not this service's
    business. Playlist and directory are two ways of saying what to play, so an MPD channel names
    exactly ONE of them - with both, which one plays would be a guess.
    """

    def test_an_mpd_channel_may_name_a_directory_instead_of_a_playlist(self) -> None:
        channel = Channel(
            number="21",
            name="Book Two",
            kind=ChannelKind.MPD,
            url="http://127.0.0.1:8000",
            mpd_directory="audiobooks/Author Two/Book Two",
        )
        assert channel.mpd_directory == "audiobooks/Author Two/Book Two"

    def test_an_mpd_channel_naming_neither_is_refused_and_says_both_ways(self) -> None:
        with pytest.raises(ChannelNumberError, match="names no playlist and no directory"):
            Channel(number="3", name="leer", kind=ChannelKind.MPD, url="http://127.0.0.1:8000")

    def test_an_mpd_channel_naming_both_is_refused(self) -> None:
        with pytest.raises(ChannelNumberError, match="names a playlist and a directory"):
            Channel(
                number="3",
                name="beides",
                kind=ChannelKind.MPD,
                url="http://127.0.0.1:8000",
                mpd_entry="buch",
                mpd_directory="audiobooks/Buch",
            )

    def test_a_radio_channel_carrying_a_directory_is_refused(self) -> None:
        with pytest.raises(ChannelNumberError, match="carries an MPD directory"):
            Channel(
                number="1",
                name="OE3",
                kind=ChannelKind.RADIO,
                url="http://example.invalid/oe3",
                mpd_directory="audiobooks/Buch",
            )

    @pytest.mark.parametrize("path", ["/srv/music/audiobooks", "audiobooks/../playlists", "..", "a//b", "a/"])
    def test_a_directory_that_is_not_plainly_under_the_music_directory_is_refused(self, path: str) -> None:
        """An absolute path or a ``..`` names something outside MPD's tree, which MPD refuses
        at dial time with the house silent; an empty segment is a typo MPD would read its own way."""
        with pytest.raises(ChannelNumberError, match="under mpd's music directory"):
            Channel(number="3", name="weg", kind=ChannelKind.MPD, url="http://127.0.0.1:8000", mpd_directory=path)

    def test_every_existing_channel_builds_without_the_field(self) -> None:
        """The control: radio channels and playlist channels written before the field existed."""
        assert _radio("1").mpd_directory == ""
        playlist = Channel(number="3", name="B", kind=ChannelKind.MPD, url="http://127.0.0.1:8000", mpd_entry="b")
        assert playlist.mpd_directory == ""


class TestTheEndOfAnMpdChannel:
    """What an MPD channel does when it runs out, decided per channel (user, 2026-09-24).

    ``wrap`` starts over, which is what a music playlist wants; ``stop`` falls silent, which is what
    a book wants. A channel that says nothing wraps, because the house's MPD channels were written
    before the field existed and the silence at their end is the defect this field fixes.
    """

    def test_a_channel_that_names_no_end_wraps(self) -> None:
        channel = Channel(
            number="12", name="Musik", kind=ChannelKind.MPD, url="http://127.0.0.1:8000", mpd_entry="musik"
        )
        assert channel.end is ChannelEnd.WRAP

    def test_an_mpd_channel_may_stop_at_its_end(self) -> None:
        channel = Channel(
            number="13",
            name="Buch",
            kind=ChannelKind.MPD,
            url="http://127.0.0.1:8000",
            mpd_entry="buch",
            end=ChannelEnd.STOP,
        )
        assert channel.end is ChannelEnd.STOP

    def test_a_radio_channel_that_says_it_stops_is_refused(self) -> None:
        """A station has no end, so the word can only mislead the person who reads the file."""
        with pytest.raises(ChannelNumberError, match="has no end to stop at"):
            Channel(
                number="1", name="OE3", kind=ChannelKind.RADIO, url="http://example.invalid/oe3", end=ChannelEnd.STOP
            )
