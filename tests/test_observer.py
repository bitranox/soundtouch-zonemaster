"""The service's observer, against a notification channel that behaves like a speaker's.

The observer is how the service learns anything about a speaker that is NOT in the zone: a box in
standby sends the master nothing, so without this the service would only ever hear from the boxes
it already holds. M0 measured that the preset NUMBER appears here and nowhere else.

A real WebSocket server on loopback, speaking the same subprotocol. Nothing is monkeypatched and
no frame is invented: the two shapes asserted on are the ones recorded in the M0 run.

The reconnection tests are the ones that earn their keep. Room2 is on radio and drops out for
minutes at a time (OPEN-WORK rank 50), so a channel that ends is the normal case rather than the
exception, and an observer that quietly stops after the first drop looks exactly like a speaker
that has gone quiet.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import pytest
from speaker_double import now_playing_frames
from websockets.asyncio.server import Server, ServerConnection, serve

from soundtouch_zonemaster.adapters.soundtouch.observer import SUBPROTOCOL, SpeakerObserver, parse_frame
from soundtouch_zonemaster.application.options import ChannelPolicy

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from soundtouch_zonemaster.domain.events import SpeakerEvent

HOST = "127.0.0.1"
DEVICE = "AABBCC000010"
FAST_BACKOFF = (0.01, 0.01)
"""The real one starts at a second. A test proving a reconnect happens should not wait it out."""

SELECTION = (
    '<updates deviceID="AABBCC000010">'
    '<nowSelectionUpdated><preset id="3">'
    '<ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" location="http://example.invalid/s">'
    "<itemName>Some Station</itemName></ContentItem></preset></nowSelectionUpdated></updates>"
)
VOLUME = (
    '<updates deviceID="AABBCC000010"><volumeUpdated>'
    "<volume><actualvolume>30</actualvolume></volume></volumeUpdated></updates>"
)


class FakeNotificationChannel:
    """A speaker's notification socket: one script per connection, in the order they arrive.

    A connection that has run out of script either closes, which is what a speaker dropping off
    the radio looks like from here, or holds open, which is what a speaker with nothing to say
    looks like. Both are needed: the difference between them is the whole point of the reconnect.
    """

    def __init__(self, scripts: Sequence[Sequence[str]], *, close_after_script: bool = False) -> None:
        self.scripts = [list(script) for script in scripts]
        self.close_after_script = close_after_script
        self.connections = 0
        self.port = 0
        self._server: Server | None = None

    async def start(self) -> None:
        self._server = await serve(self._handle, HOST, 0, subprotocols=[SUBPROTOCOL])
        # getsockname() is untyped, so the shape is stated once here rather than leaving the
        # port partially unknown at every call that passes it on.
        bound = cast("tuple[str, int]", next(iter(self._server.sockets)).getsockname())
        self.port = bound[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, connection: ServerConnection) -> None:
        index = self.connections
        self.connections += 1
        for frame in self.scripts[index] if index < len(self.scripts) else []:
            await connection.send(frame)
        if not self.close_after_script:
            await connection.wait_closed()
            # Not a bare Event: the server's own shutdown waits for its handlers, so a handler
            # parked on something the shutdown cannot reach hangs the teardown rather than the
            # test, and the run then fails a long way from the reason.


async def _running(
    channel: FakeNotificationChannel,
    *,
    addresses: list[str | None] | None = None,
) -> AsyncGenerator[tuple[asyncio.Queue[SpeakerEvent], list[str]], None]:
    """The observer against that channel, torn down however the test ends."""
    await channel.start()
    events: asyncio.Queue[SpeakerEvent] = asyncio.Queue()
    lookups: list[str] = []
    pending = list(addresses) if addresses is not None else None

    async def address_of(device_id: str) -> str | None:
        lookups.append(device_id)
        if pending is None:
            return HOST
        return pending.pop(0) if pending else HOST

    observer = SpeakerObserver(
        DEVICE,
        address_of=address_of,
        events=events,
        log=lambda _kind, _text: None,
        policy=ChannelPolicy(port=channel.port, backoff_s=FAST_BACKOFF),
    )
    task = asyncio.create_task(observer.run())
    try:
        yield events, lookups
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await channel.stop()


async def _next_event(events: asyncio.Queue[SpeakerEvent], timeout: float = 5.0) -> SpeakerEvent:
    return await asyncio.wait_for(events.get(), timeout)


def test_a_selection_frame_gives_up_its_preset_number_as_a_field() -> None:
    """Read as a named field, never as a regex at a call site: the whole digit path rests on it."""
    event = parse_frame(HOST, SELECTION, 1.0)

    assert event.kind == "nowSelectionUpdated"
    assert event.preset_id == 3
    assert event.device_id == "AABBCC000010"


def test_a_frame_that_is_not_a_selection_is_carried_rather_than_dropped() -> None:
    event = parse_frame(HOST, VOLUME, 1.0)

    assert event.kind == "volumeUpdated"
    assert event.preset_id is None


RECORDED_VOLUME = (
    '<updates deviceID="AABBCC0000A1"><volumeUpdated><volume><targetvolume>12</targetvolume>'
    "<actualvolume>12</actualvolume><muteenabled>false</muteenabled></volume></volumeUpdated></updates>"
)
"""Room1 after one tap of volume up, recorded 2026-09-24 21:01:06 (thumb-and-volume capture)."""


def test_a_volume_frame_carries_the_level_the_box_is_at() -> None:
    """The house-volume rule steps the house by the difference between two of these."""
    event = parse_frame(HOST, RECORDED_VOLUME, 1.0)

    assert event.kind == "volumeUpdated"
    assert event.volume == 12


def test_a_frame_that_is_not_a_volume_frame_carries_no_level() -> None:
    assert parse_frame(HOST, SELECTION, 1.0).volume is None


def test_a_volume_frame_whose_level_is_not_a_number_carries_no_level() -> None:
    """A box on the unauthenticated LAN can send anything; a level that is not a number is none."""
    frame = RECORDED_VOLUME.replace("<actualvolume>12<", "<actualvolume>loud<")

    assert parse_frame(HOST, frame, 1.0).volume is None


def test_a_frame_that_parses_to_nothing_useful_still_becomes_an_event() -> None:
    """Dropping it would hide the one thing worth knowing: that something arrived and was unread."""
    event = parse_frame(HOST, "not xml at all", 1.0)

    assert event.kind == "unknown"
    assert event.preset_id is None
    assert event.frame == "not xml at all"


async def test_frames_from_a_live_channel_arrive_as_events() -> None:
    channel = FakeNotificationChannel([[SELECTION, VOLUME]])

    async for events, _lookups in _running(channel):
        first = await _next_event(events)
        second = await _next_event(events)

        assert (first.kind, first.preset_id) == ("nowSelectionUpdated", 3)
        assert second.kind == "volumeUpdated"


async def test_the_stream_survives_the_speaker_dropping_the_channel() -> None:
    channel = FakeNotificationChannel([[SELECTION], [VOLUME]], close_after_script=True)

    async for events, _lookups in _running(channel):
        first = await _next_event(events)
        second = await _next_event(events)

        assert first.kind == "nowSelectionUpdated"
        assert second.kind == "volumeUpdated"
        assert channel.connections >= 2


async def test_the_address_is_looked_up_again_on_every_reconnect() -> None:
    """It must not hold the address it started with: a speaker can come back on another one."""
    channel = FakeNotificationChannel([[SELECTION], [VOLUME]], close_after_script=True)

    async for events, lookups in _running(channel):
        await _next_event(events)
        await _next_event(events)

        assert len(lookups) >= 2
        assert set(lookups) == {DEVICE}


async def test_a_speaker_the_registry_no_longer_lists_is_waited_for_rather_than_refused() -> None:
    """A registry that cannot answer is a reason to wait, never a reason to stop watching."""
    channel = FakeNotificationChannel([[SELECTION]])

    async for events, lookups in _running(channel, addresses=[None, None, HOST]):
        event = await _next_event(events)

        assert event.kind == "nowSelectionUpdated"
        assert len(lookups) >= 3


async def test_one_speaker_being_unreachable_does_not_end_its_observer() -> None:
    """Nothing is listening on that port at all, and the observer must keep trying."""
    channel = FakeNotificationChannel([[SELECTION]])
    await channel.start()
    dead_port = channel.port
    await channel.stop()

    events: asyncio.Queue[SpeakerEvent] = asyncio.Queue()
    attempts = 0

    async def address_of(_device_id: str) -> str | None:
        nonlocal attempts
        attempts += 1
        return HOST

    observer = SpeakerObserver(
        DEVICE,
        address_of=address_of,
        events=events,
        log=lambda _kind, _text: None,
        policy=ChannelPolicy(port=dead_port, backoff_s=FAST_BACKOFF),
    )
    task = asyncio.create_task(observer.run())
    try:
        await asyncio.sleep(0.2)
        assert attempts >= 2
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_a_failure_is_logged_with_the_speaker_it_belongs_to() -> None:
    """An observer that retries in silence is one nobody can tell from a quiet speaker."""
    lines: list[tuple[str, str]] = []
    channel = FakeNotificationChannel([[SELECTION]])
    await channel.start()
    dead_port = channel.port
    await channel.stop()

    observer = SpeakerObserver(
        DEVICE,
        address_of=lambda _device_id: asyncio.sleep(0, result=HOST),
        events=asyncio.Queue(),
        log=lambda kind, text: lines.append((kind, text)),
        policy=ChannelPolicy(port=dead_port, backoff_s=FAST_BACKOFF),
    )
    task = asyncio.create_task(observer.run())
    try:
        await asyncio.sleep(0.2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert lines
    assert any(DEVICE in text for _kind, text in lines)


@pytest.mark.parametrize(
    ("frame", "kind", "device_id"),
    [
        ("", "unknown", ""),
        ("<updates>", "unknown", ""),
        ("<updates></updates>", "updates", ""),
        ('<updates deviceID="X"></updates>', "updates", "X"),
    ],
)
def test_a_frame_with_no_inner_update_falls_back_to_its_root_tag(frame: str, kind: str, device_id: str) -> None:
    """The parser is fed by an unauthenticated LAN socket, so it reports rather than raises.

    Written first as "it does not raise", which a parser that returns an empty record satisfies
    as happily as a real one. What is asserted instead is the fallback ladder: the inner update
    names the kind, the root tag stands in for it, and only a frame with neither is unknown.

    An UNCLOSED ``<updates>`` is in the ladder's bottom rung rather than its middle one, and that
    moved on 2026-09-22: it is not a well-formed document, so the parser refuses it whole and the
    frame is reported as unknown, carrying its bytes for whoever reads the log. A speaker never
    sends one; what the case holds is that a fragment is not quietly read as if it were a document.
    """
    event = parse_frame(HOST, frame, 1.0)

    assert (event.kind, event.device_id) == (kind, device_id)
    assert event.frame == frame


def test_a_box_in_standby_names_itself_as_the_owner_of_what_it_plays() -> None:
    """Measured: in standby the nowPlaying deviceID is the box's own."""
    standby, _playing = now_playing_frames()

    event = parse_frame(HOST, standby, 1.0)

    assert event.source == "STANDBY"
    assert event.stream_owner == event.device_id


def test_a_box_on_the_masters_stream_names_the_master_as_the_owner() -> None:
    """This is the only thing that separates our stream from the box's own internet radio."""
    _standby, playing = now_playing_frames()

    event = parse_frame(HOST, playing, 1.0)

    assert event.source == "LOCAL_INTERNET_RADIO"
    assert event.stream_owner is not None
    assert event.stream_owner != event.device_id


def test_a_selection_frame_reports_no_source_at_all() -> None:
    """Its ContentItem carries one, and reading THAT would report what a box could play."""
    event = parse_frame(HOST, SELECTION, 1.0)

    assert event.source is None
    assert event.stream_owner is None
