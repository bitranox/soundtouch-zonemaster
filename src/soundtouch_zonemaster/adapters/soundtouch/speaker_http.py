"""The speaker's own HTTP API, as the handful of calls this program makes to it.

A speaker answers on port 8090 whatever else is going on, so these are the calls that do not belong
to a zone: read what a box is playing, read and set its volume, select a station on it, and read a
preset off it. They are module-level functions rather than methods because none of them needs a
zone to exist - the service uses them on boxes that are not members, and the zone master uses the
same two verbs on boxes that are.

The document shapes they read are parsed elsewhere: a ``<ContentItem>`` by
``xmlmodels.station_request``, a nowPlaying frame by ``observer.parse_now_playing``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx

from ...domain import zonexml
from ...domain.enums import SpeakerPath
from . import xmlmodels
from .observer import parse_now_playing
from .xmlread import parse, serialised_as

if TYPE_CHECKING:
    from ...domain.station import StationRequest
    from .xmlread import Element

if TYPE_CHECKING:
    from ...domain.events import SpeakerEvent

__all__ = [
    "SPEAKER_HTTP_TIMEOUT_S",
    "http_get",
    "http_post",
    "read_volume",
    "select_station",
    "set_volume",
    "speaker_now_playing",
    "station_from_speaker_preset",
    "volume_in",
]


async def station_from_speaker_preset(ip: str, number: int) -> StationRequest:
    """Read preset ``number`` off a speaker as a station this master can play.

    An EMPTY slot is told apart from a missing one, and the two raise different things to say so:
    a box writes an empty preset as ``<ContentItem ... />``, which is a slot that is there with
    nothing in it, while a number the box does not carry at all is not there. Reading the document
    with a parser makes that distinction the element's own rather than a matter of which spelling
    a pattern happened to accept.
    """
    document = await http_get(ip, SpeakerPath.PRESETS)
    root = parse(document)
    item = _preset_item(root, number) if root is not None else None
    if item is None:
        raise RuntimeError(f"{ip}: preset {number} not found")
    request = xmlmodels.station_request(serialised_as(item, "ContentItem"))
    if request is None:
        raise RuntimeError(f"{ip}: preset {number} has no location to play")
    return request


def _preset_item(root: Element, number: int) -> Element | None:
    """The item element of one numbered slot, or ``None`` when the box lists no such slot.

    The element rather than its values, because the document is what a speaker is later handed to
    play the station again, and it travels verbatim.
    """
    wanted = str(number)
    for preset in root.iter("preset"):
        if preset.get("id") == wanted:
            return next(iter(preset), None)
    return None


async def speaker_now_playing(ip: str, device_id: str) -> SpeakerEvent | None:
    """What a box says it is playing, asked over its HTTP face rather than waited for.

    ``None`` rather than a raise when it does not answer: a box that is off the air is unreachable,
    and the membership policy already knows what to do about that - nothing, until its timeout.
    Answering "I could not ask" as if it were "it is in standby" would take a speaker away from
    somebody over a dropped packet.
    """
    try:
        document = await http_get(ip, SpeakerPath.NOW_PLAYING)
    except Exception:  # noqa: BLE001 - every way this fails means the same thing: it did not answer
        return None
    return parse_now_playing(ip, device_id, document, time.time())


def volume_in(volume_xml: str) -> int:
    """The volume out of a speaker's ``/volume`` document, or ``-1`` when it names none."""
    root = parse(volume_xml)
    level = xmlmodels.volume(root) if root is not None else None
    return level.actual if level is not None and level.actual is not None else -1


async def read_volume(ip: str) -> int:
    """What the box is set to, or ``-1`` when its answer does not carry a level.

    ``-1`` rather than an exception or a zero, because the caller's decision is whether it may MUTE
    this box: a level that could not be read is a level that could not be put back, and zero is a
    level rather than an absence.

    A module function rather than a method, like the other speaker reads here, because putting a
    volume back must not depend on holding a zone - the service does it at start-up, before it
    knows whether the switch is even on.
    """
    return volume_in(await http_get(ip, SpeakerPath.VOLUME))


async def set_volume(ip: str, level: int) -> None:
    """Put the box at one volume.

    The document shape is the one ``research/capture_zone.py`` has been setting and restoring on
    real speakers since M0, so it is measured rather than read off an API page.
    """
    await http_post(ip, SpeakerPath.VOLUME, f"<volume>{level}</volume>")


async def select_station(ip: str, *, url: str, name: str) -> None:
    """Make one box play a station on its own, with no zone and no master stream behind it.

    The body is a bare ``<ContentItem>``, no XML declaration and no wrapper element - that is what
    a real box accepted on ``/select`` (research/captures/2026-09-05-room3-room1/events.jsonl).
    A module function like ``set_volume``, for the same reason: this must not depend on holding a
    zone, since it is meant for a box a zone was never built around.
    """
    await http_post(ip, SpeakerPath.SELECT, zonexml.station_content_item(url=url, name=name))


SPEAKER_HTTP_TIMEOUT_S = 8.0
"""How long one call to a speaker may take before it is a failure.

Named rather than typed twice because something outside this module depends on it: a box that is
off answers by making the caller wait this out, so it is how long a JOIN can take to fail, and
``JOIN_RETRY_S`` has to leave room for it inside the window a woken box counts as awake for
(``tests/test_service_loopback.py`` holds the three numbers to that rule).
"""


async def http_get(ip: str, path: str) -> str:
    async with httpx.AsyncClient(timeout=SPEAKER_HTTP_TIMEOUT_S) as c:
        r = await c.get(f"http://{ip}:8090{path}")
        r.raise_for_status()
        return r.text


async def http_post(ip: str, path: str, body: str) -> str:
    async with httpx.AsyncClient(timeout=SPEAKER_HTTP_TIMEOUT_S, headers={"User-Agent": "Bose_Lisa/27.0.6"}) as c:
        r = await c.post(f"http://{ip}:8090{path}", content=body.encode(), headers={"Content-Type": "text/plain"})
        r.raise_for_status()
        return r.text
