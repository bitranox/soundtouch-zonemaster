"""End-to-end on loopback: a fake slave joins the master over all four channels.

No speaker involved: the station is a local HTTP server handing out pseudo-random bytes, the slave
is this test. It proves the join sequence, the data pull, the clock reply and the HTTP calls a
real slave makes - the plumbing, not the speaker's acceptance (that needs the real thing).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from typing import TYPE_CHECKING

import pytest

from soundtouch_zonemaster.adapters.soundtouch import clock, connections, ipc
from soundtouch_zonemaster.adapters.soundtouch.pb import audio, audio_data
from soundtouch_zonemaster.adapters.soundtouch.placement import JoinSlot, StreamKey
from soundtouch_zonemaster.adapters.soundtouch.source import RingBuffer, StreamSource
from soundtouch_zonemaster.adapters.soundtouch.zone_master import Slave, ZoneMaster
from soundtouch_zonemaster.domain.station import Station, StationRequest

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.asyncio

# Module scope, so the CapWords alias of a generated enum class is a constant rather than a
# local variable that has to be spelled in lower case.
TransportControl = audio.AudioServerMsgTransportControl

BIND = "127.0.0.1"
CONTENT_ITEM = (
    '<ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" location="{url}" '
    'sourceAccount="" isPresetable="true"><itemName>Test</itemName></ContentItem>'
)


async def _station_server(payload: bytes, first_byte_delay: float = 0.0) -> tuple[asyncio.AbstractServer, str]:
    """A station on loopback. ``first_byte_delay`` holds the BODY back while the headers go out.

    Holding the body rather than the whole response is what a slow station really looks like, and
    it is the only way to keep a caller inside ``play()``'s wait for the first bytes on purpose.
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\n\r\n")
        await writer.drain()
        if first_byte_delay:
            await asyncio.sleep(first_byte_delay)
        for i in range(0, len(payload), 4096):
            writer.write(payload[i : i + 4096])
            await writer.drain()
            await asyncio.sleep(0.005)
        # A live station does not end, so the connection is held open - but only while somebody is
        # LISTENING. read() returns b"" at EOF, which is the master closing. It used to be a flat
        # five-second sleep, and that outlives the test that started it: the same shape held the
        # service's loopback suite open for up to half a minute per test before it was changed
        # there (2026-09-07), so it does not get to stay here either.
        with contextlib.suppress(OSError):
            await reader.read()
        writer.close()

    srv = await asyncio.start_server(handle, BIND, 0)
    port = srv.sockets[0].getsockname()[1]
    return srv, f"http://{BIND}:{port}/live"


