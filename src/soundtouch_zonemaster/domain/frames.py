"""Frame boundaries in the bytes the master serves: MPEG audio (MP3) and ADTS (AAC).

A slave renders from the first frame it can decode. Handed a byte inside a frame it discards the
rest of that frame and starts up to a frame later than the zone, which the ear takes for an echo
(hearing test 2026-09-06: a joiner placed mid-frame ran 0.75-3.8 frames ahead of the zone). The
master holds the station bytes, so it can hand a joiner a frame start instead.

A header is only believed when another header of the same kind sits exactly one frame length
after it: a sync word occurs inside compressed payload often enough that a lone one means
nothing.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from typing import Protocol

from .enums import Codec

__all__ = ["SCAN_LIMIT", "Frame", "FrameIndex", "find_frame"]

SCAN_LIMIT = 8192
"""How far past the asked offset a frame start is looked for: several of the largest frames."""

_MP3_BITRATES_KBPS = {
    # (version is MPEG-1, layer) -> bitrate table, index 1..14 (0 is free, 15 is invalid)
    (True, 1): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (True, 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (True, 3): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (False, 1): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (False, 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    (False, 3): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_MP3_SAMPLE_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}
_ADTS_SAMPLE_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350)

# Header field values, named so the decoders below read as the spec rather than as bit fiddling.
_SYNC_BYTE = 0xFF
"""First byte of both an MPEG audio and an ADTS sync word."""

_MP3_HEADER_BYTES = 4
_MP3_SYNC_MASK = 0xE0
"""Top three bits of the second header byte; all set completes the MPEG sync word."""

_MPEG_VERSION_RESERVED = 1
_MPEG_VERSION_1 = 3
_MPEG_LAYER_RESERVED = 4
_MPEG_LAYER_I = 1
_MPEG_LAYER_II = 2
_MP3_RATE_INDEX_RESERVED = 3
_MP3_MIN_FRAME_BYTES = 24
"""Shorter than the smallest legal frame, so a header claiming less is a false sync."""

_ADTS_HEADER_BYTES = 7
_ADTS_SYNC_MASK = 0xF6
_ADTS_SYNC_VALUE = 0xF0
"""Sync word plus the two layer bits, which ADTS fixes at 00."""


@dataclass(frozen=True)
class Frame:
    """One audio frame: where it starts in the data given, how long it is, how much time it plays."""

    start: int
    length: int
    duration_us: int
    codec: Codec
    kind: tuple[object, ...]  # what must match on the next header for the chain to hold


def _mp3_header(data: bytes, p: int) -> Frame | None:
    if p + _MP3_HEADER_BYTES > len(data) or data[p] != _SYNC_BYTE or data[p + 1] & _MP3_SYNC_MASK != _MP3_SYNC_MASK:
        return None
    version = (data[p + 1] >> 3) & 3  # 3 = MPEG-1, 2 = MPEG-2, 0 = MPEG-2.5, 1 = reserved
    layer = 4 - ((data[p + 1] >> 1) & 3)  # bits 01 = layer III, 10 = II, 11 = I; 00 reserved
    bitrate_index = data[p + 2] >> 4
    rate_index = (data[p + 2] >> 2) & 3
    padding = (data[p + 2] >> 1) & 1
    if (
        version == _MPEG_VERSION_RESERVED
        or layer == _MPEG_LAYER_RESERVED
        or bitrate_index in (0, 15)
        or rate_index == _MP3_RATE_INDEX_RESERVED
    ):
        return None
    mpeg1 = version == _MPEG_VERSION_1
    bitrate = _MP3_BITRATES_KBPS[(mpeg1, layer)][bitrate_index] * 1000
    sample_rate = _MP3_SAMPLE_RATES[version][rate_index]
    if layer == _MPEG_LAYER_I:
        length = (12 * bitrate // sample_rate + padding) * 4
        samples = 384
    else:
        samples = 1152 if (mpeg1 or layer == _MPEG_LAYER_II) else 576
        length = samples // 8 * bitrate // sample_rate + padding
    if length < _MP3_MIN_FRAME_BYTES:
        return None
    return Frame(p, length, round(samples * 1_000_000 / sample_rate), Codec.MP3, (version, layer, rate_index))


def _adts_header(data: bytes, p: int) -> Frame | None:
    if p + _ADTS_HEADER_BYTES > len(data) or data[p] != _SYNC_BYTE or data[p + 1] & _ADTS_SYNC_MASK != _ADTS_SYNC_VALUE:
        return None
    rate_index = (data[p + 2] >> 2) & 0xF
    if rate_index >= len(_ADTS_SAMPLE_RATES):
        return None
    length = ((data[p + 3] & 0x03) << 11) | (data[p + 4] << 3) | (data[p + 5] >> 5)
    header = 7 if data[p + 1] & 1 else 9  # protection_absent=0 adds a 2-byte CRC
    if length <= header:
        return None
    return Frame(p, length, round(1024 * 1_000_000 / _ADTS_SAMPLE_RATES[rate_index]), Codec.AAC, (rate_index,))


def _header(data: bytes, p: int) -> Frame | None:
    return _adts_header(data, p) or _mp3_header(data, p)


def find_frame(data: bytes, offset: int = 0) -> Frame | None:
    """The first frame starting at or after ``offset``, confirmed by the header that follows it.

    None when no confirmed frame starts within ``SCAN_LIMIT`` bytes, which includes the case
    that the data ends before the next header could be checked: a frame at the very end is not
    confirmable and a caller wanting it should wait for more bytes.
    """
    p = max(offset, 0)
    stop = min(len(data) - 1, offset + SCAN_LIMIT)
    while p < stop:
        f = _header(data, p)
        if f is not None:
            following = _header(data, p + f.length)
            if following is not None and following.codec == f.codec and following.kind == f.kind:
                return f
        p += 1
    return None


class _Ring(Protocol):
    start_offset: int

    @property
    def end_offset(self) -> int: ...

    def read(self, offset: int, count: int) -> bytes: ...


class FrameIndex:
    """Every frame start in a ring, numbered from the first confirmed one.

    Frames are the zone's true clock: a slave renders them at a fixed rate whatever their size,
    so a position kept in frames survives a variable-rate stream where a byte rate does not
    (Superfly: 7 kB/s over one window, 11.5 kB/s over the next, both at 21.53 frames/s). The
    index grows as bytes arrive; the last frame in the ring is unconfirmed until its successor's
    header is there, so it is not counted yet.
    """

    def __init__(self, ring: _Ring) -> None:
        self.ring = ring
        self.starts: list[int] = []  # absolute byte offset of frame k
        self.duration_us: int | None = None
        self.codec: Codec | None = None
        self._next = ring.start_offset  # the first byte that may hold an unindexed frame start
        self.extend()

    def count(self) -> int:
        self.extend()
        return len(self.starts)

    def extend(self) -> None:
        """Parse forward from the last known frame start as far as the ring confirms frames."""
        while True:
            pos = max(self._next, self.ring.start_offset)
            window = self.ring.read(pos, SCAN_LIMIT + 4096)
            frame = find_frame(window, 0) if window else None
            if frame is None:
                return
            if self.duration_us is None:
                self.duration_us, self.codec = frame.duration_us, frame.codec
            self.starts.append(pos + frame.start)
            self._next = pos + frame.start + frame.length

    def index_of(self, byte: int) -> int | None:
        """The frame number starting exactly at ``byte``, or None."""
        self.extend()
        k = bisect_left(self.starts, byte)
        return k if k < len(self.starts) and self.starts[k] == byte else None

    def index_at_or_after(self, byte: int) -> int | None:
        """The number of the first frame starting at or after ``byte``, or None if none is confirmed yet."""
        self.extend()
        k = bisect_left(self.starts, byte)
        return k if k < len(self.starts) else None

    def index_nearest(self, byte: int) -> int | None:
        """The number of the frame whose start is closest to ``byte``, or None past the confirmed frames.

        For a slave's byte counter, which sits a few bytes off a boundary (7 past it on ADTS, the
        next header already read; 24 before it on MP3): the boundary it means is the nearest one.
        """
        self.extend()
        k = bisect_left(self.starts, byte)
        if k >= len(self.starts):
            return None
        if k > 0 and byte - self.starts[k - 1] < self.starts[k] - byte:
            return k - 1
        return k

    def byte_of(self, index: int) -> int | None:
        """Where frame ``index`` starts, or None while the ring has not confirmed it."""
        if index >= len(self.starts):
            self.extend()
        return self.starts[index] if 0 <= index < len(self.starts) else None
