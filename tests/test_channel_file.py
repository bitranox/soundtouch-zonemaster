"""The channel file, and the one atomic write both files it and the state file go through.

The rule this file exists to pin is the one place the channel file deliberately behaves UNLIKE the
state file: an unusable channel file REFUSES rather than starting empty. They are not the same kind
of file. The state - the current channel and the membership - can be re-derived by asking the
speakers, so starting empty costs one reconcile. The channel list is the only copy of something a
person built, and starting empty would let the very next save overwrite it with nothing.

A service that will not start is a service somebody fixes. A service that silently empties the
house's channels is one nobody notices until the room goes quiet.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from soundtouch_zonemaster.adapters.files.atomicfile import write_atomic
from soundtouch_zonemaster.adapters.files.channel_file import (
    ChannelFileError,
    load_channels,
    save_channels,
)
from soundtouch_zonemaster.domain.channellist import Channel, ChannelList
from soundtouch_zonemaster.domain.enums import ChannelEnd, ChannelKind

if TYPE_CHECKING:
    from pathlib import Path

    from soundtouch_zonemaster.domain.logfn import LogFn


def _log_to(lines: list[tuple[str, str]]) -> LogFn:
    """The log callable the module is handed, collecting into a list a test can assert on."""

    def log(kind: str, text: str) -> None:
        lines.append((kind, text))

    return log


def _radio(number: str, name: str = "Station") -> Channel:
    return Channel(number=number, name=name, kind=ChannelKind.RADIO, url=f"http://192.0.2.1/{number}")


class TestLoading:
    def test_a_missing_file_is_an_empty_list_and_not_an_error(self, tmp_path: Path) -> None:
        """First start: there is no file yet, and the seeding is what fills it."""
        lines: list[tuple[str, str]] = []
        assert load_channels(tmp_path / "none.json", log=_log_to(lines)).channels == ()

    def test_a_saved_list_comes_back_the_same(self, tmp_path: Path) -> None:
        path = tmp_path / "channels.json"
        have = ChannelList(channels=(_radio("1", "Superfly"), _radio("11", "Technikum")))
        save_channels(path, have)
        assert load_channels(path, log=_log_to([])) == have

    def test_an_unusable_file_refuses_rather_than_starting_empty(self, tmp_path: Path) -> None:
        """The decision this module turns on. Starting empty would let the next save erase it."""
        path = tmp_path / "channels.json"
        path.write_text("{ this is not json", encoding="utf-8")
        with pytest.raises(ChannelFileError) as caught:
            load_channels(path, log=_log_to([]))
        assert str(path) in str(caught.value)

    def test_a_number_no_key_can_press_is_refused_on_load(self, tmp_path: Path) -> None:
        """A person edits this file by hand, so 7 gets typed. It must not become a channel
        nobody can reach."""
        path = tmp_path / "channels.json"
        document = {"channels": [{"number": "7", "name": "unreachable", "kind": "radio", "url": "http://x/"}]}
        path.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(ChannelFileError):
            load_channels(path, log=_log_to([]))

    def test_an_mpd_channel_with_no_playlist_is_refused_on_load(self, tmp_path: Path) -> None:
        """The same pairing rule the record enforces, at the boundary a hand editor reaches.

        It refuses the WHOLE file rather than dropping the one channel, like every other problem
        here: this file is the only copy of something a person built.
        """
        path = tmp_path / "channels.json"
        document = {"channels": [{"number": "3", "name": "leer", "kind": "mpd", "url": "http://127.0.0.1:8000"}]}
        path.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(ChannelFileError):
            load_channels(path, log=_log_to([]))

    def test_an_mpd_channel_survives_the_round_trip_with_its_playlist(self, tmp_path: Path) -> None:
        """A field the mapping loses is SILENT: nothing raises, the channel just comes back different.

        ``of`` and ``to_channel`` derive their fields rather than listing them, so the loss this
        guards against is the document model lacking a field the record has - pydantic ignores an
        unknown key on the way in, and the default comes back in its place.
        """
        path = tmp_path / "channels.json"
        have = ChannelList(
            channels=(
                Channel(
                    number="3",
                    name="Hoerbuecher",
                    kind=ChannelKind.MPD,
                    url="http://127.0.0.1:8000",
                    mpd_entry="hoerbuecher",
                ),
            )
        )
        save_channels(path, have)
        assert load_channels(path, log=_log_to([])) == have

    def test_a_channel_that_stops_at_its_end_survives_the_round_trip(self, tmp_path: Path) -> None:
        """The same silent-drop risk as the playlist: a field the mapping forgets comes back as
        the default, and a book that should stop starts over instead."""
        path = tmp_path / "channels.json"
        have = ChannelList(
            channels=(
                Channel(
                    number="3",
                    name="Buch",
                    kind=ChannelKind.MPD,
                    url="http://127.0.0.1:8000",
                    mpd_entry="buch",
                    end=ChannelEnd.STOP,
                ),
            )
        )
        save_channels(path, have)
        assert load_channels(path, log=_log_to([])) == have

    def test_a_directory_channel_survives_the_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "channels.json"
        have = ChannelList(
            channels=(
                Channel(
                    number="21",
                    name="Book Two",
                    kind=ChannelKind.MPD,
                    url="http://127.0.0.1:8000",
                    mpd_directory="audiobooks/Author Two/Book Two",
                    end=ChannelEnd.STOP,
                ),
            )
        )
        save_channels(path, have)
        assert load_channels(path, log=_log_to([])) == have

    @pytest.mark.parametrize(
        ("channel", "reason"),
        [
            (
                {
                    "number": "3",
                    "name": "beides",
                    "kind": "mpd",
                    "url": "http://127.0.0.1:8000",
                    "mpd_entry": "b",
                    "mpd_directory": "audiobooks/B",
                },
                "names a playlist and a directory",
            ),
            (
                {"number": "1", "name": "OE3", "kind": "radio", "url": "http://x/", "mpd_directory": "a"},
                "carries an MPD directory",
            ),
            (
                {"number": "3", "name": "weg", "kind": "mpd", "url": "http://x/", "mpd_directory": "../etc"},
                "under mpd's music directory",
            ),
        ],
        ids=["both a playlist and a directory", "a station with a directory", "a directory outside the tree"],
    )
    def test_a_directory_the_channel_cannot_have_is_refused_on_load_and_says_why(
        self, tmp_path: Path, channel: dict[str, str], reason: str
    ) -> None:
        """The record's own rule at the boundary, with its own words in the chained cause."""
        path = tmp_path / "channels.json"
        path.write_text(json.dumps({"channels": [channel]}), encoding="utf-8")
        with pytest.raises(ChannelFileError, match=r"\(1 problem\(s\)\)") as caught:
            load_channels(path, log=_log_to([]))
        assert reason in str(caught.value.__cause__)

    def test_a_channel_written_before_the_end_field_existed_wraps(self, tmp_path: Path) -> None:
        """Every file in a house today has no ``end``, and loading it must not refuse."""
        path = tmp_path / "channels.json"
        document = {
            "channels": [
                {"number": "3", "name": "Musik", "kind": "mpd", "url": "http://127.0.0.1:8000", "mpd_entry": "musik"}
            ]
        }
        path.write_text(json.dumps(document), encoding="utf-8")
        loaded = load_channels(path, log=_log_to([]))
        assert loaded.channels[0].end is ChannelEnd.WRAP

    @pytest.mark.parametrize(
        "channel",
        [
            {
                "number": "3",
                "name": "Buch",
                "kind": "mpd",
                "url": "http://127.0.0.1:8000",
                "mpd_entry": "b",
                "end": "halt",
            },
            {"number": "1", "name": "OE3", "kind": "radio", "url": "http://x/", "end": "stop"},
        ],
        ids=["an end that is no word the file knows", "a station that says it stops"],
    )
    def test_an_end_the_channel_cannot_have_is_refused_on_load(self, tmp_path: Path, channel: dict[str, str]) -> None:
        path = tmp_path / "channels.json"
        path.write_text(json.dumps({"channels": [channel]}), encoding="utf-8")
        with pytest.raises(ChannelFileError):
            load_channels(path, log=_log_to([]))

    def test_an_unreadable_file_refuses_too(self, tmp_path: Path) -> None:
        """A directory where the file should be is the readable case of "it is not a file"."""
        path = tmp_path / "channels.json"
        path.mkdir()
        with pytest.raises(ChannelFileError):
            load_channels(path, log=_log_to([]))

    def test_bytes_that_are_not_utf_8_refuse_by_name_rather_than_by_traceback(self, tmp_path: Path) -> None:
        """Refusing is already right here; refusing WITHOUT naming the file is not.

        This is the only unreadable-file case that escaped as a raw UnicodeDecodeError, so the one
        person who has to repair the list got a traceback that never says which path it was.
        """
        path = tmp_path / "channels.json"
        path.write_bytes(b'{"channels": "\xff"}')

        with pytest.raises(ChannelFileError) as caught:
            load_channels(path, log=_log_to([]))

        assert str(caught.value) == f"{path}: could not be read (UnicodeDecodeError)"

    def test_a_file_written_with_a_byte_order_mark_loads(self, tmp_path: Path) -> None:
        """Windows writes "UTF-8 with BOM", and this file is edited over SMB from there.

        The three bytes are invisible to whoever opened the file, so reading them as part of the
        document refuses a list that is in fact perfectly good - and this is the file a person is
        most likely to have edited by hand from there, because it is the one they built.
        """
        path = tmp_path / "channels.json"
        have = ChannelList(channels=(_radio("1", "Superfly"),))
        save_channels(path, have)
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
        lines: list[tuple[str, str]] = []

        assert load_channels(path, log=_log_to(lines)) == have
        assert lines == [("channels", f"{path}: 1 channel(s)")]


