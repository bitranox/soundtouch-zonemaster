"""The two TCP channels a slave opens to its master: transport (40002) and data (40003).

Both speak the IPC framing from ``ipc.py``. The transport channel carries the join sequence,
station changes and a 2-second ping; the data channel serves the byte-pull requests. Sequence
numbers are per connection and per direction, as the speakers do it.

The file is named for the connections and not for the channels although the protocol's own word
is "channel", which is the word used throughout below. The house has a second meaning for it: a
channel of the channel list is a station somebody dials, and that one lives in
``domain/channellist.py`` and ``application/zone_service/channels.py``. A reader opening a file
called channels.py in search of the station list found the frame traffic instead, which is the
one thing a filename is there to prevent.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NamedTuple, Protocol

from google.protobuf.message import DecodeError

from ...domain.enums import MsgTypeName
from . import ipc
from .clock import now_us
from .pb import audio, audio_data
from .reports import SlaveState, parse_track_data
from .source import RingOverrunError, StreamEndedError

if TYPE_CHECKING:
    from google.protobuf.message import Message

    from ...domain.logfn import LogFn
    from ...domain.station import Station
    from .source import StreamSource

__all__ = [
    "MAX_CHUNK_BYTES",
    "ChunkPlan",
    "ClosingWriter",
    "DataConnection",
    "PingingConnection",
    "StateReport",
    "TransportConnection",
    "chunk_plan",
    "close_quietly",
    "serve_data",
    "serve_transport",
    "state_name",
    "stop_pinging",
]

PING_INTERVAL_S = 2.0
DATA_WAIT_S = 20.0

DEFAULT_CHUNK_BYTES = 8192
"""What a request that names no size is served, which is what a real slave's first one does."""

CHUNK_ENVELOPE_HEADROOM_BYTES = 4096
"""Room for the envelope around a chunk: the ids, the sequence, and the clear-text typename."""

MAX_CHUNK_BYTES = ipc.MAX_FRAME_BYTES - CHUNK_ENVELOPE_HEADROOM_BYTES
"""The largest chunk that still fits a frame this master would accept from anyone else."""


class ChunkPlan(NamedTuple):
    """The two sizes one data request decides, so neither can be taken from the wire unbounded.

    A record rather than a bare number because the serving loop needs BOTH: how much to hand back
    (``serve``) and how few bytes are worth waking up for (``wait_for``). While only ``serve`` was
    returned, the loop reached past it for the raw ``min_byte_count`` - the very field the function
    exists to bound - and asked the ring to wait for it. Returning the pair leaves the caller
    nothing to reach for.
    """

    serve: int
    wait_for: int


def chunk_plan(byte_count: int, min_byte_count: int) -> ChunkPlan:
    """How many bytes to serve for one request, and how many to wait for, bounded at both ends.

    Both fields are ``int32`` on the wire and arrive from any device that can reach the LAN, so
    neither may be used as asked. A NEGATIVE ``byte_count`` used to collapse the pair to zero,
    and a zero-length read can never satisfy the caller's ``while not chunk`` loop: with the
    bytes already in the ring nothing in that loop suspends, so it spun inside the event loop and
    took the clock, the other slaves and the HTTP API down with it rather than one connection. A
    huge one used to build a reply past ``ipc.MAX_FRAME_BYTES``, the ceiling this master enforces
    on every frame it receives. Zero keeps its old meaning of "no preference".

    ``wait_for`` never exceeds ``serve``. A huge ``min_byte_count`` used to reach the ring's wait
    unbounded, where it can never be satisfied - the ring holds eight megabytes and the request
    can name two billion - so every request from that slave spent the full ``DATA_WAIT_S`` timing
    out with the bytes it wanted sitting in the ring, and its audio stopped while the master
    reported nothing wrong.
    """
    asked = byte_count if byte_count > 0 else DEFAULT_CHUNK_BYTES
    serve = min(max(asked, min_byte_count, 1), MAX_CHUNK_BYTES)
    return ChunkPlan(serve=serve, wait_for=min(max(min_byte_count, 1), serve))


# What the data channel asks the master for: which stream a request belongs to, and where in
# that stream this particular slave was placed.
class CannotServeError(Exception):
    """This one connection cannot be served, and ending it with a reason is the whole answer.

    Raised by what the servers below call INTO rather than by the servers themselves: the master
    hands a connection back this way when it has nothing to join it to. It says nothing is wrong
    with the peer and nothing is broken here, so it ends this connection and no other.
    """


