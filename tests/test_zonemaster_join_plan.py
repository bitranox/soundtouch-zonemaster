"""The master's join plan: what PLAY time and first byte a slave gets, per zone state.

Pure logic on a source whose ring is pre-filled; no network, no speakers.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from soundtouch_zonemaster.adapters.soundtouch.clock import now_us
from soundtouch_zonemaster.adapters.soundtouch.pb import audio
from soundtouch_zonemaster.adapters.soundtouch.placement import RESTART_PREROLL_BYTES, JoinSlot, StreamKey
from soundtouch_zonemaster.adapters.soundtouch.reports import SlaveState
from soundtouch_zonemaster.adapters.soundtouch.source import RingBuffer, StreamSource
from soundtouch_zonemaster.adapters.soundtouch.zone_master import ZoneMaster
from soundtouch_zonemaster.domain.frames import find_frame
from soundtouch_zonemaster.domain.station import Station
from soundtouch_zonemaster.domain.timeline import JOIN_LEAD_US, ZoneTimeline

pytestmark = pytest.mark.asyncio
S = 1_000_000


def _byte_of(src: StreamSource, frame: int) -> int:
    """Where a frame starts; the fixtures always hold enough of the stream for it."""
    byte = src.frames.byte_of(frame)
    assert byte is not None, f"frame {frame} is not in the ring"
    return byte


def _t0(src: StreamSource) -> int:
    """The source's start time; every fixture here sets it."""
    assert src.t0_us is not None
    return src.t0_us


async def _source(ring_bytes: int, *, t0_us: int) -> StreamSource:
    src = StreamSource(Station(1, "http://x/live", "Test", "<ContentItem/>"), RingBuffer(), lambda k, t: None)
    await src.ring.append(bytes(ring_bytes))
    src.t0_us = t0_us
    src.timeline = ZoneTimeline(t0_us=t0_us)
    return src


def _master() -> ZoneMaster:
    return ZoneMaster(bind_ip="127.0.0.1", device_id="5EB0CE000001", log=lambda k, t: None)


async def test_a_slave_joining_before_the_stream_starts_gets_byte_0_at_the_stream_t0():
    src = await _source(300_000, t0_us=now_us() + 2 * S)
    slot = await _master().plan_join("a", src)
    assert (slot.t0_us, slot.base_offset) == (src.t0_us, 0)


async def test_nobody_playing_restarts_the_zone_behind_the_live_edge_at_a_fresh_t0():
    src = await _source(300_000, t0_us=now_us() - 30 * S)
    before = now_us()
    slot = await _master().plan_join("a", src)
    assert slot.base_offset == 300_000 - RESTART_PREROLL_BYTES
    assert before + JOIN_LEAD_US <= slot.t0_us <= now_us() + JOIN_LEAD_US
    assert src.t0_us == slot.t0_us, "the stream's t0 now names the restart"


async def test_a_slave_joining_right_after_a_restart_shares_the_restart_not_byte_0():
    src = await _source(300_000, t0_us=now_us() - 30 * S)
    m = _master()
    first = await m.plan_join("a", src)
    second = await m.plan_join("b", src)
    assert second == first


async def test_a_joiner_whose_byte_is_not_in_the_ring_yet_waits_for_it_rather_than_restarting_the_zone():
    # The zone plays at 16 kB/s and is 1 s behind the live edge; the joiner's byte, 3 s ahead of the
    # zone, is 2 s beyond what the ring holds. The playing slaves must not be disturbed: the joiner
    # is planned at that byte and its data channel will wait for the bytes to arrive.
    t0_before = now_us() - 30 * S
    src = await _source(300_000, t0_us=t0_before)
    t = now_us()
    src.timeline.add_report(t_us=t - 2 * S, absolute_byte=300_000 - 3 * 16_000)
    src.timeline.add_report(t_us=t - 1 * S, absolute_byte=300_000 - 2 * 16_000)
    slot = await _master().plan_join("a", src)
    assert slot.base_offset >= src.ring.end_offset
    assert src.t0_us == t0_before, "the stream's t0 is untouched: no restart"
    assert abs(slot.base_offset - (300_000 - 16_000 + 3 * 16_000)) <= 16_000 * 0.05


async def test_a_joiner_waits_for_the_rate_when_only_one_report_exists():
    t0_before = now_us() - 30 * S
    src = await _source(300_000, t0_us=t0_before)
    src.timeline.add_report(t_us=now_us(), absolute_byte=100_000)

    async def second_report_soon():
        await asyncio.sleep(0.4)
        src.timeline.add_report(t_us=now_us(), absolute_byte=100_000 + int(16_000 * 0.4))

    task = asyncio.create_task(second_report_soon())
    slot = await _master().plan_join("a", src)
    await task
    assert src.t0_us == t0_before, "no restart happened"
    assert abs(slot.base_offset - (100_000 + 16_000 * 3.4)) <= 16_000 * 0.15


