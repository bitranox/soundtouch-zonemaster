"""The master's face on port 8090: the handful of speaker-API calls a slave makes to its master.

Slaves call ``GET /now_playing`` after joining, ``POST /slaveMsg`` when a preset is pressed on
them, and ``POST /removeZoneSlave`` when they power off; they also POST ``/notification`` chatter.
Everything else answers a bare ``<status>`` so a curious slave never sees an error.

A ``/slaveMsg`` carries one of two things, measured 2026-09-06: a ContentItem, which is a preset
press and becomes a select, or a ``keyData`` element, which is next, previous or a thumb and
carries no content at all. The second kind becomes a :class:`~soundtouch_zonemaster.events.SpeakerEvent` on
the queue the service also feeds from its observers, so there is ONE stream rather than two halves
joined later. The event names the speaker by ADDRESS, because the body carries no device id; the
registry is what turns one into the other.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ConfigDict

from ...domain.enums import HttpMethod, HttpStatusLine, SlaveAction, SpeakerPath
from ...domain.events import KeyPress, SpeakerEvent
from ...domain.xmlfmt import attr, text
from . import xmlmodels
from .connections import close_quietly
from .xmlread import attribute_anywhere, child_tag, element_anywhere, parse, serialised_as

if TYPE_CHECKING:
    from ...domain.logfn import LogFn
    from .xmlread import Element

__all__ = [
    "MAX_BODY_BYTES",
    "HttpApi",
    "MasterPort",
    "Request",
    "key_press",
    "key_press_in",
    "parse_request",
    "read_request",
    "serve_http",
]


class MasterPort(Protocol):
    """What the speaker API needs from the master behind it.

    Naming the calls here keeps the HTTP face from reaching into the master for anything else,
    and lets a test drive it with a stand-in.
    """

    device_id: str

    def info_xml(self) -> str: ...

    def now_playing_xml(self) -> str: ...

    def zone_xml(self) -> str: ...

    async def select(self, content_item_xml: str, *, origin: str) -> None: ...

    async def slave_left(self, ip: str) -> None: ...


Handler = Callable[["Request"], Awaitable[str]]


class Request(BaseModel):
    """One parsed HTTP request from a slave: the boundary this face reads the world through.

    ``method`` and ``path`` stay plain strings because a speaker may send anything at all; they
    are compared against :class:`HttpMethod` and :class:`SpeakerPath` members rather than bare
    literals, and an unrecognised path is echoed back as a bare status.
    """

    model_config = ConfigDict(frozen=True)

    method: str
    path: str
    body: str
    peer: str


def parse_request(raw: bytes, peer: str) -> Request:
    """One raw HTTP request as a validated record.

    ``peer`` comes from the socket rather than the request, because a slave identifies
    itself by the address it connected from and a body could claim anything.
    """
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("utf-8", "replace").split("\r\n")
    method, path, *_ = lines[0].split(" ")
    return Request(method=method, path=path.split("?")[0], body=body.decode("utf-8", "replace"), peer=peer)


def _response(xml: str, status: HttpStatusLine = HttpStatusLine.OK) -> bytes:
    body = xml.encode()
    return (
        f"HTTP/1.1 {status}\r\nContent-Type: text/xml\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode() + body


def key_press(body: str) -> KeyPress | None:
    """The ``keyData`` element of a slaveMessage, or ``None`` when the body carries none.

    The body is read by ``xmlread.parse``, which bounds its size, refuses a DOCTYPE outright and
    bounds its depth: these bodies arrive from the unauthenticated LAN side, so what may be parsed
    at all is decided in one place rather than at each field.

    The state is REPORTED, never filtered here. Every key arrives twice, as ``press`` and then
    ``release`` 365 to 444 ms later, and a reader that drops one of them makes the double arrival
    invisible to whoever later has to count presses.
    """
    root = parse(body)
    if root is None:
        return None
    return key_press_in(root)


def key_press_in(root: Element) -> KeyPress | None:
    """The same, from a body already parsed, so that one request is parsed once."""
    found = element_anywhere(root, "keyData")
    data = xmlmodels.key_data(found) if found is not None else None
    if data is None:
        return None
    return KeyPress(key=data.key.strip(), state=data.state, sender=data.sender)


def _notification_tag(body: str) -> str | None:
    """What a ``/notification`` body says happened: the first child of its ``<updates>`` element."""
    root = parse(body)
    updates = element_anywhere(root, "updates") if root is not None else None
    return child_tag(updates) if updates is not None else None


def status_xml(path: str) -> str:
    """The catch-all reply. ``path`` comes off the request line, so it is escaped, not echoed."""
    return f'<?xml version="1.0" encoding="UTF-8" ?><status>{text(path)}</status>'


class HttpApi:
    """Routes the speaker-API paths to the master; unknown paths get a status echo."""

    def __init__(
        self,
        master: MasterPort,
        log: LogFn,
        *,
        events: asyncio.Queue[SpeakerEvent] | None = None,
    ) -> None:
        self.master = master
        self.log = log
        self.events = events
        """Where a forwarded key press goes. ``None`` means nobody is listening, which is the
        prototype run: a press is still logged, and still must not take the HTTP face down."""
        # The event loop holds only a weak reference to a running task, so a task nobody
        # else references can be collected mid-flight and its work silently never happen.
        # A slave's select is exactly that kind of work, so hold every spawned task here.
        self._background: set[asyncio.Task[None]] = set()
        self._closed = False

    async def aclose(self) -> None:
        """Refuse further background work and wait for what is running to finish being cancelled.

        A select spawned just before a run ends would otherwise still be switching speakers while
        the zone is being dissolved, so a speaker starts playing again after being told the zone is
        over. In a flat that is the least forgivable failure this program has, which is why the
        wait is here rather than a hope that the tasks lose the race.

        Idempotent: the shutdown path calls it at the dissolve and again at the stop.
        """
        self._closed = True
        running = list(self._background)
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
            self.log("http", f"shutting down: cancelled {len(running)} background task(s)")

    def _spawn(self, coro: Coroutine[Any, Any, None], what: str) -> None:
        """Run ``coro`` in the background, keeping it referenced until it finishes."""
        if self._closed:
            # Closing it keeps Python from warning about a coroutine that was never awaited, and
            # says plainly that the work was dropped rather than lost.
            coro.close()
            self.log("http", f"{what} dropped: the master is shutting down")
            return
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        task.add_done_callback(lambda t: self._report(t, what))

    def _report(self, task: asyncio.Task[None], what: str) -> None:
        """Log what a finished background task raised; an unretrieved exception is invisible."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.log("http", f"{what} failed: {exc!r}")

    async def handle(self, req: Request) -> str:
        if req.method == HttpMethod.GET:
            return await self._get(req)
        return await self._post(req)

    async def _get(self, req: Request) -> str:
        m = self.master
        if req.path == SpeakerPath.INFO:
            return m.info_xml()
        if req.path in (SpeakerPath.NOW_PLAYING, SpeakerPath.NOW_PLAYING_CAMEL):
            return m.now_playing_xml()
        if req.path == SpeakerPath.GET_ZONE:
            return m.zone_xml()
        if req.path == SpeakerPath.PRESETS:
            return '<?xml version="1.0" encoding="UTF-8" ?><presets />'
        if req.path == SpeakerPath.VOLUME:
            return (
                f'<?xml version="1.0" encoding="UTF-8" ?><volume deviceID="{attr(m.device_id)}">'
                "<targetvolume>20</targetvolume><actualvolume>20</actualvolume>"
                "<muteenabled>false</muteenabled></volume>"
            )
        self.log("http", f"{req.peer}: unhandled GET {req.path}")
        return status_xml(req.path)

    async def _slave_msg(self, req: Request) -> None:
        """A preset press becomes a select; a key press becomes an event on the queue.

        The body is parsed ONCE here and the three readings share the tree, so a body this master
        will not read costs one refusal rather than one per field.
        """
        root = parse(req.body)
        action = attribute_anywhere(root, "action") if root is not None else None
        self.log("http", f"{req.peer}: slaveMsg action={action or '?'} body={req.body[:BODY_LOG_CHARS]}")
        if root is None:
            return
        press = key_press_in(root)
        if press is not None:
            await self._put_key(req.peer, press, req.body)
            return
        item = element_anywhere(root, "content")
        if item is not None and action == SlaveAction.SELECT:
            content_item = serialised_as(item, "ContentItem")
            self._spawn(self.master.select(content_item, origin=req.peer), f"selection from {req.peer}")

    async def _put_key(self, peer: str, press: KeyPress, body: str) -> None:
        """Hand the press on, or say that nothing was listening. Never fatal either way."""
        if self.events is None:
            self.log("http", f"{peer}: key {press.key} {press.state} with no listener")
            return
        await self.events.put(
            SpeakerEvent(
                received_at=time.time(),
                speaker=peer,
                device_id="",
                kind=SlaveAction.KEY,
                key=press,
                frame=body,
            )
        )

    async def _post(self, req: Request) -> str:
        m = self.master
        if req.path == SpeakerPath.SLAVE_MSG:
            await self._slave_msg(req)
        elif req.path == SpeakerPath.REMOVE_ZONE_SLAVE:
            root = parse(req.body)
            sender = attribute_anywhere(root, "senderIPAddress") if root is not None else None
            self.log("http", f"{req.peer}: removeZoneSlave from {sender or '?'}")
            self._spawn(m.slave_left(req.peer), f"slave_left for {req.peer}")
        elif req.path == SpeakerPath.NOTIFICATION:
            tag = _notification_tag(req.body)
            self.log("http", f"{req.peer}: notification {tag if tag else req.body[:60]}")
        else:
            self.log("http", f"{req.peer}: POST {req.path} {req.body[:120]}")
        return status_xml(req.path)


