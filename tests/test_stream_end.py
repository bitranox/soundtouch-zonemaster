"""A stream that has ended, and the difference between that and a slow one.

The data loop in ``connections._serve`` may never answer a slave with nothing - an empty chunk reads
as end-of-data - so it waits until bytes arrive. That is right while bytes can still arrive and
wrong once they cannot: measured in the flat on 2026-09-07, two of those loops outlived the zone's
dissolve by 28 minutes and wrote a line every 20 seconds each, with no socket to either box open.
A loop cannot learn that from a timeout, because a timeout is what a slow station looks like too.

So the ring says it: the source that fills it marks it ended when it stops, and a wait that can
never be satisfied says so instead of timing out.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from soundtouch_zonemaster.adapters.soundtouch import connections, ipc
from soundtouch_zonemaster.adapters.soundtouch.pb import audio_data
from soundtouch_zonemaster.adapters.soundtouch.source import RingBuffer, StreamEndedError, StreamSource
from soundtouch_zonemaster.domain.station import Station

pytestmark = pytest.mark.asyncio

BIND = "127.0.0.1"


async def test_a_wait_that_can_never_be_satisfied_says_so_rather_than_timing_out() -> None:
    ring = RingBuffer()
    await ring.append(b"abc")
    await ring.end()
    with pytest.raises(StreamEndedError):
        await ring.wait_for(3, 1, timeout=30.0)


async def test_an_ended_ring_still_hands_over_what_it_has() -> None:
    """Ending is not discarding. The bytes already in the ring are still owed to whoever asked."""
    ring = RingBuffer()
    await ring.append(b"abcde")
    await ring.end()
    await ring.wait_for(0, 8192, timeout=30.0)  # more than there is, and it must return anyway
    assert ring.read(0, 8192) == b"abcde"


async def test_a_ring_that_has_not_ended_still_times_out() -> None:
    """The control. Without this the change would be 'never wait', which is the opposite bug."""
    ring = RingBuffer()
    await ring.append(b"abc")
    with pytest.raises(TimeoutError):
        await ring.wait_for(3, 1, timeout=0.05)


async def test_a_waiter_already_waiting_is_woken_by_the_end() -> None:
    """The case the flat actually hit: the loop was already inside the wait when the source stopped."""
    import asyncio

    ring = RingBuffer()
    waiting = asyncio.create_task(ring.wait_for(0, 8192, timeout=30.0))
    await asyncio.sleep(0.05)
    assert not waiting.done(), "the wait did not block, so this proves nothing"
    await ring.end()
    with pytest.raises(StreamEndedError):
        await asyncio.wait_for(waiting, timeout=1.0)


async def test_a_data_loop_ends_with_its_stream_instead_of_writing_a_line_for_ever() -> None:
    """The defect as it happened, from the outside: the loop is INSIDE the wait when the zone goes.

    A real slave is served here over a real socket on the real port, and the source is stopped the
    way a dissolve or a station change stops it. What must not happen is what happened in the flat:
    the loop waking every DATA_WAIT_S to say it is still waiting, for ever.
    """
    logs: list[str] = []
    ring = RingBuffer()
    # Exactly one request's worth, so the first is answered at once and the second has nothing.
    await ring.append(b"x" * 8192)
    source = StreamSource(
        station=Station(url_id=1, playback_url="http://example.invalid/s", name="Test", content_item_xml="<x/>"),
        ring=ring,
        log=lambda kind, text: logs.append(f"{kind}: {text}"),
    )
    server = await connections.serve_data(
        BIND,
        lambda kind, text: logs.append(f"{kind}: {text}"),
        lambda stream_id: source if stream_id == 1 else None,
        audio_data.AudioServerMsgAcceptAudioData.NONE,
        lambda _peer, _stream_id: _base_of_zero(),
    )
    _reader, writer = await asyncio.open_connection(BIND, 40003)
    try:
        request = audio_data.AudioServerMsgAcceptAudioDataRequest(byte_count=8192, min_byte_count=8192, stream_id=1)
        # The first one is answered with everything the ring has, which leaves the cursor at the
        # end - the position a slave that has caught up sits at, and the position the loops in the
        # flat were stuck at. Only the SECOND request has nothing to wait for.
        writer.write(ipc.encode_frame(ipc.REQUEST, request, sequence=7))
        await writer.drain()
        await _eventually(lambda: any("-> 8192B at 0" in line for line in logs), "the ring was drained")
        writer.write(ipc.encode_frame(ipc.REQUEST, request, sequence=8))
        await writer.drain()
        await asyncio.sleep(0.2)
        assert not any("ended" in line for line in logs), "it did not reach the wait, so this proves nothing"

        await source.stop()

        await _eventually(lambda: any("stream 1 has ended" in line for line in logs), "the loop said its stream ended")
        # And it STOPPED. Saying it and going round again would be the same endless log with a
        # different sentence in it, so the count is the assertion, not the presence.
        await asyncio.sleep(0.3)
        ended_lines = [line for line in logs if "has ended" in line]
        assert len(ended_lines) == 1, f"the loop said it more than once: {ended_lines}"
        after_the_end = logs[logs.index(ended_lines[0]) + 1 :]
        assert not any("waiting on stream" in line for line in after_the_end), after_the_end
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        server.close()


async def _base_of_zero() -> int:
    """What the master answers for a joiner planned at byte 0."""
    return 0


async def _eventually(check: object, what: str, *, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not check():  # type: ignore[operator]  # a bare callable, kept local to this file
        assert loop.time() < deadline, f"timed out waiting for: {what}"
        await asyncio.sleep(0.02)