ENDS_ONE_CONNECTION = (
    ConnectionError,  # the peer went away
    asyncio.IncompleteReadError,  # ... and it went away mid-frame
    ipc.FrameTooLargeError,  # a peer promising a frame far larger than anything real
    DecodeError,  # ... or sending bytes that are not the message they claim to be
    RingOverrunError,  # the ring kept going and this slave did not
    CannotServeError,  # there is nothing to serve this connection
)
"""What ends ONE connection with a logged reason, rather than killing its handler task.

Both servers below catch exactly this, from one tuple rather than two copies, because two copies
is how they came to disagree: the first four were in both and the last two in neither, so a ring
that had run past a silent slave and a transport arriving before the first station each died with
an unretrieved exception and left nothing in the log but the word closed. Anything not listed here
is a defect and has to keep travelling until somebody sees it.
"""


SourceLookup = Callable[[int], "StreamSource | None"]
BaseLookup = Callable[[str, int], Awaitable["int | None"]]


class StateReport(Protocol):
    """What the transport channel hands the master on every state report a slave sends."""

    def __call__(self, report: SlaveState) -> None: ...


SetURL = audio.AudioServerMsgSetURL
Transport = audio.AudioServerMsgTransportControl


def state_name(state: int) -> str:
    """The name of a reported playback state, or the number when the schema has no name for it.

    ``state`` is an ``int32`` from any device that can reach the transport port, and the recovered
    schema names only 0 to 6. protobuf's ``Name`` raises ``ValueError`` for anything else, which is
    not in ``ENDS_ONE_CONNECTION`` - so one frame carrying a 7, from newer firmware or from a peer
    sending nonsense, left the read loop with an unhandled exception and three-line traceback where
    a log line belongs. An unknown state is data, not a defect: it is reported as it arrived.
    """
    try:
        return audio.AudioServerMsgServerState.State.Name(state)
    except ValueError:
        return f"UNNAMED({state})"


def set_url_message(master_ip: str, station: Station, *, first: bool) -> audio.AudioServerMsgSetURL:
    """The SetURL a real master sends; ``force_connect`` only appears on the very first URL."""
    url = f"stream://{master_ip}:40003?no_delay&nonblocking&assuredforwarding&master=true&id={station.url_id}"
    if first:
        url += "&force_connect=true"
    return SetURL(
        url=url,
        passthrough=0,
        url_id=station.url_id,
        url_is_realtime=True,
        connection_timeout_in_ms=120000,
        buffering_timeout_in_ms=300000,
        source_dryup_timeout_ms=30000,
        source_retry_delay_ms=5000,
        source_retry_count=5,
        playbackstreamtype=SetURL.Wifi,
    )


class _Conn:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, log: LogFn, label: str) -> None:
        self.reader, self.writer, self.log, self.label = reader, writer, log, label
        self.peer = writer.get_extra_info("peername")[0]
        self._seq = 0
        self.splitter = ipc.FrameSplitter()

    async def send(self, msg_type: ipc.MsgType, payload: Message, *, reply_to: int | None = None) -> int:
        """Events and requests take the next number of this connection's own counter; a RESPONSE
        carries the sequence of the request it answers (the speakers correlate on it, and a data
        reply with the wrong number leaves the slave waiting forever - measured 2026-09-05)."""
        if reply_to is None:
            self._seq += 1
            seq = self._seq
        else:
            seq = reply_to
        self.writer.write(ipc.encode_frame(msg_type, payload, sequence=seq))
        await self.writer.drain()
        return seq

    async def ping_loop(self) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            await self.send(ipc.EVENT, audio.AudioServerMsgSlavePingMsg())

    async def frames(self) -> AsyncIterator[ipc.Frame]:
        while True:
            data = await self.reader.read(65536)
            if not data:
                return
            for body in self.splitter.feed(data):
                yield ipc.decode_frame(body)


