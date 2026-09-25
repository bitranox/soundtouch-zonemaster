"""The summariser of the `sync` line, against lines a real run actually wrote.

The `sync` line is what a placement change is judged by before anybody listens (REPORT.md S7), so
the thing that reads it is an instrument and its failure mode matters more than its output. The
scripts this replaces failed the dangerous way: their regex had to match `placement.py` exactly,
and when it did not they printed nothing and exited 0 - indistinguishable from a run in which
every slave was perfectly placed.

So the fixtures are excerpts of real run logs rather than retyped lines, including the column
padding, and one of them is a real run that emitted no sync line at all. That one is the control:
if the refusal ever stops firing, this file says so.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from soundtouch_zonemaster.adapters.soundtouch.clock import now_us
from soundtouch_zonemaster.adapters.soundtouch.pb import audio
from soundtouch_zonemaster.adapters.soundtouch.placement import JoinSlot, StreamKey
from soundtouch_zonemaster.adapters.soundtouch.reports import SlaveState
from soundtouch_zonemaster.adapters.soundtouch.source import RingBuffer, StreamSource
from soundtouch_zonemaster.adapters.soundtouch.zone_master import ZoneMaster
from soundtouch_zonemaster.domain.station import Station
from soundtouch_zonemaster.domain.timeline import ZoneTimeline

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))
from sync_report import LineShape, build_report, main, parse_line, read_log

if TYPE_CHECKING:
    import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RUN_LOG = FIXTURES / "sync-run.log"
SILENT_LOG = FIXTURES / "sync-silent.log"
SPREAD_LOG = FIXTURES / "sync-spread.log"

AGAINST_THE_ZONE = "02:32:41.089 sync         192.168.0.31 renders +0 ms vs zone (frames; counter says -4 ms)"
AGAINST_A_PEER = "01:34:59.735 sync         192.168.0.34 renders -6 ms vs 192.168.0.31"


async def test_every_line_the_master_actually_emits_is_read_by_this_tool() -> None:
    """The contract test, because the fixtures alone cannot notice the emitter moving.

    Every line above is text typed into this file, so the emitter and the parser were pinned
    SEPARATELY and drifted apart with both green: ``placement.py`` gained a trailing ``(bytes)``
    on the peer-shaped line, and ``_AGAINST_A_PEER`` - written from an excerpt of a run older than
    that suffix - matched neither form the master emits. Every byte-placed run's lines were being
    dropped exactly the way an unrelated line is, which is the silent failure this tool exists to
    end. So this drives the REAL master and requires the REAL parser to read what it wrote.
    """
    src = StreamSource(Station(1, "http://x/live", "Test", "<ContentItem/>"), RingBuffer(), lambda _k, _t: None)
    await src.ring.append(bytes(300_000))
    t0 = now_us() - 30 * 1_000_000
    src.t0_us = t0
    src.timeline = ZoneTimeline(t0_us=t0)

    logs: list[str] = []
    master = ZoneMaster(bind_ip="127.0.0.1", device_id="5EB0CE000001", log=lambda k, t: logs.append(f"{k:12s} {t}"))
    master.sources[1] = src
    master.planner.assign(StreamKey("a", 1), JoinSlot(t0, 0))
    master.planner.assign(StreamKey("b", 1), JoinSlot(t0 + 10 * 1_000_000, 160_000))
    playing = audio.AudioServerMsgServerState.PLAYING
    # No frame_offset anywhere: that is what puts placement on the byte path, which is every run's
    # opening seconds and the whole of a run whose boxes never send trackData.
    master.on_slave_state(SlaveState(peer="a", url_id=1, state=playing, milliseconds=20_000, byte_offset=320_000))
    master.on_slave_state(SlaveState(peer="a", url_id=1, state=playing, milliseconds=21_000, byte_offset=336_000))
    master.on_slave_state(SlaveState(peer="b", url_id=1, state=playing, milliseconds=11_000, byte_offset=176_800))

    emitted = [f"01:34:59.735 {line}" for line in logs if line.startswith("sync ")]
    assert emitted, "the master emitted no sync line, so this test asserts nothing"
    for line in emitted:
        assert parse_line(line) is not None, f"the master emits a shape this tool cannot read: {line!r}"


def test_the_current_line_shape_yields_both_columns() -> None:
    sample = parse_line(AGAINST_THE_ZONE)
    assert sample is not None
    assert sample.peer == "192.168.0.31"
    assert sample.clock_ms == 0
    assert sample.counter_ms == -4
    assert sample.shape is LineShape.AGAINST_THE_ZONE


def test_the_older_line_shape_yields_no_counter_column() -> None:
    """The byte-plan runs compared two slaves and carried no cross-check; None, never 0."""
    sample = parse_line(AGAINST_A_PEER)
    assert sample is not None
    assert sample.peer == "192.168.0.34"
    assert sample.clock_ms == -6
    assert sample.counter_ms is None
    assert sample.shape is LineShape.AGAINST_A_PEER


def test_a_line_that_is_not_a_sync_line_is_not_a_sample() -> None:
    assert parse_line("02:32:36.118 source       playback descriptor -> https://example.invalid/x") is None


def test_a_sync_line_whose_wording_changed_is_not_read_as_a_sample() -> None:
    """The whole point of the refusal: a changed line must go unmatched, not half-matched.

    If this ever starts returning a sample, the tool has grown a tolerance that would let a real
    format change through while reporting numbers - which is the failure it exists to prevent.
    """
    assert parse_line("02:32:41.089 sync         192.168.0.31 is +0 ms from the zone") is None


def test_the_fixture_summarises_to_the_two_slaves_that_wrote_it() -> None:
    summary = read_log(RUN_LOG)
    assert [peer.peer for peer in summary.peers] == ["192.168.0.31", "192.168.0.34"]
    assert summary.shapes == [LineShape.AGAINST_THE_ZONE]
    assert summary.samples == sum(peer.samples for peer in summary.peers)
    for peer in summary.peers:
        assert peer.median_ms == 0, "the sign-off run placed both boxes exactly on the zone"
        assert peer.min_ms == 0 and peer.max_ms == 0
        assert peer.counter_median_ms is not None, "the current shape carries the cross-check"


def test_a_log_with_no_sync_line_is_named_rather_than_summarised_away() -> None:
    """A real run that emitted none. Zero samples has to leave a trace a caller can act on."""
    report = build_report([SILENT_LOG])
    assert report.logs[0].samples == 0
    assert report.logs[0].peers == []
    assert report.silent == [str(SILENT_LOG)]


def test_a_silent_log_exits_one_with_an_envelope_that_names_it(capsys: pytest.CaptureFixture[str]) -> None:
    """Exit 1 is "it ran and the answer is no". The old scripts exited 0 here, saying nothing."""
    rc = main(["--log", str(SILENT_LOG), "--json-bare"])
    captured = capsys.readouterr()
    assert rc == 1
    envelope = json.loads(captured.out)
    assert envelope["ok"] is False
    assert envelope["command"] == "sync_report"
    assert envelope["data"]["silent"] == [str(SILENT_LOG)]
    assert captured.out.count("\n") == 1, "--json-bare is one line, so jq can read a stream of them"


def test_a_readable_run_exits_zero_with_the_numbers_in_the_envelope(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--log", str(RUN_LOG), "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    envelope = json.loads(captured.out)
    assert envelope["ok"] is True
    peers = envelope["data"]["logs"][0]["peers"]
    assert [peer["peer"] for peer in peers] == ["192.168.0.31", "192.168.0.34"]
    assert all(peer["median_ms"] == 0 for peer in peers)


def test_one_silent_log_among_readable_ones_still_refuses(capsys: pytest.CaptureFixture[str]) -> None:
    """A batch must not average a silence away: the run that cannot answer decides the exit code."""
    rc = main(["--log", str(RUN_LOG), "--log", str(SILENT_LOG), "--json-bare"])
    envelope = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert len(envelope["data"]["logs"]) == 2, "the readable one is still reported"
    assert envelope["data"]["silent"] == [str(SILENT_LOG)]


def test_the_prose_form_names_every_slave_and_warns_on_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--log", str(RUN_LOG), "--log", str(SILENT_LOG)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "192.168.0.31: n=" in captured.out
    assert "NO sync line" in captured.err, "a warning belongs on stderr, never in the parsed stream"


def test_a_log_that_is_not_there_exits_two_with_the_envelope_every_tool_here_prints(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two is "it could not run", which is a different answer from "it ran and found nothing".

    The envelope is read as well as the code, because asserting the code alone is what let this
    tool's refusal ship without the ``ok`` field the other CLIs all carry: a caller branching on
    ``payload["ok"]`` got a KeyError from the one path it was looking at the field to detect.
    """
    assert main(["--log", str(tmp_path / "absent.log"), "--json-bare"]) == 2
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is False, "the refusal must be readable the same way as every other tool's"
    assert envelope["command"] == "sync_report"
    assert envelope["error"] == "FileNotFoundError", "the class name, so a caller can branch on it"


