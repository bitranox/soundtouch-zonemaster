"""What research/golden_check.py needs before it can replay anything, and how it says what is missing.

The replay itself needs the gitignored firmware and captures, so it runs by hand, not here. What
can run anywhere is the decision in front of it: which capture it would replay comes from the
``[golden]`` settings, and a checkout that has not named one must be told WHICH setting to fill
in, with exit 2, rather than pass with nothing compared or fail on a path that was never chosen.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))
from _settings import GoldenSettings
from golden_check import missing_inputs

UNSET = GoldenSettings(capture="", pcap="", master="", slave="")
NAMED = GoldenSettings(capture="captures/run-1", pcap="192.0.2.33.pcap", master="192.0.2.33", slave="192.0.2.31")


@pytest.mark.os_agnostic
def test_an_unconfigured_checkout_is_told_which_settings_to_fill_in(tmp_path: Path) -> None:
    absent = missing_inputs(tmp_path, UNSET)
    assert absent[:4] == [
        "setting golden.capture",
        "setting golden.pcap",
        "setting golden.master",
        "setting golden.slave",
    ]


@pytest.mark.os_agnostic
def test_a_named_capture_that_is_not_on_disk_is_named_by_path(tmp_path: Path) -> None:
    absent = missing_inputs(tmp_path, NAMED)
    capture = tmp_path / "research" / "captures" / "run-1"
    assert str(capture / "192.0.2.33.pcap") in absent
    assert str(capture / "analysis-master-side.md") in absent
    assert not [a for a in absent if a.startswith("setting ")]


@pytest.mark.os_agnostic
def test_nothing_is_missing_once_the_capture_and_firmware_are_there(tmp_path: Path) -> None:
    research = tmp_path / "research"
    capture = research / "captures" / "run-1"
    capture.mkdir(parents=True)
    for name in ("192.0.2.33.pcap", "analysis-master-side.md", "analysis-late-join.md"):
        (capture / name).write_bytes(b"")
    (research / "firmware").mkdir()
    import golden_check

    for name in golden_check.FIRMWARE:
        (research / "firmware" / name).write_bytes(b"")
    assert missing_inputs(tmp_path, NAMED) == []