@dataclass
class TransportConnection:
    """One slave's transport channel: join sequence, station switches, pings, state log."""

    conn: _Conn
    master_ip: str
    current: Station | None = None
    sent_first_url: bool = False
    last_state: str = ""
    on_state: StateReport | None = None

    async def join(self, station: Station, t0_us: int) -> None:
        await self.conn.send(
            ipc.REQUEST, audio.AudioServerMsgSetClockMasterMsg(clockMasterIp=self.master_ip, clockMasterPort=40005)
        )
        await self.conn.send(ipc.REQUEST, set_url_message(self.master_ip, station, first=not self.sent_first_url))
        self.sent_first_url = True
        await self.conn.send(
            ipc.REQUEST, Transport(control=Transport.PLAY, at_microseconds=t0_us, url_id=station.url_id)
        )
        self.current = station
        self.conn.log("transport", f"{self.conn.peer}: joined to url_id={station.url_id} t0_us={t0_us}")

    async def switch(self, station: Station, t0_us: int) -> bool:
        """The station-change sequence a real master sends; True if all of it reached the box.

        Unlike everything else a connection does, this is called from OUTSIDE its handler - by the
        pass putting a box back, and by a station change for every slave at once - so the handler's
        own ``ENDS_ONE_CONNECTION`` guard does not cover it. The box can hang up at any point of the
        sequence, above all in its one-second pause: measured 2026-09-24 14:05:26, the PLAY after it
        found the channel closed and the ``ConnectionResetError`` ended the whole service (OPEN-WORK
        rank 171). A channel ending here is that box's business, so it is said here, once, and
        answered as a switch that did not arrive; the handler is closing the channel anyway.
        """
        try:
            await self._send_the_switch(station, t0_us)
        except ENDS_ONE_CONNECTION as exc:
            self.conn.log("transport", f"{self.conn.peer}: gone in the middle of a station change ({exc!r})")
            return False
        return True

    async def _send_the_switch(self, station: Station, t0_us: int) -> None:
        """STOP, SetURL, PAUSE, ~1 s, PLAY."""
        if self.current is not None:
            # Nothing to stop before the join sequence has run; a first PLAY is not a switch.
            await self.conn.send(
                ipc.REQUEST, Transport(control=Transport.STOP, at_microseconds=now_us(), url_id=self.current.url_id)
            )
        await self.conn.send(ipc.REQUEST, set_url_message(self.master_ip, station, first=False))
        await self.conn.send(ipc.REQUEST, Transport(control=Transport.PAUSE, at_microseconds=0, url_id=station.url_id))
        await asyncio.sleep(1.0)
        await self.conn.send(
            ipc.REQUEST, Transport(control=Transport.PLAY, at_microseconds=t0_us, url_id=station.url_id)
        )
        self.current = station
        self.conn.log("transport", f"{self.conn.peer}: switched to url_id={station.url_id} t0_us={t0_us}")

    async def read_loop(self) -> None:
        async for f in self.conn.frames():
            if f.typename == MsgTypeName.SERVER_STATE and f.payload is not None:
                p = f.payload_as(audio.AudioServerMsgServerState)
                state = state_name(p.state)
                track = parse_track_data(p.trackData) if p.HasField("trackData") else None
                frames = "" if track is None else f" frames={track.frame_offset} latency_us={track.latency_microsecs}"
                line = f"{state} url_id={p.url_id} ms={p.milliseconds} byte={p.byte_offset}{frames}"
                self.conn.log("slave-state", f"{self.conn.peer}: {line}")
                self.last_state = line
                if self.on_state is not None:
                    self.on_state(
                        SlaveState(
                            peer=self.conn.peer,
                            url_id=p.url_id,
                            state=p.state,
                            milliseconds=p.milliseconds,
                            byte_offset=p.byte_offset,
                            frame_offset=None if track is None else track.frame_offset,
                        )
                    )
            elif f.typename != MsgTypeName.SLAVE_PING_RESPONSE:
                body = str(f.payload).replace("\n", " ") if f.payload is not None else f.raw_contents.hex()
                self.conn.log("slave-msg", f"{self.conn.peer}: {f.typename} {body[:200]}")


@dataclass
class _StreamCursor:
    """Where this slave sits in one stream: the byte it calls 0, and the next byte to serve."""

    base_offset: int
    next_offset: int