class TestTheAtomicWrite:
    def test_it_replaces_an_existing_file_completely(self, tmp_path: Path) -> None:
        path = tmp_path / "f.txt"
        write_atomic(path, "a much longer first document\n")
        write_atomic(path, "short\n")
        assert path.read_text(encoding="utf-8") == "short\n"

    def test_it_leaves_no_temporary_file_behind(self, tmp_path: Path) -> None:
        write_atomic(tmp_path / "f.txt", "x\n")
        assert [p.name for p in tmp_path.iterdir()] == ["f.txt"]

    def test_a_write_that_cannot_start_leaves_the_old_document_untouched(self, tmp_path: Path) -> None:
        """The property the rename is FOR: a reader sees the old document or the new one.

        A directory sitting where the temporary file goes makes the write fail at its first step,
        which is a real filesystem state rather than a patched-out function.
        """
        path = tmp_path / "f.txt"
        write_atomic(path, "the good document\n")
        (tmp_path / "f.txt.tmp").mkdir()
        with pytest.raises(OSError):
            write_atomic(path, "the document that never lands\n")
        assert path.read_text(encoding="utf-8") == "the good document\n"


class TestSavingChannels:
    def test_saving_over_a_list_replaces_it_rather_than_merging(self, tmp_path: Path) -> None:
        path = tmp_path / "channels.json"
        save_channels(path, ChannelList(channels=(_radio("1"), _radio("2"))))
        save_channels(path, ChannelList(channels=(_radio("3"),)))
        assert load_channels(path, log=_log_to([])).numbers_in_order() == ("3",)

    def test_the_file_a_person_opens_is_readable_json(self, tmp_path: Path) -> None:
        """It is a file a person is expected to repair, so it is indented and ends in a newline."""
        path = tmp_path / "channels.json"
        save_channels(path, ChannelList(channels=(_radio("1"),)))
        written = path.read_text(encoding="utf-8")
        assert written.endswith("\n")
        assert "\n  " in written
        assert json.loads(written)["channels"][0]["number"] == "1"
