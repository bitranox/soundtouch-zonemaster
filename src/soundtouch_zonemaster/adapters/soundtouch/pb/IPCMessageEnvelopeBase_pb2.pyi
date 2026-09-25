from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class IPCMessageEnvelopeBase(_message.Message):
    __slots__ = ("msg_type", "msg_module_id", "msg_id", "msg_contents", "sequence", "msg_typename")
    class MsgType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        MSG_TYPE_UNKNOWN: _ClassVar[IPCMessageEnvelopeBase.MsgType]
        MSG_TYPE_EVENT: _ClassVar[IPCMessageEnvelopeBase.MsgType]
        MSG_TYPE_REQUEST: _ClassVar[IPCMessageEnvelopeBase.MsgType]
        MSG_TYPE_RESPONSE: _ClassVar[IPCMessageEnvelopeBase.MsgType]
    MSG_TYPE_UNKNOWN: IPCMessageEnvelopeBase.MsgType
    MSG_TYPE_EVENT: IPCMessageEnvelopeBase.MsgType
    MSG_TYPE_REQUEST: IPCMessageEnvelopeBase.MsgType
    MSG_TYPE_RESPONSE: IPCMessageEnvelopeBase.MsgType
    class MsgModuleId(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        MSG_MODULE_ID_UNKNOWN: _ClassVar[IPCMessageEnvelopeBase.MsgModuleId]
    MSG_MODULE_ID_UNKNOWN: IPCMessageEnvelopeBase.MsgModuleId
    MSG_TYPE_FIELD_NUMBER: _ClassVar[int]
    MSG_MODULE_ID_FIELD_NUMBER: _ClassVar[int]
    MSG_ID_FIELD_NUMBER: _ClassVar[int]
    MSG_CONTENTS_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    MSG_TYPENAME_FIELD_NUMBER: _ClassVar[int]
    msg_type: IPCMessageEnvelopeBase.MsgType
    msg_module_id: IPCMessageEnvelopeBase.MsgModuleId
    msg_id: int
    msg_contents: bytes
    sequence: int
    msg_typename: str
    def __init__(self, msg_type: _Optional[_Union[IPCMessageEnvelopeBase.MsgType, str]] = ..., msg_module_id: _Optional[_Union[IPCMessageEnvelopeBase.MsgModuleId, str]] = ..., msg_id: _Optional[int] = ..., msg_contents: _Optional[bytes] = ..., sequence: _Optional[int] = ..., msg_typename: _Optional[str] = ...) -> None: ...