@dataclass
class DataConnection:
    """One slave's data channel: serve byte ranges from the current source ring."""

    conn: _Conn
    streams: dict[int, _StreamCursor] = field(default_factory=dict[int, _StreamCursor])
    served: int = 0
    encryption: audio_data.AudioServerMsgAcceptAudioData.EncryptionType = audio_data.AudioServerMsgAcceptAudioData.NONE

    async def read_loop(self, source_for: SourceLookup, base_for: BaseLookup) -> None:
        async for f in self.conn.frames():
            if f.typename == MsgTypeName.ACCEPT_AUDIO_DATA_REQUEST and f.payload is not None:
                await self._serve(f, source_for, base_for)
            elif f.typename != MsgTypeName.SLAVE_PING_RESPONSE:
                self.conn.log("data-msg", f"{self.conn.peer}: {f.typename}")

    async def _serve(self, f: ipc.Frame, source_for: SourceLookup, base_for: BaseLookup) -> None:
        req = f.payload_as(audio_data.AudioServerMsgAcceptAudioDataRequest)
        source: StreamSource | None = source_for(req.stream_id)
        if source is None:
            self.conn.log("data", f"{self.conn.peer}: request for unknown stream_id={req.stream_id}")
            return
        ring = source.ring
        first = req.stream_id not in self.streams
        if first:
            # The slave's first byte is whatever the master planned when it sent this slave its
            # PLAY: byte 0 of the stream for a cohort that starts together, the byte the zone will
            # reach at the joiner's own PLAY time for a late joiner (timeline.py). The slave counts
            # from that byte and renders it exactly at its at_microseconds, so it is reported as 0.
            base = await base_for(self.conn.peer, req.stream_id)
            if base is None:
                self.conn.log(
                    "data", f"{self.conn.peer}: no join planned for stream {req.stream_id}; serving from the ring start"
                )
                base = ring.start_offset
            self.streams[req.stream_id] = _StreamCursor(base_offset=base, next_offset=base)
        cursor = self.streams[req.stream_id]
        offset = cursor.next_offset
        plan = chunk_plan(req.byte_count, req.min_byte_count)
        want = plan.serve
        t_req = now_us()
        chunk = b""
        while not chunk:
            try:
                await ring.wait_for(offset, plan.wait_for, timeout=DATA_WAIT_S)
            except StreamEndedError as ended:
                # The one thing that may stop this loop. Waiting is right while bytes can still
                # arrive and only while: measured 2026-09-07, two of these outlived the zone's
                # dissolve by 28 minutes with no socket to either speaker open, because a timeout
                # and a finished stream look identical from in here (OPEN-WORK rank 28). The slave
                # is left unanswered on purpose - it has either been sent to another stream or is
                # being dissolved, and an empty chunk would tell it something else entirely.
                # The return is load-bearing rather than tidy: measured by mutation, a version that
                # logs and goes round again does not merely repeat itself, it SPINS - nothing in
                # the loop awaits once the wait raises at once - and freezes the whole event loop.
                self.conn.log("data", f"{self.conn.peer}: stream {req.stream_id} has ended at {offset}: {ended}")
                return
            except TimeoutError as exc:
                # Never answer with nothing: an empty chunk reads as end-of-data to the slave.
                self.conn.log("data", f"{self.conn.peer}: waiting on stream {req.stream_id} at {offset}: {exc}")
            chunk = ring.read(offset, want)
        waited_ms = (now_us() - t_req) // 1000
        cursor.next_offset = offset + len(chunk)
        self.served += len(chunk)
        reply = audio_data.AudioServerMsgAcceptAudioData(
            data=chunk,
            endofstream=False,
            byte_offset=offset - cursor.base_offset if first else 0,
            slave_underflow=False,
            encryption_type=self.encryption,
            stream_id=req.stream_id,
        )
        await self.conn.send(ipc.RESPONSE, reply, reply_to=f.sequence)
        self.conn.log(
            "data",
            f"{self.conn.peer}: s{req.stream_id} req {req.byte_count}/{req.min_byte_count}"
            f" -> {len(chunk)}B at {offset} wait {waited_ms} ms ring_end {ring.end_offset}",
        )


class ClosingWriter(Protocol):
    """What :func:`close_quietly` needs of a stream writer: the two halves of closing one.

    Narrower than ``asyncio.StreamWriter`` for the reason :class:`PingingConnection` gives: the
    rule is about the two calls and their exceptions, not about a socket, so a test proves it
    without opening one.
    """

    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...


async def close_quietly(writer: ClosingWriter) -> None:
    """Finish closing one connection, in the one way all three servers close theirs.

    ``close()`` only SCHEDULES the close; without the await the connection can still be half-open
    when the handler returns, and a slave reconnecting at once meets the one it just dropped.

    A peer that has already gone makes the wait raise, and that is nothing to report from a
    teardown - the connection is being closed either way.

    ``CancelledError`` is deliberately NOT suppressed, which is the difference from what the three
    handlers used to do inline. This is the last statement of each of them and nothing else is
    being waited on inside it, so a cancellation arriving here can only be the HANDLER's own -
    suppressed, the handler ran on past its own cancel and reported itself completed. The close
    above has already happened by then, so all that is given up by letting it through is the wait
    for a socket that is going away regardless.

    Written once rather than three times because the two calls are one rule and the difference
    between having the wait and not having it is invisible until the day a speaker reconnects
    inside it.
    """
    writer.close()
    with suppress(ConnectionError):
        await writer.wait_closed()


