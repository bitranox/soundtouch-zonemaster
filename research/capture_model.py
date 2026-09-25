"""The vocabulary a zone capture is read in: endpoints, directions, frames and payload types.

``analyze_capture.py`` and ``analyze_late_join.py`` both walk the same pcap shape, so the records
and fixed value sets they share live here rather than in whichever script happened to define them
first. Nothing in this module reads a file or a socket; it is the shape of what the wire said.

The protobuf payloads are described as :class:`typing.Protocol` classes. The messages themselves
are built by ``message_factory`` from schemas recovered at runtime, so there is no generated class
to annotate against; a Protocol plus a ``cast`` gives the call sites real field types, which is
the typed-facade pattern rather than a suppression.

This duplicates a handful of members of ``src/soundtouch_zonemaster/enums.py`` on purpose. The research scripts
are standalone ``uv run --script`` files that must run on a host where the package is not
installed, so they cannot import it; the shipped package must not depend on ``research/`` either.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections import Counter

    from google.protobuf.message import Message
    from scapy.plist import PacketList

__all__ = [
    "AudioData",
    "Capture",
    "ConversationKey",
    "CumulativePoint",
    "DecodedEnvelope",
    "DecodedStream",
    "DirectedSegments",
    "Direction",
    "Endpoint",
    "Envelope",
    "Frame",
    "MsgKind",
    "MsgTypeName",
    "Segment",
    "SegmentStart",
    "ServerState",
    "SetUrl",
    "SlaveState",
    "TimedDecode",
    "TransportAction",
    "TransportControl",
    "UdpFlow",
]


@dataclass(frozen=True, slots=True)
class Capture:
    """One recorded run, and who is who in it.

    Every function that reads a capture needs all four of these and nothing else about the run,
    so they travel as one value rather than as four parameters repeated down six signatures.
    ``t0`` is the first packet's time; every reported time is relative to it.
    """

    packets: PacketList
    master: str
    slave: str
    t0: float


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One side of a TCP conversation."""

    host: str
    port: int


class Direction(StrEnum):
    """Which way a reassembled byte stream runs.

    The values are the keys the reassembler has always used; the label is what the report prints,
    which is why it belongs to the member rather than to a lookup table beside the loop.
    """

    SLAVE_TO_MASTER = "a2b"
    MASTER_TO_SLAVE = "b2a"

    @property
    def label(self) -> str:
        """The direction as the report names it."""
        return "slave->master" if self is Direction.SLAVE_TO_MASTER else "master->slave"


@dataclass(frozen=True, slots=True)
class Segment:
    """One TCP payload, at the time its packet was captured."""

    at: float
    payload: bytes


@dataclass(frozen=True, slots=True)
class DirectedSegments:
    """A conversation's payloads, ordered by sequence number, per direction."""

    slave_to_master: list[Segment]
    master_to_slave: list[Segment]

    def of(self, direction: Direction) -> list[Segment]:
        """The segments running the given way."""
        return self.slave_to_master if direction is Direction.SLAVE_TO_MASTER else self.master_to_slave


@dataclass(frozen=True, slots=True)
class CumulativePoint:
    """A running byte total at a moment on the capture clock."""

    at: float
    total: int


@dataclass(frozen=True, slots=True)
class SegmentStart:
    """Where one segment begins in the concatenated stream, and when it was captured."""

    offset: int
    at: float


@dataclass(frozen=True, slots=True)
class UdpFlow:
    """A UDP flow counted by its endpoints and payload size, which is how the clock ping shows up."""

    src: Endpoint
    dst: Endpoint
    length: int


@dataclass(frozen=True, slots=True)
class Frame:
    """One length-prefixed IPC frame, timed by the first byte that carried it."""

    at: float
    body: bytes


@dataclass(frozen=True, slots=True)
class ConversationKey:
    """A TCP conversation, named by the master port it reached and the client that opened it."""

    master_port: int
    client: Endpoint


class MsgKind(IntEnum):
    """``msg_type`` on the envelope: what a frame is, as opposed to what it carries."""

    EVENT = 1
    REQUEST = 2
    RESPONSE = 3

    @property
    def label(self) -> str:
        """The three-letter form the report prints."""
        return _MSG_KIND_LABELS[self]

    @classmethod
    def label_for(cls, value: int) -> str:
        """The label for a wire value, or the number itself when the envelope names a fourth kind."""
        try:
            return cls(value).label
        except ValueError:
            return str(value)


_MSG_KIND_LABELS = {MsgKind.EVENT: "EVT", MsgKind.REQUEST: "REQ", MsgKind.RESPONSE: "RSP"}


class SlaveState(IntEnum):
    """``ServerState.state`` as a slave reports it; only the value the analysers branch on is named."""

    PLAYING = 4


class TransportAction(IntEnum):
    """``AudioServerMsgTransportControl.control``; PLAY is the one that carries at_microseconds."""

    PLAY = 1


class MsgTypeName(StrEnum):
    """``msg_typename`` as the envelope carries it in clear text (REPORT.md S3).

    Only the types these two analysers branch on are listed. A capture names many more, so a
    decoded frame keeps its typename as ``str`` and is compared against these members; a
    ``StrEnum`` member equals its own value, so the comparison is the one the bare literal made.
    """

    ACCEPT_AUDIO_DATA = "AudioServerMsgAcceptAudioData"
    SET_URL = "AudioServerMsgSetURL"
    TRANSPORT_CONTROL = "AudioServerMsgTransportControl"
    SERVER_STATE = "AudioServerMsgServerState"


class SetUrl(Protocol):
    """The SetURL payload, as the recovered schema defines it."""

    url: str
    url_id: int


class TransportControl(Protocol):
    """The transport-control payload: PLAY is control 1, timed by at_microseconds."""

    control: int
    url_id: int
    at_microseconds: int


class ServerState(Protocol):
    """A slave's state report: its own clock, its byte counter and its decoded-frame count."""

    state: int
    url_id: int
    milliseconds: int
    byte_offset: int
    trackData: str  # noqa: N815 - the field name is the one the recovered schema declares


class AudioData(Protocol):
    """The data-channel payload, as the recovered schema defines it."""

    stream_id: int
    byte_offset: int
    encryption_type: int
    endofstream: bool
    slave_underflow: bool
    data: bytes


class Envelope(Protocol):
    """The IPC envelope as the recovered schema defines it."""

    msg_typename: str
    msg_contents: bytes
    msg_id: int
    msg_type: int
    sequence: int

    # protobuf's own generated method name; a Protocol has to match it exactly.
    def ParseFromString(self, serialized: bytes) -> int: ...  # noqa: N802


@dataclass(frozen=True, slots=True)
class DecodedEnvelope:
    """An envelope, whatever payload its ``msg_typename`` resolved to, and why it did not."""

    envelope: Envelope
    payload: Message | None
    error: str


@dataclass(frozen=True, slots=True)
class TimedDecode:
    """One decoded frame on the capture clock. ``envelope`` is None when the envelope itself failed."""

    at: float
    envelope: Envelope | None
    payload: Message | None
    error: str


@dataclass(frozen=True, slots=True)
class DecodedStream:
    """One direction of one conversation, decoded: the frames in order and what they turned out to be."""

    entries: list[TimedDecode]
    counts: Counter[str]
