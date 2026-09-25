"""Bose IPC framing: ``uint32 big-endian length`` + ``IPCMessageEnvelopeBase`` carrying one message.

The envelope names its payload type in clear text (``msg_typename``) and carries a numeric
``msg_id`` per type. The ids below were read off a live zone (research/REPORT.md, S3); the
message classes come from the schemas recovered from the firmware.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import TypeVar

from google.protobuf.message import Message

from ...domain.enums import MsgTypeName
from .pb import audio, audio_data, envelope

__all__ = [
    "EVENT",
    "MAX_FRAME_BYTES",
    "MSG_IDS",
    "REQUEST",
    "RESPONSE",
    "Frame",
    "FrameSplitter",
    "FrameTooLargeError",
    "decode_frame",
    "encode_frame",
]


class FrameTooLargeError(ValueError):
    """A peer declared a frame longer than :data:`MAX_FRAME_BYTES`.

    Its own class so a channel can tell "this peer is talking nonsense, drop it" from the
    ValueErrors that mean a field would not parse.
    """

    def __init__(self, length: int, *, frames_before_it: tuple[bytes, ...] = ()) -> None:
        super().__init__(f"frame of {length} bytes over the {MAX_FRAME_BYTES} byte limit")
        self.length = length
        self.frames_before_it = frames_before_it
        """Whole frames that arrived in the same read, ahead of the oversized one.

        One TCP read carries whatever happened to be in the socket, so a refusal can meet frames
        that were already split and are perfectly good. They travel on the refusal rather than
        dying with the local that held them: today's only caller drops the connection and has
        nothing to do with them, and that is exactly why losing them was invisible. Empty from
        :func:`encode_frame`, which refuses one frame it was asked to build and has no others.
        """


MsgType = envelope.IPCMessageEnvelopeBase.MsgType

_LENGTH_PREFIX_BYTES = 4
MAX_FRAME_BYTES = 1 << 20
"""Refuse a longer frame than this.

The prefix is an unsigned 32-bit count, so a peer may declare four gigabytes, and the
splitter used to hold every byte sent against that promise while it waited for the rest.
Nothing on either channel comes close to a megabyte - the largest real frame is an audio
chunk - and the channels take connections from anyone who can reach the LAN, so the ceiling
is what stops one of them growing the master's memory until it dies.
"""
"""Every frame on the transport and data channels opens with a big-endian u32 length."""

EVENT = envelope.IPCMessageEnvelopeBase.MSG_TYPE_EVENT
REQUEST = envelope.IPCMessageEnvelopeBase.MSG_TYPE_REQUEST
RESPONSE = envelope.IPCMessageEnvelopeBase.MSG_TYPE_RESPONSE

# msg_id per type name, as the speakers use them on the zone channels (capture 2026-09-05).
MSG_IDS: dict[MsgTypeName, int] = {
    MsgTypeName.ACCEPT_AUDIO_DATA_REQUEST: 0,
    MsgTypeName.ACCEPT_AUDIO_DATA: 0,
    MsgTypeName.SET_URL: 20,
    MsgTypeName.TRANSPORT_CONTROL: 21,
    MsgTypeName.SERVER_STATE: 22,
    MsgTypeName.STREAM_STATE: 25,
    MsgTypeName.SUSPEND_STATE: 28,
    MsgTypeName.CAPABILITIES: 35,
    MsgTypeName.SLAVE_PING: 54,
    MsgTypeName.SLAVE_PING_RESPONSE: 55,
    MsgTypeName.SET_CLOCK_MASTER: 56,
    MsgTypeName.ZONE_STATE: 57,
}

_CLASSES: dict[str, type[Message]] = {}
for _mod in (audio, audio_data):
    for _name in dir(_mod):
        _obj = getattr(_mod, _name)
        if isinstance(_obj, type) and issubclass(_obj, Message):
            _CLASSES[_obj.DESCRIPTOR.name] = _obj


MessageT = TypeVar("MessageT", bound=Message)


@dataclass(frozen=True)
class Frame:
    """One decoded frame: the envelope fields plus the payload (None when the type is unknown)."""

    msg_type: MsgType
    msg_id: int
    sequence: int
    typename: str
    payload: Message | None
    raw_contents: bytes

    def payload_as(self, cls: type[MessageT]) -> MessageT:
        """The payload as the message its typename promises.

        The envelope names the payload type in clear text and decode_frame builds that class, so a
        caller that has checked the typename can read fields off a typed message. Checked here once
        rather than asserted at every call site.
        """
        if not isinstance(self.payload, cls):
            raise TypeError(f"{self.typename} is not a {cls.__name__}")
        return self.payload


def encode_frame(msg_type: MsgType, payload: Message, *, sequence: int) -> bytes:
    """Wrap ``payload`` in an envelope and prefix the big-endian length."""
    # A type this master does not know has no id to send under; ValueError says which,
    # where the bare dict lookup used to raise a KeyError naming only the string.
    name = MsgTypeName(payload.DESCRIPTOR.name)
    env = envelope.IPCMessageEnvelopeBase(
        msg_type=msg_type,
        msg_module_id=envelope.IPCMessageEnvelopeBase.MSG_MODULE_ID_UNKNOWN,
        msg_id=MSG_IDS[name],
        msg_contents=payload.SerializeToString(),
        sequence=sequence,
        msg_typename=name.value,
    )
    body = env.SerializeToString()
    if len(body) > MAX_FRAME_BYTES:
        # The ceiling was enforced on the way IN and nowhere on the way out, so a peer that asked
        # for more than a megabyte of audio was answered with a frame past the limit this master
        # refuses from anyone else. Enforced here rather than at the one caller that can reach it,
        # so no later caller has to remember.
        raise FrameTooLargeError(len(body))
    return struct.pack(">I", len(body)) + body


def decode_frame(body: bytes) -> Frame:
    """Parse one envelope body (without the length prefix)."""
    env = envelope.IPCMessageEnvelopeBase()
    env.ParseFromString(body)
    cls = _CLASSES.get(env.msg_typename)
    payload = None
    if cls is not None:
        payload = cls()
        payload.ParseFromString(env.msg_contents)
    return Frame(env.msg_type, env.msg_id, env.sequence, env.msg_typename, payload, bytes(env.msg_contents))


class FrameSplitter:
    """Incremental splitter for a TCP byte stream of length-prefixed frames."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        out: list[bytes] = []
        while len(self._buf) >= _LENGTH_PREFIX_BYTES:
            (length,) = struct.unpack(">I", self._buf[:_LENGTH_PREFIX_BYTES])
            if length > MAX_FRAME_BYTES:
                raise FrameTooLargeError(length, frames_before_it=tuple(out))
            end = _LENGTH_PREFIX_BYTES + length
            if len(self._buf) < end:
                break
            out.append(bytes(self._buf[_LENGTH_PREFIX_BYTES:end]))
            del self._buf[:end]
        return out
