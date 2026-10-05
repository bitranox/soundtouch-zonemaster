"""What the box display says while a directory channel plays (OPEN-WORK rank 235).

The user's words, 2026-10-01: show the directory name, the number and the title of the piece, and
with several directories ``<Directory Title> <dir number>/<file Number> <name of the File (without
the Number if any)>``. What a box actually shows for a changed ``<track>`` is not measured yet;
these pin only the text the master would hand it.
"""

from __future__ import annotations

import pytest

from soundtouch_zonemaster.domain.nowshowing import piece_title, showing

BOOK = ("Book/01 - Opening.mp3", "Book/02 - The Road.mp3", "Book/10 - Home.mp3")
SERIES = (
    "Series/One/01 Arrival.mp3",
    "Series/One/02 Departure.mp3",
    "Series/Two/01 Return.mp3",
    "Series/Two/02 Rest.mp3",
)


@pytest.mark.parametrize(
    ("file", "title"),
    [
        pytest.param("Book/01 - Opening.mp3", "Opening", id="number-dash"),
        pytest.param("Book/01. Opening.mp3", "Opening", id="number-dot"),
        pytest.param("Book/01_Opening.mp3", "Opening", id="number-underscore"),
        pytest.param("Book/1-Opening.flac", "Opening", id="number-hyphen-no-space"),
        pytest.param("Book/Opening.mp3", "Opening", id="no-number"),
        pytest.param("Book/Track 01.mp3", "Track 01", id="number-not-leading-stays"),
        pytest.param("Book/2001 A Space Odyssey.mp3", "A Space Odyssey", id="leading-number-is-stripped"),
        pytest.param("Book/07.mp3", "07", id="only-a-number-keeps-it"),
        pytest.param("Book/07 - .mp3", "07 - ", id="nothing-after-the-number-keeps-it"),
        pytest.param("no-directory.ogg", "no-directory", id="top-level-file"),
        pytest.param("Book/Overture", "Overture", id="no-extension"),
    ],
)
def test_the_title_is_the_file_name_without_its_number_or_extension(file: str, title: str) -> None:
    assert piece_title(file) == title


def test_one_directory_shows_its_name_the_number_and_the_title() -> None:
    assert showing(BOOK, current=0) == "Book 1 Opening"
    assert showing(BOOK, current=2) == "Book 3 Home", "the position in the queue, not the number in the name"


def test_several_directories_show_the_directory_number_and_the_number_inside_it() -> None:
    assert showing(SERIES, current=0) == "One 1/1 Arrival"
    assert showing(SERIES, current=1) == "One 1/2 Departure"
    assert showing(SERIES, current=2) == "Two 2/1 Return"
    assert showing(SERIES, current=3) == "Two 2/2 Rest"


def test_a_directory_met_twice_is_numbered_by_its_stretch() -> None:
    """A stored playlist can come back to a directory it left, and a person stepping through it
    meets that directory twice: the number counts stretches, the way a held next/previous does."""
    queue = ("A/1 x.mp3", "B/1 y.mp3", "A/2 z.mp3")
    assert showing(queue, current=2) == "A 3/1 z"


def test_files_at_the_top_level_take_the_channel_name_as_their_directory() -> None:
    assert showing(("01 Song.mp3", "02 Other.mp3"), current=1, channel_name="Mix") == "Mix 2 Other"


@pytest.mark.parametrize("current", [None, -1, 3, 99])
def test_no_playing_entry_shows_nothing_of_its_own(current: int | None) -> None:
    """The caller then keeps the channel's own name, which is what a box shows today."""
    assert showing(BOOK, current=current) is None


def test_an_empty_queue_shows_nothing_of_its_own() -> None:
    assert showing((), current=0) is None
