"""Where the zone is in a stream: in frames on the stream's own clock, or in bytes as a fallback.

``FrameTimeline`` is the rule (REPORT.md S7): a slave renders its first decoded frame at the
``at_microseconds`` of its PLAY and one every frame duration after it, on its clock, which is
the master's. The zone therefore renders frame ``frame0 + n`` exactly ``n`` durations after the
stream's t0, and a joiner is handed the first frame at least ``JOIN_LEAD_US`` ahead, at the
instant that line names. Nothing is estimated; the counters only reveal how many whole frames
a slave discarded before its first decoded one.

``ZoneTimeline`` is the byte-based fallback for a slave that reports no frames or a ring the
frame index found nothing in: each PLAYING report is a point (slave clock, absolute byte), the
rate comes from the window, and a joiner is started at the byte the zone will reach
``JOIN_LEAD_US`` later. Its errors are what S7 measured: the byte counter runs
``DECODER_READ_AHEAD_FRAMES`` ahead of what is rendered (subtracted when frames are reported),
the rate of a variable-rate stream swings between windows, and a first byte handed mid-frame
costs the joiner the rest of that frame.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

__all__ = [
    "DECODER_READ_AHEAD_FRAMES",
    "JOIN_LEAD_US",
    "REPORT_MAX_AGE_US",
    "FrameTimeline",
    "FramedPoint",
    "SlaveReport",
    "ZoneTimeline",
]

DECODER_READ_AHEAD_FRAMES = 3
"""Frames a slave's decoder has consumed beyond what it renders: ``frame_offset`` at the first
PLAYING report of every render start in the S3 capture (two plain starts, one late join, one
rejoin), and 2.9 frames measured on Room1 in the prototype's first hearing test."""

JOIN_LEAD_US = 3_000_000
"""How far ahead a joiner's first byte is scheduled: enough to connect and pull a few chunks,
short enough that the rate extrapolation stays within the ear's tolerance. The same margin every
stream start has (``source.START_DELAY_US``), which S5 run 3 proved in sync."""

REPORT_MAX_AGE_US = 3_000_000
"""A slave reports about once a second; nothing for three seconds means nobody is playing."""

_MIN_POINTS_FOR_A_SLOPE = 2
"""Two points make a line; with one, neither a byte rate nor a bytes-per-frame ratio exists."""


@dataclass(frozen=True)
class SlaveReport:
    """One PLAYING report placed on the master clock: what a slave said about where it is.

    ``frame_offset`` is its decoded-frame count from ``trackData``, counted from ITS own first
    byte exactly as ``absolute_byte``'s origin is, so it is comparable only within one ``peer``;
    None when the slave sent no ``trackData``.
    """

    t_us: int
    absolute_byte: int
    frame_offset: int | None
    peer: str

    def framed(self) -> FramedPoint | None:
        """This report as a point on a bytes-per-frame line, or None if it counted no frames."""
        if self.frame_offset is None:
            return None
        return FramedPoint(absolute_byte=self.absolute_byte, frame_offset=self.frame_offset)


@dataclass(frozen=True)
class FramedPoint:
    """A report that did carry a frame count, so both numbers a ratio needs are present."""

    absolute_byte: int
    frame_offset: int