async def test_every_report_logs_how_far_the_slave_renders_from_the_others():
    # The continuous comparison: each PLAYING report places its slave's rendered byte against the
    # other slaves' rendered bytes extrapolated to the same instant, in milliseconds of stream.
    src = await _source(300_000, t0_us=now_us() - 30 * S)
    logs: list[str] = []
    m = ZoneMaster(bind_ip="127.0.0.1", device_id="5EB0CE000001", log=lambda k, t: logs.append(f"{k} {t}"))
    m.sources[1] = src
    m.planner.assign(StreamKey("a", 1), JoinSlot(_t0(src), 0))
    m.planner.assign(StreamKey("b", 1), JoinSlot(_t0(src) + 10 * S, 160_000))
    playing = audio.AudioServerMsgServerState.PLAYING
    m.on_slave_state(
        SlaveState(peer="a", url_id=1, state=playing, milliseconds=20_000, byte_offset=320_000, frame_offset=800)
    )
    m.on_slave_state(
        SlaveState(peer="a", url_id=1, state=playing, milliseconds=21_000, byte_offset=336_000, frame_offset=840)
    )
    # b's counter, at the same instant of its own clock (10 s later start, 11 s in), says 800 B
    # further than the zone: 50 ms at 16 kB/s
    m.on_slave_state(
        SlaveState(peer="b", url_id=1, state=playing, milliseconds=11_000, byte_offset=176_800, frame_offset=440)
    )
    sync = [line for line in logs if line.startswith("sync ")]
    assert len(sync) == 1, logs
    assert "b" in sync[0] and "vs a" in sync[0]
    delta = int(sync[0].split("ms")[0].split()[-1])
    assert 45 <= delta <= 55, sync[0]


async def test_a_joiner_is_placed_on_a_frame_start_and_its_start_time_moves_with_it():
    # The zone renders mid-frame at the joiner's planned start. A slave discards the rest of a
    # frame it is handed mid-way and renders from the next one, ahead of the zone (hearing test
    # 2026-09-06, runs 1 and 3). So the joiner gets the next frame start, and a start time later by
    # exactly the bytes skipped at the zone's rate, which keeps it on the zone's timeline.
    payload = (Path(__file__).parent / "fixtures" / "technikumcity-24k.mp3").read_bytes() * 13
    src = StreamSource(Station(1, "http://x/live", "Test", "<ContentItem/>"), RingBuffer(), lambda k, t: None)
    await src.ring.append(payload)
    src.t0_us = now_us() - 30 * S
    src.timeline = ZoneTimeline(t0_us=_t0(src))
    first = find_frame(payload, 180_000)
    assert first is not None
    boundary = first.start
    unaligned = boundary - 200  # where the raw plan lands: 200 B before a frame start
    t = now_us()
    src.timeline.add_report(t_us=t - 2 * S, absolute_byte=unaligned - 5 * 16_000)
    src.timeline.add_report(t_us=t - 1 * S, absolute_byte=unaligned - 4 * 16_000)
    logs: list[str] = []
    before = now_us()
    slot = await ZoneMaster(bind_ip="127.0.0.1", device_id="5EB0CE000001", log=lambda k, x: logs.append(x)).plan_join(
        "a", src
    )
    assert slot.base_offset == boundary, logs
    shift_us = 200 / 16_000 * 1e6
    assert abs(slot.t0_us - (before + JOIN_LEAD_US + shift_us)) <= 10_000, "start time moved by the skipped bytes"
    assert any("frame start" in line for line in logs), logs


async def test_a_joiner_whose_bytes_are_not_in_the_ring_stays_unaligned_and_says_so():
    src = await _source(300_000, t0_us=now_us() - 30 * S)  # zeros: no frames in there at all
    t = now_us()
    src.timeline.add_report(t_us=t - 2 * S, absolute_byte=100_000)
    src.timeline.add_report(t_us=t - 1 * S, absolute_byte=116_000)
    logs: list[str] = []
    slot = await ZoneMaster(bind_ip="127.0.0.1", device_id="5EB0CE000001", log=lambda k, x: logs.append(x)).plan_join(
        "a", src
    )
    assert abs(slot.base_offset - (116_000 + 4 * 16_000)) <= 16_000 * 0.05, "last report plus 4 s at 16 kB/s"
    assert any("unaligned" in line for line in logs), logs


