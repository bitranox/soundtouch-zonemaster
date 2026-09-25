"""The ceilings on what an unauthenticated peer can make the master hold.

Nothing on the LAN authenticates - that is the protocol, not a choice this program made - so
every channel here takes bytes from whoever can reach the port. These tests pin the three places
where "whoever" used to be able to grow the master without limit.
"""

from __future__ import annotations

import asyncio
import struct
import time

import pytest

from soundtouch_zonemaster.adapters.soundtouch import connections, ipc
from soundtouch_zonemaster.adapters.soundtouch.clock import (
    CLIENT_TTL_US,
    CLOCK_MAGIC,
    CLOCK_VERSION,
    MAX_CLIENTS,
    ClockSyncProtocol,
    SyncPacket,
)
from soundtouch_zonemaster.adapters.soundtouch.http_api import MAX_BODY_BYTES, read_request
from soundtouch_zonemaster.adapters.soundtouch.pb import audio, audio_data


def _prefixed(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + payload


def test_a_frame_within_the_limit_still_splits() -> None:
    """The control: the ceiling must not break the traffic it is there to survive."""
    splitter = ipc.FrameSplitter()
    assert splitter.feed(_prefixed(b"hello") + _prefixed(b"there")) == [b"hello", b"there"]


def test_a_frame_longer_than_the_limit_is_refused_before_the_bytes_arrive() -> None:
    """The prefix is an unsigned 32-bit count, so a peer can promise four gigabytes."""
    splitter = ipc.FrameSplitter()
    with pytest.raises(ipc.FrameTooLargeError) as excinfo:
        splitter.feed(struct.pack(">I", 0xFFFFFFFF) + b"only a few bytes actually sent")
    assert excinfo.value.length == 0xFFFFFFFF


def test_whole_frames_that_arrived_ahead_of_an_oversized_one_are_not_lost_with_it() -> None:
    """One TCP read can carry several frames, and the refusal cannot throw away the good ones.

    ``feed`` splits what it can and raises when it meets a prefix over the ceiling, so everything
    it had already split was collected in a local that died with the exception. Today's only
    caller drops the connection either way, which is what made this harmless and invisible; the
    frames are carried out on the refusal so that it stays harmless for a caller that does not.
    """
    splitter = ipc.FrameSplitter()

    with pytest.raises(ipc.FrameTooLargeError) as excinfo:
        splitter.feed(_prefixed(b"hello") + _prefixed(b"there") + struct.pack(">I", 0xFFFFFFFF))

    assert excinfo.value.length == 0xFFFFFFFF
    assert excinfo.value.frames_before_it == (b"hello", b"there")


def test_the_limit_is_the_boundary_not_an_approximation() -> None:
    splitter = ipc.FrameSplitter()
    splitter.feed(struct.pack(">I", ipc.MAX_FRAME_BYTES))  # accepted, simply incomplete
    with pytest.raises(ipc.FrameTooLargeError):
        ipc.FrameSplitter().feed(struct.pack(">I", ipc.MAX_FRAME_BYTES + 1))


class _Reader:
    """The slice of asyncio.StreamReader that read_request uses, over a fixed buffer."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    async def readuntil(self, sep: bytes) -> bytes:
        end = self._data.index(sep, self._pos) + len(sep)
        head, self._pos = self._data[self._pos : end], end
        return head

    async def readexactly(self, n: int) -> bytes:
        chunk = self._data[self._pos : self._pos + n]
        if len(chunk) < n:
            raise asyncio.IncompleteReadError(chunk, n)
        self._pos += n
        return chunk


async def test_a_request_within_the_body_limit_is_read_whole() -> None:
    reader = _Reader(b"POST /x HTTP/1.1\r\nContent-Length: 5\r\n\r\nhello")
    assert (await read_request(reader)).endswith(b"hello")  # type: ignore[arg-type]


async def test_a_request_declaring_a_huge_body_is_refused_without_reading_it() -> None:
    """The read timeout bounds how LONG a caller may take, never how MUCH it may send."""
    declared = MAX_BODY_BYTES + 1
    reader = _Reader(f"POST /x HTTP/1.1\r\nContent-Length: {declared}\r\n\r\n".encode())
    with pytest.raises(ValueError, match="over the"):
        await read_request(reader)  # type: ignore[arg-type]


def _sync_request(t1: int) -> bytes:
    return SyncPacket(CLOCK_MAGIC, CLOCK_VERSION, t1, 0, 0, 0, 0).pack()


class _Transport(asyncio.DatagramTransport):
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def sendto(self, data: bytes | bytearray | memoryview, addr: object = None) -> None:
        self.sent.append(bytes(data))


def test_the_clock_forgets_a_client_that_stopped_asking() -> None:
    """A UDP source address is a claim, not an identity; the table tracks the zone, not history."""
    now = 1_000_000
    proto = ClockSyncProtocol(lambda _k, _t: None, clock=lambda: now)
    proto.transport = _Transport()
    for port in range(50):
        proto.datagram_received(_sync_request(1), ("10.0.0.9", port))
    assert proto.client_count == 50, "each address gets its own record while it is talking"

    # One more packet, a long silence later: the sweep runs on arrival, so the 50 go and it stays.
    now += CLIENT_TTL_US + 1
    proto.datagram_received(_sync_request(1), ("10.0.0.7", 5000))
    assert proto.client_count == 1, "records past the TTL are dropped when the next packet arrives"


def test_a_client_still_talking_is_kept() -> None:
    """The other half: eviction must not forget the speakers actually in the zone."""
    now = 1_000_000
    proto = ClockSyncProtocol(lambda _k, _t: None, clock=lambda: now)
    proto.transport = _Transport()
    proto.datagram_received(_sync_request(1), ("10.0.0.9", 5000))
    now += CLIENT_TTL_US // 2
    proto.datagram_received(_sync_request(2), ("10.0.0.8", 5000))
    assert proto.client_count == 2, "a client inside the TTL is still the zone"


def test_the_clock_table_is_capped_even_when_every_client_is_fresh() -> None:
    """The TTL bounds nothing inside its own window: a flood of spoofed sources is all fresh."""
    now = 1_000_000
    proto = ClockSyncProtocol(lambda _k, _t: None, clock=lambda: now)
    proto.transport = _Transport()
    for port in range(MAX_CLIENTS * 3):
        proto.datagram_received(_sync_request(1), ("10.0.0.9", port))
    assert proto.client_count == MAX_CLIENTS


def test_a_full_table_does_not_reset_the_sync_of_a_client_already_in_it() -> None:
    """A speaker that syncs a little less often than its peers is the front of the table.

    Making room ran on every packet, so at the cap the oldest record went whether or not the
    packet that arrived was ITS OWN. The record was then rebuilt empty three lines later, which
    reads as a client that has never been heard from: the reply carries no previous t1/t3 pair and
    that speaker's sync starts again from nothing, in the middle of a zone, with nothing logged.
    """
    now = 1_000_000
    proto = ClockSyncProtocol(lambda _k, _t: None, clock=lambda: now)
    proto.transport = _Transport()

    slow_speaker = ("10.0.0.30", 5000)
    proto.datagram_received(_sync_request(1), slow_speaker)
    for port in range(MAX_CLIENTS - 1):  # fills the table exactly, leaving the speaker at the front
        now += 1
        proto.datagram_received(_sync_request(1), ("10.0.0.9", port))
    assert proto.client_count == MAX_CLIENTS

    now += 1
    proto.datagram_received(_sync_request(2), slow_speaker)
    reply = SyncPacket.parse(proto.transport.sent[-1])  # type: ignore[union-attr]
    assert reply.t1_prev == 1, "its own previous exchange must have survived the sweep"


def test_a_client_first_heard_from_at_clock_zero_still_ages_out() -> None:
    """`if state.last_us` reads a legitimate timestamp of 0 as never-seen and keeps it forever."""
    now = 0
    proto = ClockSyncProtocol(lambda _k, _t: None, clock=lambda: now)
    proto.transport = _Transport()
    proto.datagram_received(_sync_request(1), ("10.0.0.9", 5000))
    assert proto.client_count == 1

    now = CLIENT_TTL_US + 1
    proto.datagram_received(_sync_request(1), ("10.0.0.7", 5000))
    assert proto.client_count == 1, "the zero-stamped client must have been evicted, not kept"


def test_the_clock_sweep_does_not_get_slower_as_the_table_grows() -> None:
    """Scanning the whole table per datagram is O(n squared) under the flood it exists to survive.

    The event loop is shared with the audio channels, so a stall here is a stall there. The bound
    is deliberately loose - this asserts the SHAPE is flat, not a particular machine's speed.

    What has to differ between the arms is the size of the TABLE, and that takes the cap off one of
    them: under the shipped cap both arms ran over 512 entries whatever they were sent, so a sweep
    that scanned the whole table cost the same in each and the ratio stayed at one. The sizes are
    asserted rather than assumed - an arm whose independent variable never moved still produces a
    number, and that number reads exactly like a pass.
    """
    packet = _sync_request(1)

    def cost_per_packet(count: int, *, max_clients: int) -> tuple[float, int]:
        proto = ClockSyncProtocol(lambda _k, _t: None, clock=lambda: 1_000_000, max_clients=max_clients)
        proto.transport = _Transport()
        start = time.perf_counter()
        for port in range(count):
            proto.datagram_received(packet, ("10.0.0.9", port % 65536))
        return (time.perf_counter() - start) / count, proto.client_count

    small, small_table = cost_per_packet(2000, max_clients=MAX_CLIENTS)
    large, large_table = cost_per_packet(16000, max_clients=16000)
    assert (small_table, large_table) == (MAX_CLIENTS, 16000), (
        f"the arms must differ in the size of the table: {small_table} and {large_table}"
    )
    assert large < small * 4, f"per-packet cost grew from {small * 1e6:.1f}us to {large * 1e6:.1f}us"


def test_a_negative_byte_count_cannot_ask_for_a_zero_length_chunk() -> None:
    """The one that freezes the whole master rather than one connection.

    ``byte_count`` is an int32 from anyone who can reach the data port. Read as asked, -1 made
    the served size 0, and a zero-length read never satisfies the serving loop's ``while not
    chunk``. With the bytes already in the ring that loop has no suspension point either, so it
    spun inside the event loop: the clock server, the other slaves and the HTTP API all stopped
    with it, and only killing the process recovered it.
    """
    assert connections.chunk_plan(-1, 0).serve >= 1
    assert connections.chunk_plan(-2_000_000_000, -5).serve >= 1


def test_a_huge_byte_count_is_bounded_by_the_frame_this_master_would_accept() -> None:
    """A thirty-byte request used to be answered with the whole ring, past the frame ceiling."""
    served = connections.chunk_plan(2_000_000_000, 0).serve
    assert served <= connections.MAX_CHUNK_BYTES
    assert served + connections.CHUNK_ENVELOPE_HEADROOM_BYTES <= ipc.MAX_FRAME_BYTES


def test_a_request_that_names_no_size_still_gets_the_default() -> None:
    """Zero means "no preference" on the wire, which is what a real slave's first request sends."""
    assert connections.chunk_plan(0, 0).serve == connections.DEFAULT_CHUNK_BYTES


def test_a_request_that_names_a_minimum_gets_at_least_it() -> None:
    """min_byte_count is the slave saying how little is worth answering; it still binds."""
    assert connections.chunk_plan(0, 20_000).serve == 20_000


def test_a_huge_minimum_cannot_ask_the_ring_to_wait_for_more_than_it_holds() -> None:
    """The other int32 on that request, and the one that was still read as asked.

    The ring holds eight megabytes; ``min_byte_count`` can name two billion. Waiting for a count
    the ring can never reach means every request from that slave spends the whole ``DATA_WAIT_S``
    timing out with the bytes it asked for already sitting there, so its audio stops while the
    master logs a timeout and reports nothing wrong. ``wait_for`` is therefore never more than the
    master is willing to serve in one reply, which the ring CAN reach.
    """
    plan = connections.chunk_plan(0, 2_000_000_000)
    assert plan.wait_for <= plan.serve
    assert plan.wait_for <= connections.MAX_CHUNK_BYTES


def test_the_wait_is_never_zero_and_never_negative() -> None:
    """A wait of zero returns at once, which turns the serving loop into the spin above."""
    for byte_count, min_byte_count in ((0, 0), (0, -1), (-1, -2_000_000_000), (8192, 0)):
        assert connections.chunk_plan(byte_count, min_byte_count).wait_for >= 1


def test_a_modest_minimum_is_still_what_the_ring_is_waited_for() -> None:
    """The control: bounding the huge case must not stop a real slave's minimum from binding."""
    assert connections.chunk_plan(65_536, 20_000).wait_for == 20_000


def test_a_named_state_is_still_reported_by_its_name() -> None:
    """The control: naming the unknown case must not stop the six real ones being named."""
    assert connections.state_name(audio.AudioServerMsgServerState.PLAYING) == "PLAYING"
    assert connections.state_name(0) == "UNKNOWN"


def test_a_state_the_schema_has_no_name_for_is_reported_rather_than_raised() -> None:
    """``state`` is an int32 from anyone who can reach the transport port; only 0 to 6 are named.

    protobuf's ``Name`` raises ``ValueError``, which is not in ``ENDS_ONE_CONNECTION``, so a single
    frame carrying a 7 - newer firmware, or a peer sending nonsense - ended the read loop with an
    unhandled exception and a traceback in the journal instead of a line saying what arrived.
    """
    for unnamed in (7, 99, -1, 2_000_000_000):
        assert connections.state_name(unnamed) == f"UNNAMED({unnamed})"


def test_a_frame_over_the_ceiling_is_refused_on_the_way_out_too() -> None:
    """The limit was enforced on every frame read and on none of the frames written."""
    payload = audio_data.AudioServerMsgAcceptAudioData(data=b"\x00" * (ipc.MAX_FRAME_BYTES + 1))
    with pytest.raises(ipc.FrameTooLargeError):
        ipc.encode_frame(ipc.RESPONSE, payload, sequence=1)
