"""The zone clock: one monotonic microsecond clock, and the UDP sync server slaves query.

Wire layout (56 bytes, big-endian), measured on firmware 27.0.6 (research/REPORT.md, S3):
``u64 magic 0x0B05E901`` ("BOSE 901"), ``u32 version 3``, ``u32 0``, then five ``u64``
microsecond stamps: T1 (client send, client clock), T2 (server receive), T3 (server send),
T1 of the previous exchange, and the precise transmit time of the previous reply. The client
echoes the last T2/T3 in its next request. The server is the time reference; the client slews.
"""

from __future__ import annotations

import asyncio
import struct
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...domain.logfn import LogFn

# Master-clock microseconds; the master passes its own now_us down to whatever needs to place
# something in time.
Clock = Callable[[], int]

__all__ = [
    "CLIENT_TTL_US",
    "CLOCK_MAGIC",
    "CLOCK_VERSION",
    "MAX_CLIENTS",
    "Clock",
    "ClockSyncProtocol",
    "SyncPacket",
    "now_us",
    "serve_clock",
]

CLOCK_MAGIC = 0x0B05E901
CLOCK_VERSION = 3
MAX_CLIENTS = 512
"""Hard ceiling on the client table, whatever the TTL says.

    The sweep runs before the record is inserted, so the table holds at most this many.

The TTL alone bounds nothing inside its own window: a flood of spoofed source addresses
creates a record each, and they are all fresh. A zone is a handful of speakers, so this is
three orders of magnitude of headroom and still a bound.
"""
CLIENT_TTL_US = 300_000_000
"""Forget a client that has not asked the time for five minutes.

A slave in a zone syncs continuously, so a gap this long means it is gone. Without the
eviction the server keeps one record per source address it has ever answered, and a UDP
source address is a claim rather than an identity: anyone on the LAN can make the server
hold a record for an address that never existed, as many times as they like.
"""
_FMT = ">QIIQQQQQ"
_SIZE = struct.calcsize(_FMT)


def now_us() -> int:
    """Master clock in microseconds. Every at_microseconds we send uses this same clock."""
    return time.monotonic_ns() // 1000


@dataclass(frozen=True)
class SyncPacket:
    magic: int
    version: int
    t1: int
    t2: int
    t3: int
    t1_prev: int
    t3_prev_precise: int

    @classmethod
    def parse(cls, data: bytes) -> SyncPacket:
        if len(data) != _SIZE:
            raise ValueError(f"clock packet is {len(data)} bytes, expected {_SIZE}")
        magic, version, _zero, t1, t2, t3, t1p, t3p = struct.unpack(_FMT, data)
        return cls(magic, version, t1, t2, t3, t1p, t3p)

    def pack(self) -> bytes:
        return struct.pack(
            _FMT, self.magic, self.version, 0, self.t1, self.t2, self.t3, self.t1_prev, self.t3_prev_precise
        )


@dataclass
class ClientState:
    t1_prev: int = 0
    t3_prev: int = 0
    exchanges: int = 0
    last_us: int | None = None
    """When this client was last heard from, on the server clock. None until its first packet."""


def build_reply(req: SyncPacket, state: ClientState, *, now: int) -> SyncPacket:
    """Answer one request: T2 = T3 = now (a LAN reply within the same microsecond budget)."""
    reply = SyncPacket(CLOCK_MAGIC, CLOCK_VERSION, req.t1, now, now, state.t1_prev, state.t3_prev)
    state.t1_prev = req.t1
    state.t3_prev = now
    state.exchanges += 1
    return reply


