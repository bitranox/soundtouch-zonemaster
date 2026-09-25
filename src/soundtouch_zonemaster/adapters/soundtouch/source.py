"""The station source: fetch the stream once, keep a ring of recent bytes with absolute offsets.

The zone master forwards the compressed source stream unchanged; slaves decode themselves. So the
master's only job here is to pull the station (following the local service's redirect to the real
stream) and remember the last few hundred kilobytes, addressed by absolute byte offset since the
station started. A slave that joins late is started a little behind the live edge and pulls
forward from there.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...domain.enums import ContentType
from ...domain.frames import FrameIndex, find_frame
from ...domain.timeline import FrameTimeline, ZoneTimeline

if TYPE_CHECKING:
    from ...domain.logfn import LogFn
    from ...domain.station import Station
    from .clock import Clock

__all__ = [
    "START_DELAY_US",
    "PlaybackDescriptor",
    "RingBuffer",
    "RingOverrunError",
    "StreamEndedError",
    "StreamSource",
]

START_DELAY_US = 3_000_000

_EARLY_END_SECONDS = 5.0
"""A stream that ended sooner than this is a broken connection, not a finished station."""

_FETCH_REPORT_INTERVAL_SECONDS = 5.0
"""How often the fetch loop logs its byte total; often enough to see a stall, rare enough to read."""


class RingOverrunError(LookupError):
    """The ring dropped the byte a caller still wants, because it kept going and the caller did not.

    A subclass of ``LookupError`` so the old contract still holds, and its OWN type because that is
    the whole point: at roughly eight megabytes the ring holds a few minutes of a 128 kbit/s
    station, so a slave off the radio for longer comes back asking for a byte that is gone. That is
    ordinary. Catching a bare ``LookupError`` to handle it would also swallow every ``KeyError``
    and ``IndexError`` raised anywhere under the same try, which is a different bug wearing this
    one's clothes.
    """


class StreamEndedError(LookupError):
    """A wait that can never be satisfied, because the source that filled this ring has stopped.

    Its own type, and not a ``TimeoutError``, because the whole defect it exists for is that the two
    are indistinguishable to the waiter and mean opposite things. A timeout says "not yet, ask
    again", which is right for a station that is slow; this says "never", which is the only thing
    that can stop a loop whose contract is that it may not answer a slave with nothing.

    Measured 2026-09-07: without it, two data loops outlived the zone's dissolve by 28 minutes,
    writing a line every 20 s each, with no socket to either speaker open (OPEN-WORK rank 28).
    """


class RingBuffer:
    """Bytes with absolute offsets; the oldest are dropped past ``max_bytes``."""

    def __init__(self, max_bytes: int = 8_000_000) -> None:
        self.max_bytes = max_bytes
        self._data = bytearray()
        self.start_offset = 0
        self.changed = asyncio.Condition()
        self.ended = False
        """Whether the source that fills this ring has stopped, so no byte can ever be added.

        Set once and never cleared: a source is not restarted, a new station gets a new ring.
        """

    @property
    def end_offset(self) -> int:
        return self.start_offset + len(self._data)

    async def append(self, chunk: bytes) -> None:
        async with self.changed:
            self._data += chunk
            overflow = len(self._data) - self.max_bytes
            if overflow > 0:
                del self._data[:overflow]
                self.start_offset += overflow
            self.changed.notify_all()

    async def end(self) -> None:
        """Say that no byte will ever be added, and wake everybody waiting for one.

        Waking them is half the point. A waiter is asleep inside the condition when a zone
        dissolves, so a flag nobody is notified about would only be read on the next timeout - and
        the timeout is exactly what this exists to stop being the answer.
        """
        async with self.changed:
            self.ended = True
            self.changed.notify_all()

    def read(self, offset: int, count: int) -> bytes:
        if offset < self.start_offset:
            raise RingOverrunError(f"offset {offset} already dropped (ring starts at {self.start_offset})")
        rel = offset - self.start_offset
        return bytes(self._data[rel : rel + count])

    async def wait_for(self, offset: int, count: int, *, timeout: float) -> None:
        """Block until ``count`` bytes from ``offset`` are present.

        Raises ``TimeoutError`` if they have not arrived in time, and :class:`StreamEndedError` if
        they never can. An ended ring RETURNS rather than raising while it still holds bytes at
        ``offset``: ending is not discarding, and what is already there is still owed to the caller
        that asked for it - the caller reads what there is and the loop above it is satisfied.
        """
        deadline = time.monotonic() + timeout
        async with self.changed:
            while self.end_offset < offset + count:
                if self.ended:
                    if self.end_offset > offset:
                        return
                    raise StreamEndedError(f"stream ended at {self.end_offset}, nothing at {offset}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"only {self.end_offset - offset} of {count} bytes at {offset}")
                # A wait that times out is not the caller's timeout: the deadline above owns
                # that. Re-check the condition instead.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.changed.wait(), remaining)


@dataclass
class StreamSource:
    """Fetches one station into a RingBuffer; ``t0_us`` is the master-clock time of byte 0."""

    station: Station
    ring: RingBuffer
    log: LogFn
    t0_us: int | None = None
    timeline: ZoneTimeline = field(default_factory=lambda: ZoneTimeline(t0_us=0))
    content_type: str = ""
    bytes_total: int = 0
    _task: asyncio.Task[None] | None = field(default=None, repr=False)
    frames: FrameIndex = field(init=False, repr=False)
    frame_timeline: FrameTimeline | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.frames = FrameIndex(self.ring)

    def begin_at(self, t0_us: int) -> None:
        """Set byte 0's master-clock time, on the source AND on its byte timeline.

        The two must never disagree. A slave's PLAY carries ``t0_us`` while every placement is
        measured from ``timeline.t0_us``, so writing one alone tells the zone that its own stream
        began at clock zero: ``on_slave_state`` then reads each report as if PLAY had happened in
        1970, the ``sync`` line reports nonsense, and every later joiner is placed against it.
        Two callers set a start time, and having one method is what stops the second forgetting.

        The restart in ``placement.py`` is the exception and stays as it is: it replaces the whole
        timeline because it also moves byte 0, which is a different thing from naming its time.
        """
        self.t0_us = t0_us
        self.timeline.t0_us = t0_us

    def frame_position(self) -> FrameTimeline | None:
        """The zone's position in frames, created once the ring has shown what a frame is.

        Anchored on the stream's start: the first slave renders the first frame at or after
        byte ``timeline.t0_byte`` at ``t0_us``. Reset with the byte timeline on a restart.
        """
        if self.frame_timeline is None and self.t0_us is not None and self.frames.duration_us:
            first = self.frames.index_at_or_after(self.timeline.t0_byte)
            if first is not None:
                self.frame_timeline = FrameTimeline(duration_us=self.frames.duration_us, t0_us=self.t0_us, frame0=first)
        return self.frame_timeline

    def start(self, clock_now_us: Clock) -> None:
        self._task = asyncio.create_task(self._run(clock_now_us), name=f"source-{self.station.url_id}")

    async def stop(self) -> None:
        """End the fetch task and wait for it, without swallowing the caller's OWN cancellation.

        Waited for with :func:`asyncio.wait` rather than ``await task``, because that await raises
        CancelledError for two reasons that cannot be told apart afterwards: the task just
        cancelled has died, which is what this method is for, or the CALLER is itself being
        cancelled - cancelling a task that is awaiting another task is delivered THROUGH that
        child. Reading the second as the first leaves the caller alive after its own cancellation:
        measured 2026-09-07, a stop that landed inside a station switch left the service's dialling
        worker running, so the ``finally`` that dissolves the zone waited for it for ever and the
        speakers would have been left in a zone whose master had gone. ``wait`` never re-raises
        what the awaited task raised, so the only cancellation that can arrive here is the
        caller's, and it is meant to.
        """
        # Before the cancel, and before the early return: a source with no task still owns a ring
        # somebody may be waiting on, and the waiter has to be woken either way.
        await self.ring.end()
        task = self._task
        if task is None:
            return
        task.cancel()
        await asyncio.wait({task})
        if task.cancelled():
            self.log("source", f"{self.station.url_id}: fetch task cancelled")
            return
        ended = task.exception()
        if ended is not None:
            self.log("source", f"{self.station.url_id}: fetch task ended with {ended!r}")

    async def _run(self, clock_now_us: Clock) -> None:
        backoff = 1.0
        while True:
            started = time.monotonic()
            try:
                await self._fetch_once(clock_now_us)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a live stream drops; reconnect with backoff
                self.log("source", f"url_id={self.station.url_id} fetch failed: {exc!r}")
            # A stream that ended quickly is a broken one, not a finished one: keep backing off.
            if time.monotonic() - started < _EARLY_END_SECONDS:
                self.log("source", f"url_id={self.station.url_id} ended early; retry in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            else:
                backoff = 1.0

    async def _fetch_once(self, clock_now_us: Clock) -> None:
        timeout = httpx.Timeout(connect=15.0, read=30.0, write=15.0, pool=15.0)
        headers = {"User-Agent": "Bose_Lisa/27.0.6", "Icy-MetaData": "0"}
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers=headers) as client:
            url = await resolve_stream_url(client, self.station.playback_url, self.log)
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                self.content_type = resp.headers.get("content-type", "")
                self.log(
                    "source", f"url_id={self.station.url_id} {resp.status_code} {self.content_type} from {resp.url}"
                )
                last_report = time.monotonic()
                async for chunk in resp.aiter_bytes(8192):
                    if self.t0_us is None:
                        # Byte 0 is scheduled START_DELAY after it arrived: that delay IS the slaves'
                        # buffer against the live edge. A real master has its own ~2 s of buffering
                        # before it plays, and its slaves inherit that margin the same way.
                        self.begin_at(clock_now_us() + START_DELAY_US)
                        frame = find_frame(chunk, 0)
                        head = (
                            "no confirmed frame in the first chunk"
                            if frame is None
                            else "byte 0 is a frame start"
                            if frame.start == 0
                            else f"first frame start at byte {frame.start}"
                        )
                        self.log(
                            "source",
                            f"url_id={self.station.url_id} first bytes; t0_us={self.t0_us} "
                            f"(+{START_DELAY_US // 1000} ms); {head}",
                        )
                    self.bytes_total += len(chunk)
                    await self.ring.append(chunk)
                    if time.monotonic() - last_report >= _FETCH_REPORT_INTERVAL_SECONDS:
                        last_report = time.monotonic()
                        ring = f"{self.ring.start_offset}..{self.ring.end_offset}"
                        self.log(
                            "source",
                            f"url_id={self.station.url_id} fetched {self.bytes_total} B, ring {ring}",
                        )
                self.log("source", f"url_id={self.station.url_id} stream ended after {self.bytes_total} B")


PLAYLIST_TYPES = frozenset(
    {
        ContentType.M3U,
        ContentType.APPLE_M3U,
        ContentType.PLS,
        ContentType.TEXT,
        ContentType.PLS_XML,
    }
)

PLAYLIST_SUFFIXES = (".m3u", ".m3u8", ".pls")
"""A station that serves a playlist under an audio content type is recognised by its URL instead."""


class _AudioBlock(BaseModel):
    """The ``audio`` object of a playback descriptor. Only the stream URL is used."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    stream_url: str | None = Field(default=None, alias="streamUrl")