async def test_a_report_is_placed_at_the_slave_s_own_clock_not_at_its_arrival():
    # The slaves are on WLAN: a report arrives up to 100 ms late (measured 2026-09-06, run 2). Its
    # ``milliseconds`` is the slave's clock since its PLAY time, in the master's clock domain, so
    # that is the instant the counter belongs to, whenever the report shows up.
    src = await _source(300_000, t0_us=now_us() - 30 * S)
    m = ZoneMaster(bind_ip="127.0.0.1", device_id="5EB0CE000001", log=lambda k, t: None)
    m.sources[1] = src
    m.planner.assign(StreamKey("a", 1), JoinSlot(_t0(src), 0))
    playing = audio.AudioServerMsgServerState.PLAYING
    m.on_slave_state(SlaveState(peer="a", url_id=1, state=playing, milliseconds=20_000, byte_offset=320_000))
    m.on_slave_state(SlaveState(peer="a", url_id=1, state=playing, milliseconds=21_000, byte_offset=336_000))
    assert src.timeline.byte_at(_t0(src) + 24 * S) == 336_000 + 3 * 16_000


# --- planning in frames: the variable-rate case ---------------------------------------------------

AAC = (Path(__file__).parent / "fixtures" / "superfly-24k.aac").read_bytes()
FRAME_US = 46440


async def _aac_source() -> StreamSource:
    src = StreamSource(Station(1, "http://x/live", "Test", "<ContentItem/>"), RingBuffer(), lambda k, t: None)
    await src.ring.append(AAC * 13)
    src.t0_us = now_us() - 30 * S
    src.timeline = ZoneTimeline(t0_us=_t0(src))
    return src


async def test_with_frame_reports_the_joiner_starts_on_the_frame_the_zone_renders_at_its_start_time():
    # Superfly, run 6 (2026-09-06): the byte rate read 7 kB/s over one window and 11.5 kB/s over
    # the next, and the joiner landed 170 ms off. Frames tick at 21.53/s regardless: the first
    # slave started at frame 0 at t0, so the zone renders frame F at t0 + F * 46.44 ms exactly,
    # and the joiner is handed frame F's bytes for that instant.
    src = await _aac_source()
    logs: list[str] = []
    m = ZoneMaster(bind_ip="127.0.0.1", device_id="5EB0CE000001", log=lambda k, t: logs.append(f"{k} {t}"))
    m.sources[1] = src
    m.planner.assign(StreamKey("a", 1), JoinSlot(_t0(src), 0))
    playing = audio.AudioServerMsgServerState.PLAYING
    for ms in (26_000, 27_000, 28_000, 29_000):
        rendered = round(ms * 1000 / FRAME_US)
        # what a real slave reports: the bytes of every frame it decoded (three beyond the rendered one)
        m.on_slave_state(
            SlaveState(
                peer="a",
                url_id=1,
                state=playing,
                milliseconds=ms,
                byte_offset=_byte_of(src, rendered + 3),
                frame_offset=rendered + 3,
            )
        )
    before = now_us()
    slot = await m.plan_join("b", src)
    frame = src.frames.index_of(slot.base_offset)
    assert frame is not None, f"joiner's first byte {slot.base_offset} is not a frame start; {logs}"
    assert abs(slot.t0_us - (_t0(src) + frame * FRAME_US)) <= 25_000, "started when the zone renders that frame"
    assert before + JOIN_LEAD_US - FRAME_US <= slot.t0_us <= before + JOIN_LEAD_US + 2 * FRAME_US
    assert any(line.startswith("master b: joins at frame ") for line in logs), logs
    assert not any("joins at byte" in line for line in logs), "the byte plan must not have run"


async def test_the_sync_line_is_measured_in_frames_when_the_slaves_report_them():
    # A slave's offset from the zone is fixed the moment it starts: where its first decoded
    # frame sits against the zone's frame at its PLAY time. b was handed frame 300 two frames
    # early (a start time of 298 frames after t0), so it renders two frames ahead, for good.
    src = await _aac_source()
    logs: list[str] = []
    m = ZoneMaster(bind_ip="127.0.0.1", device_id="5EB0CE000001", log=lambda k, t: logs.append(f"{k} {t}"))
    m.sources[1] = src
    m.planner.assign(StreamKey("a", 1), JoinSlot(_t0(src), 0))
    base_b = _byte_of(src, 300)
    m.planner.assign(StreamKey("b", 1), JoinSlot(_t0(src) + 298 * FRAME_US, base_b))
    playing = audio.AudioServerMsgServerState.PLAYING
    f_a = round(20_000_000 / FRAME_US) + 3
    m.on_slave_state(
        SlaveState(
            peer="a", url_id=1, state=playing, milliseconds=20_000, byte_offset=_byte_of(src, f_a), frame_offset=f_a
        )
    )
    f_b = round(11_000_000 / FRAME_US) + 3
    m.on_slave_state(
        SlaveState(
            peer="b",
            url_id=1,
            state=playing,
            milliseconds=11_000,
            byte_offset=_byte_of(src, 300 + f_b) - base_b,
            frame_offset=f_b,
        )
    )
    sync = [line for line in logs if line.startswith("sync ")]
    assert len(sync) == 2, logs
    assert int(sync[0].split("ms")[0].split()[-1]) == 0, sync[0]
    delta = int(sync[1].split("ms")[0].split()[-1])
    assert 90 <= delta <= 96, sync[1]  # two frames of 46.44 ms: 92.9


