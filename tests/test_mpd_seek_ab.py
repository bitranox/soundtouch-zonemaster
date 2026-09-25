"""The spare MPD of research/mpd_seek_ab.py: it must never share anything of the house's but the music.

The A/B runs on a container host next to the house's MPD. What makes that safe is the
configuration it writes, so that is what is pinned here: the music and playlists come from the live
file, and everything that could collide with the house or write into its state is the spare's own.
The run itself starts mpd and is measured on the machine, not here.
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))

import mpd_seek_ab as ab
import pytest
from _settings import MpdAbSettings

LIVE = """music_directory                 "/srv/music"
playlist_directory              "/srv/music/playlists"
db_file                         "/var/lib/mpd/tag_cache"
state_file                      "/var/lib/mpd/state"
sticker_file                    "/var/lib/mpd/sticker.sql"
user                            "mpd"
bind_to_address                 "127.0.0.1"
port                            "6600"
auto_update                     "yes"
filesystem_charset              "UTF-8"
audio_output {
        type                    "httpd"
        port                    "8100"
}
include_optional "mpd_local.conf"
"""


def _spare(tmp_path: Path) -> str:
    return ab.build_config(live_conf=LIVE, workdir=tmp_path, control_port=6611, httpd_port=8111)


def test_the_spare_reads_the_house_s_music_and_playlists(tmp_path: Path) -> None:
    conf = _spare(tmp_path)
    assert 'music_directory                 "/srv/music"' in conf
    assert 'playlist_directory              "/srv/music/playlists"' in conf


def test_the_spare_names_none_of_the_house_s_ports_files_or_user(tmp_path: Path) -> None:
    conf = _spare(tmp_path)
    for house_only in ('"6600"', '"8100"', "/var/lib/mpd/", "sticker_file", "user ", "mpd_local.conf"):
        assert house_only not in conf, house_only
    assert f'db_file "{tmp_path / "tag_cache"}"' in conf
    assert f'state_file "{tmp_path / "state"}"' in conf
    assert 'port "6611"' in conf
    assert 'port "8111"' in conf


def test_the_spare_never_rescans_and_binds_both_ports_to_loopback(tmp_path: Path) -> None:
    conf = _spare(tmp_path)
    assert 'auto_update "no"' in conf
    assert conf.count('bind_to_address "127.0.0.1"') == 2, "control port AND httpd output"


def test_a_verdict_is_the_signal_that_ended_the_process() -> None:
    assert ab.verdict_of(None) is ab.Verdict.SURVIVED
    assert ab.verdict_of(-signal.SIGABRT) is ab.Verdict.ABORTED
    assert ab.verdict_of(-signal.SIGSEGV) is ab.Verdict.SEGFAULTED
    assert ab.verdict_of(0) is ab.Verdict.OTHER, "an exit is not survival: the arm wanted it running"


def test_the_live_files_come_from_the_settings_not_from_the_code(tmp_path: Path) -> None:
    """A run reads the live MPD's files where ``[mpd_ab]`` says, and stops before starting mpd when
    they are not there: the tag database is copied first, so the error names the configured path."""
    live = MpdAbSettings(live_conf=tmp_path / "absent.conf", live_db=tmp_path / "absent_tag_cache")
    with pytest.raises(FileNotFoundError, match="absent_tag_cache"):
        ab.run(rounds=1, idle_s=0.0, live=live)
