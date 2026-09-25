"""Where each slave sits in the stream: its PLAY time, its first byte, and how it is running.

This is the half of the zone that was signed off by ear on real hardware (REPORT.md S7). What
is known about one slave in one stream is one record, ``StreamBook``: a slot says what the slave
was told, and the other three fields say what it did with it, so they are kept and forgotten
together.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

from ...domain.frames import SCAN_LIMIT, find_frame
from ...domain.timeline import DECODER_READ_AHEAD_FRAMES, JOIN_LEAD_US, REPORT_MAX_AGE_US, ZoneTimeline
from .clock import now_us
from .pb import audio

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from ...domain.logfn import LogFn
    from .reports import SlaveState
    from .source import StreamSource

__all__ = [
    "RATE_WAIT_S",
    "RESTART_PREROLL_BYTES",
    "Alignment",
    "JoinPlanner",
    "JoinSlot",
    "Observation",
    "Rendered",
    "StreamBook",
    "StreamKey",
]

RESTART_PREROLL_BYTES = 160_000  # data a restarting cohort gets behind the live edge: ~10 s at 128 kbit/s
RATE_WAIT_S = 2.5  # how long a joiner waits for the playing slaves' second report before falling back


@dataclass(frozen=True)
class StreamKey:
    """Which slave, in which stream. The books below are all kept per (peer, url_id).

    A frozen dataclass rather than a bare tuple, because both fields would otherwise be
    positional and nothing would catch reading the stream where the peer belongs.
    """

    peer: str
    url_id: int


@dataclass(frozen=True)
class Rendered:
    """The last point a slave was seen at: master-clock time, and the absolute byte it rendered."""

    t_us: int
    absolute_byte: int


@dataclass(frozen=True)
class Alignment:
    """A joiner's first byte moved to a frame start, and the start time moved with it."""

    base_offset: int
    t0_us: int


@dataclass(frozen=True)
class JoinSlot:
    """What one slave was told for one stream: its PLAY time, and the absolute byte it calls 0."""

    t0_us: int
    base_offset: int


@dataclass(frozen=True)
class Observation:
    """What one state report says about where a slave has got to.

    The three always travel together and are only meaningful together: the master-clock time the
    report describes, the slave's byte counter relative to its own byte 0, and the frames it has
    decoded since then.
    """

    t: int
    byte_offset: int
    frame_offset: int


@dataclass
class StreamBook:
    """Everything known about one slave in one stream.

    ``slot`` is None between a frame plan and the PLAY it leads to: the plan writes down the
    joiner's first frame before the master records what it told the slave. One record rather
    than four dicts, so that forgetting a slave forgets that frame too.
    """

    slot: JoinSlot | None = None
    rendered: Rendered | None = None
    """The last point the slave was seen at, on the byte-counter path."""
    base_frame: int | None = None
    """The frame the slave's first byte starts."""
    discarded: int | None = None
    """Whole frames the slave threw away before its first decoded one."""


