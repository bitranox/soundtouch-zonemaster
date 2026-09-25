"""The XML documents the slaves ask for, and the ones the master pushes at them.

Every document a speaker sees is built here, so the escaping rule (``xmlfmt``: by POSITION,
between tags or inside quotes) is applied in one place rather than at each f-string. Pure
string building - nothing here reaches a socket or reads master state on its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from .enums import ArtImageStatus, PlayStatus, SourceName, StreamType
from .xmlfmt import attr, fragment, text

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .station import Station

__all__ = [
    "XML_HEAD",
    "ZoneMember",
    "dissolve_body",
    "info_xml",
    "members_xml",
    "now_playing_inner",
    "now_playing_notification",
    "now_playing_xml",
    "selection_message",
    "station_content_item",
    "zone_body",
    "zone_xml",
]

XML_HEAD = '<?xml version="1.0" encoding="UTF-8" ?>'


class ZoneMember(Protocol):
    """What rendering a zone document needs of a slave.

    A Protocol rather than the master's ``Slave``: the master sits above this module, so naming
    its record here would be an upward import. ``Slave`` satisfies this structurally.
    """

    ip: str
    device_id: str


def members_xml(members: Iterable[ZoneMember]) -> str:
    """The zone's members. Both zone documents render the same list, so they render it here."""
    return "".join(f'<member ipaddress="{attr(m.ip)}">{text(m.device_id)}</member>' for m in members)


def zone_body(*, device_id: str, bind_ip: str, members: Iterable[ZoneMember], sender_is_master: bool) -> str:
    """The ``/setZone`` document a slave is told to join."""
    flag = ' senderIsMaster="true"' if sender_is_master else ""
    return (
        f'{XML_HEAD}<zone master="{attr(device_id)}" senderIPAddress="{attr(bind_ip)}"{flag}>'
        f"{members_xml(members)}</zone>"
    )


def dissolve_body(*, device_id: str, bind_ip: str) -> str:
    """An empty zone; a real slave goes to standby on that (E6)."""
    return f'{XML_HEAD}<zone master="{attr(device_id)}" senderIPAddress="{attr(bind_ip)}" senderIsMaster="true" />'


def zone_xml(*, device_id: str, members: Iterable[ZoneMember]) -> str:
    """The answer to a slave's ``/getZone``."""
    return f'{XML_HEAD}<zone master="{attr(device_id)}">{members_xml(members)}</zone>'


def info_xml(*, device_id: str, bind_ip: str) -> str:
    """The answer to ``/info``: what this master claims to be."""
    return (
        f'{XML_HEAD}<info deviceID="{attr(device_id)}"><name>ZoneMaster</name><type>SoundTouch 20</type>'
        f'<margeAccountUUID>0</margeAccountUUID><networkInfo type="SCM">'
        f"<macAddress>{text(device_id)}</macAddress>"
        f"<ipAddress>{text(bind_ip)}</ipAddress></networkInfo></info>"
    )


def now_playing_inner(*, device_id: str, station: Station, play_status: PlayStatus) -> str:
    """The ``<nowPlaying>`` element itself, without the XML declaration.

    Shared by the ``/now_playing`` answer and the notification pushed on a selection, which is
    why it is separate: the two documents disagreed about the station name when they were built
    apart.
    """
    return (
        f'<nowPlaying deviceID="{attr(device_id)}" '
        f'source="{SourceName.LOCAL_INTERNET_RADIO}" sourceAccount="">'
        f"{fragment(station.content_item_xml)}<track>{text(station.name)}</track><artist></artist><album></album>"
        f"<stationName>{text(station.name)}</stationName>"
        f'<art artImageStatus="{ArtImageStatus.SHOW_DEFAULT_IMAGE}" />'
        f"<playStatus>{play_status}</playStatus><streamType>{StreamType.RADIO_STREAMING}</streamType></nowPlaying>"
    )


def now_playing_xml(*, device_id: str, station: Station | None) -> str:
    """The answer to ``/now_playing``; the standby document when no station is selected."""
    if station is None:
        return (
            f'{XML_HEAD}<nowPlaying deviceID="{attr(device_id)}" source="{SourceName.STANDBY}">'
            f'<ContentItem source="{SourceName.STANDBY}" isPresetable="false" /></nowPlaying>'
        )
    return XML_HEAD + now_playing_inner(device_id=device_id, station=station, play_status=PlayStatus.PLAY_STATE)


def station_content_item(*, url: str, name: str) -> str:
    """A ``<ContentItem>`` for a station that was given as a URL rather than read off a speaker.

    A preset press hands the master an item a box already had; a channel the service is configured
    with has none, so one is built. It is built HERE because it ends up quoted into two documents a
    speaker parses, and the escaping rule lives in this module.
    """
    return (
        f'<ContentItem source="{SourceName.LOCAL_INTERNET_RADIO}" type="stationurl" location="{attr(url)}" '
        f'sourceAccount="" isPresetable="true"><itemName>{text(name)}</itemName></ContentItem>'
    )


def selection_message(station: Station) -> str:
    """The ``/masterMsg`` telling a slave what the zone has just selected."""
    return (
        f'{XML_HEAD}<masterMessage action="nowSelection"><selection id="1">{fragment(station.content_item_xml)}'
        f"</selection></masterMessage>"
    )


def now_playing_notification(*, device_id: str, bind_ip: str, station: Station) -> str:
    """The ``/notification`` that follows a selection, so the slave's display catches up."""
    inner = now_playing_inner(device_id=device_id, station=station, play_status=PlayStatus.PLAY_STATE)
    return (
        f'<updates deviceID="{attr(device_id)}"><nowPlayingUpdated masterIP="{attr(bind_ip)}">'
        f"{inner}</nowPlayingUpdated></updates>"
    )
