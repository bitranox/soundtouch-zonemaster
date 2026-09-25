"""The fixed value sets the master speaks, in one place.

Every one of these is a string on the wire, so they are ``StrEnum``: a member compares and
serialises exactly as the literal it replaces, which is what lets the speaker protocol stay
byte-identical while the code stops repeating bare strings. The Python floor here is 3.12, so
``enum.StrEnum`` is available and the ``class X(str, Enum)`` fallback -- whose ``f"{member}"``
renders as ``X.MEMBER`` from 3.11 on -- is not in play.

The names come from a capture or a live run, never from reading a binary (research/REPORT.md).
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ArtImageStatus",
    "ChannelEnd",
    "ChannelKind",
    "Codec",
    "ContentType",
    "Encryption",
    "FrameKind",
    "HttpMethod",
    "HttpStatusLine",
    "JoinMode",
    "KeyName",
    "KeyState",
    "MpdState",
    "MsgTypeName",
    "PlayStatus",
    "ProductCode",
    "SlaveAction",
    "SourceName",
    "SpeakerPath",
    "StreamType",
]


class Codec(StrEnum):
    """The two codecs the stations serve, as ``frames.py`` names them."""

    MP3 = "mp3"
    AAC = "aac"


class MsgTypeName(StrEnum):
    """``msg_typename`` as the envelope carries it in clear text (REPORT.md S3).

    A wire envelope may name a type absent from this set, so a decoded frame keeps its typename
    as ``str`` and is compared against these members; a ``StrEnum`` member equals its own value,
    so the comparison is the same one the bare literal made.
    """

    ACCEPT_AUDIO_DATA_REQUEST = "AudioServerMsgAcceptAudioDataRequest"
    ACCEPT_AUDIO_DATA = "AudioServerMsgAcceptAudioData"
    SET_URL = "AudioServerMsgSetURL"
    TRANSPORT_CONTROL = "AudioServerMsgTransportControl"
    SERVER_STATE = "AudioServerMsgServerState"
    STREAM_STATE = "AudioServerMsgStreamState"
    SUSPEND_STATE = "AudioServerMsgSuspendState"
    CAPABILITIES = "AudioServerMsgCapabilities"
    SLAVE_PING = "AudioServerMsgSlavePingMsg"
    SLAVE_PING_RESPONSE = "AudioServerMsgSlavePingResponseMsg"
    SET_CLOCK_MASTER = "AudioServerMsgSetClockMasterMsg"
    ZONE_STATE = "AudioServerMsgZoneState"


class ContentType(StrEnum):
    """Content types ``resolve_stream_url`` branches on while following a station to its stream.

    ``TEXT`` is here because stations serve .m3u as ``text/plain`` often enough to matter; the
    URL suffix is checked alongside it, since some serve a playlist as ``audio/mpeg``.
    """

    JSON = "application/json"
    M3U = "audio/x-mpegurl"
    APPLE_M3U = "application/vnd.apple.mpegurl"
    PLS = "audio/x-scpls"
    TEXT = "text/plain"
    PLS_XML = "application/pls+xml"


class Encryption(StrEnum):
    """How the data channel obfuscates its payload; slaves accept NONE from a non-Bose master."""

    NONE = "none"
    OBFUSCATED = "obfuscated"


class JoinMode(StrEnum):
    """How a late slave is brought in: the S6 rule, or the control arm that restarts the stream."""

    SCHEDULE = "schedule"
    RESTART = "restart"


class HttpMethod(StrEnum):
    """The two methods a slave uses against its master's port 8090."""

    GET = "GET"
    POST = "POST"


class HttpStatusLine(StrEnum):
    """Status lines the master's minimal HTTP server writes.

    Not ``http.HTTPStatus``: what goes on the wire here is the whole line, code and reason
    together, and the reason text is part of what a speaker's parser sees.
    """

    OK = "200 OK"
    INTERNAL_SERVER_ERROR = "500 Internal Server Error"


class SpeakerPath(StrEnum):
    """The speaker-API paths a slave calls on its master."""

    INFO = "/info"
    NOW_PLAYING = "/now_playing"
    NOW_PLAYING_CAMEL = "/nowPlaying"
    GET_ZONE = "/getZone"
    PRESETS = "/presets"
    VOLUME = "/volume"
    SLAVE_MSG = "/slaveMsg"
    REMOVE_ZONE_SLAVE = "/removeZoneSlave"
    NOTIFICATION = "/notification"
    ERROR = "/error"
    SELECT = "/select"


