"""The zone timeline: where the playing slaves are in a stream, from their own reports.

A slave reports PLAYING once a second with the bytes its decoder has consumed since ITS first
byte. The master knows which absolute byte that first one was, so every report is a point
(master time, absolute byte). A joiner is started at the byte the zone will reach a few seconds
later, which is a short extrapolation from the latest point at the measured rate.
"""

from __future__ import annotations

import pytest

from soundtouch_zonemaster.domain.timeline import FrameTimeline, ZoneTimeline

S = 1_000_000  # microseconds


def test_no_reports_means_no_position_and_not_playing() -> None:
    tl = ZoneTimeline(t0_us=100 * S)
    assert tl.byte_at(103 * S) is None
    assert tl.rate() is None
    assert not tl.is_playing(now_us=103 * S)


def test_one_report_gives_no_rate_yet() -> None:
    tl = ZoneTimeline(t0_us=100 * S)
    tl.add_report(t_us=101 * S, absolute_byte=16000)
    assert tl.rate() is None
    assert tl.byte_at(104 * S) is None


def test_two_reports_give_the_rate_and_the_extrapolated_byte() -> None:
    tl = ZoneTimeline(t0_us=100 * S)
    tl.add_report(t_us=101 * S, absolute_byte=16000)
    tl.add_report(t_us=102 * S, absolute_byte=32000)
    assert tl.rate() == pytest.approx(16000.0)
    assert tl.byte_at(105 * S) == 32000 + 3 * 16000


def test_the_rate_uses_a_window_of_recent_reports_not_the_first_one() -> None:
    tl = ZoneTimeline(t0_us=0, window=4)
    for i in range(1, 11):
        tl.add_report(t_us=i * S, absolute_byte=i * 10000)  # 10 kB/s early on
    for i in range(11, 21):
        tl.add_report(t_us=i * S, absolute_byte=100000 + (i - 10) * 20000)  # 20 kB/s lately
    assert tl.rate() == pytest.approx(20000.0)


def test_is_playing_only_while_a_report_is_fresh() -> None:
    tl = ZoneTimeline(t0_us=0)
    tl.add_report(t_us=10 * S, absolute_byte=1)
    assert tl.is_playing(now_us=12 * S)
    assert not tl.is_playing(now_us=12 * S, max_age_us=1 * S)


def test_reports_from_a_slave_running_backwards_are_ignored() -> None:
    # A slave that restarts its stream counts from 0 again; a lower absolute byte than the last
    # one is a reset, not the zone moving backwards.
    tl = ZoneTimeline(t0_us=0)
    tl.add_report(t_us=1 * S, absolute_byte=16000)
    tl.add_report(t_us=2 * S, absolute_byte=32000)
    tl.add_report(t_us=3 * S, absolute_byte=500)
    assert tl.byte_at(3 * S) == 48000


def test_byte_at_is_the_rendered_byte_three_frames_behind_the_decoder_read_position() -> None:
    # A slave's byte_offset is what its decoder has consumed; it renders three frames behind that
    # (every render start in the S3 capture reports frame_offset=3). Reports carry the frame count,
    # so bytes per frame come from the reports themselves, whatever the codec or rate.
    tl = ZoneTimeline(t0_us=100 * S)
    tl.add_report(t_us=101 * S, absolute_byte=16000, frame_offset=40)
    tl.add_report(t_us=102 * S, absolute_byte=32000, frame_offset=80)
    assert tl.bytes_per_frame() == pytest.approx(400.0)
    assert tl.read_ahead_bytes() == pytest.approx(1200.0)
    assert tl.byte_at(105 * S) == 32000 + 3 * 16000 - 1200


def test_without_frame_counts_the_read_ahead_is_unknown_and_byte_at_is_the_read_position() -> None:
    tl = ZoneTimeline(t0_us=100 * S)
    tl.add_report(t_us=101 * S, absolute_byte=16000)
    tl.add_report(t_us=102 * S, absolute_byte=32000)
    assert tl.bytes_per_frame() is None
    assert tl.read_ahead_bytes() is None
    assert tl.byte_at(105 * S) == 32000 + 3 * 16000


# --- the zone's position in frames ----------------------------------------------------------------


def test_frame_timeline_is_a_straight_line_from_the_stream_s_start() -> None:
    # A slave renders its first decoded frame at its PLAY time and one every frame duration after
    # it, on its clock (which is the master's). The zone's position is therefore the stream's t0
    # plus frames elapsed, and nothing has to be estimated from the counters.
    tl = FrameTimeline(duration_us=46440, t0_us=100 * S, frame0=0)
    assert tl.frame_at(100 * S) == 0
    assert tl.frame_at(105 * S) == pytest.approx(5 * S / 46440)
    assert tl.time_of_frame(200) == 100 * S + 200 * 46440


def test_frame_timeline_moves_with_the_frames_the_first_slave_discarded() -> None:
    # The first slave discarded two whole frames after its first frame start (a bit-reservoir
    # dependency, seen on TechnikumCity), so what it renders at t0 is frame 2, not frame 0.
    tl = FrameTimeline(duration_us=26122, t0_us=100 * S, frame0=0)
    tl.frame0 = 2
    assert tl.frame_at(100 * S) == 2
    assert tl.time_of_frame(2) == 100 * S
