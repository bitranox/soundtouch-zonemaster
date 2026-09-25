"""Where a press inside an MPD channel lands, and when a channel has run out.

A press past either end of the queue ALWAYS wraps, on every channel (user, 2026-09-24): ``stop``
is for the natural end, when nobody is there, and a person who presses next on the last file wants
the first one rather than silence. MPD's own ``next`` stops at the end unless ``repeat`` is on, so
the service works out the entry itself and asks MPD for exactly that one.
"""

from __future__ import annotations

import pytest

from soundtouch_zonemaster.domain.enums import MpdState
from soundtouch_zonemaster.domain.mpd import MpdStatus, entry_after


class TestEntryAfter:
    @pytest.mark.parametrize(
        ("current", "steps", "expected"),
        [
            (2, 1, 3),
            (2, -1, 1),
            (2, 3, 0),
            (4, 1, 0),
            (0, -1, 4),
            (0, -6, 4),
        ],
    )
    def test_a_press_moves_by_whole_entries_and_wraps_at_both_ends(
        self, current: int, steps: int, expected: int
    ) -> None:
        assert entry_after(current=current, length=5, steps=steps) == expected

    def test_from_a_stopped_queue_next_is_the_first_entry(self) -> None:
        """A ``stop`` channel that ran out has no current entry, and next starts it again."""
        assert entry_after(current=None, length=5, steps=1) == 0

    def test_from_a_stopped_queue_previous_is_the_last_entry(self) -> None:
        assert entry_after(current=None, length=5, steps=-1) == 4

    def test_an_empty_queue_has_nowhere_to_go(self) -> None:
        assert entry_after(current=None, length=0, steps=1) is None


class TestRanOut:
    """Stopped with a queue still loaded is what the natural end of a ``stop`` channel leaves.

    A restarted MPD that lost its queue is stopped with NOTHING loaded, and that one must not
    read as an end: it would throw away a real place in a book with nothing to report.
    """

    def test_stopped_with_a_queue_ran_out(self) -> None:
        assert MpdStatus(state=MpdState.STOP, playlist_length=5).ran_out()

    def test_stopped_with_an_empty_queue_did_not(self) -> None:
        assert not MpdStatus(state=MpdState.STOP, playlist_length=0).ran_out()

    def test_playing_did_not(self) -> None:
        assert not MpdStatus(state=MpdState.PLAY, song=4, elapsed=1.0, playlist_length=5).ran_out()