@dataclass
class JoinPlanner:
    """The book of every slave in every stream, and the arithmetic that writes it.

    ``source_for`` is how the planner reaches the stream a report names; the master owns the
    sources, and handing the lookup in rather than the dict keeps this side unable to add or
    remove one.
    """

    log: LogFn
    source_for: Callable[[int], StreamSource | None]
    _books: dict[StreamKey, StreamBook] = field(default_factory=dict[StreamKey, StreamBook])

    # --- the books -------------------------------------------------------------------------------

    @property
    def slots(self) -> Mapping[StreamKey, JoinSlot]:
        """What each slave was told, for every stream it has been told about.

        A read-only snapshot: a write into it would change nothing, so it raises instead.
        Writes go through ``assign``.
        """
        return MappingProxyType({key: book.slot for key, book in self._books.items() if book.slot is not None})

    def _book(self, key: StreamKey) -> StreamBook:
        return self._books.setdefault(key, StreamBook())

    def assign(self, key: StreamKey, slot: JoinSlot) -> None:
        """Record what one slave was told for one stream."""
        self._book(key).slot = slot

    def drop_peer(self, peer: str) -> None:
        """Forget everything about one slave, in every stream."""
        for key in [k for k in self._books if k.peer == peer]:
            del self._books[key]

    def drop_stream(self, url_id: int) -> None:
        """Forget everything about one stream, for every slave."""
        for key in [k for k in self._books if k.url_id == url_id]:
            del self._books[key]

    async def base_for(self, peer: str, stream_id: int) -> int | None:
        """The absolute byte a slave's data channel starts at, planned when its PLAY was sent."""
        for _ in range(30):
            book = self._books.get(StreamKey(peer, stream_id))
            if book is not None and book.slot is not None:
                return book.slot.base_offset
            await asyncio.sleep(0.1)
        return None

    # --- placing a joiner ------------------------------------------------------------------------

    async def plan_join(self, peer: str, source: StreamSource) -> JoinSlot:
        """Decide a slave's PLAY time and first byte for ``source`` (REPORT.md S7 rule).

        Three cases: the stream has not started yet, so the slave joins the starting cohort at byte
        0 and the stream's t0; nobody is playing it any more, so the slave restarts the zone a
        little behind the live edge at a fresh t0; or the zone is playing, so the slave is handed
        the first frame at least JOIN_LEAD_US ahead, at the instant the stream's frame timeline
        renders it (``_plan_in_frames``), falling back to the byte the rate extrapolation names,
        moved to a frame start, when no frames are known.
        """
        tl = source.timeline
        if tl.t0_us > now_us():
            return JoinSlot(tl.t0_us, tl.t0_byte)
        deadline = now_us() + int(RATE_WAIT_S * 1_000_000)
        while tl.is_playing(now_us=now_us()) and tl.rate() is None and now_us() < deadline:
            await asyncio.sleep(0.1)  # the first slave reports once a second; the second report gives the rate
        in_frames = await self._plan_in_frames(peer, source)
        if in_frames is not None:
            return in_frames
        t0 = now_us() + JOIN_LEAD_US
        base = tl.byte_at(t0) if tl.is_playing(now_us=now_us()) else None
        rate = tl.rate()
        if base is not None and rate is not None:
            # A playing zone is never restarted for a joiner. A byte the ring does not hold yet is
            # simply ahead of the live edge; the data channel waits for it.
            read_ahead = tl.read_ahead_bytes()
            self.log(
                "master",
                f"{peer}: joins at byte {base} (zone rate {rate:.0f} B/s, decoder read-ahead "
                f"{'unmeasured, raw counter used' if read_ahead is None else f'{read_ahead:.0f} B'})",
            )
            base = max(base, source.ring.start_offset)
            aligned = self._on_frame_start(peer, source, base, t0, rate)
            return JoinSlot(aligned.t0_us, aligned.base_offset)
        ring = source.ring
        restart = JoinSlot(t0, max(ring.start_offset, ring.end_offset - RESTART_PREROLL_BYTES))
        source.t0_us = t0
        source.timeline = ZoneTimeline(t0_us=t0, t0_byte=restart.base_offset)
        source.frame_timeline = None  # re-anchored on the restart by frame_position()
        self.log(
            "master",
            f"{peer}: nobody playing url_id={source.station.url_id}; zone restarts at byte "
            f"{restart.base_offset} t0_us={t0}",
        )
        return restart

    async def _plan_in_frames(self, peer: str, source: StreamSource) -> JoinSlot | None:
        """The joiner's slot from the zone's position in frames: exact whatever the byte rate does.

        The zone renders frame F at a time the frame timeline names to within half a frame, so
        the joiner is handed frame F's bytes for exactly that instant, F being the first frame
        at least ``JOIN_LEAD_US`` away. None when no slave has reported frames yet (the byte plan
        takes over), or when the ring has not confirmed frame F after a short wait (a station
        without a burst: the joiner would sit at the live edge).
        """
        ft = source.frame_position()
        if ft is None or not source.timeline.is_playing(now_us=now_us()):
            return None
        target = ft.frame_at(now_us() + JOIN_LEAD_US)
        frame = math.ceil(target)
        t0 = ft.time_of_frame(frame)
        deadline = now_us() + int(RATE_WAIT_S * 1_000_000)
        while (byte := source.frames.byte_of(frame)) is None and now_us() < deadline:
            await asyncio.sleep(0.1)
        if byte is None:
            self.log(
                "master",
                f"{peer}: frame {frame} not in the ring yet "
                f"({source.frames.count()} frames indexed); byte plan instead",
            )
            return None
        self.log(
            "master",
            f"{peer}: joins at frame {frame} = byte {byte}, the zone renders it in "
            f"{(t0 - now_us()) / 1000:.0f} ms ({source.frames.codec} frames of {ft.duration_us / 1000:.2f} ms, "
            f"zone at frame {target:.1f} then)",
        )
        self._book(StreamKey(peer, source.station.url_id)).base_frame = frame
        return JoinSlot(t0, byte)

    @staticmethod
    def _base_frame(book: StreamBook, source: StreamSource, slot: JoinSlot) -> int | None:
        """The frame a slave counts from: the first frame start at or after its first byte."""
        if book.base_frame is None:
            book.base_frame = source.frames.index_at_or_after(slot.base_offset)
        return book.base_frame

    def _on_frame_start(self, peer: str, source: StreamSource, base: int, t0_us: int, rate: float) -> Alignment:
        """Move a joiner's first byte forward to a frame start, and its start time with it.

        A slave discards the remainder of a frame it is handed mid-way and renders from the next
        one, so a mid-frame first byte puts it up to a frame ahead of the zone. Moving the start
        time by the skipped bytes at the zone's rate keeps the frame on the zone's timeline. When
        the ring does not hold those bytes yet (a station without a burst, the joiner ahead of the
        live edge) there is nothing to inspect; the joiner starts unaligned and the log says so.
        """
        window = source.ring.read(base, SCAN_LIMIT + 4096)
        frame = find_frame(window, 0) if window else None
        if frame is None:
            self.log(
                "master", f"{peer}: no frame start found after byte {base} ({len(window)} B in the ring): unaligned"
            )
            return Alignment(base_offset=base, t0_us=t0_us)
        shift_us = round(frame.start / rate * 1_000_000)
        self.log(
            "master",
            f"{peer}: first byte moved to the {frame.codec} frame start at {base + frame.start} "
            f"(+{frame.start} B, +{shift_us / 1000:.1f} ms); frames of {frame.duration_us / 1000:.2f} ms",
        )
        return Alignment(base_offset=base + frame.start, t0_us=t0_us + shift_us)

    # --- watching a slave run --------------------------------------------------------------------

    def on_slave_state(self, report: SlaveState) -> None:
        """Feed a slave's PLAYING report into its stream's timeline, and place it against the others.

        The report is placed at the slave's OWN clock, ``report.milliseconds`` after its PLAY time,
        which is the master's clock domain (REPORT.md S6): its arrival is up to 100 ms later on WLAN
        (measured 2026-09-06), and a late point extrapolates into a joiner placed behind the zone.

        The ``sync`` line is the running comparison: this slave's rendered byte against every other
        slave's, extrapolated to this instant, in milliseconds of stream. Positive means ahead. It
        is measured on the byte counters, which the ear confirmed (runs 1 and 3: +100 ms, echo).
        """
        if report.state != audio.AudioServerMsgServerState.PLAYING:
            return
        key = StreamKey(report.peer, report.url_id)
        book = self._books.get(key)
        slot = None if book is None else book.slot
        source = self.source_for(report.url_id)
        if book is None or slot is None or source is None:
            return
        t = slot.t0_us + report.milliseconds * 1000
        tl = source.timeline
        rate = tl.rate()  # before this report joins the window: a misplaced slave must not bend the yardstick
        tl.add_report(
            t_us=t,
            absolute_byte=slot.base_offset + report.byte_offset,
            frame_offset=report.frame_offset,
            peer=report.peer,
        )
        if report.frame_offset is not None and self._frame_report(
            key,
            book,
            source,
            slot,
            Observation(t=t, byte_offset=report.byte_offset, frame_offset=report.frame_offset),
        ):
            return
        read_ahead = tl.read_ahead_bytes()
        mine = slot.base_offset + report.byte_offset - (read_ahead or 0.0)
        self._log_byte_sync(key, mine=mine, t=t, rate=rate, read_ahead=read_ahead)
        book.rendered = Rendered(t_us=t, absolute_byte=round(mine))

    def _log_byte_sync(
        self, key: StreamKey, *, mine: float, t: int, rate: float | None, read_ahead: float | None
    ) -> None:
        """The ``sync`` line for a slave placed on byte counters: where it sits against each peer."""
        if rate is None:
            return
        for other, book in self._books.items():
            point = book.rendered
            if point is None or other.peer == key.peer or other.url_id != key.url_id:
                continue
            if t - point.t_us > REPORT_MAX_AGE_US:
                continue
            zone_byte = point.absolute_byte + rate * (t - point.t_us) / 1_000_000
            delta_ms = (mine - zone_byte) / rate * 1000
            self.log(
                "sync",
                f"{key.peer} renders {delta_ms:+.0f} ms vs {other.peer} (bytes"
                f"{'' if read_ahead is not None else ', read-ahead unmeasured'})",
            )

    def _frame_report(
        self, key: StreamKey, book: StreamBook, source: StreamSource, slot: JoinSlot, obs: Observation
    ) -> bool:
        """Place a frame-counting slave against the zone, from what its counters reveal.

        Its offset from the zone is fixed the moment it starts: its first decoded frame is the
        first frame start at or after its first byte, plus the whole frames it discarded before
        decoding (bytes consumed against frames decoded, a bit-reservoir dependency on MP3), and
        it renders that frame at its PLAY time. False when its first byte is not in the frame
        index, so the byte-based path handles the report instead.
        """
        base_frame = self._base_frame(book, source, slot)
        ft = source.frame_position()
        if base_frame is None or ft is None:
            return False
        pos = slot.base_offset + obs.byte_offset
        consumed_to = source.frames.index_nearest(pos)  # the counter sits a few bytes off a boundary
        if consumed_to is not None:
            discarded = consumed_to - base_frame - obs.frame_offset
            if discarded != book.discarded:
                start = source.frames.byte_of(consumed_to)
                sits = "at an unconfirmed frame" if start is None else f"{pos - start:+d} B from"
                self.log(
                    "master",
                    f"{key.peer}: discarded {discarded} whole frame(s) before its first decoded one"
                    f" (consumed to frame {consumed_to}, decoded {obs.frame_offset} from frame {base_frame};"
                    f" counter sits {sits} frame {consumed_to}'s start)",
                )
                book.discarded = discarded
                if slot.t0_us == source.t0_us and slot.base_offset == source.timeline.t0_byte:
                    # The first slave defines what the zone renders at t0.
                    ft.frame0 = base_frame + discarded
        first_decoded = base_frame + (book.discarded or 0)
        mine = first_decoded + (obs.t - slot.t0_us) / ft.duration_us
        delta_ms = (mine - ft.frame_at(obs.t)) * ft.duration_us / 1000
        # The comparison column: where the slave's own decoded-frame counter puts it, which is
        # the read-ahead approximation rather than the frame plan the placement actually used.
        counter_frames = base_frame + obs.frame_offset - DECODER_READ_AHEAD_FRAMES - ft.frame_at(obs.t)
        counter_ms = counter_frames * ft.duration_us / 1000
        self.log(
            "sync",
            f"{key.peer} renders {delta_ms:+.0f} ms vs zone (frames; counter says {counter_ms:+.0f} ms)",
        )
        return True