class PingingConnection(Protocol):
    """What :func:`stop_pinging` needs of a connection: how to say something about it.

    Narrower than ``_Conn`` on purpose - the rule it enforces is about tasks and not about
    sockets, so a test proves it without opening one.
    """

    label: str
    peer: str
    log: LogFn


async def stop_pinging(conn: PingingConnection, ping: asyncio.Task[None]) -> None:
    """End a connection's ping task and wait for it, without swallowing the CALLER's cancellation.

    Two traps in three lines, both measured on this program.

    ``await ping`` raises ``CancelledError`` for two reasons that cannot be told apart afterwards:
    the task just cancelled has died, which is what this is for, or the HANDLER itself is being
    cancelled, because a cancel of a task that is awaiting another task is delivered THROUGH that
    child. :func:`asyncio.wait` never re-raises what the awaited task raised, so the only
    cancellation that can arrive here is the handler's own, and it is meant to.

    And a ping that had already failed on ``drain`` re-raises that failure at the await. Suppressed
    for cancellation alone, it escaped from the handler's ``finally``, replaced the shutdown's own
    cancellation, and asyncio reported the whole chain as "Unhandled exception in
    client_connected_cb": three full tracebacks in the journal at the exact moment of a SIGINT
    stop, which is where somebody looking for a real fault starts reading (measured 2026-09-07
    21:10:20). The connection going away is WHY the pings stopped, so it is not news. Anything
    else is, and it is said in one line rather than a traceback, because a defect that ends a
    ping loop is still a defect.
    """
    ping.cancel()
    await asyncio.wait({ping})
    if ping.cancelled():
        return
    ended = ping.exception()
    if ended is not None and not isinstance(ended, ENDS_ONE_CONNECTION):
        conn.log(conn.label, f"{conn.peer}: ping loop ended with {ended!r}")


async def serve_transport(
    bind: str,
    log: LogFn,
    on_connect: Callable[[TransportConnection], Awaitable[None]],
    on_close: Callable[[TransportConnection], None] | None = None,
) -> asyncio.AbstractServer:
    """The transport channel on 40002, listening.

    ``on_connect`` and ``on_close`` are how the master learns about a slave without this
    module importing it; the layering only works while they stay callables.

    ``on_close`` is handed the CONNECTION and not its address, because a box holds more than one
    of these at a time and an address does not say which of them ended (measured 2026-09-07; see
    ``master.SlaveTransports``).
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = _Conn(reader, writer, log, "transport")
        log("transport", f"{conn.peer}: connected")
        tc = TransportConnection(conn=conn, master_ip=bind)
        ping = asyncio.create_task(conn.ping_loop())
        try:
            await on_connect(tc)
            await tc.read_loop()
        except ENDS_ONE_CONNECTION as exc:
            log("transport", f"{conn.peer}: {exc!r}")
        finally:
            await stop_pinging(conn, ping)
            if on_close:
                on_close(tc)
            log("transport", f"{conn.peer}: closed")
            await close_quietly(writer)

    return await asyncio.start_server(handle, bind, 40002)


async def serve_data(
    bind: str,
    log: LogFn,
    source_for: SourceLookup,
    encryption: audio_data.AudioServerMsgAcceptAudioData.EncryptionType,
    base_for: BaseLookup,
) -> asyncio.AbstractServer:
    """The data channel on 40003, listening.

    ``source_for`` finds the stream a slave asked for and ``base_for`` the absolute byte it
    starts at, both looked up per connection because a joiner's start is decided when its
    PLAY is sent, not when this server was built.
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = _Conn(reader, writer, log, "data")
        log("data", f"{conn.peer}: connected")
        dc = DataConnection(conn=conn, encryption=encryption)
        ping = asyncio.create_task(conn.ping_loop())
        try:
            await dc.read_loop(source_for, base_for)
        except ENDS_ONE_CONNECTION as exc:
            log("data", f"{conn.peer}: {exc!r}")
        finally:
            await stop_pinging(conn, ping)
            log("data", f"{conn.peer}: closed after {dc.served} bytes")
            await close_quietly(writer)

    return await asyncio.start_server(handle, bind, 40003)