@dataclass
class ZoneTimeline:
    """Points (slave clock, absolute byte) from the slaves playing one stream, and their rate.

    ``t0_us`` and ``t0_byte`` name the cohort start: the absolute byte every slave that joins
    before ``t0_us`` is handed as its byte 0, and the time it renders it. Its freshness also
    answers whether anybody is playing at all, which the frame plan relies on too.
    """

    t0_us: int
    t0_byte: int = 0
    window: int = 10
    _points: deque[SlaveReport] = field(default_factory=deque[SlaveReport], repr=False)

    def __post_init__(self) -> None:
        self._points = deque(maxlen=self.window)

    def add_report(self, *, t_us: int, absolute_byte: int, frame_offset: int | None = None, peer: str = "") -> None:
        """Record a PLAYING report; a byte below the last one is a slave that reset, not the zone.

        ``frame_offset`` is the slave's decoded-frame count from its ``trackData``, counted from
        ITS first byte like ``byte_offset`` is, so it is only comparable within one ``peer``.
        Without it the read-ahead cannot be measured and ``byte_at`` degrades to the raw counter.
        """
        if self._points and absolute_byte < self._points[-1].absolute_byte:
            return
        self._points.append(SlaveReport(t_us=t_us, absolute_byte=absolute_byte, frame_offset=frame_offset, peer=peer))

    def rate(self) -> float | None:
        """Bytes per second across the window, or None with fewer than two points."""
        if len(self._points) < _MIN_POINTS_FOR_A_SLOPE:
            return None
        first, last = self._points[0], self._points[-1]
        if last.t_us <= first.t_us:
            return None
        return (last.absolute_byte - first.absolute_byte) / ((last.t_us - first.t_us) / 1_000_000)

    def bytes_per_frame(self) -> float | None:
        """Bytes one decoded frame costs, or None until some slave has reported frames twice.

        Measured per slave (its own first and last framed points in the window) and averaged
        over the slaves, because each counts frames from its own first byte.
        """
        ratios = [r for r in (self._ratio_for(peer) for peer in {p.peer for p in self._points}) if r is not None]
        return sum(ratios) / len(ratios) if ratios else None

    def _ratio_for(self, peer: str) -> float | None:
        """Bytes per decoded frame for one slave, or None until it has reported frames twice."""
        framed = [point for point in (r.framed() for r in self._points if r.peer == peer) if point is not None]
        if len(framed) < _MIN_POINTS_FOR_A_SLOPE or framed[-1].frame_offset <= framed[0].frame_offset:
            return None
        return (framed[-1].absolute_byte - framed[0].absolute_byte) / (framed[-1].frame_offset - framed[0].frame_offset)

    def read_ahead_bytes(self) -> float | None:
        """How far the reported byte counter runs ahead of what is rendered, or None if unmeasured."""
        per_frame = self.bytes_per_frame()
        return None if per_frame is None else DECODER_READ_AHEAD_FRAMES * per_frame

    def byte_at(self, t_us: int) -> int | None:
        """The absolute byte the zone renders at ``t_us``, or None while the rate is unknown.

        The raw counter when the read-ahead is unmeasured (a slave sending no ``trackData``); the
        caller can tell the two apart through ``read_ahead_bytes``.
        """
        rate = self.rate()
        if rate is None:
            return None
        last = self._points[-1]
        read_at = last.absolute_byte + rate * (t_us - last.t_us) / 1_000_000
        return round(read_at - (self.read_ahead_bytes() or 0.0))

    def is_playing(self, *, now_us: int, max_age_us: int = REPORT_MAX_AGE_US) -> bool:
        """True while the latest report is younger than ``max_age_us``."""
        return bool(self._points) and now_us - self._points[-1].t_us <= max_age_us


@dataclass
class FrameTimeline:
    """Where the zone is in a stream, in frames: a straight line from the stream's start.

    A slave renders its first decoded frame at its PLAY time and one every ``duration_us``
    after it, on its clock, which is the master's (REPORT.md S6, and the hearing test of
    2026-09-06: every offset the ear caught was explained by where a slave's first decoded
    frame sat, never by drift). So the zone renders frame ``frame0`` at ``t0_us`` and frame
    ``frame0 + n`` exactly ``n`` durations later; nothing is estimated from the counters.

    Frames are the zone's true clock whatever the bytes do: Superfly read 7 kB/s over one
    window and 11.5 kB/s over the next, both at 21.53 frames/s, and a joiner planned on the
    byte rate landed 170 ms off (run 6).

    ``frame0`` is the first frame the first slave decoded: the first frame start at or after
    the stream's byte 0, plus whatever whole frames it discarded after that, which its byte
    and frame counters together reveal.
    """

    duration_us: int
    t0_us: int
    frame0: float = 0.0

    def frame_at(self, t_us: int) -> float:
        """The (fractional) frame the zone renders at ``t_us``."""
        return self.frame0 + (t_us - self.t0_us) / self.duration_us

    def time_of_frame(self, frame: int) -> int:
        """When the zone renders the start of ``frame``."""
        return round(self.t0_us + (frame - self.frame0) * self.duration_us)