async def serve_http(bind: str, api: HttpApi, log: LogFn) -> asyncio.AbstractServer:
    """The speaker HTTP API on 8090. One bad request is answered and logged, never fatal."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")[0]
        try:
            raw = await asyncio.wait_for(read_request(reader), 10.0)
            req = parse_request(raw, peer)
            xml = await api.handle(req)
            writer.write(_response(xml))
            await writer.drain()
        except Exception as exc:  # noqa: BLE001 - one bad request must not take the server down
            log("http", f"{peer}: {exc!r}")
            try:
                writer.write(_response(status_xml(SpeakerPath.ERROR), HttpStatusLine.INTERNAL_SERVER_ERROR))
                await writer.drain()
            except Exception as unreachable:  # noqa: BLE001 - the peer is gone; say so and move on
                log("http", f"{peer}: could not send the error response: {unreachable!r}")
        finally:
            await close_quietly(writer)

    return await asyncio.start_server(handle, bind, 8090)


MAX_BODY_BYTES = 1 << 20
"""Refuse a larger request body than this.

The bodies here are short XML fragments. The ten-second read timeout in ``serve_http`` bounds
how LONG a caller may take, never how MUCH it may send, so on a LAN it allows megabytes.
"""

BODY_LOG_CHARS = 400
"""How much of a slaveMessage body reaches the log.

A slaveMessage carrying keyData is short; one carrying a ContentItem is around 400 characters.
The whole message is logged rather than the fields this code recognises, because what a speaker
sends for a key that has no content is not known and is exactly what a run has to show.
"""


async def read_request(reader: asyncio.StreamReader) -> bytes:
    """Head and body of one request, refusing a body over :data:`MAX_BODY_BYTES`."""
    head = await reader.readuntil(b"\r\n\r\n")
    m = re.search(rb"Content-Length:\s*(\d+)", head, re.IGNORECASE)
    if m is None:
        return head
    declared = int(m.group(1))
    if declared > MAX_BODY_BYTES:
        msg = f"Content-Length {declared} over the {MAX_BODY_BYTES} byte limit"
        raise ValueError(msg)
    return head + await reader.readexactly(declared)