async def _headers_then_silence(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """A station that answers and then never sends a byte: the case play() has to give up on."""
    await reader.readuntil(b"\r\n\r\n")
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\n\r\n")
    await writer.drain()
    await asyncio.sleep(30)
    writer.close()


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _read_frames(reader: asyncio.StreamReader, n: int, timeout: float = 5.0) -> list[ipc.Frame]:
    splitter = ipc.FrameSplitter()
    out: list[ipc.Frame] = []
    while len(out) < n:
        data = await asyncio.wait_for(reader.read(65536), timeout)
        assert data, "master closed the connection"
        out += [ipc.decode_frame(b) for b in splitter.feed(data)]
    return out


async def test_fake_slave_joins_pulls_data_and_syncs_clock() -> None:
    payload = os.urandom(300_000)
    station_srv, url = await _station_server(payload)
    logs: list[str] = []
    master = ZoneMaster(bind_ip=BIND, device_id="5EB0CE000001", log=lambda k, t: logs.append(f"{k}: {t}"))
    # The master must not try to talk to a real speaker at 127.0.0.1 during this test.
    await master.start()
    try:
        await master.play(StationRequest(url, "Test", CONTENT_ITEM.format(url=url)))
        assert master.station is not None and master.sources[master.station.url_id].t0_us is not None

        # --- transport channel: the join sequence arrives in order --------------------------------
        r, w = await asyncio.open_connection(BIND, 40002)
        frames = await _read_frames(r, 3)
        assert [f.typename for f in frames] == [
            "AudioServerMsgSetClockMasterMsg",
            "AudioServerMsgSetURL",
            "AudioServerMsgTransportControl",
        ]
        clock_msg = frames[0].payload_as(audio.AudioServerMsgSetClockMasterMsg)
        assert clock_msg.clockMasterPort == 40005 and clock_msg.clockMasterIp == BIND
        set_url = frames[1].payload_as(audio.AudioServerMsgSetURL)
        assert set_url.url.startswith(f"stream://{BIND}:40003?") and "force_connect=true" in set_url.url
        play = frames[2].payload_as(audio.AudioServerMsgTransportControl)
        assert play.control == audio.AudioServerMsgTransportControl.PLAY
        assert master.station is not None
        assert play.at_microseconds == master.sources[master.station.url_id].t0_us
        assert [f.sequence for f in frames] == [1, 2, 3]
        # a ping follows within the interval
        ping = await _read_frames(r, 1, timeout=4.0)
        assert ping[0].typename == "AudioServerMsgSlavePingMsg"
        w.write(ipc.encode_frame(ipc.EVENT, audio.AudioServerMsgSlavePingResponseMsg(), sequence=1))

        # --- data channel: pull two chunks; the first carries the absolute offset --------------------
        dr, dw = await asyncio.open_connection(BIND, 40003)
        req = audio_data.AudioServerMsgAcceptAudioDataRequest(byte_count=8192, min_byte_count=8192, stream_id=1)
        # Sequence numbers like a real slave after a ping exchange: not starting at 1, with a gap.
        dw.write(ipc.encode_frame(ipc.REQUEST, req, sequence=30))
        dw.write(ipc.encode_frame(ipc.REQUEST, req, sequence=32))
        got: list[ipc.Frame] = []
        # A deadline of this loop's own, like the switch sequence below: the master pings whatever
        # else it is doing, so every read is satisfied and a chunk that never comes reads as traffic.
        deadline = asyncio.get_running_loop().time() + 20.0
        while len(got) < 2:
            assert asyncio.get_running_loop().time() < deadline, f"only {len(got)} of 2 chunks answered within 20s"
            got += [f for f in await _read_frames(dr, 1) if f.typename == "AudioServerMsgAcceptAudioData"]
        assert [g.sequence for g in got] == [30, 32], "a response carries the sequence of its request"
        first = got[0].payload_as(audio_data.AudioServerMsgAcceptAudioData)
        second = got[1].payload_as(audio_data.AudioServerMsgAcceptAudioData)
        assert first.stream_id == 1 and first.encryption_type == audio_data.AudioServerMsgAcceptAudioData.NONE
        assert len(first.data) == 8192 and len(second.data) == 8192 and second.byte_offset == 0
        start = first.byte_offset
        assert payload[start : start + 8192] == first.data, "served bytes are the station bytes at that offset"
        assert payload[start + 8192 : start + 16384] == second.data, "the cursor advances contiguously"
        assert got[0].msg_type == ipc.RESPONSE

        # --- clock: a request gets a reply that echoes T1 and stamps T2/T3 -------------------------
        # A blocking recv would stall the loop that runs the UDP server, so go through asyncio.
        loop = asyncio.get_running_loop()
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.setblocking(False)
        t1 = 123456789
        await loop.sock_sendto(u, clock.SyncPacket(clock.CLOCK_MAGIC, 3, t1, 0, 0, 0, 0).pack(), (BIND, 40005))
        rep = clock.SyncPacket.parse(await asyncio.wait_for(loop.sock_recv(u, 64), 2.0))
        assert rep.t1 == t1 and rep.t2 > 0 and rep.t3 >= rep.t2 and rep.t1_prev == 0
        await loop.sock_sendto(
            u, clock.SyncPacket(clock.CLOCK_MAGIC, 3, t1 + 400_000, rep.t2, rep.t3, 0, 0).pack(), (BIND, 40005)
        )
        rep2 = clock.SyncPacket.parse(await asyncio.wait_for(loop.sock_recv(u, 64), 2.0))
        u.close()
        assert rep2.t1_prev == t1 and rep2.t3_prev_precise == rep.t3

        # --- http: now_playing and getZone answer; slaveMsg select switches the station -------------
        hr, hw = await asyncio.open_connection(BIND, 8090)
        hw.write(b"GET /now_playing HTTP/1.1\r\nHost: x\r\n\r\n")
        resp = await asyncio.wait_for(hr.read(), 5.0)
        assert b"200 OK" in resp and b'source="LOCAL_INTERNET_RADIO"' in resp and b"<itemName>Test</itemName>" in resp

        station2_srv, url2 = await _station_server(os.urandom(100_000))
        body = (
            '<?xml version="1.0" encoding="UTF-8" ?><slaveMessage action="select">'
            '<content source="LOCAL_INTERNET_RADIO" type="stationurl" '
            f'location="{url2}" sourceAccount="" isPresetable="true">'
            "<itemName>Two</itemName></content></slaveMessage>"
        )
        hr, hw = await asyncio.open_connection(BIND, 8090)
        hw.write(f"POST /slaveMsg HTTP/1.1\r\nHost: x\r\nContent-Length: {len(body)}\r\n\r\n{body}".encode())
        resp = await asyncio.wait_for(hr.read(), 5.0)
        assert b"<status>/slaveMsg</status>" in resp
        # the transport channel receives the switch sequence: STOP, SetURL(id 2), PAUSE, PLAY
        seq: list[ipc.Frame] = []
        deadline = asyncio.get_running_loop().time() + 8.0
        while len(seq) < 4:
            assert asyncio.get_running_loop().time() < deadline, (
                f"switch sequence incomplete: {[f.typename for f in seq]}"
            )
            seq += [f for f in await _read_frames(r, 1, timeout=8.0) if f.typename != "AudioServerMsgSlavePingMsg"]
        kinds = [(f.typename, getattr(f.payload, "control", None), getattr(f.payload, "url_id", None)) for f in seq[:4]]
        assert kinds == [
            ("AudioServerMsgTransportControl", TransportControl.STOP, 1),
            ("AudioServerMsgSetURL", None, 2),
            ("AudioServerMsgTransportControl", TransportControl.PAUSE, 2),
            ("AudioServerMsgTransportControl", TransportControl.PLAY, 2),
        ]
        assert "force_connect" not in seq[1].payload_as(audio.AudioServerMsgSetURL).url
        assert master.station.name == "Two"
        station2_srv.close()
    finally:
        await master.stop()
        station_srv.close()


# --- late join --------------------------------------------------------------------------------------

REPORT_RATE = 16000  # bytes per second the fake slave claims to consume (128 kbit/s MP3)
BYTES_PER_FRAME = 400  # the fake slave's frame size; its decoder reads 3 frames ahead of rendering


async def _eventually(check: Callable[[], bool], what: str, *, timeout: float = 5.0) -> None:
    """Wait for something the master does on its own, or fail naming what never happened.

    A close is seen by the master when ITS handler runs, which is after the writer this test
    closed has gone - so asserting straight after ``wait_closed`` reads the state one step early.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not check():
        assert loop.time() < deadline, f"timed out waiting for: {what}"
        await asyncio.sleep(0.02)


async def _join_transport() -> tuple[asyncio.StreamReader, asyncio.StreamWriter, ipc.Frame]:
    """Open a transport channel and return it with the PLAY frame of the join sequence."""
    r, w = await asyncio.open_connection(BIND, 40002)
    frames = await _read_frames(r, 3, timeout=8.0)
    assert [f.typename for f in frames] == [
        "AudioServerMsgSetClockMasterMsg",
        "AudioServerMsgSetURL",
        "AudioServerMsgTransportControl",
    ]
    control_msg = frames[2].payload_as(audio.AudioServerMsgTransportControl)
    assert control_msg.control == audio.AudioServerMsgTransportControl.PLAY
    return r, w, frames[2]


async def _report_playing(w: asyncio.StreamWriter, url_id: int, t0_us: int, stop: asyncio.Event) -> None:
    """What a real slave does once its PLAY time has come: PLAYING every 0.5 s with the bytes it
    consumed since its first byte, at a steady rate."""
    seq = 10
    while not stop.is_set():
        now = clock.now_us()
        if now >= t0_us:
            ms = (now - t0_us) // 1000
            consumed = REPORT_RATE * ms // 1000
            frames = consumed // BYTES_PER_FRAME
            track = (
                f'<?xml version="1.0" encoding="UTF-8" ?><AudioServerMsgTrackData frame_offset="{frames}" '
                f'byte_offset="{consumed}" time_offset="{ms}" latency_microsecs="0" />'
            )
            state = audio.AudioServerMsgServerState(
                state=audio.AudioServerMsgServerState.PLAYING,
                url_id=url_id,
                milliseconds=ms,
                byte_offset=consumed,
                trackData=track,
            )
            seq += 1
            w.write(ipc.encode_frame(ipc.EVENT, state, sequence=seq))
            await w.drain()
        await asyncio.sleep(0.5)


async def _first_chunk(stream_id: int) -> audio_data.AudioServerMsgAcceptAudioData:
    dr, dw = await asyncio.open_connection(BIND, 40003)
    req = audio_data.AudioServerMsgAcceptAudioDataRequest(byte_count=8192, min_byte_count=8192, stream_id=stream_id)
    dw.write(ipc.encode_frame(ipc.REQUEST, req, sequence=7))
    got: list[ipc.Frame] = []
    deadline = asyncio.get_running_loop().time() + 20.0
    while not got:
        assert asyncio.get_running_loop().time() < deadline, "no chunk was answered within 20s"
        got = [f for f in await _read_frames(dr, 1, timeout=10.0) if f.typename == "AudioServerMsgAcceptAudioData"]
    dw.close()
    return got[0].payload_as(audio_data.AudioServerMsgAcceptAudioData)


async def test_a_slave_joining_a_running_stream_starts_where_the_zone_will_be_in_three_seconds():
    payload = os.urandom(400_000)
    station_srv, url = await _station_server(payload)
    logs: list[str] = []
    master = ZoneMaster(bind_ip=BIND, device_id="5EB0CE000001", log=lambda k, t: logs.append(f"{k}: {t}"))
    await master.start()
    stop = asyncio.Event()
    try:
        await master.play(StationRequest(url, "Test", CONTENT_ITEM.format(url=url)))
        t0 = master.sources[1].t0_us
        assert t0 is not None, "the stream has a start time once it is playing"
        _ra, wa, play_a = await _join_transport()
        assert play_a.payload_as(audio.AudioServerMsgTransportControl).at_microseconds == t0
        reporter = asyncio.create_task(_report_playing(wa, 1, t0, stop))
        # the zone is playing: wait until the first slave has reported a couple of times
        await asyncio.sleep((t0 - clock.now_us()) / 1e6 + 1.6)

        _rb, wb, play_b = await _join_transport()
        joined_at = clock.now_us()
        play_b_msg = play_b.payload_as(audio.AudioServerMsgTransportControl)
        t0_b = play_b_msg.at_microseconds
        assert play_b_msg.url_id == 1, "the joiner is on the same stream"
        assert t0_b != t0, "a late joiner gets its own start time, not the stream's original one"
        assert 2_500_000 <= t0_b - joined_at <= 3_500_000, f"start {(t0_b - joined_at) / 1e6:.2f}s ahead, expected ~3 s"

        first = await _first_chunk(1)
        assert first.byte_offset == 0, "the joiner's first byte is its byte 0"
        pos = payload.find(first.data)
        expected = REPORT_RATE * (t0_b - t0) / 1e6 - 3 * BYTES_PER_FRAME  # the counter reads 3 frames ahead
        assert pos >= 0, "the chunk is station data"
        assert abs(pos - expected) <= REPORT_RATE * 0.02, (
            f"first byte at {pos}, the zone renders {expected:.0f} at the joiner's start time"
        )
        stop.set()
        reporter.cancel()
        wa.close()
        wb.close()
    finally:
        stop.set()
        await master.stop()
        station_srv.close()


async def test_a_slave_joining_before_the_zone_starts_shares_the_first_slave_s_start():
    payload = os.urandom(200_000)
    station_srv, url = await _station_server(payload)
    logs: list[str] = []
    master = ZoneMaster(bind_ip=BIND, device_id="5EB0CE000001", log=lambda k, t: logs.append(f"{k}: {t}"))
    await master.start()
    try:
        await master.play(StationRequest(url, "Test", CONTENT_ITEM.format(url=url)))
        t0 = master.sources[1].t0_us
        assert t0 is not None, "the stream has a start time once it is playing"
        _ra, wa, play_a = await _join_transport()
        _rb, wb, play_b = await _join_transport()
        control = audio.AudioServerMsgTransportControl
        assert play_a.payload_as(control).at_microseconds == t0
        assert play_b.payload_as(control).at_microseconds == t0
        first = await _first_chunk(1)
        assert first.byte_offset == 0 and first.data == payload[:8192]
        wa.close()
        wb.close()
    finally:
        await master.stop()
        station_srv.close()


async def test_the_newer_of_two_overlapping_selects_wins_and_the_older_drops_its_own_source():
    """Two presses inside play()'s wait for first bytes: the LATER press has to win.

    This is the window a preset press on a slave opens. Every /slaveMsg select starts its own
    background task, and nothing outside play() orders them, so two presses a second apart are two
    calls running at once - which is exactly what the M0 runbook asks a person to do five times over
    when it measures how quick presses arrive.

    The station that starts SLOWLY is pressed first, so without ordering it finishes last and
    overwrites the station the listener pressed most recently, stopping that station's source and
    dropping its placement books on the way out. Audibly the zone falls back to the older press.

    No servers are started: play() needs none, and binding the real ports would make this test
    fight the others for them.
    """
    slow_srv, slow_url = await _station_server(os.urandom(100_000), first_byte_delay=0.6)
    fast_srv, fast_url = await _station_server(os.urandom(100_000))
    logs: list[str] = []
    master = ZoneMaster(bind_ip=BIND, device_id="5EB0CE000001", log=lambda k, t: logs.append(f"{k}: {t}"))
    try:
        overtaken = asyncio.create_task(
            master.play(StationRequest(slow_url, "Slow", CONTENT_ITEM.format(url=slow_url)))
        )
        await asyncio.sleep(0.15)  # the first call is now inside its wait for the first bytes
        winner = await master.play(StationRequest(fast_url, "Fast", CONTENT_ITEM.format(url=fast_url)))
        assert winner is not None and winner.name == "Fast"
        assert await overtaken is None, "the overtaken call must not report a station it did not install"
        assert master.station is not None
        assert master.station.name == "Fast", "the zone plays what was pressed last, not what answered last"
        assert list(master.sources) == [winner.url_id], "the overtaken call stopped and dropped its own source"
    finally:
        await master.stop()
        slow_srv.close()
        fast_srv.close()


async def test_a_select_in_flight_cannot_switch_a_speaker_after_the_zone_is_dissolved():
    """A preset press arriving as a run ends must not reach the speakers after the dissolve.

    Every select runs as a background task of the HTTP face, and play() sits in its wait for the
    station's first bytes for up to ten seconds. A run whose --duration expires in that window
    dissolves the zone and shuts down while the task is still going, and the task then sends the
    switch sequence to a speaker that has just been told the zone is over. It starts playing again,
    in a flat, with nobody expecting it. That is the least forgivable failure this program has.

    So the press here is aimed at a station that answers slowly, the dissolve happens while the
    task waits, and what is asserted is what a real speaker would receive: nothing.
    """
    fast_srv, fast_url = await _station_server(os.urandom(100_000))
    slow_srv, slow_url = await _station_server(os.urandom(100_000), first_byte_delay=1.0)
    logs: list[str] = []
    master = ZoneMaster(bind_ip=BIND, device_id="5EB0CE000001", log=lambda k, t: logs.append(f"{k}: {t}"))
    await master.start()
    try:
        await master.play(StationRequest(fast_url, "Fast", CONTENT_ITEM.format(url=fast_url)))
        playing = master.station
        r, w, _play = await _join_transport()
        body = (
            '<?xml version="1.0" encoding="UTF-8" ?><slaveMessage action="select">'
            '<content source="LOCAL_INTERNET_RADIO" type="stationurl" '
            f'location="{slow_url}" sourceAccount="" isPresetable="true">'
            "<itemName>Slow</itemName></content></slaveMessage>"
        )
        hr, hw = await asyncio.open_connection(BIND, 8090)
        hw.write(f"POST /slaveMsg HTTP/1.1\r\nHost: x\r\nContent-Length: {len(body)}\r\n\r\n{body}".encode())
        assert b"<status>/slaveMsg</status>" in await asyncio.wait_for(hr.read(), 5.0)
        await asyncio.sleep(0.2)  # the select is now inside play()'s wait for the first bytes

        await master.dissolve()
        await asyncio.sleep(1.5)  # well past the slow station's first byte

        arrived: list[ipc.Frame] = []
        splitter = ipc.FrameSplitter()
        try:
            while True:
                data = await asyncio.wait_for(r.read(65536), 0.2)
                if not data:
                    break
                arrived += [ipc.decode_frame(b) for b in splitter.feed(data)]
        except TimeoutError:
            pass  # nothing more to read is the outcome this test wants
        switch_kinds = {"AudioServerMsgSetURL", "AudioServerMsgTransportControl"}
        switched = [f.typename for f in arrived if f.typename in switch_kinds]
        assert switched == [], f"the speaker was switched after the zone was dissolved: {switched}"
        assert master.station is playing, "the zone must not change station after it was dissolved"
        w.close()
        hw.close()
    finally:
        await master.stop()
        fast_srv.close()
        slow_srv.close()


async def test_a_station_that_sends_no_bytes_still_places_a_joiner_on_a_real_clock():
    """The give-up branch has to set BOTH halves of the start time, or the sync line lies.

    A station that answers and then sends nothing leaves play() to invent a start time after its
    wait runs out. That time reaches the speaker in its PLAY, while every placement is measured
    from the source's byte timeline - so setting one and not the other tells the zone its stream
    began at clock zero. on_slave_state then reads each report as if PLAY had happened in 1970,
    the sync line (the instrument this whole project is judged by) reports nonsense, and every
    later joiner is placed against that.

    The wait is shortened through play()'s own parameter rather than by patching the module, so
    the branch under test is the one the program runs.
    """
    fast_srv, fast_url = await _station_server(os.urandom(100_000))
    silent = await asyncio.start_server(_headers_then_silence, BIND, 0)
    silent_url = f"http://{BIND}:{silent.sockets[0].getsockname()[1]}/live"
    logs: list[str] = []
    master = ZoneMaster(bind_ip=BIND, device_id="5EB0CE000001", log=lambda k, t: logs.append(f"{k}: {t}"))
    await master.start()
    try:
        await master.play(StationRequest(fast_url, "Fast", CONTENT_ITEM.format(url=fast_url)))
        _r, w, _play = await _join_transport()
        peer = master.transports.peers()[0]

        station = await master.play(
            StationRequest(silent_url, "Silent", CONTENT_ITEM.format(url=silent_url)),
            first_bytes_timeout_s=0.3,
        )
        assert station is not None
        source = master.sources[station.url_id]
        assert source.t0_us is not None
        assert any("no bytes after" in line for line in logs), "the give-up branch is the one under test"
        assert source.timeline.t0_us == source.t0_us, "both halves of the start time, or neither"
        slot = master.planner.slots[StreamKey(peer, station.url_id)]
        assert slot.t0_us == source.t0_us, "the joiner is placed on the clock the PLAY actually named"
        w.close()
    finally:
        await master.stop()
        fast_srv.close()
        silent.close()


async def test_a_second_transport_re_plans_the_join_and_the_open_data_channel_keeps_its_own_place():
    """OPEN-WORK rank 104, reproduced: what a re-planned join does to a data channel already open.

    A box really does open a SECOND transport channel while the first is live - measured in the
    flat on 2026-09-07, all four boxes did it during one burst of membership changes. That second
    channel is placed by ``_on_transport``, which plans a LATE join and writes it to the same
    ``StreamKey(peer, url_id)``, because the box and the stream are both unchanged. Its data
    channel is a different connection with a cursor of its own, and that cursor is read once, when
    the stream is first asked for: every later request continues from where the bytes got to.

    So the two disagree, and this pins by how much. The box is told to start rendering at the new
    PLAY time, which assumes the new first byte, while the bytes it is handed carry on from the old
    position - the whole gap between them is how far out of sync it would render.

    What this does NOT settle is whether a real box re-requests at all after a second transport
    join, or goes on decoding the stream it already has. That decides whether the gap is audible or
    is only arithmetic, it needs a speaker in the room to answer, and until it is answered this
    records what the program does rather than asserting that it is right.
    """
    payload = os.urandom(400_000)
    station_srv, url = await _station_server(payload)
    logs: list[str] = []
    master = ZoneMaster(bind_ip=BIND, device_id="5EB0CE000001", log=lambda k, t: logs.append(f"{k}: {t}"))
    await master.start()
    stop = asyncio.Event()
    try:
        await master.play(StationRequest(url, "Test", CONTENT_ITEM.format(url=url)))
        t0 = master.sources[1].t0_us
        assert t0 is not None
        _ra, wa, play_a = await _join_transport()
        assert play_a.payload_as(audio.AudioServerMsgTransportControl).at_microseconds == t0
        peer = master.transports.peers()[0]
        planned_first = master.planner.slots[StreamKey(peer, 1)]

        # ONE data channel, held open across the re-plan: that is the whole point.
        dr, dw = await asyncio.open_connection(BIND, 40003)

        async def pull(sequence: int) -> audio_data.AudioServerMsgAcceptAudioData:
            req = audio_data.AudioServerMsgAcceptAudioDataRequest(byte_count=8192, min_byte_count=8192, stream_id=1)
            dw.write(ipc.encode_frame(ipc.REQUEST, req, sequence=sequence))
            got: list[ipc.Frame] = []
            deadline = asyncio.get_running_loop().time() + 20.0
            while not got:
                assert asyncio.get_running_loop().time() < deadline, f"no chunk for sequence {sequence} within 20s"
                got = [
                    f for f in await _read_frames(dr, 1, timeout=10.0) if f.typename == "AudioServerMsgAcceptAudioData"
                ]
            return got[0].payload_as(audio_data.AudioServerMsgAcceptAudioData)

        opened = await pull(41)
        assert payload.find(opened.data) == planned_first.base_offset, "it starts where its join was planned"

        reporter = asyncio.create_task(_report_playing(wa, 1, t0, stop))
        await asyncio.sleep((t0 - clock.now_us()) / 1e6 + 1.6)

        _rb, wb, play_b = await _join_transport()
        replanned = master.planner.slots[StreamKey(peer, 1)]
        assert replanned.base_offset > planned_first.base_offset, (
            "the second channel is placed as a late joiner, so the key now names a byte further on"
        )
        assert play_b.payload_as(audio.AudioServerMsgTransportControl).at_microseconds == replanned.t0_us

        carried_on = await pull(42)
        where = payload.find(carried_on.data)
        assert where == planned_first.base_offset + 8192, (
            "the open data channel continues from its own cursor and never reads the new plan"
        )
        assert where != replanned.base_offset
        stop.set()
        reporter.cancel()
        dw.close()
        wa.close()
        wb.close()
    finally:
        stop.set()
        await master.stop()
        station_srv.close()


async def test_a_station_change_leaves_each_stream_s_key_holding_its_own_placement():
    """A channel joins, a press moves the zone, and neither stream ends up holding the other's byte.

    What it stages is a join that COMPLETES and then a station change: the joiner is placed on the
    station that was playing when it arrived, the old stream's placements are dropped with the old
    stream, and the new stream's key holds the new stream's own co-starter slot. Mutation-verified
    on that last one - deleting the assign loop in ``play()`` makes this fail with a ``KeyError``
    on the new stream's key.

    **It does NOT stage the contended ordering, and it was written believing it did.** The claim
    was that the zone is made to be PLAYING and that this makes ``plan_join`` wait; measured
    2026-09-18, ``ZoneTimeline.is_playing`` is true only while a SLAVE REPORT is younger than its
    max age, and this test sends none - so ``plan_join`` returns without suspending once, the join
    is finished before the press, and removing the switch lock from ``_on_transport`` leaves this
    test green. ``test_a_press_holding_the_switch_lock_leaves_a_queued_channel_its_join_sequence``
    is what covers that ordering, and it is the only test in this file that fails without the lock.
    """
    first_srv, first_url = await _station_server(os.urandom(200_000))
    second_srv, second_url = await _station_server(os.urandom(200_000))
    logs: list[str] = []
    master = ZoneMaster(bind_ip=BIND, device_id="5EB0CE000001", log=lambda k, t: logs.append(f"{k}: {t}"))
    await master.start()
    try:
        first = await master.play(StationRequest(first_url, "First", CONTENT_ITEM.format(url=first_url)))
        assert first is not None
        started = master.sources[first.url_id].t0_us
        assert started is not None
        # Past byte 0's time, so the joiner is placed as a late arrival rather than dropped into
        # the starting cohort. It is not enough to make the zone PLAYING - that needs a report -
        # so the join completes here rather than suspending.
        await asyncio.sleep((started - clock.now_us()) / 1e6 + 0.3)

        r, w = await asyncio.open_connection(BIND, 40002)
        await asyncio.sleep(0.3)  # let the join finish before the press
        second = await master.play(StationRequest(second_url, "Second", CONTENT_ITEM.format(url=second_url)))
        assert second is not None and second.url_id != first.url_id

        frames = await _read_frames(r, 3, timeout=8.0)
        # The FIRST three: one read can deliver more than were asked for, and what is under test is
        # what reached this channel first.
        assert [f.typename for f in frames[:3]] == [
            "AudioServerMsgSetClockMasterMsg",
            "AudioServerMsgSetURL",
            "AudioServerMsgTransportControl",
        ], "the join sequence reaches the channel whole, before any station switch"
        play = frames[2].payload_as(audio.AudioServerMsgTransportControl)
        peer = master.transports.peers()[0]
        assert play.url_id == first.url_id, "it joins the station that was playing when it arrived"
        assert StreamKey(peer, first.url_id) not in master.planner.slots, (
            "the old stream's placements are dropped with the old stream, this one included"
        )

        # And then it is switched like every other slave, on a slot planned in the NEW stream.
        moved = master.sources[second.url_id]
        assert master.planner.slots[StreamKey(peer, second.url_id)] == JoinSlot(
            moved.timeline.t0_us, moved.timeline.t0_byte
        ), "the new stream's key must hold a byte of the new stream"
        w.close()
    finally:
        await master.stop()
        first_srv.close()
        second_srv.close()


# --- the two conditions that used to escape both channel loops ----------------------------------


async def test_a_transport_arriving_before_any_station_is_closed_with_its_reason_logged() -> None:
    """A slave can open its transport between start() and play(), and it must be told no out loud.

    The connection cannot be kept: the first play() switches the transports it finds only when a
    station was already installed, so one accepted here would stay attached to nothing for good.
    Ending it is right. Ending it in silence is not - the reason is the only thing that tells
    somebody reading the log why a speaker never started.
    """
    logs: list[str] = []
    master = ZoneMaster(bind_ip=BIND, device_id="5EB0CE000003", log=lambda k, t: logs.append(f"{k}: {t}"))
    await master.start()
    try:
        reader, writer = await asyncio.open_connection(BIND, 40002)
        assert await asyncio.wait_for(reader.read(), timeout=5.0) == b"", "the master has to end it"
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()
        assert any("no station selected yet" in line for line in logs), (
            f"the close must carry its reason; the log only said {logs}"
        )
    finally:
        await master.stop()


async def test_a_data_channel_the_ring_has_run_past_is_closed_with_its_reason_logged() -> None:
    """A slave that stops asking is left behind by the ring, and that is ordinary, not hostile.

    Eight megabytes is a few minutes of a 128 kbit/s station, and Room2 drops off the radio for
    about that long (OPEN-WORK rank 50). The byte it still wants is then gone and this connection
    cannot be served again, so it ends - saying which byte, because the alternative is a speaker
    that goes quiet with nothing in the log but the word closed.
    """
    logs: list[str] = []

    def log(kind: str, text: str) -> None:
        logs.append(f"{kind}: {text}")

    station = Station(
        url_id=1, playback_url="http://example.invalid/s", name="Gone", content_item_xml="<ContentItem />"
    )
    source = StreamSource(station, RingBuffer(max_bytes=1024), log)
    await source.ring.append(os.urandom(4096))
    assert source.ring.start_offset > 0, "the control: the ring really did drop its own start"

    async def base_for(_peer: str, _stream_id: int) -> int | None:
        return 0  # the byte this slave is still waiting for, dropped minutes ago

    server = await connections.serve_data(
        BIND,
        log,
        lambda stream_id: source if stream_id == 1 else None,
        audio_data.AudioServerMsgAcceptAudioData.NONE,
        base_for,
    )
    try:
        reader, writer = await asyncio.open_connection(BIND, 40003)
        request = audio_data.AudioServerMsgAcceptAudioDataRequest(byte_count=512, min_byte_count=1, stream_id=1)
        writer.write(ipc.encode_frame(ipc.REQUEST, request, sequence=7))
        assert await asyncio.wait_for(reader.read(), timeout=5.0) == b"", "the channel has to end"
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()
        assert any("already dropped" in line for line in logs), (
            f"the close must name the byte that is gone; the log only said {logs}"
        )
    finally:
        server.close()
        await server.wait_closed()


async def test_a_registered_slave_s_transport_is_attached_to_its_record() -> None:
    """A slave the master knows must end up holding its own channel, not just be logged.

    The registry is how the master addresses a box over HTTP and the transport is how it addresses
    it over the protocol; a zone member with the two halves apart can be told to join and never
    told anything again. Every other test here connects an UNREGISTERED transport, which the
    master accepts with a log line and a different branch, so this one is what proves the halves
    are joined at all.
    """
    payload = os.urandom(64_000)
    station_srv, url = await _station_server(payload)
    logs: list[str] = []
    master = ZoneMaster(bind_ip=BIND, device_id="5EB0CE000004", log=lambda k, t: logs.append(f"{k}: {t}"))
    master.slaves[BIND] = Slave(ip=BIND, device_id="AABBCC000004")
    await master.start()
    try:
        await master.play(StationRequest(url, "Test", CONTENT_ITEM.format(url=url)))
        reader, writer, _play = await _join_transport()
        assert master.slaves[BIND].transport is not None, "the record must hold the channel"
        assert master.slaves[BIND].transport is master.transports.driven_for(BIND), "and it is the same one"
        assert not [line for line in logs if "transport from unknown" in line], (
            "a registered slave must not take the unknown branch"
        )
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()
        reader.feed_eof()
    finally:
        await master.stop()
        station_srv.close()


async def test_a_master_that_stopped_can_be_started_again_on_the_same_ports() -> None:
    """A service stands down and comes back, and its ports have to be free the moment it does.

    A datagram transport's close is SCHEDULED rather than immediate - it goes through call_soon -
    so the clock's UDP socket outlives its master by one turn of the loop, and the next master
    dies on bind with "address already in use". A run never noticed, because a run stops once and
    then the process ends; a service stops every time somebody flips the switch.
    """
    first = ZoneMaster(bind_ip=BIND, device_id="5EB0CE000005", log=lambda _k, _t: None)
    await first.start()
    await first.stop()

    second = ZoneMaster(bind_ip=BIND, device_id="5EB0CE000005", log=lambda _k, _t: None)
    await second.start()  # the assertion IS this call: it must not raise
    await second.stop()


async def test_stopping_a_source_hands_on_the_cancellation_of_whoever_is_stopping_it() -> None:
    """A stop that lands inside a station switch must still end the task that was switching.

    ``StreamSource.stop`` awaits the fetch task it has just cancelled, and that await raises
    CancelledError for two different reasons: the fetch task has died, which is what the method is
    for, or the CALLER is being cancelled - because cancelling a task that is awaiting another task
    is delivered THROUGH that child, so both reasons arrive as the same exception. Read as the
    first, the second is swallowed and the caller outlives its own cancellation.

    Measured 2026-09-07, and it is not a test-only concern: that is what hung the service's
    loopback suite. A dialled channel was still inside ``play()`` swapping stations when the run
    was cancelled, the swap's ``stop()`` of the old source ate the worker's cancel, and
    ``ZoneService.run``'s ``finally`` then waited for that worker for ever. In the flat that is the
    zone never being dissolved and real speakers left in a zone whose master has gone.
    """
    station_srv, url = await _station_server(os.urandom(64_000), first_byte_delay=5.0)
    logs: list[str] = []
    source = StreamSource(
        Station(url_id=1, playback_url=url, name="Held", content_item_xml=CONTENT_ITEM.format(url=url)),
        RingBuffer(),
        lambda kind, text: logs.append(f"{kind}: {text}"),
    )
    try:
        source.start(clock.now_us)
        for _ in range(500):
            if source.content_type:
                break
            await asyncio.sleep(0.01)
        assert source.content_type, "the control: the fetch task has to be streaming, or nothing is stopped"

        stopper = asyncio.create_task(source.stop())
        await asyncio.sleep(0)
        assert not stopper.done(), "the control: the stopper has to be INSIDE the wait when it is cancelled"

        stopper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopper
    finally:
        station_srv.close()


async def test_a_box_that_holds_two_channels_keeps_the_zone_when_the_older_one_ends():
    """The flat's silence of 2026-09-07 21:04, reproduced without a speaker (OPEN-WORK rank 22).

    A box holds more than one transport channel at a time - measured that evening, all four boxes
    did during a burst of membership changes. Both channels come from one address, so a registry
    keyed by address held only the newest, and the OLDER one ending removed the address: the live
    channel went with it, the next station switch skipped that box, and it played the stream it
    was left on until that stream stopped. Room4 went quiet with the new station's name on its
    display, and the master had written a ``slave-state`` line about it every second.

    So: two channels, the older ends, and the station changes. The surviving channel must be told.
    """
    payload = os.urandom(200_000)
    station_a, url_a = await _station_server(payload)
    station_b, url_b = await _station_server(payload)
    logs: list[str] = []
    master = ZoneMaster(bind_ip=BIND, device_id="5EB0CE000001", log=lambda k, t: logs.append(f"{k}: {t}"))
    await master.start()
    try:
        first = await master.play(StationRequest(url_a, "A", CONTENT_ITEM.format(url=url_a)))
        assert first is not None
        _r_old, w_old, _play_old = await _join_transport()
        r_new, w_new, _play_new = await _join_transport()
        assert master.transports.driven_for(BIND) is not None, "the box is driven on its newest channel"

        # The older channel ends, exactly as the box's own reconnect ends it. The newer one is
        # untouched and is still the one the master drives.
        w_old.close()
        with contextlib.suppress(ConnectionError):
            await w_old.wait_closed()
        await _eventually(
            lambda: sum(1 for line in logs if line == "transport: 127.0.0.1: closed") == 1,
            "the older channel was seen to end",
        )
        assert master.transports.driven_for(BIND) is not None, "the box still has a channel and is still driven"
        assert master.planner.slots, "and its placement survived the close, because the box did"

        second = await master.play(StationRequest(url_b, "B", CONTENT_ITEM.format(url=url_b)))
        assert second is not None and second.url_id != first.url_id

        # STOP for the old stream, SetURL, PAUSE, then PLAY for the new one: the switch sequence,
        # on the channel that was still open. Reading it is the whole assertion.
        frames = await _read_frames(r_new, 4, timeout=8.0)
        assert [f.typename for f in frames] == [
            "AudioServerMsgTransportControl",
            "AudioServerMsgSetURL",
            "AudioServerMsgTransportControl",
            "AudioServerMsgTransportControl",
        ]
        stop = frames[0].payload_as(audio.AudioServerMsgTransportControl)
        play = frames[3].payload_as(audio.AudioServerMsgTransportControl)
        assert stop.control == audio.AudioServerMsgTransportControl.STOP
        assert stop.url_id == first.url_id, "the old stream is stopped by its own id"
        assert play.control == audio.AudioServerMsgTransportControl.PLAY
        assert play.url_id == second.url_id, "and the box is put on the new one"
        w_new.close()
    finally:
        await master.stop()
        station_a.close()
        station_b.close()


async def test_the_last_channel_of_a_box_ending_is_what_drops_its_placement():
    """The books belong to the box, not to one of its channels.

    The other half of the same defect: dropping the placement on ANY close threw away the
    placement of a channel that was still carrying audio, so a box that lost one of two channels
    was replanned from nothing while it played.
    """
    payload = os.urandom(200_000)
    station_srv, url = await _station_server(payload)
    logs: list[str] = []
    master = ZoneMaster(bind_ip=BIND, device_id="5EB0CE000001", log=lambda k, t: logs.append(f"{k}: {t}"))
    await master.start()
    try:
        await master.play(StationRequest(url, "Test", CONTENT_ITEM.format(url=url)))
        _r_old, w_old, _play_old = await _join_transport()
        _r_new, w_new, _play_new = await _join_transport()
        assert master.planner.slots, "both channels are placed"

        w_old.close()
        with contextlib.suppress(ConnectionError):
            await w_old.wait_closed()
        await _eventually(
            lambda: sum(1 for line in logs if line == "transport: 127.0.0.1: closed") == 1,
            "the older channel was seen to end",
        )
        assert master.planner.slots, "one channel ending is not the box leaving"

        w_new.close()
        with contextlib.suppress(ConnectionError):
            await w_new.wait_closed()
        await _eventually(lambda: not master.planner.slots, "the box's placement goes with its last channel")
        assert master.transports.driven_for(BIND) is None, "and it is driven on nothing"
    finally:
        await master.stop()
        station_srv.close()


async def test_when_the_driven_channel_ends_the_older_one_is_kept_and_the_mismatch_is_said_out_loud():
    """The mirror of the same defect, and the half of rank 22 that is NOT closed here.

    A switch is sent on ONE channel per box - twice over two channels would tell the box to stop
    the stream it has just been started on - so a superseded channel stays on the old station. If
    the driven channel then ends, the box is driven on that older channel, which is on a stream
    the zone has left. It is not silent yet: it plays until that stream stops.

    Keeping it is right (the alternative is a box with no control channel at all), and re-setting
    it is the safety net rank 22 asks for and this does not build. What is owed meanwhile is that
    the master SAYS so, because the journal is where this house is diagnosed - the first time it
    happened the evidence was there and nothing had drawn the conclusion.
    """
    payload = os.urandom(200_000)
    station_a, url_a = await _station_server(payload)
    station_b, url_b = await _station_server(payload)
    logs: list[str] = []
    master = ZoneMaster(bind_ip=BIND, device_id="5EB0CE000001", log=lambda k, t: logs.append(f"{k}: {t}"))
    await master.start()
    try:
        first = await master.play(StationRequest(url_a, "A", CONTENT_ITEM.format(url=url_a)))
        assert first is not None
        _r_old, w_old, _play_old = await _join_transport()
        _r_new, w_new, _play_new = await _join_transport()
        superseded = master.transports.driven_for(BIND)

        second = await master.play(StationRequest(url_b, "B", CONTENT_ITEM.format(url=url_b)))
        assert second is not None
        assert master.transports.driven_for(BIND) is superseded, "the switch went to the channel it was driven on"

        w_new.close()
        with contextlib.suppress(ConnectionError):
            await w_new.wait_closed()
        await _eventually(
            lambda: any("driven channel ended" in line for line in logs),
            "the master said which stream the box is left on",
        )
        left_on = master.transports.driven_for(BIND)
        assert left_on is not None, "the box keeps the channel it still has"
        assert left_on is not superseded, "and it is the other one"
        assert left_on.current is not None and left_on.current.url_id == first.url_id, (
            "which is still on the station the zone has left"
        )
        said = next(line for line in logs if "driven channel ended" in line)
        assert f"url_id={first.url_id}" in said and f"url_id={second.url_id}" in said, (
            f"the line names both streams, so a reader can act on it: {said}"
        )
        w_old.close()
    finally:
        await master.stop()
        station_a.close()
        station_b.close()


# --- the switch that must not reach a channel still inside its join ------------------------------


async def _frames_ignoring_pings(reader: asyncio.StreamReader, n: int, timeout: float = 8.0) -> list[ipc.Frame]:
    """The next ``n`` frames on a channel that are not the master's two-second keepalive.

    A channel nobody has read from for a few seconds has pings waiting in its buffer, and they say
    nothing about the sequence under test; a channel opened during a slow step collects them too.
    """
    out: list[ipc.Frame] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while len(out) < n:
        assert loop.time() < deadline, f"only {len(out)} of {n} frames in {timeout}s: {[f.typename for f in out]}"
        out += [f for f in await _read_frames(reader, 1, timeout) if f.typename != "AudioServerMsgSlavePingMsg"]
    return out[:n]


async def test_a_press_holding_the_switch_lock_leaves_a_queued_channel_its_join_sequence():
    """The guard in ``play()``: a station switch goes only to channels that have JOINED.

    A switch is a STOP and a PLAY, and it carries neither the clock master nor the first URL -
    only the join sequence sends those - so a channel still inside its join must not be switched.
    Since ``_on_transport`` holds the switch lock across its whole join, such a channel is visible
    to ``play()`` only while a THIRD party holds that lock and the two of them queue behind it.
    asyncio wakes lock waiters in turn, so the order that puts the guard in the way is: the third
    party holds the lock, ``play()`` queues, the new channel queues behind ``play()``.

    The third party is the real one rather than a stand-in: ``put_back_on_the_station``, which the
    service's pass calls for every box a station switch was never sent to (``zone.py``). The box it
    repairs is built the way this module documents - two channels, the newer one driven and
    switched to B while the older stays on A, then the newer one ends - which leaves the older
    channel driven on a stream the zone has left, and ``slaves_left_on_an_old_stream`` names it.

    Both queue points are READ rather than waited out, because each coroutine runs to its lock
    without an await in between: ``play()`` from the line it writes when it stops waiting for the
    first bytes, ``_on_transport`` from the moment it registers the channel. So the log line and
    the register entry each prove the coroutine behind them is already in the queue, and the three
    assertions taken at that instant prove the order held rather than assuming it did.
    """
    payload = os.urandom(200_000)
    station_a, url_a = await _station_server(payload)
    station_b, url_b = await _station_server(payload)
    station_c, url_c = await _station_server(payload)
    logs: list[str] = []
    master = ZoneMaster(bind_ip=BIND, device_id="5EB0CE000001", log=lambda k, t: logs.append(f"{k}: {t}"))
    await master.start()
    try:
        first = await master.play(StationRequest(url_a, "A", CONTENT_ITEM.format(url=url_a)))
        assert first is not None
        r_old, w_old, _play_old = await _join_transport()
        _r_sup, w_sup, _play_sup = await _join_transport()

        # The newer channel is the driven one, so the switch to B reaches it alone and the older
        # one stays on A. Ending it leaves the box driven on the stream the zone has left.
        second = await master.play(StationRequest(url_b, "B", CONTENT_ITEM.format(url=url_b)))
        assert second is not None and second.url_id != first.url_id
        w_sup.close()
        with contextlib.suppress(ConnectionError):
            await w_sup.wait_closed()
        await _eventually(
            lambda: sum(1 for line in logs if line == "transport: 127.0.0.1: closed") == 1,
            "the driven channel was seen to end",
        )
        left_behind = master.transports.driven_for(BIND)
        assert left_behind is not None and left_behind.current is not None
        assert left_behind.current.url_id == first.url_id, "the box is driven on the station the zone has left"
        assert master.slaves_left_on_an_old_stream() == [BIND], "which is what the service's pass acts on"

        # One: the third party takes the lock. Its switch sends STOP, SetURL, PAUSE and then sleeps
        # a second before the PLAY, so reading the PAUSE says the lock is held and stays held.
        put_back = asyncio.create_task(master.put_back_on_the_station(BIND))
        held = await _frames_ignoring_pings(r_old, 3)
        assert [f.typename for f in held] == [
            "AudioServerMsgTransportControl",
            "AudioServerMsgSetURL",
            "AudioServerMsgTransportControl",
        ]
        assert held[2].payload_as(audio.AudioServerMsgTransportControl).control == (
            audio.AudioServerMsgTransportControl.PAUSE
        ), "the third party is inside the second this switch waits before its PLAY"

        # Two: a press queues behind it. The zero timeout is what makes the queueing readable - the
        # press then runs from that log line to the lock with nothing to yield at, so the line
        # means it is in the queue and not on its way there.
        pressed = asyncio.create_task(
            master.play(StationRequest(url_c, "C", CONTENT_ITEM.format(url=url_c)), first_bytes_timeout_s=0.0)
        )
        await _eventually(
            lambda: any("no bytes after 0 s" in line for line in logs), "the press queued on the switch lock"
        )

        # Three: a box opens a transport channel behind the press. _on_transport registers it
        # before it takes the lock, so the register says the same thing about this one.
        r_late, w_late = await asyncio.open_connection(BIND, 40002)
        await _eventually(
            lambda: master.transports.driven_for(BIND) is not left_behind, "the new channel queued behind the press"
        )
        late = master.transports.driven_for(BIND)
        assert late is not None and late.current is None, "it is the driven channel now, and it has joined nothing"
        assert not put_back.done(), "the third party still holds the lock"
        assert master.station is not None and master.station.url_id == second.url_id, (
            "and the press has not run yet: the three are in the order the guard needs"
        )

        assert await put_back, "the box left behind was put back, which frees the lock"
        switched = await pressed
        assert switched is not None and switched.url_id != second.url_id

        frames = await _frames_ignoring_pings(r_late, 3)
        assert [f.typename for f in frames] == [
            "AudioServerMsgSetClockMasterMsg",
            "AudioServerMsgSetURL",
            "AudioServerMsgTransportControl",
        ], "a channel that has not joined gets its join sequence, never the switch that overtook it"
        played = frames[2].payload_as(audio.AudioServerMsgTransportControl)
        assert played.control == audio.AudioServerMsgTransportControl.PLAY
        assert played.url_id == switched.url_id, "and it joins the station the press installed"
        w_late.close()
        w_old.close()
    finally:
        await master.stop()
        station_a.close()
        station_b.close()
        station_c.close()