async def test_a_slave_s_whole_frames_discarded_show_up_in_its_offset_from_the_zone():
    # Run 3 (TechnikumCity, 2026-09-06): the joiner consumed 2.85 frames of bytes more than it
    # decoded, and the zone heard it 100 ms ahead. Bytes consumed against frames decoded says
    # how many whole frames a slave threw away before its first decoded one.
    src = await _aac_source()
    logs: list[str] = []
    m = ZoneMaster(bind_ip="127.0.0.1", device_id="5EB0CE000001", log=lambda k, t: logs.append(f"{k} {t}"))
    m.sources[1] = src
    m.planner.assign(StreamKey("a", 1), JoinSlot(_t0(src), 0))
    base_b = _byte_of(src, 300)
    m.planner.assign(StreamKey("b", 1), JoinSlot(_t0(src) + 300 * FRAME_US, base_b))
    playing = audio.AudioServerMsgServerState.PLAYING
    f_b = round(11_000_000 / FRAME_US) + 3
    # b consumed the bytes of two frames it never decoded: frames 300 and 301
    m.on_slave_state(
        SlaveState(
            peer="b",
            url_id=1,
            state=playing,
            milliseconds=11_000,
            byte_offset=_byte_of(src, 302 + f_b) - base_b,
            frame_offset=f_b,
        )
    )
    sync = [line for line in logs if line.startswith("sync ")]
    assert len(sync) == 1, logs
    assert 90 <= int(sync[0].split("ms")[0].split()[-1]) <= 96, sync[0]
    assert any("discard" in line and "b" in line for line in logs), logs


async def test_a_box_that_leaves_between_its_plan_and_its_play_is_forgotten_whole():
    # The frame plan writes down the joiner's first frame BEFORE the master records its slot. A
    # box that leaves in that gap used to keep that frame: forgetting it walked the slots, and it
    # had none. Planned again later on another frame, it was then measured from the stale one.
    src = await _aac_source()
    logs: list[str] = []
    m = ZoneMaster(bind_ip="127.0.0.1", device_id="5EB0CE000001", log=lambda k, t: logs.append(f"{k} {t}"))
    m.sources[1] = src
    m.planner.assign(StreamKey("a", 1), JoinSlot(_t0(src), 0))
    playing = audio.AudioServerMsgServerState.PLAYING
    for ms in (26_000, 27_000, 28_000, 29_000):
        rendered = round(ms * 1000 / FRAME_US)
        m.on_slave_state(
            SlaveState(
                peer="a",
                url_id=1,
                state=playing,
                milliseconds=ms,
                byte_offset=_byte_of(src, rendered + 3),
                frame_offset=rendered + 3,
            )
        )
    planned = await m.plan_join("b", src)
    assert src.frames.index_of(planned.base_offset) != 300, "the fixture needs the plan on another frame"
    m.planner.drop_peer("b")
    base_b = _byte_of(src, 300)
    m.planner.assign(StreamKey("b", 1), JoinSlot(_t0(src) + 300 * FRAME_US, base_b))
    logs.clear()
    f_b = round(11_000_000 / FRAME_US) + 3
    m.on_slave_state(
        SlaveState(
            peer="b",
            url_id=1,
            state=playing,
            milliseconds=11_000,
            byte_offset=_byte_of(src, 300 + f_b) - base_b,
            frame_offset=f_b,
        )
    )
    # The stale frame does not move the sync figure: the discard count absorbs it, as a NEGATIVE
    # number of frames thrown away, which is the one thing no real box can do.
    discards = [line for line in logs if line.startswith("master b: discarded ")]
    assert len(discards) == 1, logs
    assert discards[0].startswith("master b: discarded 0 whole frame(s)"), discards[0]
    assert "from frame 300;" in discards[0], discards[0]
