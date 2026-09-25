"""What a slave says about itself: the whole state report, and the counters inside it.

Every ``AudioServerMsgServerState`` carries ``trackData``, an XML fragment with the decoder's
frame count (REPORT.md S6). Frames are exact time whatever the codec or its rate, which the byte
counter is not, so this is the field the zone timeline needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import xmlmodels
from .xmlread import element_anywhere, parse

if TYPE_CHECKING:
    from .pb import audio

__all__ = ["SlaveState", "TrackData", "parse_track_data"]


@dataclass(frozen=True)
class TrackData:
    frame_offset: int
    byte_offset: int
    time_offset: int
    latency_microsecs: int


def parse_track_data(xml: str) -> TrackData | None:
    """The four counters of a ``trackData`` fragment, or None when it is not one.

    This fragment reaches the master inside a slave's state report on the zone's data channel,
    which is LAN input like everything else, so it is read through the same bounded parser. All
    four counters must be there and be numbers: a report carrying some of them is not a position.

    The element is looked for ANYWHERE in the fragment rather than as the root, because a speaker
    wraps it differently in different reports.
    """
    root = parse(xml)
    element = element_anywhere(root, "AudioServerMsgTrackData") if root is not None else None
    counters = xmlmodels.track_data(element) if element is not None else None
    if counters is None:
        return None
    return TrackData(
        frame_offset=counters.frame_offset,
        byte_offset=counters.byte_offset,
        time_offset=counters.time_offset,
        latency_microsecs=counters.latency_microsecs,
    )


@dataclass(frozen=True)
class SlaveState:
    """One ``AudioServerMsgServerState`` as the master needs it: the fields of one wire message.

    These six travel together and mean nothing apart -- which slave, which stream, what it is
    doing, how far into its PLAY it is, and how much it has consumed by byte and by frame. Passed
    as one record, they cost one parameter instead of six on every signature that carries them.

    ``frame_offset`` is present only when the report carried ``trackData``; it is what the frame
    timeline is placed on, and None sends the report down the byte-based path instead.
    """

    peer: str
    url_id: int
    state: audio.AudioServerMsgServerState.State
    milliseconds: int
    byte_offset: int
    frame_offset: int | None = None
