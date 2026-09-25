from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class AudioServerMsgAcceptAudioDataRequest(_message.Message):
    __slots__ = ("byte_count", "byte_offset", "byte_offset_64", "min_byte_count", "stream_id")
    BYTE_COUNT_FIELD_NUMBER: _ClassVar[int]
    BYTE_OFFSET_FIELD_NUMBER: _ClassVar[int]
    BYTE_OFFSET_64_FIELD_NUMBER: _ClassVar[int]
    MIN_BYTE_COUNT_FIELD_NUMBER: _ClassVar[int]
    STREAM_ID_FIELD_NUMBER: _ClassVar[int]
    byte_count: int
    byte_offset: int
    byte_offset_64: int
    min_byte_count: int
    stream_id: int
    def __init__(self, byte_count: _Optional[int] = ..., byte_offset: _Optional[int] = ..., byte_offset_64: _Optional[int] = ..., min_byte_count: _Optional[int] = ..., stream_id: _Optional[int] = ...) -> None: ...

class AudioServerMsgAcceptAudioData(_message.Message):
    __slots__ = ("data", "endofstream", "byte_offset", "slave_underflow", "encryption_type", "byte_offset_64", "stream_id")
    class EncryptionType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        UNKNOWN: _ClassVar[AudioServerMsgAcceptAudioData.EncryptionType]
        NONE: _ClassVar[AudioServerMsgAcceptAudioData.EncryptionType]
        OBFUSCATED: _ClassVar[AudioServerMsgAcceptAudioData.EncryptionType]
        AES: _ClassVar[AudioServerMsgAcceptAudioData.EncryptionType]
    UNKNOWN: AudioServerMsgAcceptAudioData.EncryptionType
    NONE: AudioServerMsgAcceptAudioData.EncryptionType
    OBFUSCATED: AudioServerMsgAcceptAudioData.EncryptionType
    AES: AudioServerMsgAcceptAudioData.EncryptionType
    DATA_FIELD_NUMBER: _ClassVar[int]
    ENDOFSTREAM_FIELD_NUMBER: _ClassVar[int]
    BYTE_OFFSET_FIELD_NUMBER: _ClassVar[int]
    SLAVE_UNDERFLOW_FIELD_NUMBER: _ClassVar[int]
    ENCRYPTION_TYPE_FIELD_NUMBER: _ClassVar[int]
    BYTE_OFFSET_64_FIELD_NUMBER: _ClassVar[int]
    STREAM_ID_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    endofstream: bool
    byte_offset: int
    slave_underflow: bool
    encryption_type: AudioServerMsgAcceptAudioData.EncryptionType
    byte_offset_64: int
    stream_id: int
    def __init__(self, data: _Optional[bytes] = ..., endofstream: _Optional[bool] = ..., byte_offset: _Optional[int] = ..., slave_underflow: _Optional[bool] = ..., encryption_type: _Optional[_Union[AudioServerMsgAcceptAudioData.EncryptionType, str]] = ..., byte_offset_64: _Optional[int] = ..., stream_id: _Optional[int] = ...) -> None: ...
