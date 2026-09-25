"""Frame boundaries in the station bytes the master serves.

A slave renders from the first frame it can decode, so a joiner handed a byte inside a frame
starts up to a frame late and the whole zone hears it as an echo (hearing test 2026-09-06). The
master holds the bytes, so it can hand the joiner a frame start. Proven on 24 kB grabbed from each
live station: TechnikumCity (MPEG-1 Layer III, 128 kbit/s, 44.1 kHz) and Superfly (HE-AAC in
ADTS, 22.05 kHz core rate, variable frame size).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from soundtouch_zonemaster.adapters.soundtouch.source import RingBuffer
from soundtouch_zonemaster.domain.frames import Frame, FrameIndex, find_frame

FIXTURES = Path(__file__).parent / "fixtures"
MP3 = (FIXTURES / "technikumcity-24k.mp3").read_bytes()
AAC = (FIXTURES / "superfly-24k.aac").read_bytes()


def _walk(data: bytes, start: int, count: int) -> list[Frame]:
    frames: list[Frame] = []
    pos = start
    for _ in range(count):
        f = find_frame(data, pos)
        assert f is not None and f.start == pos, f"chain broke at {pos}"
        frames.append(f)
        pos = f.start + f.length
    return frames


def test_the_mp3_grab_is_a_chain_of_128_kbit_frames_at_44100_hz() -> None:
    frames = _walk(MP3, 0, 50)
    assert {f.codec for f in frames} == {"mp3"}
    assert {f.length for f in frames} <= {417, 418}, "128 kbit/s at 44.1 kHz: 417 or 418 bytes with padding"
    assert all(f.duration_us == 26122 for f in frames), "1152 samples at 44100 Hz"


def test_the_aac_grab_is_a_chain_of_adts_frames_at_the_22050_hz_core_rate() -> None:
    frames = _walk(AAC, 0, 40)
    assert {f.codec for f in frames} == {"aac"}
    assert len({f.length for f in frames}) > 5, "variable bit rate: the frame sizes differ"
    assert all(f.duration_us == 46440 for f in frames), "1024 samples at 22050 Hz (HE-AAC core rate)"


@pytest.mark.parametrize("data", [MP3, AAC], ids=["mp3", "aac"])
def test_an_offset_inside_a_frame_yields_the_next_frame_start_not_the_current_one(data: bytes) -> None:
    second = _walk(data, 0, 2)[1]
    inside = second.start - 1  # the last byte of the first frame
    found = find_frame(data, inside)
    assert found is not None and found.start == second.start


@pytest.mark.parametrize("data", [MP3, AAC], ids=["mp3", "aac"])
def test_a_frame_start_is_only_believed_when_the_next_header_follows_it(data: bytes) -> None:
    # A lone sync word inside the payload (planted here) must not be taken for a frame.
    first = find_frame(data, 0)
    assert first is not None
    forged = bytearray(data[: first.length + 8])  # one real frame, then junk: the chain cannot be verified
    forged[first.length : first.length + 4] = b"\x00\x00\x00\x00"
    assert find_frame(bytes(forged), 0) is None, "the first frame's successor is gone, so it is not confirmed"
    planted = bytearray(data[:4096])  # several frames, so the real second frame can be confirmed
    planted[10:14] = data[:4]  # a header copy inside the first frame's payload
    replanted = find_frame(bytes(planted), 5)
    assert replanted is not None
    assert replanted.start == first.length, "the planted header has no successor at its length"


def test_random_bytes_hold_no_frame() -> None:
    assert find_frame(os.urandom(4096), 0) is None


def test_an_offset_past_the_data_is_none() -> None:
    assert find_frame(MP3, len(MP3) - 3) is None


# --- the frame index over a ring -------------------------------------------------------------------


def _chain(data: bytes) -> list[Frame]:
    out: list[Frame] = []
    pos = 0
    while (f := find_frame(data, pos)) is not None and f.start == pos:
        out.append(f)
        pos += f.length
    return out


@pytest.mark.parametrize("data", [MP3, AAC], ids=["mp3", "aac"])
def test_the_index_numbers_every_frame_start_in_the_ring_from_the_first_one(data: bytes) -> None:
    ring = RingBuffer()
    asyncio.run(ring.append(data))
    idx = FrameIndex(ring)
    chain = _chain(data)
    assert len(chain) > 30
    assert idx.duration_us == chain[0].duration_us
    for k, f in enumerate(chain[:-1]):  # the last frame has no successor in the data, so it is unconfirmed
        assert idx.index_of(f.start) == k
        assert idx.byte_of(k) == f.start


def test_a_byte_inside_a_frame_has_no_index_but_names_the_next_frame() -> None:
    ring = RingBuffer()
    asyncio.run(ring.append(MP3))
    idx = FrameIndex(ring)
    second = _chain(MP3)[1]
    assert idx.index_of(second.start - 1) is None
    assert idx.index_at_or_after(second.start - 1) == 1
    assert idx.index_at_or_after(second.start) == 1


def test_the_index_grows_as_bytes_arrive_and_a_frame_not_there_yet_is_none() -> None:
    ring = RingBuffer()
    asyncio.run(ring.append(MP3[:2000]))
    idx = FrameIndex(ring)
    n_first = idx.count()
    assert 3 <= n_first <= 5
    assert idx.byte_of(n_first + 20) is None
    asyncio.run(ring.append(MP3[2000:]))
    assert idx.byte_of(n_first + 20) == _chain(MP3)[n_first + 20].start


def test_an_index_over_bytes_with_no_frames_is_empty() -> None:
    ring = RingBuffer()
    asyncio.run(ring.append(bytes(50_000)))
    idx = FrameIndex(ring)
    assert idx.count() == 0 and idx.duration_us is None and idx.index_at_or_after(0) is None


def test_the_nearest_frame_boundary_serves_a_counter_that_sits_a_few_bytes_off_it() -> None:
    # A slave's byte counter sits a little off a frame boundary: 7 bytes past it on ADTS (the
    # next header already read), 24 bytes before it on MP3. Rounding up read that as a whole
    # frame discarded on AAC (runs 8 and 9, 2026-09-06) and moved the zone by one frame.
    ring = RingBuffer()
    asyncio.run(ring.append(AAC))
    idx = FrameIndex(ring)
    third = _chain(AAC)[3].start
    assert idx.index_nearest(third + 7) == 3
    assert idx.index_nearest(third - 24) == 3
    assert idx.index_nearest(third + 200) in (3, 4), "mid-frame: whichever boundary is closer"
    assert idx.index_nearest(10_000_000) is None


def test_the_index_picks_up_again_when_the_ring_drops_bytes_it_had_not_reached() -> None:
    """A ring that overtakes the parser must not end the index; it resumes at the oldest byte kept.

    Every other test here holds the default 8 MB ring against 24 kB of data, so the ring never
    overtakes anything and the guard that catches it never runs. A small ring is what a slow
    reader on a fast station looks like: the bytes the index had not parsed yet are gone, and
    reading on from where it left off raises RingOverrunError instead of numbering a frame.
    """
    ring = RingBuffer(max_bytes=8_000)
    asyncio.run(ring.append(MP3[:8_000]))
    idx = FrameIndex(ring)
    numbered_before = idx.count()
    assert numbered_before > 0, "the first pass has to have parsed something for the second to skip a gap"

    asyncio.run(ring.append(MP3[8_000:24_000]))
    assert ring.start_offset == 16_000, "the ring dropped its front, the parser's next byte with it"

    boundaries = [f.start for f in _chain(MP3)]
    resumed_at = next(start for start in boundaries if start >= ring.start_offset)
    assert idx.count() > numbered_before, "the index went on rather than stopping at the gap"
    assert idx.byte_of(numbered_before) == resumed_at, "it resumed at the first frame the ring still holds"
    assert set(idx.starts) <= set(boundaries), "and every frame it numbered is a real boundary"
    joiner = idx.index_at_or_after(ring.start_offset)
    assert joiner is not None and idx.byte_of(joiner) == resumed_at, "so a joiner is still handed a frame start"