class ChannelKind(StrEnum):
    """What kind of source a channel of the house's channel list names.

    ``RADIO`` is the one M2 accepts and the one the master already knows how to play: a URL pulled
    at the server's pace. ``MPD`` is the same thing from a different producer - MPD's ``httpd``
    output is an HTTP MP3 stream - so it costs the master nothing; what it adds is a CONTROL side,
    because the playlist behind that URL is chosen by talking to MPD. ``FILES`` and ``SINK`` arrive
    with M3b and M4.

    The field is written into the channel file from its first day ON PURPOSE, although only one
    value is legal today. People edit that file by hand, so a later release adding a variant is
    cheaper than one migrating every file that exists.
    """

    RADIO = "radio"
    MPD = "mpd"


class ChannelEnd(StrEnum):
    """What an MPD channel does when its queue runs out by itself (user, 2026-09-24).

    ``WRAP`` starts again from the first entry, which is what a music playlist wants and what MPD
    does with ``repeat`` on. ``STOP`` falls silent and forgets where the house was, so the book
    begins again the next time somebody dials it instead of playing its last seconds and falling
    silent a second time. Only the NATURAL end: a press past either end wraps on every channel,
    because somebody is standing there and asked for something.
    """

    WRAP = "wrap"
    STOP = "stop"


class SlaveAction(StrEnum):
    """The ``action`` attribute of a ``/slaveMsg`` body."""

    SELECT = "select"
    KEY = "key"


class FrameKind(StrEnum):
    """The notification frames the service matches by NAME rather than by what they carry.

    A box sends far more kinds than this, and every other one the service reads is recognised by
    its content instead - a preset id makes a frame a selection, a ``<nowPlaying>`` element makes
    it a report. Only this one carries nothing at all, so its name is the whole message.

    ``userActivityUpdate`` is what a box sends when a HUMAN touched it, and it is the only thing
    that separates a preset somebody pressed from the master's own station change coming back
    (:mod:`soundtouch_zonemaster.presses`).
    """

    USER_ACTIVITY_UPDATE = "userActivityUpdate"


class KeyName(StrEnum):
    """The keys a slave forwards that carry no ContentItem, measured 2026-09-06.

    All four arrived from a slave in a zone, which is what keeps next, previous and the thumbs as
    real inputs rather than something the speaker handles alone. A speaker may name a key absent
    from this set, so an event keeps the wire string and compares against these members.
    """

    NEXT_TRACK = "NEXT_TRACK"
    PREV_TRACK = "PREV_TRACK"
    THUMBS_UP = "THUMBS_UP"
    THUMBS_DOWN = "THUMBS_DOWN"


class KeyState(StrEnum):
    """The ``state`` attribute of a ``keyData`` element.

    Every key arrives TWICE, once in each state, 285 to 448 ms apart over the nineteen pairs the
    service's journal holds. That is NARROWER than the dialling window's floor of 500 ms, which is
    why both halves of one press land inside one window and anything counting key events without
    telling the two states apart reads one press as two.
    """

    PRESS = "press"
    RELEASE = "release"


class SourceName(StrEnum):
    """The ``source`` a nowPlaying document reports."""

    LOCAL_INTERNET_RADIO = "LOCAL_INTERNET_RADIO"
    STANDBY = "STANDBY"


class PlayStatus(StrEnum):
    """The ``playStatus`` a nowPlaying document reports."""

    PLAY_STATE = "PLAY_STATE"
    STOP_STATE = "STOP_STATE"


class ArtImageStatus(StrEnum):
    """The ``artImageStatus`` attribute of a nowPlaying ``<art>`` element."""

    SHOW_DEFAULT_IMAGE = "SHOW_DEFAULT_IMAGE"


class StreamType(StrEnum):
    """The ``streamType`` a nowPlaying document reports."""

    RADIO_STREAMING = "RADIO_STREAMING"


class ProductCode(StrEnum):
    """What a device IS, as the replacement service's device list reports it.

    Measured 2026-09-06 against the deployed AfterTouch: ``SoundTouch`` for a speaker and
    ``Lifestyle`` for the console. It is what separates a box the zone may take on from one an
    API POWER would flip to another input, so the rule that keeps the console out no longer needs
    an address written into the source.

    A registry entry may name a model absent from this set, so a record keeps its product code as
    ``str`` and compares against these members, the same way a decoded frame treats
    :class:`MsgTypeName`. There is exactly one deployment behind these two strings.
    """

    SOUNDTOUCH = "SoundTouch"
    LIFESTYLE = "Lifestyle"


class MpdState(StrEnum):
    """The three values MPD's ``status`` reports for ``state``, measured 2026-09-10.

    It is a closed set here rather than a bare string, unlike :class:`MsgTypeName` and
    :class:`ProductCode`, because the producer is one program at a known version answering its own
    documented protocol, not a fleet of speakers whose firmware may say something new.
    """

    PLAY = "play"
    PAUSE = "pause"
    STOP = "stop"