class PlaybackDescriptor(BaseModel):
    """The JSON the local service answers for a station's playback URL (REPORT.md S4).

    Parsed at the boundary rather than walked key by key. Every field is optional and unknown
    keys are ignored on purpose: a station's descriptor is whatever that station feels like
    sending, so this validates the shape it promises without refusing the ones it does not.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    audio: _AudioBlock | None = None

    @classmethod
    def parse(cls, text: str) -> PlaybackDescriptor | None:
        """The descriptor in ``text``, or None when it is not one this master can read."""
        try:
            return cls.model_validate_json(text)
        except ValidationError:
            return None

    def stream_url(self) -> str | None:
        """The URL that actually streams audio, or None when the descriptor names none."""
        if self.audio is None or not self.audio.stream_url:
            return None
        return self.audio.stream_url


def _looks_like_a_playlist(ctype: str, url: str) -> bool:
    """Whether this response is a playlist, by content type or by the URL's suffix."""
    return ctype in PLAYLIST_TYPES or url.lower().endswith(PLAYLIST_SUFFIXES)


async def resolve_stream_url(client: httpx.AsyncClient, playback_url: str, log: LogFn, hops: int = 3) -> str:
    """Turn the local service's playback URL into the URL that actually streams audio.

    The speakers' LOCAL_INTERNET_RADIO source asks the service for a JSON descriptor
    (``{"audio": {"streamUrl": ...}}``) and fetches the stream itself; a station may then hand out
    an .m3u/.pls playlist whose first entry is the stream. This follows both, a few hops at most.
    """
    url = playback_url
    for _ in range(hops):
        ctype, text = await _peek(client, url)
        if ctype == ContentType.JSON:
            descriptor = PlaybackDescriptor.parse(text)
            if descriptor is None:
                # A descriptor that will not parse is not a broken connection, so there is
                # nothing for the fetch loop's backoff to retry: say so, and try the playback
                # URL as the stream rather than failing the station outright.
                log("source", f"playback descriptor at {url} did not parse; using it as the stream")
                return url
            url = descriptor.stream_url() or url
            log("source", f"playback descriptor -> {url}")
            continue
        if _looks_like_a_playlist(ctype, url):
            lines = [ln.strip() for ln in text.splitlines()]
            candidates = [
                ln.split("=", 1)[1] if ln.lower().startswith("file") else ln
                for ln in lines
                if "http" in ln and not ln.startswith("#")
            ]
            if candidates:
                url = candidates[0].strip()
                log("source", f"playlist -> {url}")
                continue
        return url
    return url


async def _peek(client: httpx.AsyncClient, url: str, limit: int = 65536) -> tuple[str, str]:
    """Content type plus at most ``limit`` body bytes; a live stream never ends, so never read it whole."""
    async with client.stream("GET", url) as resp:
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if not (ctype == ContentType.JSON or _looks_like_a_playlist(ctype, url)):
            return ctype, ""
        buf = bytearray()
        async for chunk in resp.aiter_bytes(8192):
            buf += chunk
            if len(buf) >= limit:
                break
        return ctype, buf.decode("utf-8", "replace")
