from SoundTouchInterface import Zone_pb2 as _Zone_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class AudioServerMsgRegisterAudioSlave(_message.Message):
    __slots__ = ("connect", "result")
    CONNECT_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    connect: bool
    result: bool
    def __init__(self, connect: _Optional[bool] = ..., result: _Optional[bool] = ...) -> None: ...

class AudioServerMsgRegisterSourceApp(_message.Message):
    __slots__ = ("connect", "result", "passthrough", "turn_on_cpu")
    CONNECT_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    PASSTHROUGH_FIELD_NUMBER: _ClassVar[int]
    TURN_ON_CPU_FIELD_NUMBER: _ClassVar[int]
    connect: bool
    result: bool
    passthrough: int
    turn_on_cpu: bool
    def __init__(self, connect: _Optional[bool] = ..., result: _Optional[bool] = ..., passthrough: _Optional[int] = ..., turn_on_cpu: _Optional[bool] = ...) -> None: ...

class AudioServerMsgAbsoluteTimeValue(_message.Message):
    __slots__ = ("seconds", "microseconds")
    SECONDS_FIELD_NUMBER: _ClassVar[int]
    MICROSECONDS_FIELD_NUMBER: _ClassVar[int]
    seconds: int
    microseconds: int
    def __init__(self, seconds: _Optional[int] = ..., microseconds: _Optional[int] = ...) -> None: ...

