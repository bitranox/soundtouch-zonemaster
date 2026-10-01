"""A speaker that connects in the very turn the master stops is answered, never left on an open socket.

The selector event loop accepts a connection in its accept callback and builds the transport in a
separate task one turn later. A ``Server.close()`` that lands between the two makes the transport's
constructor fail ``Server._attach``'s ``assert self._sockets is not None``; the loop swallows that
(it is reported only in debug mode), and the accepted socket stays open until the garbage collector
happens to reach it (CPython issue 109564, still open in 3.14.5). Until then the speaker holds a
connection nobody reads or closes, and the collection itself raises ``TypeError`` from
``Server._wakeup`` - the ``PytestUnraisableExceptionWarning`` the loopback suite showed.

The race is staged deterministically rather than hoped for. A blocking connect puts the connection
in the kernel's accept queue before the loop runs; one yield later this test's own step runs ahead
of the accept callback in the same loop turn, so a stop scheduled from that step runs in the next
turn ahead of the task that builds the transport. Garbage collection is off for the duration, so
the only thing that can close the speaker's socket is the master.
"""

from __future__ import annotations

import asyncio
import gc
import socket

import pytest

from soundtouch_zonemaster.adapters.soundtouch.zone_master import ZoneMaster

pytestmark = pytest.mark.asyncio

BIND = "127.0.0.1"
HTTP_PORT = 8090
ANSWER_WITHIN_S = 2.0


def _reply_or_close(sock: socket.socket) -> bytes:
    """Whatever the master sends first; ``b""`` when it closed the connection. Raises on silence."""
    sock.settimeout(ANSWER_WITHIN_S)
    return sock.recv(4096)


async def test_a_speaker_connecting_as_the_master_stops_is_answered_or_closed() -> None:
    master = ZoneMaster(bind_ip=BIND, device_id="5EB0CE000001", log=lambda _kind, _text: None)
    await master.start()
    gc.disable()
    speaker = socket.create_connection((BIND, HTTP_PORT), timeout=ANSWER_WITHIN_S)
    try:
        speaker.sendall(b"GET /info HTTP/1.1\r\nHost: master\r\n\r\n")
        # This step resumes ahead of the accept callback, which the same turn's select appends
        # after it; the stop it schedules therefore runs before the transport is built.
        await asyncio.sleep(0)
        stopping = asyncio.create_task(master.stop())
        await stopping
        try:
            first = await asyncio.to_thread(_reply_or_close, speaker)
        except TimeoutError:
            pytest.fail(f"the speaker heard nothing for {ANSWER_WITHIN_S} s: its connection was left open")
        assert first == b"" or first.startswith(b"HTTP/1.1 "), f"a reply or a close, got {first[:60]!r}"
    finally:
        speaker.close()
        gc.enable()