def test_the_five_numbers_are_the_five_numbers_and_not_four_of_them() -> None:
    """A spread the sign-off fixture cannot provide, because it is zero on every sample.

    The expected values are read off the fixture's own lines, not off this tool's output: the
    subject slave's five samples are +98, +75, +71, +101, +98, so sorted they are 71, 75, 98, 98,
    101. Median 98 and mean 88.6 differ, and so do minimum 71 and maximum 101, which is what makes
    these assertions able to fail.
    """
    summary = read_log(SPREAD_LOG)
    assert summary.shapes == [LineShape.AGAINST_A_PEER], "the older shape, read end to end"
    subject = next(peer for peer in summary.peers if peer.peer == "192.168.0.34")
    assert subject.samples == 5
    assert subject.median_ms == 98, "the median, not the mean, which would be 88.6"
    assert subject.min_ms == 71
    assert subject.max_ms == 101
    assert subject.counter_median_ms is None, "this shape carries no cross-check column"


def test_samples_are_grouped_by_the_slave_they_are_about() -> None:
    """The other slave in the same fixture reads negative where the first reads positive."""
    summary = read_log(SPREAD_LOG)
    other = next(peer for peer in summary.peers if peer.peer == "192.168.0.31")
    assert other.samples == 3
    assert other.median_ms == -91
    assert other.min_ms == -93
    assert other.max_ms == -90
