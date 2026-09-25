"""What a speaker says when it is not in the zone: its own notification channel.

A speaker in standby, on AUX, or simply not a member sends the master nothing at all, so without
this the service would only ever hear from the boxes it already holds. It is also the only place
the preset NUMBER appears: a forwarded press reaches the master with the ContentItem and no
number, measured in the M0 run of 2026-09-06.

One observer per speaker, each holding its channel open and reconnecting on its own. A channel
that ends is the normal case rather than the exception here - one box is on radio and drops out
for minutes at a time - so nothing about a failure ends the loop, and every attempt asks the
registry for the address again rather than holding the one it started with.

``research/observe_keys.py`` keeps its own copy of the parser below and is deliberately not
refactored to import this. It is a research instrument that has to run on a machine where this
package is not installed, the same reason ``research/_click.py`` and ``research/capture_model.py``
are copies; and this one is free to grow what the service needs while that one stays the thing
whose recorded output is a contract.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

import websockets

from ...application.options import ChannelPolicy
from ...domain.events import SpeakerEvent
from . import xmlmodels
from .xmlread import attribute_anywhere, child_tag, element_anywhere, parse

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from ...domain.logfn import LogFn
    from .xmlread import Element

__all__ = [
    "SUBPROTOCOL",
    "UNREADABLE",
    "SpeakerObserver",
    "parse_frame",
    "parse_now_playing",
]


SUBPROTOCOL = websockets.Subprotocol("gabbo")
"""The subprotocol the channel is opened with (research/capture_zone.py).

Spelled as the library's own NewType rather than a bare string because that is what ``connect``
declares it takes, so the constant carries the right type from its one definition.
"""


UNREADABLE = "unknown"
"""The kind a frame gets when nothing could be read out of it - including an unparseable one."""


def parse_frame(speaker: str, frame: str, received_at: float) -> SpeakerEvent:
    """One frame as a record, keeping the frame itself whatever happens.

    Read through ``xmlread.parse``, which bounds what arrives from the unauthenticated LAN side by
    size and depth and refuses a DOCTYPE outright. A frame it will not parse still becomes an
    event, carrying the frame and the kind ``unknown``: a dropped frame and a silent speaker look
    identical afterwards, and only one of them is a problem.

    The source and the stream owner are read from the attributes of the ``<nowPlaying>`` element
    and nowhere else. A selection frame carries a ``source`` inside the preset's ContentItem too,
    and reading that one would report what a box could play as what it is playing.
    """
    root = parse(frame)
    if root is None:
        return SpeakerEvent(received_at=received_at, speaker=speaker, device_id="", kind=UNREADABLE, frame=frame)
    playing = element_anywhere(root, "nowPlaying")
    preset = element_anywhere(root, "preset")
    return SpeakerEvent(
        received_at=received_at,
        speaker=speaker,
        device_id=attribute_anywhere(root, "deviceID") or "",
        kind=_kind_of(root),
        preset_id=_preset_number(preset),
        source=playing.get("source") if playing is not None else None,
        stream_owner=playing.get("deviceID") if playing is not None else None,
        volume=_level(element_anywhere(root, "volume")),
        frame=frame,
    )


def _level(held: Element | None) -> int | None:
    """The level a ``<volume>`` element names, or None when there is none or it is not a number.

    Read with the same model as a ``/volume`` answer, because it is the same element: a frame
    wraps it in ``<volumeUpdated>`` and the answer does not. The ACTUAL level rather than the target,
    because it is where the box is, and every frame recorded on 2026-09-24 carried the two equal.
    """
    level = xmlmodels.volume(held) if held is not None else None
    return level.actual if level is not None else None


def _kind_of(root: Element) -> str:
    """What a frame announces: the first child of ``<updates>``, or the root's own tag."""
    updates = element_anywhere(root, "updates")
    inner = child_tag(updates) if updates is not None else None
    return inner if inner is not None else str(root.tag)


def _preset_number(preset: Element | None) -> int | None:
    """The number of the first ``<preset>``, or None when there is none or it is not a number."""
    if preset is None:
        return None
    number = preset.get("id", "")
    return int(number) if number.isdigit() else None


def parse_now_playing(speaker: str, device_id: str, document: str, received_at: float) -> SpeakerEvent:
    """A ``/now_playing`` ANSWER as an event, with the device id supplied rather than read.

    The service asks a box this question once at start, because everything else it knows arrives
    in a frame the box chose to send. A box that was already in standby has sent nothing, so its
    next frame - the one where somebody switches it on - would read as a box playing its own
    radio, which is a reason to stay OUT rather than the POWER-on that it is.

    The device id is supplied because the only one in that document is the OWNER of what is
    playing: the box itself when it plays its own, the MASTER when the stream is ours. Reading the
    speaker's identity out of it would name the master as the speaker for every member.
    """
    root = parse(document)
    playing = element_anywhere(root, "nowPlaying") if root is not None else None
    return SpeakerEvent(
        received_at=received_at,
        speaker=speaker,
        device_id=device_id,
        kind="nowPlaying",
        source=playing.get("source") if playing is not None else None,
        stream_owner=playing.get("deviceID") if playing is not None else None,
        frame=document,
    )


class SpeakerObserver:
    """One speaker's notification channel, held open for as long as the observer runs.

    It is addressed by device id rather than by address, and asks ``address_of`` again on every
    attempt: the registry is what knows where a box currently is, and a speaker that comes back on
    a different address must not need the service restarted. ``None`` from that lookup means the
    registry does not list it right now, which is a reason to wait and ask again, never a reason
    to stop watching.
    """

    def __init__(
        self,
        device_id: str,
        *,
        address_of: Callable[[str], Awaitable[str | None]],
        events: asyncio.Queue[SpeakerEvent],
        log: LogFn,
        policy: ChannelPolicy | None = None,
    ) -> None:
        self.device_id = device_id
        self.address_of = address_of
        self.events = events
        self.log = log
        self.policy = policy if policy is not None else ChannelPolicy()
        self._attempt = 0

    async def run(self) -> None:
        """Watch until cancelled. Nothing short of cancellation ends this."""
        while True:
            try:
                await self._one_connection()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any failure here is a retry, never the end
                self.log("observer", f"{self.device_id}: {type(exc).__name__}: {exc}")
                await self._wait_before_retrying()

    async def _one_connection(self) -> None:
        """Resolve the address, hold the channel, and put every frame on the queue."""
        address = await self.address_of(self.device_id)
        if address is None:
            self.log("observer", f"{self.device_id}: not in the registry right now")
            await self._wait_before_retrying()
            return

        url = f"ws://{address}:{self.policy.port}"
        async with websockets.connect(url, subprotocols=[SUBPROTOCOL]) as channel:
            self._attempt = 0
            self.log("observer", f"{self.device_id}: watching {url}")
            while True:
                frame = await channel.recv()
                text = frame.decode("utf-8", "replace") if isinstance(frame, bytes) else frame
                await self.events.put(parse_frame(address, text, time.time()))

    async def _wait_before_retrying(self) -> None:
        delay = self.policy.backoff_s[min(self._attempt, len(self.policy.backoff_s) - 1)]
        self._attempt += 1
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.Event().wait(), delay)
