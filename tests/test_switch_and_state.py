"""The two files the service is steered by and remembers itself in.

The switch is WATCHED, not read once, so the tests here write it while a watcher runs. The case
that matters is not the obvious one: most editors do not modify a file in place, they write a new
one and rename it over the old, so anything holding on to what it opened at start never sees the
change and reports the old value forever.

The state file is read back after a restart, which means it is read after whatever ended the last
run - including a power cut in the middle of a write. A half-written document must start the
service empty and say so, never raise, and a save must never be able to produce one.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from soundtouch_zonemaster.adapters.files.state_file import load_state, save_state
from soundtouch_zonemaster.adapters.files.switch_file import Switch
from soundtouch_zonemaster.domain.state import Place, ZoneState

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


def _quiet(_kind: str, _text: str) -> None:
    return


async def _next_value(watcher: AsyncGenerator[bool, None], timeout: float = 2.0) -> bool:
    """The next value the watcher reports, or TimeoutError if it reports none.

    Asking for the NEXT value rather than collecting into a list is what lets a test assert that
    the watcher said nothing at all, which a list of what it happened to say cannot.
    """
    return await asyncio.wait_for(anext(watcher), timeout)


def test_a_switch_file_that_says_off_is_off(tmp_path: Path) -> None:
    path = tmp_path / "switch"
    path.write_text("off\n", encoding="utf-8")

    assert Switch(path, log=_quiet).is_on() is False


def test_a_switch_file_that_says_on_is_on(tmp_path: Path) -> None:
    path = tmp_path / "switch"
    path.write_text("on\n", encoding="utf-8")

    assert Switch(path, log=_quiet).is_on() is True


def test_the_switch_is_off_only_when_the_file_says_so(tmp_path: Path) -> None:
    """One rule, so there is nothing to get wrong: a missing or unreadable file does not stop the
    house working, and the switch is a deliberate act rather than an accident of a lost file."""
    path = tmp_path / "switch"

    assert Switch(path, log=_quiet).is_on() is True

    path.write_text("", encoding="utf-8")
    assert Switch(path, log=_quiet).is_on() is True

    path.write_text("banana", encoding="utf-8")
    assert Switch(path, log=_quiet).is_on() is True


def test_off_is_read_however_it_is_spelled_and_spaced(tmp_path: Path) -> None:
    path = tmp_path / "switch"
    for written in ("off", "OFF", " Off \n", "off\r\n"):
        path.write_text(written, encoding="utf-8")
        assert Switch(path, log=_quiet).is_on() is False, written


def test_off_written_with_a_byte_order_mark_is_still_off(tmp_path: Path) -> None:
    """Windows writes "UTF-8 with BOM", and this file is edited over SMB from there.

    The mark is invisible to whoever typed the word, so reading it as part of the word turns a
    deliberate off into on - the one direction this module's whole rule exists to make impossible
    by accident.
    """
    path = tmp_path / "switch"
    path.write_bytes(b"\xef\xbb\xbfoff\n")

    assert Switch(path, log=_quiet).is_on() is False


def test_a_switch_file_that_is_not_utf_8_is_on_rather_than_fatal(tmp_path: Path) -> None:
    """Unreadable means on, and that has to hold for every way a file can be unreadable.

    A decode error raised here leaves the polling task, and the bare gather that runs it takes the
    whole service down with it - so this one byte is the difference between the house staying up
    and the house stopping.
    """
    path = tmp_path / "switch"
    path.write_bytes(b"off\xff")

    assert Switch(path, log=_quiet).is_on() is True


async def test_the_watcher_reports_a_change_written_while_it_runs(tmp_path: Path) -> None:
    path = tmp_path / "switch"
    path.write_text("on", encoding="utf-8")
    watcher = Switch(path, log=_quiet, poll_s=0.01).watch()

    assert await _next_value(watcher) is True
    path.write_text("off", encoding="utf-8")

    assert await _next_value(watcher) is False
    await watcher.aclose()


async def test_the_watcher_survives_the_file_being_replaced_rather_than_edited(tmp_path: Path) -> None:
    """What an editor really does: write a new file and rename it over the old one.

    A watcher holding the file it opened at start keeps reporting the old value forever, and
    nothing about it looks broken.
    """
    path = tmp_path / "switch"
    path.write_text("on", encoding="utf-8")
    watcher = Switch(path, log=_quiet, poll_s=0.01).watch()
    assert await _next_value(watcher) is True

    replacement = tmp_path / "switch.new"
    replacement.write_text("off", encoding="utf-8")
    replacement.replace(path)

    assert await _next_value(watcher) is False
    await watcher.aclose()


async def test_the_watcher_reports_a_file_that_appears_later(tmp_path: Path) -> None:
    """It is normal for the file not to exist yet: nobody has turned anything off."""
    path = tmp_path / "switch"
    watcher = Switch(path, log=_quiet, poll_s=0.01).watch()
    assert await _next_value(watcher) is True

    path.write_text("off", encoding="utf-8")

    assert await _next_value(watcher) is False
    await watcher.aclose()


async def test_the_watcher_says_nothing_while_nothing_changes(tmp_path: Path) -> None:
    """A watcher that re-reports its value every poll makes a log nobody can read, and would put
    the service through a dissolve or a rejoin once a second."""
    path = tmp_path / "switch"
    path.write_text("on", encoding="utf-8")
    watcher = Switch(path, log=_quiet, poll_s=0.01).watch()
    assert await _next_value(watcher) is True

    with pytest.raises(TimeoutError):
        await _next_value(watcher, timeout=0.2)

    await watcher.aclose()


def test_the_state_file_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = ZoneState(channel="3", members=("AABBCC000010", "AABBCC000011"))

    save_state(path, state)

    assert load_state(path, log=_quiet) == state


def test_the_position_of_each_mpd_channel_round_trips(tmp_path: Path) -> None:
    """The one thing this file keeps that is not about speakers: where in a book the house got to.
    A place is a track and an offset, the offset is a float and a channel number is a digit string,
    so every part of the map has to survive JSON - a key written as a number would come back as one
    and match no channel."""
    path = tmp_path / "state.json"
    state = ZoneState(channel="12", positions={"12": Place(track=2, seconds=61.5), "3": Place(track=0, seconds=0.0)})

    save_state(path, state)

    assert load_state(path, log=_quiet) == state
    assert load_state(path, log=_quiet).positions["3"].seconds == 0.0, "the very start is still a place"


def test_the_volume_steps_a_box_missed_round_trip(tmp_path: Path) -> None:
    """A box that was off when the house was turned up takes the step when it next joins, and a
    restart in between must not lose it - a negative step included, which is the quieter house."""
    path = tmp_path / "state.json"
    state = ZoneState(owed_volume={"AABBCC0000A2": -5, "AABBCC0000A4": 5})

    save_state(path, state)

    assert load_state(path, log=_quiet) == state


def test_a_state_file_written_before_owed_volume_existed_loads_owing_nothing(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"channel": "3", "members": ["AABBCC000010"], "muted": {}}', encoding="utf-8")

    assert load_state(path, log=_quiet).owed_volume == {}


def test_a_state_file_that_remembers_only_the_offset_loads_as_the_first_track(tmp_path: Path) -> None:
    """The house has one of these on disk: written by the version that kept seconds and no track.

    It loads as track zero, which is exactly what that version would have played - it always
    resumed the first file - so a file from before the change means the same thing after it.
    """
    path = tmp_path / "state.json"
    path.write_text('{"channel": "11", "positions": {"11": 1.539}}', encoding="utf-8")

    state = load_state(path, log=_quiet)

    assert state.positions == {"11": Place(track=0, seconds=1.539)}


def test_a_state_file_written_before_positions_existed_loads_with_none(tmp_path: Path) -> None:
    """The house has one of these on disk right now. A new field must not make it unusable, and it
    must not invent positions either: no key means no channel has ever been left."""
    path = tmp_path / "state.json"
    path.write_text('{"channel": "3", "members": ["AABBCC000010"], "muted": {}}', encoding="utf-8")

    state = load_state(path, log=_quiet)

    assert state.channel == "3"
    assert state.positions == {}


def test_a_missing_state_file_starts_empty(tmp_path: Path) -> None:
    assert load_state(tmp_path / "nothing.json", log=_quiet) == ZoneState()


def test_a_half_written_state_file_starts_empty_and_says_so(tmp_path: Path) -> None:
    """A power cut mid-write is exactly what a state file is read after."""
    path = tmp_path / "state.json"
    path.write_text('{"channel": "3", "mem', encoding="utf-8")
    lines: list[str] = []

    state = load_state(path, log=lambda _kind, text: lines.append(text))

    assert state == ZoneState()
    assert any(str(path) in line for line in lines)


def test_a_state_file_that_is_not_utf_8_starts_empty_and_says_so(tmp_path: Path) -> None:
    """ "Never raises" has to include the bytes, not only the JSON.

    An atomic write makes this unlikely rather than impossible: a disk error, or one hand in an
    editor set to the wrong encoding, and the service stopped on a traceback at startup instead of
    starting empty from a file it named.
    """
    path = tmp_path / "state.json"
    path.write_bytes(b'{"channel": "\xff"}')
    lines: list[str] = []

    state = load_state(path, log=lambda _kind, text: lines.append(text))

    assert state == ZoneState()
    assert any(str(path) in line for line in lines)


def test_a_state_file_written_with_a_byte_order_mark_loads(tmp_path: Path) -> None:
    """Windows writes "UTF-8 with BOM", and this file is edited over SMB from there.

    The three bytes are invisible to whoever opened the file, so reading them as part of the
    document makes a file a person can read and repair report itself unusable - and the channel
    it remembers is then lost on the very next restart, which is the one thing this file is for.
    """
    path = tmp_path / "state.json"
    path.write_bytes(b'\xef\xbb\xbf{"channel": "1"}')
    lines: list[str] = []

    state = load_state(path, log=lambda _kind, text: lines.append(text))

    assert state == ZoneState(channel="1")
    assert lines == []


def test_a_state_file_holding_the_wrong_shape_starts_empty(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"channel": 3, "members": "not a list"}', encoding="utf-8")

    assert load_state(path, log=_quiet) == ZoneState()


def test_saving_replaces_the_file_rather_than_writing_through_it(tmp_path: Path) -> None:
    """The reason a reader can never see half a document: the old file is never opened to write.

    Asserted on the inode, because that is the difference an in-place truncate would not make.
    """
    path = tmp_path / "state.json"
    save_state(path, ZoneState(channel="1"))
    before = path.stat().st_ino

    save_state(path, ZoneState(channel="2"))

    assert path.stat().st_ino != before
    assert load_state(path, log=_quiet).channel == "2"


def test_saving_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    path = tmp_path / "state.json"

    save_state(path, ZoneState(channel="1"))

    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_a_leftover_temporary_file_is_not_what_gets_loaded(tmp_path: Path) -> None:
    """A save killed halfway leaves its temporary behind; the real file must still be read."""
    path = tmp_path / "state.json"
    save_state(path, ZoneState(channel="1"))
    (tmp_path / "state.json.tmp").write_text("{ broken", encoding="utf-8")

    assert load_state(path, log=_quiet).channel == "1"


def test_the_saved_file_is_readable_by_a_person(tmp_path: Path) -> None:
    """It is a file somebody may have to repair at two in the morning."""
    path = tmp_path / "state.json"

    save_state(path, ZoneState(channel="11", members=("AABBCC000010",)))

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["channel"] == "11"
    assert written["members"] == ["AABBCC000010"]
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_coming_back_to_a_place_starts_a_little_before_it() -> None:
    """The overlap an audiobook needs (user, 2026-09-20): you hear your way back in.

    The stored place stays exactly where the house stopped - it is the RESUME that steps back, so
    the overlap can be changed without rewriting what was recorded.
    """
    assert Place(track=4, seconds=930.0).resumed(rewind_s=20.0) == Place(track=4, seconds=910.0)


def test_an_offset_shorter_than_the_overlap_starts_that_same_file_again() -> None:
    """It never steps back into the PREVIOUS file (user, 2026-09-20).

    The file before it is a different recording, often a different chapter, and a person who
    stopped ten seconds into one did not ask to hear the end of the other. The track is kept and
    the offset goes to zero.
    """
    assert Place(track=4, seconds=12.5).resumed(rewind_s=20.0) == Place(track=4, seconds=0.0)
    assert Place(track=0, seconds=0.0).resumed(rewind_s=20.0) == Place(track=0, seconds=0.0)


def test_a_place_remembers_the_file_by_name_and_round_trips(tmp_path: Path) -> None:
    """A directory channel remembers WHICH file by its path (OPEN-WORK rank 11): the index moves
    when somebody adds a file to the directory, and the name does not."""
    path = tmp_path / "state.json"
    state = ZoneState(positions={"21": Place(track=3, seconds=61.5, file="audiobooks/Buch/04.mp3")})

    save_state(path, state)

    assert load_state(path, log=_quiet) == state


def test_a_place_written_before_the_file_name_was_kept_loads_without_one(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"positions": {"12": {"track": 2, "seconds": 61.5}}}', encoding="utf-8")

    assert load_state(path, log=_quiet).positions["12"] == Place(track=2, seconds=61.5)


def test_coming_back_keeps_the_file_name() -> None:
    place = Place(track=4, seconds=930.0, file="Buch/05.mp3")

    assert place.resumed(rewind_s=20.0) == Place(track=4, seconds=910.0, file="Buch/05.mp3")


class TestAPlaceFoundInTheFilesOfADirectory:
    """Where a remembered place is in a directory as it is NOW (user, 2026-09-20, OPEN-WORK rank 11).

    By the file's NAME first, because a file added in front of it moves its index and the index
    alone would then point at the wrong chapter with nothing saying so. The index is the fallback,
    for when the file itself is gone.
    """

    FILES = ("Buch/01.mp3", "Buch/02.mp3", "Buch/02a.mp3", "Buch/03.mp3")

    def test_the_file_is_found_where_it_is_now_with_its_offset(self) -> None:
        """03 was the third file when the house left it; a new 02a made it the fourth."""
        place = Place(track=2, seconds=61.5, file="Buch/03.mp3")

        assert place.found_in(self.FILES) == Place(track=3, seconds=61.5, file="Buch/03.mp3")

    def test_a_file_that_is_gone_falls_back_to_its_index_from_the_start_of_that_file(self) -> None:
        """The offset belonged to the file that went, so it is not carried into another one."""
        place = Place(track=1, seconds=61.5, file="Buch/weg.mp3")

        assert place.found_in(self.FILES) == Place(track=1, seconds=0.0, file="Buch/02.mp3")

    def test_a_file_that_is_gone_with_an_index_past_the_end_starts_from_the_beginning(self) -> None:
        assert Place(track=9, seconds=61.5, file="Buch/weg.mp3").found_in(self.FILES) is None

    def test_a_place_with_no_file_name_is_trusted_by_its_index(self) -> None:
        """A place written before names were kept; its index is all there is to go on."""
        assert Place(track=2, seconds=61.5).found_in(self.FILES) == Place(track=2, seconds=61.5)
        assert Place(track=4, seconds=61.5).found_in(self.FILES) is None

    def test_an_empty_directory_has_no_place_in_it(self) -> None:
        assert Place(track=0, seconds=1.0, file="Buch/01.mp3").found_in(()) is None