class ClockSyncProtocol(asyncio.DatagramProtocol):
    """UDP server on the clock port; one state record per client address."""

    def __init__(self, log: LogFn, *, clock: Clock = now_us, max_clients: int = MAX_CLIENTS) -> None:
        self._log = log
        self._clock = clock
        # The cap is a constructor argument for the same reason the master's bind retry is one:
        # module state cannot be asked for a different answer, and the test that asks whether the
        # sweep gets slower as the table GROWS cannot ask it while the cap holds both of its arms
        # at the same size. Nothing in the program passes it; the default IS the shipped cap.
        self._max_clients = max_clients
        # Ordered by last-seen, oldest first, so eviction reads the front instead of scanning.
        self._clients: OrderedDict[tuple[str, int], ClientState] = OrderedDict()
        self.transport: asyncio.DatagramTransport | None = None

    @property
    def client_count(self) -> int:
        """How many clients the server is currently keeping time for."""
        return len(self._clients)

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        # The base signature can only promise a BaseTransport. asyncio hands a
        # DatagramTransport to a DatagramProtocol, so this narrows rather than asserting;
        # datagram_received already has a path for a transport that is not there.
        self.transport = transport if isinstance(transport, asyncio.DatagramTransport) else None

    def _forget_silent_clients(self, now: int, *, admitting: bool) -> None:
        """Drop records from the oldest end until the front is inside the TTL and the table fits.

        Amortised O(1) per packet. Scanning the whole table on every datagram was O(n) per packet
        and therefore O(n squared) under the very flood the TTL exists to survive - measured at
        139 microseconds per packet by 8000 distinct sources and still climbing. That runs on the
        one event loop, so it stalls the transport and data channels this process exists to serve:
        it traded an unbounded-memory problem for an unbounded-CPU one.

        ``last_us is None`` rather than a falsy test, because a client first heard from at clock
        zero has a legitimate timestamp of 0 and must still age out.

        ``admitting`` says a record is about to be MADE, which is the only reason to evict for the
        CAP. Making room runs unconditionally evicted the least-recently-seen record even when the
        packet came from a client already in the table - and when that client was itself the oldest,
        its own record was deleted and rebuilt empty a moment later, losing the t1/t3 pair its next
        reply is measured against. A full table is precisely the flood this eviction exists to
        survive, so that is exactly when a real speaker's sync would have been reset. The TTL half
        still runs either way: a record past its TTL is stale whoever the packet came from.
        """
        while self._clients:
            addr, state = next(iter(self._clients.items()))
            too_old = state.last_us is not None and now - state.last_us > CLIENT_TTL_US
            over_cap = admitting and len(self._clients) >= self._max_clients
            if not (too_old or over_cap):
                return
            del self._clients[addr]

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        try:
            req = SyncPacket.parse(data)
        except ValueError as exc:
            self._log("clock", f"{addr[0]}: {exc}")
            return
        if req.magic != CLOCK_MAGIC or req.version != CLOCK_VERSION:
            self._log("clock", f"{addr[0]}: bad magic/version {req.magic:#x}/{req.version}")
            return
        now = self._clock()
        # Asked BEFORE the sweep: whether this address needs a NEW record is what decides if the
        # cap may evict, and the sweep is what would otherwise delete the answer.
        self._forget_silent_clients(now, admitting=addr not in self._clients)
        state = self._clients.get(addr)
        if state is None:
            state = ClientState()
            self._clients[addr] = state
        state.last_us = now
        self._clients.move_to_end(addr)
        reply = build_reply(req, state, now=now)
        # T3 is stamped before the send; the next reply carries this value as the "precise"
        # transmit time, which on a LAN is within a few hundred microseconds of the truth.
        transport = self.transport
        if transport is None:
            # asyncio calls connection_made before any datagram arrives, so this is unreachable
            # in practice; say so rather than assert, which -O would strip out entirely.
            self._log("clock", f"{addr[0]}: reply dropped, no transport yet")
            return
        transport.sendto(reply.pack(), addr)
        if state.exchanges in (1, 2, 5, 30) or state.exchanges % 300 == 0:
            self._log("clock", f"{addr[0]} exchange {state.exchanges}: t1={req.t1} t2={reply.t2}")


async def serve_clock(bind: str, port: int, log: LogFn) -> asyncio.DatagramTransport:
    """The UDP clock server, listening. The returned transport is what closes it."""
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: ClockSyncProtocol(log), local_addr=(bind, port))
    return transport
