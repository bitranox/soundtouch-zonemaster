"""AfterTouch's device list on loopback: the one GET the service learns the speakers from.

Raw HTTP rather than a framework, for the same reason ``speaker_double.py`` is raw: what is under
test is what comes back off a socket. It binds port 0 so a test never collides with the real
service, and reports the paths it was asked for.

``body`` and ``status_line`` are plain attributes rather than constructor-only values, because a
service that runs for days meets a registry that changes and one that goes away, and both are
things a test has to be able to do to it while it is running.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

__all__ = ["DEVICES_FIXTURE", "FakeRegistry", "devices_at"]

HOST = "127.0.0.1"
DEVICES_FIXTURE = Path(__file__).parent / "fixtures" / "aftertouch-devices.json"
"""The real SHAPE with invented values: the live response holds the flat's addresses, MACs and
serial numbers, and none of those has a redaction rule."""


def devices_at(addresses: dict[str, str]) -> str:
    """The fixture with each named device moved to a given address, as a JSON body.

    A test that wants a speaker it can actually reach has to say where it is, and the fixture's
    documentation addresses are deliberately unroutable. Everything else stays as measured.
    """
    entries: list[dict[str, object]] = json.loads(DEVICES_FIXTURE.read_text(encoding="utf-8"))
    for entry in entries:
        address = addresses.get(str(entry["device_id"]))
        if address is not None:
            entry["ip_address"] = address
    return json.dumps(entries)


class FakeRegistry:
    """The slice of AfterTouch the service reads: one GET that returns a JSON array."""

    def __init__(self, body: str, *, status_line: str = "200 OK") -> None:
        self.body = body
        self.status_line = status_line
        self.paths: list[str] = []
        self._server: asyncio.AbstractServer | None = None
        self.port = 0
        self._callers: set[asyncio.StreamWriter] = set()
        """Whoever is connected right now, so stopping can drop them rather than wait for them."""

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, HOST, 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        """Stop listening and drop whoever is still connected, in that order.

        The drop is what keeps this bounded: ``wait_closed`` waits for every client transport to
        be gone, and a caller that vanished without closing its socket - what a cancelled HTTP
        call leaves behind - would hold it for ever. Same reasoning as ``FakeSpeaker.stop``.
        """
        if self._server is not None:
            self._server.close()
            for caller in list(self._callers):
                caller.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def base_url(self) -> str:
        return f"http://{HOST}:{self.port}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._callers.add(writer)
        try:
            await self._answer_one(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass  # the list was stopped under this caller, or the caller went away; neither is news
        finally:
            self._callers.discard(writer)
            writer.close()

    async def _answer_one(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        request_line = head.decode("utf-8", "replace").split("\r\n")[0]
        self.paths.append(request_line.split(" ")[1])
        payload = self.body.encode()
        writer.write(
            f"HTTP/1.1 {self.status_line}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            f"Connection: close\r\n\r\n".encode()
        )
        writer.write(payload)
        await writer.drain()
        writer.close()
