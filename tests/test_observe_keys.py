"""The observer's frame parser and its empty run, against frames a real speaker sent.

Reaching a real speaker needs the flat and is what the measurement run itself does. The two
frames here are copied out of the 2026-09-05 capture, because a parser tested against retyped
text is tested against the wrong bytes; only the two device ids are swapped for placeholders of
the same width and alphabet, which the parser reads the same way.

The run tests point at 192.0.2.1, the reserved documentation address, so they reach no speaker in
the house and depend on no port being free on this machine. What they hold is the shape of a run
that recorded nothing: the file exists, the report counts zero, and the exit code is 1 rather than
0 or 2. Those three are the CLI contract, and a contract nothing checks is a contract that drifts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))
from observe_keys import main, observe, parse_frame

if TYPE_CHECKING:
    import pytest

UNREACHABLE = "192.0.2.1"
"""TEST-NET-1 (RFC 5737). Reserved for documentation, so it is routed nowhere and owned by nobody."""

SELECTION = (
    '<updates deviceID="AABBCC0000A1"><nowSelectionUpdated><preset id="2">'
    '<ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" location="http://x/y" />'
    "</preset></nowSelectionUpdated></updates>"
)
ACTIVITY = '<userActivityUpdate deviceID="AABBCC0000A3" />'


def test_a_selection_frame_yields_its_preset_number() -> None:
    event = parse_frame("192.168.0.31", SELECTION, 1788641085.4676714)
    assert event.device_id == "AABBCC0000A1"
    assert event.kind == "nowSelectionUpdated"
    assert event.preset_id == 2
    assert event.received_at == 1788641085.4676714


def test_a_frame_with_no_preset_yields_none_rather_than_a_guess() -> None:
    event = parse_frame("192.168.0.33", ACTIVITY, 1.0)
    assert event.device_id == "AABBCC0000A3"
    assert event.kind == "userActivityUpdate"
    assert event.preset_id is None


def test_an_unparseable_frame_is_kept_whole_rather_than_dropped() -> None:
    event = parse_frame("192.168.0.31", "not xml at all", 2.0)
    assert event.kind == "unknown"
    assert event.preset_id is None
    assert event.frame == "not xml at all"


async def test_a_run_that_reaches_nobody_still_leaves_a_file_and_a_zero_report(tmp_path: Path) -> None:
    """It has to end when the time is up, not when a speaker answers, or a run could never stop."""
    out = tmp_path / "nested" / "keys.jsonl"
    report = await observe([UNREACHABLE], 0.2, out)
    assert out.exists(), "the directory is made and the file opened before anything connects"
    assert out.read_text(encoding="utf-8") == ""
    assert report.frames == {UNREACHABLE: 0}
    assert report.total == 0


def test_recording_nothing_exits_one_with_an_envelope_on_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 1 is "it ran and the answer is no"; 2 would say it could not run, which is not true."""
    out = tmp_path / "keys.jsonl"
    rc = main(["--speaker", UNREACHABLE, "--seconds", "0.2", "--out", str(out), "--json-bare"])
    captured = capsys.readouterr()
    assert rc == 1
    envelope = json.loads(captured.out)
    assert envelope["ok"] is False
    assert envelope["command"] == "observe_keys"
    assert envelope["data"]["total"] == 0
    assert captured.out.count("\n") == 1, "--json-bare is one line, so jq can read a stream of them"
