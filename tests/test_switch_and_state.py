"""The old switch file's word, and a remembered place as the house comes back to it.

The switch file is read by the installer's switch seed and can be WATCHED, so the tests here also
write it while a watcher runs. The case that matters is not the obvious one: most editors do not
modify a file in place, they write a new one and rename it over the old, so anything holding on to
what it opened at start never sees the change and reports the old value forever.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from soundtouch_zonemaster.adapters.files.switch_file import Switch
from soundtouch_zonemaster.domain.state import Place

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
