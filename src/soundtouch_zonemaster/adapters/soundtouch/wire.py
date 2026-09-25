"""The wire values the protocol fixes, mapped from this program's own names.

One tiny module rather than a constant inside whichever adapter happened to need it first: the
mapping reads the generated protobuf enum, and `application` may not import any of it (the
framework contract forbids `google`), so an option set carries the NAME and the wire value is
looked up here, at the edge that sends it.
"""

from __future__ import annotations

from ...domain.enums import Encryption
from .pb import audio_data

__all__ = ["encryption_type"]


_ENCRYPTION_TYPES = {
    Encryption.NONE: audio_data.AudioServerMsgAcceptAudioData.NONE,
    Encryption.OBFUSCATED: audio_data.AudioServerMsgAcceptAudioData.OBFUSCATED,
}


def encryption_type(encryption: Encryption) -> audio_data.AudioServerMsgAcceptAudioData.EncryptionType:
    """The wire enum for the chosen encryption.

    It was a method on ``Options`` until the rebuild. It is a function here because the record it
    hung off lives in `application`, which may not see the generated protobuf modules at all.
    """
    return _ENCRYPTION_TYPES[encryption]