class AudioServerMsgSetURL(_message.Message):
    __slots__ = ("url", "result", "passthrough", "url_id", "url_is_realtime", "protocol_hint", "connection_timeout_in_ms", "buffering_timeout_in_ms", "source_dryup_timeout_ms", "source_retry_delay_ms", "source_retry_count", "trackData", "resume_stream", "auto_select_protocol", "parse_document", "low_latency", "start_time", "seek_time_in_ms", "start_offset_ms", "playbackstreamtype", "playlist_start_at_livepoint", "disableVaribleLatency")
    class ProtocolHint(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        PROTOCOL_HINT_NONE: _ClassVar[AudioServerMsgSetURL.ProtocolHint]
        PROTOCOL_HINT_MMS: _ClassVar[AudioServerMsgSetURL.ProtocolHint]
        PROTOCOL_HINT_MMSH: _ClassVar[AudioServerMsgSetURL.ProtocolHint]
        PROTOCOL_HINT_RTSP: _ClassVar[AudioServerMsgSetURL.ProtocolHint]
        PROTOCOL_HINT_HTTP: _ClassVar[AudioServerMsgSetURL.ProtocolHint]
        PROTOCOL_HINT_RTSPU: _ClassVar[AudioServerMsgSetURL.ProtocolHint]
        PROTOCOL_HINT_RTSPT: _ClassVar[AudioServerMsgSetURL.ProtocolHint]
    PROTOCOL_HINT_NONE: AudioServerMsgSetURL.ProtocolHint
    PROTOCOL_HINT_MMS: AudioServerMsgSetURL.ProtocolHint
    PROTOCOL_HINT_MMSH: AudioServerMsgSetURL.ProtocolHint
    PROTOCOL_HINT_RTSP: AudioServerMsgSetURL.ProtocolHint
    PROTOCOL_HINT_HTTP: AudioServerMsgSetURL.ProtocolHint
    PROTOCOL_HINT_RTSPU: AudioServerMsgSetURL.ProtocolHint
    PROTOCOL_HINT_RTSPT: AudioServerMsgSetURL.ProtocolHint
    class PlaybackStreamType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        Wifi: _ClassVar[AudioServerMsgSetURL.PlaybackStreamType]
        AirPlay: _ClassVar[AudioServerMsgSetURL.PlaybackStreamType]
        Aux: _ClassVar[AudioServerMsgSetURL.PlaybackStreamType]
    Wifi: AudioServerMsgSetURL.PlaybackStreamType
    AirPlay: AudioServerMsgSetURL.PlaybackStreamType
    Aux: AudioServerMsgSetURL.PlaybackStreamType
    URL_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    PASSTHROUGH_FIELD_NUMBER: _ClassVar[int]
    URL_ID_FIELD_NUMBER: _ClassVar[int]
    URL_IS_REALTIME_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_HINT_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_TIMEOUT_IN_MS_FIELD_NUMBER: _ClassVar[int]
    BUFFERING_TIMEOUT_IN_MS_FIELD_NUMBER: _ClassVar[int]
    SOURCE_DRYUP_TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    SOURCE_RETRY_DELAY_MS_FIELD_NUMBER: _ClassVar[int]
    SOURCE_RETRY_COUNT_FIELD_NUMBER: _ClassVar[int]
    TRACKDATA_FIELD_NUMBER: _ClassVar[int]
    RESUME_STREAM_FIELD_NUMBER: _ClassVar[int]
    AUTO_SELECT_PROTOCOL_FIELD_NUMBER: _ClassVar[int]
    PARSE_DOCUMENT_FIELD_NUMBER: _ClassVar[int]
    LOW_LATENCY_FIELD_NUMBER: _ClassVar[int]
    START_TIME_FIELD_NUMBER: _ClassVar[int]
    SEEK_TIME_IN_MS_FIELD_NUMBER: _ClassVar[int]
    START_OFFSET_MS_FIELD_NUMBER: _ClassVar[int]
    PLAYBACKSTREAMTYPE_FIELD_NUMBER: _ClassVar[int]
    PLAYLIST_START_AT_LIVEPOINT_FIELD_NUMBER: _ClassVar[int]
    DISABLEVARIBLELATENCY_FIELD_NUMBER: _ClassVar[int]
    url: str
    result: bool
    passthrough: int
    url_id: int
    url_is_realtime: bool
    protocol_hint: AudioServerMsgSetURL.ProtocolHint
    connection_timeout_in_ms: int
    buffering_timeout_in_ms: int
    source_dryup_timeout_ms: int
    source_retry_delay_ms: int
    source_retry_count: int
    trackData: str
    resume_stream: bool
    auto_select_protocol: bool
    parse_document: bool
    low_latency: bool
    start_time: AudioServerMsgAbsoluteTimeValue
    seek_time_in_ms: int
    start_offset_ms: int
    playbackstreamtype: AudioServerMsgSetURL.PlaybackStreamType
    playlist_start_at_livepoint: bool
    disableVaribleLatency: bool
    def __init__(self, url: _Optional[str] = ..., result: _Optional[bool] = ..., passthrough: _Optional[int] = ..., url_id: _Optional[int] = ..., url_is_realtime: _Optional[bool] = ..., protocol_hint: _Optional[_Union[AudioServerMsgSetURL.ProtocolHint, str]] = ..., connection_timeout_in_ms: _Optional[int] = ..., buffering_timeout_in_ms: _Optional[int] = ..., source_dryup_timeout_ms: _Optional[int] = ..., source_retry_delay_ms: _Optional[int] = ..., source_retry_count: _Optional[int] = ..., trackData: _Optional[str] = ..., resume_stream: _Optional[bool] = ..., auto_select_protocol: _Optional[bool] = ..., parse_document: _Optional[bool] = ..., low_latency: _Optional[bool] = ..., start_time: _Optional[_Union[AudioServerMsgAbsoluteTimeValue, _Mapping]] = ..., seek_time_in_ms: _Optional[int] = ..., start_offset_ms: _Optional[int] = ..., playbackstreamtype: _Optional[_Union[AudioServerMsgSetURL.PlaybackStreamType, str]] = ..., playlist_start_at_livepoint: _Optional[bool] = ..., disableVaribleLatency: _Optional[bool] = ...) -> None: ...

class AudioServerMsgPlayChime(_message.Message):
    __slots__ = ("path",)
    PATH_FIELD_NUMBER: _ClassVar[int]
    path: str
    def __init__(self, path: _Optional[str] = ...) -> None: ...

class AudioServerMsgAmpControl(_message.Message):
    __slots__ = ("amp_state",)
    class AmpState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        AMP_STANDBY: _ClassVar[AudioServerMsgAmpControl.AmpState]
        AMP_READY: _ClassVar[AudioServerMsgAmpControl.AmpState]
    AMP_STANDBY: AudioServerMsgAmpControl.AmpState
    AMP_READY: AudioServerMsgAmpControl.AmpState
    AMP_STATE_FIELD_NUMBER: _ClassVar[int]
    amp_state: AudioServerMsgAmpControl.AmpState
    def __init__(self, amp_state: _Optional[_Union[AudioServerMsgAmpControl.AmpState, str]] = ...) -> None: ...

class AudioServerMsgTransportControl(_message.Message):
    __slots__ = ("control", "result", "at_microseconds", "playtime_us", "url_id")
    class Control(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        STOP: _ClassVar[AudioServerMsgTransportControl.Control]
        PLAY: _ClassVar[AudioServerMsgTransportControl.Control]
        PAUSE: _ClassVar[AudioServerMsgTransportControl.Control]
    STOP: AudioServerMsgTransportControl.Control
    PLAY: AudioServerMsgTransportControl.Control
    PAUSE: AudioServerMsgTransportControl.Control
    CONTROL_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    AT_MICROSECONDS_FIELD_NUMBER: _ClassVar[int]
    PLAYTIME_US_FIELD_NUMBER: _ClassVar[int]
    URL_ID_FIELD_NUMBER: _ClassVar[int]
    control: AudioServerMsgTransportControl.Control
    result: bool
    at_microseconds: int
    playtime_us: int
    url_id: int
    def __init__(self, control: _Optional[_Union[AudioServerMsgTransportControl.Control, str]] = ..., result: _Optional[bool] = ..., at_microseconds: _Optional[int] = ..., playtime_us: _Optional[int] = ..., url_id: _Optional[int] = ...) -> None: ...

class AudioServerMsgAudioControl(_message.Message):
    __slots__ = ("volume", "mute", "result", "passthrough")
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    MUTE_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    PASSTHROUGH_FIELD_NUMBER: _ClassVar[int]
    volume: int
    mute: bool
    result: bool
    passthrough: int
    def __init__(self, volume: _Optional[int] = ..., mute: _Optional[bool] = ..., result: _Optional[bool] = ..., passthrough: _Optional[int] = ...) -> None: ...

class AudioServerMsgVolumeControl(_message.Message):
    __slots__ = ("volume", "result", "passthrough")
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    PASSTHROUGH_FIELD_NUMBER: _ClassVar[int]
    volume: int
    result: bool
    passthrough: int
    def __init__(self, volume: _Optional[int] = ..., result: _Optional[bool] = ..., passthrough: _Optional[int] = ...) -> None: ...

class AudioServerMsgMuteControl(_message.Message):
    __slots__ = ("mute", "result", "passthrough")
    MUTE_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    PASSTHROUGH_FIELD_NUMBER: _ClassVar[int]
    mute: bool
    result: bool
    passthrough: int
    def __init__(self, mute: _Optional[bool] = ..., result: _Optional[bool] = ..., passthrough: _Optional[int] = ...) -> None: ...

class AudioServerMsgBassControl(_message.Message):
    __slots__ = ("bass", "result", "passthrough")
    BASS_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    PASSTHROUGH_FIELD_NUMBER: _ClassVar[int]
    bass: int
    result: bool
    passthrough: int
    def __init__(self, bass: _Optional[int] = ..., result: _Optional[bool] = ..., passthrough: _Optional[int] = ...) -> None: ...

class AudioServerMsgServerError(_message.Message):
    __slots__ = ("error", "protocol_error")
    class Error(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        NONE: _ClassVar[AudioServerMsgServerError.Error]
        UNKNOWN: _ClassVar[AudioServerMsgServerError.Error]
        BAD_URL: _ClassVar[AudioServerMsgServerError.Error]
        DECODER: _ClassVar[AudioServerMsgServerError.Error]
        TIMEOUT: _ClassVar[AudioServerMsgServerError.Error]
        BAD_MEDIA_PLAYLIST: _ClassVar[AudioServerMsgServerError.Error]
        SLAVE_PLAYBACK_FAILURE: _ClassVar[AudioServerMsgServerError.Error]
    NONE: AudioServerMsgServerError.Error
    UNKNOWN: AudioServerMsgServerError.Error
    BAD_URL: AudioServerMsgServerError.Error
    DECODER: AudioServerMsgServerError.Error
    TIMEOUT: AudioServerMsgServerError.Error
    BAD_MEDIA_PLAYLIST: AudioServerMsgServerError.Error
    SLAVE_PLAYBACK_FAILURE: AudioServerMsgServerError.Error
    ERROR_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_ERROR_FIELD_NUMBER: _ClassVar[int]
    error: AudioServerMsgServerError.Error
    protocol_error: int
    def __init__(self, error: _Optional[_Union[AudioServerMsgServerError.Error, str]] = ..., protocol_error: _Optional[int] = ...) -> None: ...

class AudioServerMsgServerState(_message.Message):
    __slots__ = ("state", "url_id", "reason", "milliseconds", "byte_offset", "total_length", "terminalError", "trackData", "absolute_play_point")
    class State(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        UNKNOWN: _ClassVar[AudioServerMsgServerState.State]
        WAITING: _ClassVar[AudioServerMsgServerState.State]
        STOPPED: _ClassVar[AudioServerMsgServerState.State]
        BUFFERING: _ClassVar[AudioServerMsgServerState.State]
        PLAYING: _ClassVar[AudioServerMsgServerState.State]
        PAUSED: _ClassVar[AudioServerMsgServerState.State]
        CONNECTING: _ClassVar[AudioServerMsgServerState.State]
    UNKNOWN: AudioServerMsgServerState.State
    WAITING: AudioServerMsgServerState.State
    STOPPED: AudioServerMsgServerState.State
    BUFFERING: AudioServerMsgServerState.State
    PLAYING: AudioServerMsgServerState.State
    PAUSED: AudioServerMsgServerState.State
    CONNECTING: AudioServerMsgServerState.State
    class Reason(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        NOTAPPLICABLE: _ClassVar[AudioServerMsgServerState.Reason]
        INTERRUPTED: _ClassVar[AudioServerMsgServerState.Reason]
        ENDOFTRACK: _ClassVar[AudioServerMsgServerState.Reason]
    NOTAPPLICABLE: AudioServerMsgServerState.Reason
    INTERRUPTED: AudioServerMsgServerState.Reason
    ENDOFTRACK: AudioServerMsgServerState.Reason
    STATE_FIELD_NUMBER: _ClassVar[int]
    URL_ID_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    MILLISECONDS_FIELD_NUMBER: _ClassVar[int]
    BYTE_OFFSET_FIELD_NUMBER: _ClassVar[int]
    TOTAL_LENGTH_FIELD_NUMBER: _ClassVar[int]
    TERMINALERROR_FIELD_NUMBER: _ClassVar[int]
    TRACKDATA_FIELD_NUMBER: _ClassVar[int]
    ABSOLUTE_PLAY_POINT_FIELD_NUMBER: _ClassVar[int]
    state: AudioServerMsgServerState.State
    url_id: int
    reason: AudioServerMsgServerState.Reason
    milliseconds: int
    byte_offset: int
    total_length: int
    terminalError: AudioServerMsgServerError
    trackData: str
    absolute_play_point: AudioServerMsgAbsoluteTimeValue
    def __init__(self, state: _Optional[_Union[AudioServerMsgServerState.State, str]] = ..., url_id: _Optional[int] = ..., reason: _Optional[_Union[AudioServerMsgServerState.Reason, str]] = ..., milliseconds: _Optional[int] = ..., byte_offset: _Optional[int] = ..., total_length: _Optional[int] = ..., terminalError: _Optional[_Union[AudioServerMsgServerError, _Mapping]] = ..., trackData: _Optional[str] = ..., absolute_play_point: _Optional[_Union[AudioServerMsgAbsoluteTimeValue, _Mapping]] = ...) -> None: ...

class AudioServerMsgZoneState(_message.Message):
    __slots__ = ("is_slave",)
    IS_SLAVE_FIELD_NUMBER: _ClassVar[int]
    is_slave: bool
    def __init__(self, is_slave: _Optional[bool] = ...) -> None: ...

class AudioServerMsgStreamState(_message.Message):
    __slots__ = ("is_silent",)
    IS_SILENT_FIELD_NUMBER: _ClassVar[int]
    is_silent: bool
    def __init__(self, is_silent: _Optional[bool] = ...) -> None: ...

class AudioServerMsgSuspendState(_message.Message):
    __slots__ = ("is_suspended",)
    IS_SUSPENDED_FIELD_NUMBER: _ClassVar[int]
    is_suspended: bool
    def __init__(self, is_suspended: _Optional[bool] = ...) -> None: ...

class AudioServerMsgSetMaster(_message.Message):
    __slots__ = ("ipaddress", "zoneList", "connection_timeout_in_ms", "buffering_timeout_in_ms")
    IPADDRESS_FIELD_NUMBER: _ClassVar[int]
    ZONELIST_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_TIMEOUT_IN_MS_FIELD_NUMBER: _ClassVar[int]
    BUFFERING_TIMEOUT_IN_MS_FIELD_NUMBER: _ClassVar[int]
    ipaddress: str
    zoneList: _Zone_pb2.zone
    connection_timeout_in_ms: int
    buffering_timeout_in_ms: int
    def __init__(self, ipaddress: _Optional[str] = ..., zoneList: _Optional[_Union[_Zone_pb2.zone, _Mapping]] = ..., connection_timeout_in_ms: _Optional[int] = ..., buffering_timeout_in_ms: _Optional[int] = ...) -> None: ...

class AudioServerMsgVolumeState(_message.Message):
    __slots__ = ("volume",)
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    volume: int
    def __init__(self, volume: _Optional[int] = ...) -> None: ...

class AudioServerMsgMuteState(_message.Message):
    __slots__ = ("mute",)
    MUTE_FIELD_NUMBER: _ClassVar[int]
    mute: bool
    def __init__(self, mute: _Optional[bool] = ...) -> None: ...

class AudioServerMsgBassState(_message.Message):
    __slots__ = ("bass",)
    BASS_FIELD_NUMBER: _ClassVar[int]
    bass: int
    def __init__(self, bass: _Optional[int] = ...) -> None: ...

class AudioServerMsgCapabilities(_message.Message):
    __slots__ = ("bass_capability", "AuxInputURLs", "NormalStereoImage", "MonoStereoAvailable", "MonoImage", "StereoPairAvailable", "LeftImage", "RightImage", "balance_capability", "productOwnsInitialVolume")
    class AudioParameter(_message.Message):
        __slots__ = ("minimum_value", "maximum_value", "default_value")
        MINIMUM_VALUE_FIELD_NUMBER: _ClassVar[int]
        MAXIMUM_VALUE_FIELD_NUMBER: _ClassVar[int]
        DEFAULT_VALUE_FIELD_NUMBER: _ClassVar[int]
        minimum_value: int
        maximum_value: int
        default_value: int
        def __init__(self, minimum_value: _Optional[int] = ..., maximum_value: _Optional[int] = ..., default_value: _Optional[int] = ...) -> None: ...
    BASS_CAPABILITY_FIELD_NUMBER: _ClassVar[int]
    AUXINPUTURLS_FIELD_NUMBER: _ClassVar[int]
    NORMALSTEREOIMAGE_FIELD_NUMBER: _ClassVar[int]
    MONOSTEREOAVAILABLE_FIELD_NUMBER: _ClassVar[int]
    MONOIMAGE_FIELD_NUMBER: _ClassVar[int]
    STEREOPAIRAVAILABLE_FIELD_NUMBER: _ClassVar[int]
    LEFTIMAGE_FIELD_NUMBER: _ClassVar[int]
    RIGHTIMAGE_FIELD_NUMBER: _ClassVar[int]
    BALANCE_CAPABILITY_FIELD_NUMBER: _ClassVar[int]
    PRODUCTOWNSINITIALVOLUME_FIELD_NUMBER: _ClassVar[int]
    bass_capability: AudioServerMsgCapabilities.AudioParameter
    AuxInputURLs: _containers.RepeatedScalarFieldContainer[str]
    NormalStereoImage: str
    MonoStereoAvailable: bool
    MonoImage: str
    StereoPairAvailable: bool
    LeftImage: str
    RightImage: str
    balance_capability: AudioServerMsgCapabilities.AudioParameter
    productOwnsInitialVolume: bool
    def __init__(self, bass_capability: _Optional[_Union[AudioServerMsgCapabilities.AudioParameter, _Mapping]] = ..., AuxInputURLs: _Optional[_Iterable[str]] = ..., NormalStereoImage: _Optional[str] = ..., MonoStereoAvailable: _Optional[bool] = ..., MonoImage: _Optional[str] = ..., StereoPairAvailable: _Optional[bool] = ..., LeftImage: _Optional[str] = ..., RightImage: _Optional[str] = ..., balance_capability: _Optional[_Union[AudioServerMsgCapabilities.AudioParameter, _Mapping]] = ..., productOwnsInitialVolume: _Optional[bool] = ...) -> None: ...

class AudioServerMsgMonoControl(_message.Message):
    __slots__ = ("mono",)
    MONO_FIELD_NUMBER: _ClassVar[int]
    mono: bool
    def __init__(self, mono: _Optional[bool] = ...) -> None: ...

class AudioServerMsgMonoState(_message.Message):
    __slots__ = ("mono",)
    MONO_FIELD_NUMBER: _ClassVar[int]
    mono: bool
    def __init__(self, mono: _Optional[bool] = ...) -> None: ...

class AudioServerMsgOutputLatency(_message.Message):
    __slots__ = ("output_latency",)
    OUTPUT_LATENCY_FIELD_NUMBER: _ClassVar[int]
    output_latency: int
    def __init__(self, output_latency: _Optional[int] = ...) -> None: ...

class AudioServerMsgReconfigure(_message.Message):
    __slots__ = ("config",)
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    config: str
    def __init__(self, config: _Optional[str] = ...) -> None: ...

class AudioServerMsgTrackData(_message.Message):
    __slots__ = ("frame_offset", "byte_offset", "time_offset", "latency_microsecs")
    FRAME_OFFSET_FIELD_NUMBER: _ClassVar[int]
    BYTE_OFFSET_FIELD_NUMBER: _ClassVar[int]
    TIME_OFFSET_FIELD_NUMBER: _ClassVar[int]
    LATENCY_MICROSECS_FIELD_NUMBER: _ClassVar[int]
    frame_offset: int
    byte_offset: int
    time_offset: int
    latency_microsecs: int
    def __init__(self, frame_offset: _Optional[int] = ..., byte_offset: _Optional[int] = ..., time_offset: _Optional[int] = ..., latency_microsecs: _Optional[int] = ...) -> None: ...

class AudioServerMsgToSlaveMsg(_message.Message):
    __slots__ = ("msg",)
    MSG_FIELD_NUMBER: _ClassVar[int]
    msg: str
    def __init__(self, msg: _Optional[str] = ...) -> None: ...

class AudioServerMsgToMasterMsg(_message.Message):
    __slots__ = ("msg",)
    MSG_FIELD_NUMBER: _ClassVar[int]
    msg: str
    def __init__(self, msg: _Optional[str] = ...) -> None: ...

class AudioServerMsgFromMasterMsg(_message.Message):
    __slots__ = ("msg",)
    MSG_FIELD_NUMBER: _ClassVar[int]
    msg: str
    def __init__(self, msg: _Optional[str] = ...) -> None: ...

class AudioServerMsgFromSlaveMsg(_message.Message):
    __slots__ = ("msg",)
    MSG_FIELD_NUMBER: _ClassVar[int]
    msg: str
    def __init__(self, msg: _Optional[str] = ...) -> None: ...

class AudioServerMsgSlaveStateMsg(_message.Message):
    __slots__ = ("disconnected_slave", "connected_slave", "current_slaves")
    DISCONNECTED_SLAVE_FIELD_NUMBER: _ClassVar[int]
    CONNECTED_SLAVE_FIELD_NUMBER: _ClassVar[int]
    CURRENT_SLAVES_FIELD_NUMBER: _ClassVar[int]
    disconnected_slave: str
    connected_slave: str
    current_slaves: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, disconnected_slave: _Optional[str] = ..., connected_slave: _Optional[str] = ..., current_slaves: _Optional[_Iterable[str]] = ...) -> None: ...

class AudioServerMsgSlavePingMsg(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class AudioServerMsgSlavePingResponseMsg(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class AudioServerMsgGroupMember(_message.Message):
    __slots__ = ("deviceId", "role", "ipAddress")
    class Role(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        NORMAL: _ClassVar[AudioServerMsgGroupMember.Role]
        LEFT: _ClassVar[AudioServerMsgGroupMember.Role]
        RIGHT: _ClassVar[AudioServerMsgGroupMember.Role]
    NORMAL: AudioServerMsgGroupMember.Role
    LEFT: AudioServerMsgGroupMember.Role
    RIGHT: AudioServerMsgGroupMember.Role
    DEVICEID_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    IPADDRESS_FIELD_NUMBER: _ClassVar[int]
    deviceId: str
    role: AudioServerMsgGroupMember.Role
    ipAddress: str
    def __init__(self, deviceId: _Optional[str] = ..., role: _Optional[_Union[AudioServerMsgGroupMember.Role, str]] = ..., ipAddress: _Optional[str] = ...) -> None: ...

class AudioServerMsgSetGroupMsg(_message.Message):
    __slots__ = ("master", "members", "deviceId")
    MASTER_FIELD_NUMBER: _ClassVar[int]
    MEMBERS_FIELD_NUMBER: _ClassVar[int]
    DEVICEID_FIELD_NUMBER: _ClassVar[int]
    master: AudioServerMsgGroupMember
    members: _containers.RepeatedCompositeFieldContainer[AudioServerMsgGroupMember]
    deviceId: str
    def __init__(self, master: _Optional[_Union[AudioServerMsgGroupMember, _Mapping]] = ..., members: _Optional[_Iterable[_Union[AudioServerMsgGroupMember, _Mapping]]] = ..., deviceId: _Optional[str] = ...) -> None: ...

class AudioServerMsgGroupMasterStateMsg(_message.Message):
    __slots__ = ("disconnected_slave", "connected_slave", "current_slaves")
    DISCONNECTED_SLAVE_FIELD_NUMBER: _ClassVar[int]
    CONNECTED_SLAVE_FIELD_NUMBER: _ClassVar[int]
    CURRENT_SLAVES_FIELD_NUMBER: _ClassVar[int]
    disconnected_slave: AudioServerMsgGroupMember
    connected_slave: AudioServerMsgGroupMember
    current_slaves: _containers.RepeatedCompositeFieldContainer[AudioServerMsgGroupMember]
    def __init__(self, disconnected_slave: _Optional[_Union[AudioServerMsgGroupMember, _Mapping]] = ..., connected_slave: _Optional[_Union[AudioServerMsgGroupMember, _Mapping]] = ..., current_slaves: _Optional[_Iterable[_Union[AudioServerMsgGroupMember, _Mapping]]] = ...) -> None: ...

class AudioServerMsgGroupSlaveStateMsg(_message.Message):
    __slots__ = ("master", "connected")
    MASTER_FIELD_NUMBER: _ClassVar[int]
    CONNECTED_FIELD_NUMBER: _ClassVar[int]
    master: AudioServerMsgGroupMember
    connected: bool
    def __init__(self, master: _Optional[_Union[AudioServerMsgGroupMember, _Mapping]] = ..., connected: _Optional[bool] = ...) -> None: ...

class AudioServerMsgSetClockMasterMsg(_message.Message):
    __slots__ = ("clockMasterIp", "clockMasterPort", "ignoreMaster")
    CLOCKMASTERIP_FIELD_NUMBER: _ClassVar[int]
    CLOCKMASTERPORT_FIELD_NUMBER: _ClassVar[int]
    IGNOREMASTER_FIELD_NUMBER: _ClassVar[int]
    clockMasterIp: str
    clockMasterPort: int
    ignoreMaster: bool
    def __init__(self, clockMasterIp: _Optional[str] = ..., clockMasterPort: _Optional[int] = ..., ignoreMaster: _Optional[bool] = ...) -> None: ...

class AudioServerMsgSetBalance(_message.Message):
    __slots__ = ("balance", "result")
    BALANCE_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    balance: int
    result: bool
    def __init__(self, balance: _Optional[int] = ..., result: _Optional[bool] = ...) -> None: ...

class AudioServerMsgBalanceState(_message.Message):
    __slots__ = ("balance",)
    BALANCE_FIELD_NUMBER: _ClassVar[int]
    balance: int
    def __init__(self, balance: _Optional[int] = ...) -> None: ...
