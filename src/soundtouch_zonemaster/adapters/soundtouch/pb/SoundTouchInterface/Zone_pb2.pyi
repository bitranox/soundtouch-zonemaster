from ProtoToMarkup import MarkupOptions_pb2 as _MarkupOptions_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Role(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    NORMAL: _ClassVar[Role]
    LEFT: _ClassVar[Role]
    RIGHT: _ClassVar[Role]
    INVALID_ROLE: _ClassVar[Role]
NORMAL: Role
LEFT: Role
RIGHT: Role
INVALID_ROLE: Role

class Member(_message.Message):
    __slots__ = ("text", "ipaddress", "role")
    TEXT_FIELD_NUMBER: _ClassVar[int]
    IPADDRESS_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    text: str
    ipaddress: str
    role: Role
    def __init__(self, text: _Optional[str] = ..., ipaddress: _Optional[str] = ..., role: _Optional[_Union[Role, str]] = ...) -> None: ...

class zone(_message.Message):
    __slots__ = ("master", "member", "senderIPAddress", "senderIsMaster")
    MASTER_FIELD_NUMBER: _ClassVar[int]
    MEMBER_FIELD_NUMBER: _ClassVar[int]
    SENDERIPADDRESS_FIELD_NUMBER: _ClassVar[int]
    SENDERISMASTER_FIELD_NUMBER: _ClassVar[int]
    master: str
    member: _containers.RepeatedCompositeFieldContainer[Member]
    senderIPAddress: str
    senderIsMaster: bool
    def __init__(self, master: _Optional[str] = ..., member: _Optional[_Iterable[_Union[Member, _Mapping]]] = ..., senderIPAddress: _Optional[str] = ..., senderIsMaster: _Optional[bool] = ...) -> None: ...
