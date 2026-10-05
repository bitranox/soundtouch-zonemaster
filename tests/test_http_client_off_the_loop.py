"""What the first HTTP request costs the event loop: nothing that could have been done before it.

The first httpx client of a run used to import httpcore and h11 and build an SSL context, and its
first request imported anyio's asyncio backend - all of it on the event loop, which also answers
the zone's clock and frames. Measured on the service's first ``OrionBase.warm()``, in a fresh
interpreter: the loop stalled 66 to 93 ms. Every later client built an SSL context of its own too,
a few milliseconds per speaker call.

The test is counted rather than timed. In a fresh interpreter that has imported what the service
imports, a first request on the loop must import no module at all, and two clients must share one
SSL context: both are exact, where a stall in milliseconds depends on the machine.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

_FIRST_REQUEST = textwrap.dedent(
    """
    import asyncio, json, sys

    import soundtouch_zonemaster.composition  # what the service has imported before its loop runs
    from soundtouch_zonemaster.adapters.http_client import client_without_deadline


    async def answer(reader, writer):
        await reader.readuntil(b"\\r\\n\\r\\n")
        writer.write(b"HTTP/1.1 200 OK\\r\\nContent-Length: 2\\r\\n\\r\\n{}")
        await writer.drain()
        writer.close()


    async def main():
        server = await asyncio.start_server(answer, "127.0.0.1", 0)
        url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
        before = set(sys.modules)
        first, second = client_without_deadline(), client_without_deadline()
        async with first, second:
            status = (await first.get(url)).status_code
        contexts = {id(client._transport._pool._ssl_context) for client in (first, second)}
        server.close()
        await server.wait_closed()
        return {
            "status": status,
            "imported_on_the_loop": sorted(set(sys.modules) - before),
            "ssl_contexts": len(contexts),
        }


    print(json.dumps(asyncio.run(main())))
    """
)


def _first_request() -> dict[str, object]:
    """Run the first request in a fresh interpreter, so nothing this test session imported counts."""
    done = subprocess.run(  # noqa: S603 - our own interpreter running our own fixed script
        [sys.executable, "-c", _FIRST_REQUEST], capture_output=True, text=True, encoding="utf-8", timeout=60, check=True
    )
    report: dict[str, object] = json.loads(done.stdout)
    return report


def test_the_first_request_imports_nothing_on_the_loop_and_clients_share_one_ssl_context() -> None:
    report = _first_request()

    assert report["status"] == 200, "the control: the request reached the server and was answered"
    assert report["imported_on_the_loop"] == [], "the first client and request imported modules on the loop"
    assert report["ssl_contexts"] == 1, "each client built an SSL context of its own"
