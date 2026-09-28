"""A stop that lands on the same turn of the loop as a timeout must still stop.

httpx runs on anyio, and anyio tells its own cancellation from anybody else's by the message it
cancels with. When a deadline anyio armed fires and a stop cancels the same task before that task
next runs, asyncio keeps the exception the deadline already threw, so the task wakes with anyio's
message: anyio takes the stop for its own timeout, swallows it, and raises a timeout instead. The
fetch loop reads a timeout as a station that went quiet and reconnects, and the stop waits for it
for ever. Measured 2026-09-28: a local gate hung seven hours in exactly that state, the fetch task
still counting the stop (``cancelling() == 1``) while it reconnected every 30 seconds.

It needs nothing unusual - only a loop that is late, for any reason, at the moment somebody stops.
So the test makes the loop late on purpose: it queues the stop on a timer due BEFORE the read's
deadline and then holds the loop past both, so the next turn runs them in that order.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

import pytest
from service_log import recording_into

from soundtouch_zonemaster.adapters.soundtouch.source import RingBuffer, StreamSource
from soundtouch_zonemaster.domain.station import Station

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.asyncio

READ_S = 0.5
"""The read deadline under test. Short, so the late loop needs to be held only a second."""

STOP_BOUND_S = 5.0
"""How long a stop may take before the test calls it lost. A working one takes milliseconds."""

PAYLOAD = b"\x00" * 16384
"""Two whole chunks of the fetch's 8192-byte reads, so the read left pending is the silent one."""


async def _station_that_goes_quiet() -> tuple[asyncio.AbstractServer, str]:
    """Headers, a little audio, then nothing - held open until the fetch hangs up."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.0 200 OK\r\nContent-Type: audio/mpeg\r\n\r\n" + PAYLOAD)
        with contextlib.suppress(OSError):
            await writer.drain()
            await reader.read()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/live"


async def _eventually(check: Callable[[], bool], what: str, *, within: float = 5.0) -> None:
    deadline = time.monotonic() + within
    while not check():
        assert time.monotonic() < deadline, f"never happened: {what}"
        await asyncio.sleep(0.01)


async def test_a_stop_that_meets_the_read_deadline_still_stops_the_fetch() -> None:
    logs: list[str] = []
    server, url = await _station_that_goes_quiet()
    source = StreamSource(
        station=Station(url_id=1, playback_url=url, name="Test", content_item_xml="<x/>"),
        ring=RingBuffer(),
        log=recording_into(logs),
        read_timeout_s=READ_S,
    )
    source.start(lambda: 0)
    stopping: list[asyncio.Task[None]] = []
    try:
        await _eventually(lambda: source.bytes_total == len(PAYLOAD), "the station's audio arrived")
        # The read now pending began moments ago, so its deadline is about READ_S away, and a timer
        # due now is due before it. Held past both, the loop's next turn fires the stop's timer
        # first and the deadline second - both before the fetch runs again.
        asyncio.get_running_loop().call_later(0, lambda: stopping.append(asyncio.ensure_future(source.stop())))
        time.sleep(READ_S * 2)  # the late loop, on purpose - an await here would let both run in turn
        await _eventually(lambda: bool(stopping), "the stop was queued")
        done, _ = await asyncio.wait(stopping, timeout=STOP_BOUND_S)
        assert done, (
            f"the stop was lost: {STOP_BOUND_S}s later the fetch was still running; "
            f"it said {[line for line in logs if 'fetch' in line]}"
        )
        assert not any("fetch failed" in line for line in logs), f"the stop was read as a failure: {logs}"
    finally:
        for task in stopping:
            task.cancel()
        # A lost stop leaves the fetch reconnecting; a second one, on an ordinary turn, ends it.
        await asyncio.wait_for(source.stop(), timeout=STOP_BOUND_S)
        server.close()
        await server.wait_closed()


async def test_a_stop_on_an_ordinary_turn_stops_the_fetch() -> None:
    """The control: the same stop with the loop on time. It must pass with or without the fix, which
    is what shows the harness above can see a stop succeed, and that its failure was the race."""
    logs: list[str] = []
    server, url = await _station_that_goes_quiet()
    source = StreamSource(
        station=Station(url_id=1, playback_url=url, name="Test", content_item_xml="<x/>"),
        ring=RingBuffer(),
        log=recording_into(logs),
        read_timeout_s=READ_S,
    )
    source.start(lambda: 0)
    try:
        await _eventually(lambda: source.bytes_total == len(PAYLOAD), "the station's audio arrived")
        await asyncio.wait_for(source.stop(), timeout=STOP_BOUND_S)
        assert not any("fetch failed" in line for line in logs), logs
    finally:
        server.close()
        await server.wait_closed()
