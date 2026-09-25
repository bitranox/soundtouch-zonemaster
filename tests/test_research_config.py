"""The research scripts' settings: public defaults in the tree, a house's own values beside them.

``research/_settings.py`` reads ``research/defaultconfig.toml`` and every ``*.toml`` in
``research/defaultconfig.d/`` through ``lib_layered_config``. The tracked files carry values that
are safe anywhere - no speaker refused, no capture named - and a house puts its own in a
gitignored ``9N-<scope>-rnhome.toml`` beside them, which sorts after every tracked file and so
wins.

Every test here reads a COPY of the tracked tree in a temporary directory, never the checkout
itself: the checkout may hold this developer's ``-rnhome`` files, and a test that read them would
pass or fail by whose machine it ran on.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))
import _settings as rc

RESEARCH = Path(__file__).resolve().parents[1] / "research"


def _tracked_tree(into: Path) -> Path:
    """Copy the tracked defaults (header plus every ``.d`` file that is not a house's) and return the header."""
    (into / "defaultconfig.d").mkdir(parents=True)
    shutil.copyfile(RESEARCH / "defaultconfig.toml", into / "defaultconfig.toml")
    for source in sorted((RESEARCH / "defaultconfig.d").iterdir()):
        if "-rnhome." in source.name and not source.name.endswith(".example"):
            continue
        shutil.copyfile(source, into / "defaultconfig.d" / source.name)
    return into / "defaultconfig.toml"


def _load(default_file: Path | None, tmp_path: Path) -> rc.Config:
    return rc.load(default_file=default_file, start_dir=tmp_path)


@pytest.mark.os_agnostic
def test_the_tracked_defaults_refuse_no_speaker_and_name_no_capture(tmp_path: Path) -> None:
    config = _load(_tracked_tree(tmp_path / "tree"), tmp_path)
    assert rc.capture_settings(config) == rc.CaptureSettings(never_touch=(), ssh_user="root")
    assert rc.golden_settings(config) == rc.GoldenSettings(capture="", pcap="", master="", slave="")
    assert rc.mpd_ab_settings(config) == rc.MpdAbSettings(
        live_conf=Path("/etc/mpd.conf"), live_db=Path("/var/lib/mpd/tag_cache")
    )


@pytest.mark.os_agnostic
def test_each_setting_has_one_default_the_tracked_file_mirrors(tmp_path: Path) -> None:
    """With no file at all the code's fallback answers, and it must be what the tracked file says."""
    bare = _load(None, tmp_path)
    tracked = _load(_tracked_tree(tmp_path / "tree"), tmp_path)
    assert rc.capture_settings(bare) == rc.capture_settings(tracked)
    assert rc.golden_settings(bare) == rc.golden_settings(tracked)
    assert rc.mpd_ab_settings(bare) == rc.mpd_ab_settings(tracked)


@pytest.mark.os_agnostic
def test_a_house_file_beside_the_defaults_overrides_them(tmp_path: Path) -> None:
    header = _tracked_tree(tmp_path / "tree")
    (header.parent / "defaultconfig.d" / "91-capture-rnhome.toml").write_text(
        '[capture]\nnever_touch = ["192.168.0.30"]\n', encoding="utf-8"
    )
    (header.parent / "defaultconfig.d" / "92-golden-rnhome.toml").write_text(
        '[golden]\ncapture = "captures/run-1"\npcap = "192.168.0.33.pcap"\n'
        'master = "192.168.0.33"\nslave = "192.168.0.31"\n',
        encoding="utf-8",
    )
    config = _load(header, tmp_path)
    assert rc.capture_settings(config) == rc.CaptureSettings(never_touch=("192.168.0.30",), ssh_user="root")
    assert rc.golden_settings(config).capture == "captures/run-1"
    assert rc.golden_settings(config).slave == "192.168.0.31"


@pytest.mark.os_agnostic
def test_an_example_file_is_documentation_and_never_read(tmp_path: Path) -> None:
    """The ``.example`` files ship with house-shaped values; the loader must not take them as settings."""
    header = _tracked_tree(tmp_path / "tree")
    examples = sorted((header.parent / "defaultconfig.d").glob("*.example"))
    assert examples, "each scope ships a -rnhome.toml.example beside its default"
    (header.parent / "defaultconfig.d" / "99-planted-rnhome.toml.example").write_text(
        '[capture]\nnever_touch = ["192.168.0.99"]\n', encoding="utf-8"
    )
    assert rc.capture_settings(_load(header, tmp_path)).never_touch == ()


@pytest.mark.os_agnostic
def test_a_value_of_the_wrong_type_is_refused_by_name(tmp_path: Path) -> None:
    header = _tracked_tree(tmp_path / "tree")
    (header.parent / "defaultconfig.d" / "91-capture-rnhome.toml").write_text(
        '[capture]\nnever_touch = "192.168.0.30"\n', encoding="utf-8"
    )
    with pytest.raises(rc.SettingsError, match=r"capture\.never_touch"):
        rc.capture_settings(_load(header, tmp_path))


@pytest.mark.os_agnostic
def test_a_missing_header_file_falls_back_to_the_code_defaults(tmp_path: Path) -> None:
    """A script copied to another host without the tree beside it still runs, on the safe answer."""
    config = _load(tmp_path / "absent" / "defaultconfig.toml", tmp_path)
    assert rc.capture_settings(config).never_touch == ()
