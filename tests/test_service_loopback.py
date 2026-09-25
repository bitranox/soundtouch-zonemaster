"""The service end to end on loopback: the contract M1 is accepted against.

No speaker in the flat and nothing audible. Three boxes on three loopback addresses, each serving
the two faces a real one has - the HTTP API the master calls and the notification WebSocket the
service listens to - plus AfterTouch's device list on a fourth port and a station on a fifth.
Nothing is monkeypatched: the service is handed addresses, and everything it reaches is a real
server answering real bytes.

What is asserted is what a box RECEIVED, because that is what a person in the room hears. A
``/setZone`` naming members is a box being taken into the zone; one naming none is the zone being
dissolved, which puts a real box into standby (E6); and no request at all is a box being left
alone. That last one is the assertion this house cares most about: a speaker somebody is using on
AUX must be dropped from the zone WITHOUT being sent anything.

The frames the boxes send are the ones the M0 run recorded, with the device ids substituted
(``speaker_double``). The wake is the design's POWER-on rule, and it only reads as a wake because
the service asks every box what it is playing before it does anything else - a box that was
already in standby when the service started has never sent us a frame, and its wake frame says
"playing its own radio", which on its own is a reason to stay OUT.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import pytest
from mpdfake import HOST as MPD_HOST
from mpdfake import FakeMpd
from registry_double import FakeRegistry, devices_at
from speaker_double import (
    FakeSpeaker,
    key_body,
    now_playing_document,
    now_playing_frame,
    open_transport,
    read_frames_but_not_pings,
    selection_frame,
    user_activity_frame,
)

from soundtouch_zonemaster.adapters.files.channel_file import load_channels, save_channels
from soundtouch_zonemaster.adapters.files.state_file import load_state, save_state
from soundtouch_zonemaster.adapters.soundtouch.pb import audio
from soundtouch_zonemaster.adapters.soundtouch.reports import SlaveState
from soundtouch_zonemaster.adapters.soundtouch.speaker_http import SPEAKER_HTTP_TIMEOUT_S
from soundtouch_zonemaster.adapters.soundtouch.zone_master import ZoneMaster
from soundtouch_zonemaster.application.options import ChannelPolicy, ServiceOptions
from soundtouch_zonemaster.application.zone_service.constants import (
    FADE_S,
    JOIN_RETRY_S,
    MUTE_HOLD_S,
    PORTS_BUSY_RETRY_S,
)
from soundtouch_zonemaster.application.zone_service.service import ZoneService
from soundtouch_zonemaster.composition import build_production, hold_the_zone
from soundtouch_zonemaster.domain.channellist import Channel, ChannelList
from soundtouch_zonemaster.domain.dialling import WINDOW_DEFAULT_S, WINDOW_FLOOR_S
from soundtouch_zonemaster.domain.enums import ChannelEnd, ChannelKind, KeyName, KeyState, SourceName
from soundtouch_zonemaster.domain.longpress import HOLD_THRESHOLD_DEFAULT_S
from soundtouch_zonemaster.domain.membership import UNREACHABLE_TIMEOUT_S, WAKE_WINDOW_S
from soundtouch_zonemaster.domain.presses import CONFIRM_BACK_WINDOW_S
from soundtouch_zonemaster.domain.state import Place, ZoneState
from soundtouch_zonemaster.domain.zonexml import station_content_item

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
    from pathlib import Path

MASTER = "127.0.0.1"
MASTER_ID = "5EB0CE000001"

STUDIO_ID, STUDIO_IP = "AABBCC000010", "127.0.0.2"
HALLWAY_ID, HALLWAY_IP = "AABBCC000011", "127.0.0.3"
CONSOLE_ID, CONSOLE_IP = "AABBCC000012", "127.0.0.4"
"""The speaker the channel list is seeded from, named the way the registry names it.

A NAME and not an address, because the design picks one speaker deliberately rather than the first
one to answer: the result must not depend on start order or on the radio.
"""

MOVED_IP = "127.0.0.5"
"""Where the studio turns up after a reboot onto a new lease, in the one test that moves it."""
"""The three devices of the shipped fixture, moved onto addresses a test can reach. The third is
the Lifestyle console, and the service must leave it alone without being told its address."""

AUX = "AUX"
"""What a box reports with somebody listening on the aux input. Never measured here, and nothing
in the policy matches on it - that is the point: anything that is not our stream keeps a box out."""

RADIO = SourceName.LOCAL_INTERNET_RADIO


def free_port() -> int:
    """A port nothing is using, taken once and then bound on each speaker's own address.

    The observers all reach for the same port because a real box has one; two boxes can only share
    it here because they are on different addresses, which is true of the flat too.
    """
    with socket.socket() as probe:
        probe.bind((MASTER, 0))
        return int(probe.getsockname()[1])


@dataclass
class World:
    """The house as this test builds it: a registry, three boxes, and a station to play."""

    registry: FakeRegistry
    studio: FakeSpeaker
    hallway: FakeSpeaker
    console: FakeSpeaker
    station_url: str
    notify_port: int
    fetches: list[str]
    """One entry per HTTP request that reached the station. A stream nobody hears still costs a
    fetch, so counting them is how a test sees the master start the same channel twice."""
    fetched_at: list[float]
    """``time.monotonic()`` per entry above, on the clock ``mpdfake`` stamps itself from.

    An MPD channel has to be LOADED before the master is pointed at the stream, and no count of
    either side can say which happened first."""


async def _station(
    payload: bytes,
    *,
    first_byte_delay: float = 0.0,
    fetches: list[str] | None = None,
    fetched_at: list[float] | None = None,
) -> tuple[asyncio.AbstractServer, str]:
    """A station on loopback that keeps sending, the way somebody else's server would.

    ``first_byte_delay`` holds the BODY back while the headers go out, which is what a slow station
    really looks like and the only way to keep the service inside its wait for the first bytes.
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        if fetches is not None:
            fetches.append(head.split(b"\r\n", 1)[0].decode("utf-8", "replace"))
        if fetched_at is not None:
            fetched_at.append(time.monotonic())
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\n\r\n")
        await writer.drain()
        if first_byte_delay:
            await asyncio.sleep(first_byte_delay)
        # The master dropping the connection mid-write is what a live station sees every time a
        # zone ends, and drain() raises ConnectionResetError for it. Unsuppressed, asyncio logs
        # every teardown as "Unhandled exception in client_connected_cb", which is noise standing
        # in the report of whatever test happens to fail next.
        with contextlib.suppress(OSError):
            for start in range(0, len(payload), 4096):
                writer.write(payload[start : start + 4096])
                await writer.drain()
                await asyncio.sleep(0.005)
        # A live station does not end, so this holds the connection open - but it holds it only
        # while somebody is LISTENING. read() returns b"" at EOF, which is the master closing.
        # It used to be a flat 30 s sleep, and that outlived the test: pytest-asyncio's loop
        # finalizer waits for pending tasks, so a handler still sleeping made the whole suite hang
        # for up to half a minute per test, intermittently, depending on whether the master's
        # disconnect happened to raise inside drain() first (measured 2026-09-07).
        with contextlib.suppress(OSError):
            await reader.read()
        writer.close()

    server = await asyncio.start_server(handle, MASTER, 0)
    return server, f"http://{MASTER}:{server.sockets[0].getsockname()[1]}/live"


@pytest.fixture
async def world() -> AsyncIterator[World]:
    notify_port = free_port()
    boxes = {
        "studio": FakeSpeaker({}, host=STUDIO_IP, device_id=STUDIO_ID, notify_port=notify_port),
        "hallway": FakeSpeaker({}, host=HALLWAY_IP, device_id=HALLWAY_ID, notify_port=notify_port),
        "console": FakeSpeaker({}, host=CONSOLE_IP, device_id=CONSOLE_ID, notify_port=notify_port),
    }
    registry = FakeRegistry(devices_at({STUDIO_ID: STUDIO_IP, HALLWAY_ID: HALLWAY_IP, CONSOLE_ID: CONSOLE_IP}))
    fetches: list[str] = []
    fetched_at: list[float] = []
    station, url = await _station(os.urandom(400_000), fetches=fetches, fetched_at=fetched_at)
    await registry.start()
    for box in boxes.values():
        await box.start()
    try:
        yield World(
            registry=registry,
            station_url=url,
            notify_port=notify_port,
            fetches=fetches,
            fetched_at=fetched_at,
            **boxes,
        )
    finally:
        for box in boxes.values():
            await box.stop()
        await registry.stop()
        station.close()


def _options(
    world: World,
    tmp_path: Path,
    *,
    station_url: str | None = None,
    unreachable_timeout_s: float | None = None,
    seed: bool = False,
    dial_window_s: float = WINDOW_DEFAULT_S,
) -> ServiceOptions:
    """The real record, with only the waits shortened; nothing else is substituted.

    ``station_url`` is no longer an option of the service - M2 gave it a channel list - so it is
    written into the channel file here as channel 1, which is exactly what the seeding would
    otherwise have put there. Pass ``seed=True`` to leave the file absent and let the service seed
    itself from the named speaker, which only the seeding tests want.
    """
    channel_file = tmp_path / "channels.json"
    if not seed:
        save_channels(
            channel_file,
            ChannelList(
                channels=(
                    Channel(
                        number="1",
                        name="Channel 1",
                        kind=ChannelKind.RADIO,
                        url=station_url if station_url is not None else world.station_url,
                    ),
                )
            ),
        )
    return ServiceOptions(
        bind_ip=MASTER,
        device_id=MASTER_ID,
        registry_url=world.registry.base_url,
        switch_file=tmp_path / "zone.switch",
        state_file=tmp_path / "zone-state.json",
        channel_file=channel_file,
        dial_window_s=dial_window_s,
        registry_poll_s=0.2,
        switch_poll_s=0.05,
        unreachable_timeout_s=unreachable_timeout_s if unreachable_timeout_s is not None else UNREACHABLE_TIMEOUT_S,
        channel_policy=ChannelPolicy(port=world.notify_port, backoff_s=(0.2,)),
    )


def _preset(url: str, name: str) -> str:
    """One preset as a speaker stores it: the ContentItem the master already knows how to read."""
    return station_content_item(url=url, name=name)


def _channels_of(options: ServiceOptions) -> ChannelList:
    """The channel file as the service left it, read the way a restart reads it."""
    return load_channels(options.channel_file, log=lambda _kind, _text: None)


@asynccontextmanager
async def _running(options: ServiceOptions, logs: list[str]) -> AsyncGenerator[ZoneService, None]:
    """The service as the unit runs it, ended the way SIGINT ends it: cancelled, then cleaned up."""
    service = ZoneService(
        options,
        log=lambda kind, text: logs.append(f"{kind}: {text}"),
        ports=build_production().zone_ports,
    )
    task = asyncio.create_task(service.run())
    try:
        yield service
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def the_master(service: ZoneService) -> ZoneMaster:
    """The real master the production ports built, named as what it is.

    ``service.master`` is typed as ``ZoneMasterPort``, which is everything the SERVICE does
    to a zone and no more. The two assertions that use this read the adapter's own state,
    which only an end-to-end file wired with the production ports may do.
    """
    master = service.master
    assert isinstance(master, ZoneMaster)
    return master


async def eventually(check: Callable[[], bool], what: str, *, timeout: float = 10.0) -> None:
    """Wait for something the service does on its own, or fail naming what never happened."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not check():
        assert loop.time() < deadline, f"timed out waiting for: {what}"
        await asyncio.sleep(0.02)


async def nothing_answers_on(host: str, port: int, *, timeout: float = 10.0) -> None:
    """Wait for a port to stop answering, rather than reading it the moment something else changed.

    A connection attempt is a coroutine, so it cannot be a predicate for :func:`eventually`, and
    the version of this that read the port ONCE was a race: the service clears ``self.master``
    before it stops the master, so the socket outlives the attribute and a run slow enough to
    notice connects to a server that is on its way out.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            _reader, writer = await asyncio.open_connection(host, port)
        except OSError:
            return
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        assert loop.time() < deadline, f"timed out waiting for: nothing answers on {host}:{port}"
        await asyncio.sleep(0.02)


def joins(box: FakeSpeaker) -> list[str]:
    """Every ``/setZone`` that took this box INTO a zone; a member list is what makes it a join."""
    return [body for body in box.bodies_for("/setZone") if "<member" in body]


def dissolves(box: FakeSpeaker) -> list[str]:
    """Every ``/setZone`` with no members: the zone is over, and a real box goes to standby."""
    return [body for body in box.bodies_for("/setZone") if "<member" not in body]


def believed(options: ServiceOptions) -> tuple[str, ...]:
    """Who the state file says the zone belongs to, read the way a restart reads it."""
    return load_state(options.state_file, log=lambda _kind, _text: None).members


def out_of_multiroom(options: ServiceOptions) -> tuple[str, ...]:
    """Which boxes the state file says a person switched out, read the way a restart reads it."""
    return load_state(options.state_file, log=lambda _kind, _text: None).out_of_multiroom


def _slaves(service: ZoneService) -> tuple[str, ...]:
    """The addresses the zone holds right now, and nothing at all while there is no zone."""
    return tuple(service.master.slaves) if service.master is not None else ()


async def _both_wake(world: World) -> None:
    """Both boxes leave standby, which is the design's POWER-on rule, and the zone takes them."""
    await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
    await eventually(lambda: len(joins(world.studio)) == 1, "the studio was taken into the zone")
    await world.hallway.notify(now_playing_frame(device_id=HALLWAY_ID, source=RADIO))
    await eventually(lambda: len(joins(world.hallway)) == 1, "the hallway was taken into the zone")


async def test_a_box_joins_when_it_wakes_and_is_let_go_untouched_when_somebody_takes_it(
    world: World, tmp_path: Path
) -> None:
    """The whole membership rule, from the outside: who is taken, who is not, and who is left alone."""
    options = _options(world, tmp_path)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
        await eventually(lambda: len(joins(world.studio)) == 1, "the studio was taken into the zone")

        assert joins(world.hallway) == [], "a box that has said nothing is not taken into the zone"
        assert service.master is not None and service.master.station is not None, "the zone is playing something"
        assert believed(options) == (STUDIO_ID,)

        await world.hallway.notify(now_playing_frame(device_id=HALLWAY_ID, source=RADIO))
        await eventually(lambda: len(joins(world.hallway)) == 1, "the hallway was taken into the zone")
        assert "<member" in joins(world.hallway)[-1]
        assert believed(options) == (STUDIO_ID, HALLWAY_ID)

        # --- somebody switches the hallway to its aux input --------------------------------------
        untouched = len(world.hallway.requests)
        await world.hallway.notify(now_playing_frame(device_id=HALLWAY_ID, source=AUX))
        await eventually(lambda: believed(options) == (STUDIO_ID,), "the hallway left the zone")
        assert len(world.hallway.requests) == untouched, "a box somebody is listening to is not sent anything"
        assert dissolves(world.hallway) == [], "and it is certainly never told the zone is over"

        # --- and it comes back the way it left, by being switched off and on again ----------------
        await world.hallway.notify(now_playing_frame(device_id=HALLWAY_ID, source=SourceName.STANDBY))
        await world.hallway.notify(now_playing_frame(device_id=HALLWAY_ID, source=RADIO))
        await eventually(lambda: len(joins(world.hallway)) == 2, "the hallway was taken back into the zone")
        assert believed(options) == (STUDIO_ID, HALLWAY_ID)

        # The studio played through all of it - a box leaving AND that box coming back - and was
        # handed exactly one zone document, its own, at its own join. Asserted here rather than
        # right after the leave because a negative needs a later anchor than the thing it denies:
        # the pass writes the state file before it would send a document, so waiting on the file
        # and then reading the count would pass even if the document were still on its way. The
        # hallway's second join is that later anchor. A slave stops and re-buffers for about four
        # seconds on any /setZone, so a second entry here is four seconds of silence in a room
        # nobody touched (measured on the hardware 2026-09-08; master.slave_left).
        assert len(joins(world.studio)) == 1, "the box that stayed must never be handed a second zone document"

        assert world.console.requests == [], "the Lifestyle console is never called"
        assert world.console.connections == 0, "and nothing even watches it"


async def test_a_box_that_wakes_and_dials_is_taken_in_on_the_number_it_dialled(world: World, tmp_path: Path) -> None:
    """Rank 16 from the outside, and the reason the wake is marked when the NUMBER is complete.

    The box presses and never leaves standby, which is what a real one does for 0.17 to 4.45 s
    after somebody switches it on (three wakes, 2026-09-08). It is taken in on the press: waiting
    for its own ``nowPlayingUpdated`` is waiting for it to start its OWN station, which is exactly
    the sound the person did not ask for - measured at 03:00, a box pressed at 03:00:42.99 was not
    a member until 03:00:46.472 and did not join until 03:00:47.480.

    What the channel assertion is for: the mark sits at the dial completion rather than at the
    press, so the house starts on the number that was dialled. Marked at the press, a pass would
    take the box in while the number was still being typed and start the REMEMBERED channel, and
    the dial completing would move every box to the dialled one - a stop and a re-buffer of 3.5 to
    4 s in each room (``master.slave_left``).

    The count is of what the STATION saw, because a log line cannot show a stream that was started
    twice. One stream costs two requests here, a peek for the content type and the fetch itself
    (``source._peek``).
    """
    options = _dialable_world(world, tmp_path, dial_window_s=0.5)
    save_state(options.state_file, ZoneState(channel="1", members=()))
    logs: list[str] = []

    async with _running(options, logs) as service:
        await eventually(lambda: service.master is not None, "the switch is on and the master is up")
        await _press_preset(world.studio, STUDIO_ID, 1)
        await _press_preset(world.studio, STUDIO_ID, 2)
        await eventually(lambda: len(joins(world.studio)) == 1, "the box that dialled was taken into the zone")
        # A deliberate bare sleep, not a wait for an event: it establishes that nothing further is
        # pending, which is what makes the count below mean something.
        await asyncio.sleep(0.5)

        assert _playing(service).endswith("?c=12"), "the house started on the remembered channel, not the dialled one"
        assert len(world.fetches) == 2, f"one stream is one peek and one fetch, got {world.fetches}"
        assert believed(options) == (STUDIO_ID,)


async def test_a_second_box_pressed_while_the_house_plays_is_taken_in_on_the_press(
    world: World, tmp_path: Path
) -> None:
    """The other half of rank 16: the house is already playing, so the press is not a choice.

    ``_may_choose_the_channel`` sends this box down the joining branch rather than the dialling
    one - a POWER-on in one room must not move the others to whatever that room last had - and
    that branch is where the press marks the wake. The box says nothing else at all, which is a
    real box for the 0.17 to 4.45 s before its first ``nowPlayingUpdated``.

    It must also not restart the stream the other box is listening to: a ``/setZone`` or a new
    station costs a playing slave 3.5 to 4 s of silence (``master.slave_left``, 2026-09-08).
    """
    options = _options(world, tmp_path)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
        await eventually(lambda: len(joins(world.studio)) == 1, "the studio was taken into the zone")
        await eventually(lambda: _station_url(service) is not None, "the house is playing")
        playing, fetched = _playing(service), len(world.fetches)

        await _press_preset(world.hallway, HALLWAY_ID, 1)

        await eventually(lambda: len(joins(world.hallway)) == 1, "the box that was pressed was taken into the zone")
        assert believed(options) == (STUDIO_ID, HALLWAY_ID)
        # A deliberate bare sleep, not a wait for an event: it establishes that nothing further is
        # pending, which is what makes the two unchanged readings below mean something.
        await asyncio.sleep(0.5)
        assert _playing(service) == playing, "the zone left the channel it was on"
        assert len(world.fetches) == fetched, "the station the other room was listening to was fetched again"
        assert len(joins(world.studio)) == 1, "the box that stayed must never be handed a second zone document"


async def test_a_box_arriving_while_a_number_is_still_open_waits_for_the_number(world: World, tmp_path: Path) -> None:
    """One press names one channel, so the house must fetch one station and not two.

    Measured in the fifth live run, 2026-09-08 (OPEN-WORK rank 31). Room2 was pressed, named its
    own source 0.19 s later and so became a member the ORDINARY way, and the pass that took it in
    started the REMEMBERED channel at 03:39:14.902. Its number completed 0.4 s after that naming a
    different one, and ``_dialled_number`` then had to wait for the lock the pass was holding - by
    the time it got it the zone was no longer empty, so it played the dialled station and switched
    the box over at 03:39:18.193. Two stations fetched, and every room already playing pays a stop
    and a re-buffer of 3.5 to 4 s for it.

    The box here reports its own source between its two digits, which is what makes it a member
    while the window is still open. It is the ordering that fixes this and not another guard: while
    a number is in flight the channel is not decided yet, so there is nothing correct to start.

    The count is of what the STATION saw. One stream costs two requests here, a peek for the content
    type and the fetch itself (``source._peek``), so four is two stations.
    """
    options = _dialable_world(world, tmp_path, dial_window_s=1.0)
    save_state(options.state_file, ZoneState(channel="1", members=()))
    logs: list[str] = []

    async with _running(options, logs) as service:
        await eventually(lambda: service.master is not None, "the switch is on and the master is up")
        await _press_preset(world.studio, STUDIO_ID, 1)
        await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
        # Waited on rather than slept through, and it is the state file because that is written at
        # the TOP of a pass, before anything would be started: reaching it proves a pass has seen
        # the box as a member, which is the moment the old code started the remembered channel.
        await eventually(lambda: believed(options) == (STUDIO_ID,), "a pass has seen the box as a member")

        await _press_preset(world.studio, STUDIO_ID, 2)
        # The number FIRST and the join after it. Waiting on the join alone would be satisfied
        # before the window had even closed - which is exactly the defect - and the count would
        # then be read while the second fetch was still on its way.
        await eventually(lambda: [line for line in logs if "dialled 12" in line] != [], "the number completed")
        await eventually(lambda: len(joins(world.studio)) == 1, "the box was taken into the zone")
        # A deliberate bare sleep, not a wait for an event: it establishes that nothing further is
        # pending, which is what makes the count below mean something.
        await asyncio.sleep(0.5)

        assert _playing(service).endswith("?c=12"), "the zone is not on the number that was dialled"
        assert len(world.fetches) == 2, f"one press named one channel and fetched {world.fetches}"


async def test_a_dial_from_an_awake_box_starts_exactly_one_stream(world: World, tmp_path: Path) -> None:
    """The anti-churn rule, re-recorded after rank 34 changed who gets taken in (user, 2026-09-20).

    What this test has always really been about is CHURN, and that part is unchanged and still
    asserted: measured in the flat 2026-09-08 at 02:09 with two boxes switched on three seconds
    apart, a wake dialled and started the station before any pass had taken it in, the next pass
    saw an empty zone and stopped it again, and one station was fetched three times in 3.7 s. The
    damage was what the churn enabled - ``_may_choose_the_channel`` lets a WAKING box pick the
    channel only while ``master.station is None``, so every stop reopened that door and a box
    already in the zone was stopped on one stream and re-buffered onto the next.

    What CHANGED is the membership half. This used to assert that an awake box which dials is left
    out of the zone entirely, on the reasoning that it is choosing a channel for a house it is not
    part of. Rank 34 measured what that costs: the box is already playing that station on its own
    by the time the dial reaches us, so leaving it out does not spare it anything - it just puts
    that room seconds out of step with every other one. It is now taken in.

    The zone is therefore no longer empty when the station starts, so a fetch here is correct. The
    count is what proves no churn came back: ONE stream is one peek for the content type and one
    fetch (``source._peek``), so two requests and no more.
    """
    options = _dialable_world(world, tmp_path, dial_window_s=0.5)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await eventually(lambda: service.master is not None, "the switch is on and the master is up")
        await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=AUX))
        await _press_preset(world.studio, STUDIO_ID, 1)
        await eventually(lambda: [line for line in logs if "dialled 1" in line] != [], "the number was dialled")
        await eventually(lambda: joins(world.studio) != [], "the box that dialled was taken into the zone")
        # A deliberate bare sleep, not a wait for an event: it establishes that nothing further is
        # pending, which is what makes the COUNT below mean something. The pass loop is
        # event-driven, so there is no interval to wait out - only a quiet window.
        await asyncio.sleep(0.5)

        assert len(world.fetches) == 2, f"one stream is one peek and one fetch, got {world.fetches}"
        assert believed(options) == (STUDIO_ID,)


async def test_an_awake_box_that_dials_is_taken_into_the_zone(world: World, tmp_path: Path) -> None:
    """Rank 34. A box already awake that picks a channel joins the house rather than playing alone.

    Measured in the flat 2026-09-20 with a finger on the speaker, twice. At 02:13 the zone was
    empty and an awake box dialled 2: nothing started and nobody was in. At 02:21, with one box
    already in the zone, the same awake box dialled 3 - the HOUSE moved to OE3, the box in the
    zone followed cleanly, and the box that was PRESSED stayed outside on its own copy of the
    station. That is the ordinary gesture, pressing the station in the room you are standing in,
    and it put that room seconds out of step with the next one.

    The dial reaches us from the box's own ``nowSelectionUpdated``, so by then it IS playing that
    preset. The choice is therefore never "play or stay silent" - it is "in sync with the house,
    or alone and adrift", and the second is what a listener hears as an echo between rooms.

    This does not reopen the churn the empty-zone rule was written for (one station fetched three
    times in 3.7 s, 2026-09-08). The box is marked a member BEFORE the pass runs, so the pass
    takes it in and starts the station once, instead of a station being started into an empty
    zone and stopped again.
    """
    options = _dialable_world(world, tmp_path, dial_window_s=0.5)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await eventually(lambda: service.master is not None, "the switch is on and the master is up")
        # AUX first, then its own radio. The AUX frame is what makes this box AWAKE rather than
        # WAKING: observe() clears the wake on any source that is not internet radio, and only a
        # standby-to-radio transition sets one. Going straight to RADIO from the start-up probe's
        # STANDBY would mark a wake, and the box would then join because it woke - which is the
        # case the test above already covers, and would make this one pass without the fix.
        await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=AUX))
        await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
        await eventually(lambda: not service.policy.is_asleep(STUDIO_ID), "the box is awake, not in standby")
        assert joins(world.studio) == [], "precondition: an awake box on its own station is not in the zone yet"

        await _press_preset(world.studio, STUDIO_ID, 1)

        await eventually(lambda: joins(world.studio) != [], "the awake box that dialled was taken into the zone")
        assert believed(options) == (STUDIO_ID,)


async def test_a_wake_whose_number_names_the_channel_already_playing_starts_no_second_stream(
    world: World, tmp_path: Path
) -> None:
    """The wake and the pass race, and the wake must lose gracefully rather than re-start the house.

    A box coming out of standby says two things: the preset it woke on, and that it has left
    standby. The second makes a pass take it in, and that pass starts the channel; the first is a
    dialled number that completes a dial window LATER and used to call ``play()`` again, for the
    station already playing. Every start costs each box in the zone a STOP and a re-buffer - 3.5 to
    4 s, measured on the hardware 2026-09-08 - so the second start is silence in every room in
    exchange for nothing, and in the flat at 02:09 it was the middle link of a chain that fetched
    one station three times.

    The count is of what the STATION saw, because that is what cannot be satisfied by a log line.
    One stream costs two requests here, a peek for the content type and the fetch itself
    (``source._peek``), so the assertion is that the number does not MOVE, not what it is.
    """
    options = _options(world, tmp_path)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _wake_on_preset(world.studio, STUDIO_ID, 1)
        await eventually(lambda: len(joins(world.studio)) == 1, "the studio was taken into the zone")
        await eventually(lambda: _station_url(service) is not None, "the house is playing")
        playing, before = _playing(service), len(world.fetches)

        await eventually(
            lambda: [line for line in logs if "dialled 1" in line] != [], "the wake's own number completed"
        )
        # A deliberate bare sleep, not a wait for an event: it establishes that nothing further is
        # pending, which is what makes an unchanged count below mean something.
        await asyncio.sleep(0.5)

        assert len(world.fetches) == before, "the channel already playing was fetched a second time"
        assert _playing(service) == playing, "and the zone is on the same stream it was on"


async def test_dialling_the_channel_the_zone_is_already_on_starts_no_second_stream(
    world: World, tmp_path: Path
) -> None:
    """The same refusal as the one above, but reached from the other side, which is the side a
    person uses: a box that is ALREADY a slave presses the preset it is already listening to.

    The one above never gets here. Its dial completes before the pass has taken the box in, so the
    zone is still empty and an earlier branch answers - which left the branch that says "already
    playing it" with nothing holding it at all, and that is how it came to compare the wrong thing
    for a fortnight (see the MPD test further down).

    The count is of what the STATION saw, for the reason the test above gives: a log line cannot
    say whether a stream was fetched again, and one start costs every room 3.5 to 4 s of silence.
    """
    options = _options(world, tmp_path, seed=True, dial_window_s=0.5)
    save_channels(
        options.channel_file,
        ChannelList(
            channels=(
                Channel(number="1", name="One", kind=ChannelKind.RADIO, url=f"{world.station_url}?c=1"),
                Channel(number="2", name="Two", kind=ChannelKind.RADIO, url=f"{world.station_url}?c=2"),
            )
        ),
    )
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        await _press_preset(world.studio, STUDIO_ID, 2)
        await eventually(lambda: _playing(service).endswith("?c=2"), "the zone moved to channel 2")
        fetched = len(world.fetches)

        await _press_preset(world.studio, STUDIO_ID, 2)
        await eventually(lambda: any("already playing it" in line for line in logs), "it said the channel was on")
        # A deliberate bare sleep, not a wait for an event: it establishes that nothing further is
        # pending, which is what makes an unchanged count below mean something.
        await asyncio.sleep(0.5)

        assert len(world.fetches) == fetched, "the channel already playing was fetched a second time"
        assert _playing(service).endswith("?c=2"), "and the zone is on the same stream it was on"


def _volume_writes(box: FakeSpeaker) -> list[int]:
    """Every level this box was set to, in order, read off what actually arrived at it."""
    return box.volumes


async def test_a_box_is_at_zero_when_the_zone_document_reaches_it(world: World, tmp_path: Path) -> None:
    """The whole point of the mute, and the ORDER is the assertion.

    A box that has just been switched on plays its own last station until the zone takes it over -
    4.7 s of it, measured in the flat on 2026-09-08 and heard by the user. Turning it down AFTER the
    join would hide nothing, so the test does not ask whether the volume was touched but WHEN: the
    zero has to arrive before the document, and the box has to end back where it was.
    """
    options = _options(world, tmp_path)
    world.studio.volume = 27

    async with _running(options, []):
        await _wake_on_preset(world.studio, STUDIO_ID, 1)
        await eventually(lambda: len(joins(world.studio)) == 1, "the studio was taken into the zone")

        paths = [(method, path, body) for method, path, body in world.studio.requests if method == "POST"]
        muted_at = next(i for i, (_m, path, body) in enumerate(paths) if path == "/volume" and "<volume>0<" in body)
        joined_at = next(i for i, (_m, path, _b) in enumerate(paths) if path == "/setZone")
        assert muted_at < joined_at, f"the box heard its own station: {paths}"

        await eventually(lambda: world.studio.volume == 27, "the box was put back where it was")
        assert _volume_writes(world.studio)[0] == 0
        assert _volume_writes(world.studio)[-1] == 27
        assert len(_volume_writes(world.studio)) > 2, "and it climbed rather than jumping"


async def test_a_join_that_finished_is_never_reported_as_one_that_did_not(world: World, tmp_path: Path) -> None:
    """The rescue is the only signal a service ever died mid-join, so it must not fire on a join.

    ``_put_back_any_volume_we_took_away`` covers the two ways a mute can outlive its join, and the
    line it logs is the only place either is ever said out loud. A pass that reaches it while a
    fade is still putting its box back turns the box up itself and says so - about a join that
    finished - and from then on the journal reports a service that died where nothing died. That
    line is what made the eighth listening run of 2026-09-08 first read as "the fade never ran at
    all"; the journal showed all seven of its steps.

    The sound is what hides it. By then the climb has made every step but the last, so the rescue
    writes exactly the level the fade was about to write, which is why the assertion is on the LINE
    and not on the levels: ``len(writes) > 2`` is satisfied by seven steps and a rescue as readily
    as by a fade that finished its own.

    The box is slowed so the wait the fade opens is one a pass can land in. Nothing here arranges a
    pass; what is arranged is that the ordinary ones - the registry poll's, every
    ``registry_poll_s`` - have somewhere to land.
    """
    options = _options(world, tmp_path)
    logs: list[str] = []
    slow_s = 0.3
    world.studio.volume = 27
    world.studio.slow = {"POST /volume": slow_s}

    async with _running(options, logs):
        await _wake_on_preset(world.studio, STUDIO_ID, 1)
        await eventually(lambda: len(joins(world.studio)) == 1, "the studio was taken into the zone")
        # The note coming out is the last thing the fade does, and no sweep that STARTS after it
        # can say anything - but one that started before it is still inside its own slow POST and
        # logs when that returns. So the wait is the note, and then longer than one of those.
        await eventually(
            lambda: load_state(options.state_file, log=lambda _kind, _text: None).muted == {},
            "the box was put back and its note came out",
            timeout=20.0,
        )
        await asyncio.sleep(slow_s * 3)

        # The fade's own steps, which the rescue never writes: without them the box was never
        # climbed and the absence below would prove nothing.
        climbed = [level for level in _volume_writes(world.studio) if 0 < level < 27]
        assert climbed == [3, 7, 10, 14, 17, 20, 24], f"the fade did not run: {_volume_writes(world.studio)}"
        assert _volume_writes(world.studio)[-1] == 27
        rescued = [line for line in logs if "after a join that did not finish" in line]
        assert rescued == [], f"a join that finished was reported as one that did not: {rescued}"


async def test_a_box_that_will_not_say_its_volume_is_joined_at_its_own(world: World, tmp_path: Path) -> None:
    """A level that cannot be read is a level that cannot be put back, so the box is not touched.

    The alternative - mute anyway and hope - risks the one failure this feature can cause, which is
    a speaker left silently at zero. That reads as broken hardware, and it is worse than the few
    seconds of its own station that the mute exists to hide.
    """
    options = _options(world, tmp_path)
    world.studio.refuse = frozenset({"/volume"})

    async with _running(options, []):
        await _wake_on_preset(world.studio, STUDIO_ID, 1)
        await eventually(lambda: len(joins(world.studio)) == 1, "the studio was taken into the zone anyway")
        assert _volume_writes(world.studio) == [], "a box whose volume could not be read must not be moved"


async def test_a_box_that_cannot_be_turned_down_keeps_no_note_that_would_move_it_later(
    world: World, tmp_path: Path
) -> None:
    """The other half of the mute's failure, and the one where a leftover note does harm.

    A level that cannot be READ leaves nothing behind (tested above). A level read fine and then
    NOT taken away is different: the note is written before the box is muted, so a service that
    stops here has recorded a level it never took. A later pass would then "put back" a box that
    was never turned down, moving a speaker somebody had set by hand.
    """
    options = _options(world, tmp_path)
    logs: list[str] = []
    world.studio.volume = 19
    world.studio.refuse = frozenset({"POST /volume"})  # answers the read, drops the write

    async with _running(options, logs):
        await _wake_on_preset(world.studio, STUDIO_ID, 1)
        await eventually(lambda: [line for line in logs if "could not be turned down" in line] != [], "the mute failed")
        await eventually(lambda: len(joins(world.studio)) == 1, "and the box was taken in anyway")
        assert _volume_writes(world.studio) == [], "a box that dropped the write was never moved"
        muted = load_state(options.state_file, log=lambda _kind, _text: None).muted
        assert STUDIO_ID not in muted, f"the note must come back out, or a later pass moves the box: {muted}"


async def test_a_box_that_stops_answering_mid_fade_is_reported_and_left_to_the_next_pass(
    world: World, tmp_path: Path
) -> None:
    """The climb is the one call to a speaker this module used to make unguarded.

    Every other one logs and lets the next pass be the retry; this one raised out of a task nobody
    retrieves - its ``finally`` has already taken it out of ``_fading``, so not even the stand-down
    waits on it - and asyncio printed a traceback of its own to a logger that is not the service's.
    A traceback in the journal is where somebody looking for a real fault starts reading, and the
    fault here is a radio that went quiet for a moment.

    The mute note has to survive: it is the only thing that brings the box back up on a later pass.
    """
    options = _options(world, tmp_path)
    logs: list[str] = []
    world.studio.volume = 24

    async with _running(options, logs):
        await _wake_on_preset(world.studio, STUDIO_ID, 1)
        await eventually(lambda: _volume_writes(world.studio) == [0], "the box was muted for its join")
        # Inside MUTE_HOLD_S, so the box goes quiet before the climb's first step rather than
        # during the read that decides the level - which is a different, already-tested path.
        world.studio.refuse = frozenset({"/volume"})

        await eventually(lambda: [line for line in logs if "fade stopped at step" in line] != [], "the fade gave up")
        # Read from the state FILE rather than the service's own map: that file is what a restart
        # has, so it is the thing that actually has to still name the box and its level.
        muted = load_state(options.state_file, log=lambda _kind, _text: None).muted
        assert muted.get(STUDIO_ID) == 24, f"the note must survive, or nothing ever puts the box back: {muted}"


async def test_a_box_that_refuses_to_join_is_turned_straight_back_up(world: World, tmp_path: Path) -> None:
    """It is still playing its own station, so every step of a fade is a step somebody is missing.

    This is also the path where leaving the mute in place would be worst: the box was never taken
    over, so nothing else would ever give it its volume back.
    """
    options = _options(world, tmp_path)
    logs: list[str] = []
    world.studio.volume = 31
    world.studio.refuse = frozenset({"/setZone"})

    async with _running(options, logs):
        await _wake_on_preset(world.studio, STUDIO_ID, 1)
        # On the refusal, not on the volume: the box is ALREADY at 31, so a wait for that would be
        # satisfied before anything had happened and would assert nothing.
        await eventually(lambda: [line for line in logs if "did not join" in line] != [], "the box refused")
        await eventually(lambda: _volume_writes(world.studio) == [0, 31], "it was muted and put straight back")
        assert world.studio.volume == 31
        # Not `joins(...) == []`: the double records a request before it refuses it, so the document
        # is in its list although the box never answered. What decides is whether the SERVICE took
        # it in.
        assert [line for line in logs if "joined;" in line] == []


def test_a_refused_join_is_tried_again_while_the_box_still_counts_as_awake() -> None:
    """Three numbers in three layers that are one rule, and nothing connected them.

    A box that refuses is left alone for ``JOIN_RETRY_S``; a box that was switched on counts as
    belonging for ``WAKE_WINDOW_S`` after the press, and the refusal itself can take a whole
    ``SPEAKER_HTTP_TIMEOUT_S`` to arrive. Set so that the first two are equal, the retry window
    always opened at or after the wake had expired: the box had stopped belonging, so nothing
    asked for it again and no line said so. One transient failure and the room was out until
    somebody switched the box off and on.

    The rule cannot be written where any one of the three lives - they are a domain window, an
    application interval and an adapter's timeout - so it is written here, where all three are in
    scope. It bounds what the numbers can promise rather than what they can do: a refusal that
    takes longer than one nominal timeout still loses the window, and no value of the interval
    changes that.
    """
    assert JOIN_RETRY_S + SPEAKER_HTTP_TIMEOUT_S < WAKE_WINDOW_S


async def test_a_box_that_refused_once_is_taken_in_without_being_switched_on_again(
    world: World, tmp_path: Path
) -> None:
    """The retry has to happen by itself, on a box that has done nothing since it failed.

    A refusal is the ordinary case - the box is still starting up, the network blinked - and the
    only thing that used to clear it was a second wake, which means a person walking over to the
    speaker. Nothing here presses anything after the refusal.
    """
    options = _options(world, tmp_path)
    logs: list[str] = []
    world.studio.refuse = frozenset({"/setZone"})

    async with _running(options, logs):
        await _wake_on_preset(world.studio, STUDIO_ID, 1)
        await eventually(lambda: [line for line in logs if "did not join" in line] != [], "the box refused once")
        world.studio.refuse = frozenset()

        await eventually(
            lambda: [line for line in logs if "joined;" in line] != [],
            "it was tried again and taken in",
            timeout=JOIN_RETRY_S + 10.0,
        )


async def test_a_service_that_died_mid_join_puts_the_volume_back_when_it_starts(world: World, tmp_path: Path) -> None:
    """The failure this feature can cause, and the thing that undoes it.

    The level is written to the state file BEFORE the box is muted, so a service killed in between
    still finds it. Nothing else would: the box itself reports zero, which is a level rather than a
    complaint, and a person walking up to a silent speaker has no way to tell the difference from
    broken hardware.
    """
    options = _options(world, tmp_path)
    save_state(options.state_file, ZoneState(muted={STUDIO_ID: 22}))
    world.studio.volume = 0

    async with _running(options, []):
        await eventually(lambda: world.studio.volume == 22, "the box was turned back up on start-up")
        await eventually(
            lambda: load_state(options.state_file, log=lambda _k, _t: None).muted == {},
            "and the note was cleared once it was there",
        )


async def test_a_box_left_at_zero_is_turned_back_up_even_when_the_switch_is_off(world: World, tmp_path: Path) -> None:
    """The start-up restore is the one that has to work, because it is the one that always runs.

    Its sibling call inside the pass is not reached in two ordinary situations: the switch off,
    which is how the deployed service sits most of the time, and the ports briefly busy - both
    return from the pass above it. So this is not the same test one step earlier. A box a killed
    service left at zero would otherwise wait for somebody to turn the house on before it got its
    volume back, and a silent speaker nobody can explain is the one failure this feature can cause.
    """
    options = _options(world, tmp_path)
    options.switch_file.write_text("off\n", encoding="utf-8")
    save_state(options.state_file, ZoneState(muted={STUDIO_ID: 22}))
    world.studio.volume = 0

    async with _running(options, []):
        await eventually(lambda: world.studio.volume == 22, "the box was turned back up with the house off")
        await eventually(
            lambda: load_state(options.state_file, log=lambda _k, _t: None).muted == {},
            "and the note was cleared once it was there",
        )


async def test_a_note_the_start_up_could_not_clear_is_cleared_by_a_pass_with_the_switch_off(
    world: World, tmp_path: Path
) -> None:
    """The start-up restore runs once, and a note that outlives it has only the pass left.

    The test above proves that one call, and it was the whole cover a switched-off house had: the
    pass gives up at the switch ABOVE its own sweep, so from the first pass onwards a note nothing
    cleared is a speaker sitting at zero until somebody switches the house on or restarts the
    service. A silent speaker nobody can explain is the one failure this feature can cause.

    The note is made to outlive the start-up by letting the box drop the write, which is what a box
    briefly off the air does; a fade whose own last step failed leaves exactly the same thing
    behind, a note in the state file with no fade running under it.

    What is asserted is the level the BOX is on, because that is what a person in the room hears.
    """
    options = _options(world, tmp_path)
    options.switch_file.write_text("off\n", encoding="utf-8")
    save_state(options.state_file, ZoneState(muted={STUDIO_ID: 22}))
    world.studio.volume = 0
    world.studio.refuse = frozenset({"POST /volume"})
    logs: list[str] = []

    async with _running(options, logs):
        await eventually(
            lambda: [line for line in logs if "volume still not put back" in line] != [],
            "the start-up restore tried and the box dropped it",
        )
        assert world.studio.volume == 0, "the box is where a service killed mid-join left it"
        world.studio.refuse = frozenset()

        await eventually(lambda: world.studio.volume == 22, "a pass turned the box back up with the house off")
        await eventually(
            lambda: load_state(options.state_file, log=lambda _k, _t: None).muted == {},
            "and the note came out",
        )


async def test_a_note_the_start_up_could_not_clear_is_cleared_by_a_pass_that_gives_up_on_a_busy_port(
    world: World, tmp_path: Path
) -> None:
    """The second way a pass returns above its own sweep, and it needs no switch at all.

    A listening port held by somebody else costs the pass and nothing more, which is the whole
    point of that branch - but it used to cost the volumes too, because it returns before the
    sweep as well. Here the house is ON and every pass gives up on the port, so the box would wait
    for a stranger's connection to end before it was audible again.

    The port is taken by a real socket, which is what took it in the flat on 2026-09-07.
    """
    options = _options(world, tmp_path)
    save_state(options.state_file, ZoneState(muted={STUDIO_ID: 22}))
    world.studio.volume = 0
    world.studio.refuse = frozenset({"POST /volume"})
    logs: list[str] = []
    held = socket.socket()
    # SO_REUSEADDR for the reason the sibling test names: a slave connection leaves 40002 in
    # TIME-WAIT for about a minute and a plain bind over that fails in setup, which is not this.
    held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    held.bind((MASTER, 40002))
    held.listen(1)
    try:
        async with _running(options, logs):
            await eventually(
                lambda: [line for line in logs if "volume still not put back" in line] != [],
                "the start-up restore tried and the box dropped it",
            )
            await eventually(lambda: any("a port is busy" in line for line in logs), "and the pass gave up on the port")
            assert world.studio.volume == 0, "the box is where a service killed mid-join left it"
            world.studio.refuse = frozenset()

            await eventually(lambda: world.studio.volume == 22, "a pass turned the box back up with the port held")
            await eventually(
                lambda: load_state(options.state_file, log=lambda _k, _t: None).muted == {},
                "and the note came out",
            )
            assert joins(world.studio) == [], "and no box was taken into a zone while the port was held"
    finally:
        with contextlib.suppress(OSError):
            held.close()


async def test_a_port_held_by_something_else_costs_one_pass_and_not_the_service(world: World, tmp_path: Path) -> None:
    """A busy port must not take the house down with it.

    Measured in the flat on 2026-09-07 22:07: switching the service on killed it three times with
    ``address already in use`` on 40002, because the protocol's fixed ports sit inside the kernel's
    ephemeral range and an outbound connection was holding one. Dying there costs the observers,
    the registry and everything the membership rule has learned, and the boxes get probed again on
    every restart - so the pass gives up and the next one tries again.

    The port is taken by a real socket, which is what took it in the flat.
    """
    # The registry poll is pushed out of the way so it is not what drives the recovery, and the
    # port is freed only after a quiet window, so nothing else can be.
    options = replace(_options(world, tmp_path), registry_poll_s=30.0)
    logs: list[str] = []
    held = socket.socket()
    # SO_REUSEADDR because the port is the protocol's own and the suite has just used it: a slave's
    # connection to 40002 leaves a TIME-WAIT socket whose LOCAL port is 40002 for about a minute,
    # and a plain bind over that raises EADDRINUSE (measured 2026-09-08). That failure lands in
    # setup, reads as "address already in use" from whatever was last changed, and is not the
    # condition this simulates. Holding a LIVE listener still refuses the master's own bind, which
    # asyncio also makes with SO_REUSEADDR - measured the same way.
    held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    held.bind((MASTER, 40002))
    held.listen(1)
    try:
        async with _running(options, logs):
            await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
            await eventually(lambda: any("a port is busy" in line for line in logs), "the busy port was reported")
            # Not `service.master is None`: a pass runs every 50 ms here and sets the attribute
            # before it binds, so it is transiently set while the short retry runs. What a person
            # observes is that no box was taken in.
            assert joins(world.studio) == [], "no box was taken into a zone while the port was held"

            # Now let the house go quiet. This is not waiting for anything to happen - it is
            # establishing that nothing is pending, which is what makes the join below attributable
            # to the self-retry and to nothing else. Without it this test proved less than it read
            # as proving: measured by mutation on 2026-09-08, freeing the port while a pass driven
            # by the studio's own wake frame was still inside its three bind attempts let THAT pass
            # take the zone, so removing the self-retry entirely left every assertion here green.
            # Instrumented in the same run, no waker fires during the window - the observers stay
            # connected, the doubles push nothing on connect, and the switch yields only on change.
            await asyncio.sleep(2 * PORTS_BUSY_RETRY_S)
            assert joins(world.studio) == [], "and still none while the port stayed held"
            assert len([line for line in logs if "a port is busy" in line]) == 1, "and it is said once, not per pass"

            # The service is still ALIVE and still watching: this is the half the crash destroyed.
            # Nobody asks for the pass that follows, so the pass that takes the zone is the one the
            # loop brought back by itself.
            held.close()
            await eventually(lambda: len(joins(world.studio)) == 1, "the studio was taken in once the port freed")
            assert believed(options) == (STUDIO_ID,)
            assert any("the ports are free again" in line for line in logs), "the recovery was not reported"
            busy_lines = [line for line in logs if "a port is busy" in line]
            assert len(busy_lines) == 1, f"the busy port was reported once per pass: {len(busy_lines)} lines"
    finally:
        with contextlib.suppress(OSError):
            held.close()


async def test_a_box_that_goes_off_the_air_keeps_its_place_while_the_others_go_on(world: World, tmp_path: Path) -> None:
    """Room2 drops off the radio for minutes at a time; that must cost nobody their music.

    Both halves are in the one assertion at the end: the studio is still a member although nothing
    can reach it, and the hallway's departure was noticed while it was unreachable - so one box
    failing is that box's failure and not the service's.
    """
    options = _options(world, tmp_path)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        assert service.master is not None

        await world.studio.stop()
        await eventually(
            lambda: any(line.startswith("observer") and STUDIO_ID in line for line in logs),
            "the observer says out loud that it lost the studio",
        )

        await world.hallway.notify(now_playing_frame(device_id=HALLWAY_ID, source=AUX))
        await eventually(lambda: believed(options) == (STUDIO_ID,), "the hallway left while the studio was down")


async def test_the_switch_dissolves_the_zone_and_a_restart_takes_back_only_what_is_still_ours(
    world: World, tmp_path: Path
) -> None:
    """The switch, and what a restart is allowed to assume about a house it was not watching.

    The state file is what a restart starts from, and it is deliberately not the whole answer: each
    remembered box is asked what it is playing before it is taken back. Here the studio was never
    told the zone ended and is still on our stream, while the hallway went to standby - so exactly
    one of them comes back, and the file is corrected to say so.
    """
    options = _options(world, tmp_path)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)

        options.switch_file.write_text("off\n", encoding="utf-8")
        await eventually(
            lambda: len(dissolves(world.studio)) == 1 and len(dissolves(world.hallway)) == 1,
            "both boxes were told the zone is over",
        )
        await eventually(lambda: service.master is None, "the master is gone, not merely idle")
        # _stand_down clears service.master BEFORE it stops the master, so the port outlives the
        # attribute; reading it once, right here, connected to a server on its way out.
        await nothing_answers_on(MASTER, 8090)
        assert believed(options) == (STUDIO_ID, HALLWAY_ID), "the switch says nothing about who the zone belongs to"

    # --- the house while the service is down ----------------------------------------------------
    world.studio.now_playing = now_playing_document(device_id=STUDIO_ID, source=RADIO, owner=MASTER_ID)
    world.hallway.now_playing = now_playing_document(device_id=HALLWAY_ID, source=SourceName.STANDBY)
    options.switch_file.write_text("on\n", encoding="utf-8")
    studio_joins, hallway_joins = len(joins(world.studio)), len(joins(world.hallway))

    async with _running(options, logs):
        await eventually(lambda: len(joins(world.studio)) > studio_joins, "the studio was taken back into the zone")
        await eventually(lambda: believed(options) == (STUDIO_ID,), "the file was corrected to what is true now")
        assert len(joins(world.hallway)) == hallway_joins, "a box that went to standby is not taken back"


async def test_the_last_box_leaving_stops_the_stream_nobody_is_listening_to(world: World, tmp_path: Path) -> None:
    """An empty zone must stop costing bandwidth, not keep a radio station running for nobody.

    The zone emptying is not the switch going off: the service stays up, keeps watching, and takes
    the boxes back the moment one of them wakes. What it stops is the fetch, which is the only part
    of an empty zone that costs anything.
    """
    options = _options(world, tmp_path)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        assert service.master is not None and service.master.station is not None

        await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=AUX))
        await world.hallway.notify(now_playing_frame(device_id=HALLWAY_ID, source=AUX))

        await eventually(lambda: believed(options) == (), "both boxes left the zone")
        await eventually(lambda: service.master is not None and service.master.station is None, "the stream stopped")


async def test_a_restart_keeps_the_member_it_cannot_ask(world: World, tmp_path: Path) -> None:
    """A box off the air when the service comes back is still ours until the membership times out.

    Nothing but the state file knows that. The box cannot answer, and the registry cannot tell a
    speaker that has dropped off the radio from one that is playing - so forgetting it here would
    mean a dead spot during a restart quietly costs somebody their music, which is the failure the
    fifteen-minute timeout exists to prevent.
    """
    options = _options(world, tmp_path)
    logs: list[str] = []

    async with _running(options, logs):
        await _both_wake(world)
        await eventually(lambda: believed(options) == (STUDIO_ID, HALLWAY_ID), "both boxes are in the zone")

    world.studio.now_playing = now_playing_document(device_id=STUDIO_ID, source=RADIO, owner=MASTER_ID)
    await world.hallway.stop()

    async with _running(options, logs) as service:
        await eventually(lambda: any("did not join" in line for line in logs), "the hallway was tried and could not")
        assert any("did not answer" in line for line in logs), "and it was asked what it is playing first"
        # The file already said this before the restart, so it is asserted together with what THIS
        # run derived: the hallway is still a member (it was tried) and only the studio is in the
        # zone (it answered). Either one alone could pass on bytes the previous run left behind.
        assert believed(options) == (STUDIO_ID, HALLWAY_ID), "the box that could not answer keeps its place"
        assert service.master is not None
        master = service.master
        # Waited for rather than read: the box that CANNOT answer fails its join in a millisecond
        # while the one that can is still being talked to, so the log line above says nothing about
        # whether the studio is in yet. Read straight after it, this asserted an empty zone about
        # one run in ten (measured 2026-09-07).
        await eventually(lambda: set(master.slaves) == {STUDIO_IP}, "only the box that answered ended up in the zone")


async def test_a_key_a_member_forwards_is_read_as_the_box_that_pressed_it(world: World, tmp_path: Path) -> None:
    """The other half of the one event stream, and the half that arrives with no id in it.

    A forwarded key reaches the master's own HTTP face carrying no device id anywhere - the body
    has none - so the box is known only by the address it connected from, and the registry is what
    turns that into a speaker. M0 measured that the preset NUMBER never comes this way and a key
    with no content never comes the other, so a service that could not name this speaker would
    have two half-streams to join later.
    """
    options = _options(world, tmp_path)
    logs: list[str] = []

    async with _running(options, logs):
        await _both_wake(world)

        body = key_body(KeyName.NEXT_TRACK, KeyState.PRESS)
        reader, writer = await asyncio.open_connection(MASTER, 8090, local_addr=(STUDIO_IP, 0))
        writer.write(f"POST /slaveMsg HTTP/1.1\r\nHost: x\r\nContent-Length: {len(body)}\r\n\r\n{body}".encode())
        assert b"<status>/slaveMsg</status>" in await asyncio.wait_for(reader.read(), 5.0)
        writer.close()

        await eventually(
            lambda: any(line.startswith("key") and "Bose Studio" in line for line in logs),
            "the press was read as the studio's, by the address it came from",
        )
        assert any(KeyName.NEXT_TRACK in line and KeyState.PRESS in line for line in logs if line.startswith("key"))


async def test_the_registry_going_quiet_does_not_empty_the_zone(world: World, tmp_path: Path) -> None:
    """AfterTouch restarting must cost nobody their music.

    The device list freezes rather than empties when it cannot be read: a speaker missing from a
    later read is a gap in ITS view of the house and not evidence that a box has gone, and
    membership has its own reachability timeout for the real thing. With AfterTouch down the house
    has no radio and no presets anyway, so the zone service is not the part that is broken.
    """
    options = _options(world, tmp_path)
    logs: list[str] = []

    async with _running(options, logs):
        await _both_wake(world)
        await world.registry.stop()

        await eventually(
            lambda: any("keeping the 2 speaker(s) already known" in line for line in logs),
            "the service said what it is keeping",
        )
        assert believed(options) == (STUDIO_ID, HALLWAY_ID), "a list that cannot be read drops nobody"


async def test_the_command_runs_the_service_the_unit_will_run(world: World, tmp_path: Path) -> None:
    """The seam every other test substitutes has to be the thing the systemd unit actually runs.

    Nothing else here goes through the command's own run function, so without this the wiring
    between the two - which options record, which log, which class - is the one piece of the
    service that reaches the flat unproven.
    """
    task = asyncio.create_task(hold_the_zone(_options(world, tmp_path)))
    try:
        await eventually(lambda: world.studio.connections >= 1, "the service the command built is watching")
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_a_box_that_changes_its_mind_while_the_station_starts_is_not_taken_after_all(
    world: World, tmp_path: Path
) -> None:
    """The window a station's first bytes open, and what would otherwise walk through it.

    Starting a station waits up to ten seconds for somebody else's server to answer, and the house
    can change its mind inside that wait. A box that goes to AUX in those seconds and is taken in
    anyway is dropped again on the next pass WITHOUT being sent anything - which is right for a box
    that left by itself and leaves THIS one playing our stream for good, with the aux input somebody
    just selected gone. So the decision is asked again after the wait, on what the house says now.
    """
    slow, slow_url = await _station(os.urandom(200_000), first_byte_delay=1.5)
    options = _options(world, tmp_path, station_url=slow_url)
    logs: list[str] = []

    try:
        async with _running(options, logs) as service:
            await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
            await eventually(
                lambda: service.master is not None and bool(the_master(service).sources),
                "the station was started for the box that woke",
            )
            await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=AUX))

            await eventually(
                lambda: any("changed its mind" in line for line in logs),
                "the service noticed before it sent anything",
            )
            assert joins(world.studio) == [], "a box that changed its mind must not be taken in afterwards"
    finally:
        slow.close()


async def test_a_member_that_only_reports_on_its_transport_channel_is_not_dropped(world: World, tmp_path: Path) -> None:
    """A zone nobody touches must not empty itself, and the report that proves it is not a frame.

    A box playing our stream has nothing to say on its notification channel and says nothing, so
    the only thing that keeps it alive is the state report it sends on the transport channel about
    once a second. Here the hallway goes quiet in both ways and ages out, while the studio keeps
    reporting - through the seam the real transport channel calls - and stays.
    """
    options = _options(world, tmp_path, unreachable_timeout_s=1.0)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        assert service.master is not None

        # The studio reports for as long as it takes, not for a fixed count: the hallway is only
        # silent once its fade has finished, because a real box answers every volume write with a
        # frame and that frame proves it alive. A fixed 1.8 s of reports ended before the hallway
        # could age out, and the studio then aged out beside it.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10.0
        while believed(options) != (STUDIO_ID,):
            assert loop.time() < deadline, "the box that kept reporting stayed and the silent one aged out"
            the_master(service).on_slave_state(
                SlaveState(
                    peer=STUDIO_IP,
                    url_id=1,
                    state=audio.AudioServerMsgServerState.PLAYING,
                    milliseconds=0,
                    byte_offset=0,
                )
            )
            await asyncio.sleep(0.15)


async def test_an_unusable_registry_entry_is_said_once_and_not_on_every_poll(world: World, tmp_path: Path) -> None:
    """A placeholder in the device list stays there as long as its device is on the network.

    So a line per poll is the same sentence every 30 s for as long as that lasts - measured, about
    120 an hour in the house. The skip is news when the set of skipped entries changes, and when
    it empties again; the polls in between say nothing.
    """
    entries = json.loads(devices_at({STUDIO_ID: STUDIO_IP, HALLWAY_ID: HALLWAY_IP, CONSOLE_ID: CONSOLE_IP}))
    placeholder = {"ip_address": "192.0.2.9", "name": "upnp placeholder"}
    world.registry.body = json.dumps([*entries, placeholder])
    options = _options(world, tmp_path)
    logs: list[str] = []

    async with _running(options, logs):
        await asyncio.sleep(options.registry_poll_s * 6)
        skipped = [line for line in logs if "skipping a device entry" in line]
        assert len(skipped) == 1, f"the same skip was said on every poll: {skipped}"

        world.registry.body = json.dumps(entries)
        await eventually(lambda: any("no unusable entry" in line for line in logs), "the skip ending was said")
        await asyncio.sleep(options.registry_poll_s * 3)
        assert len([line for line in logs if "no unusable entry" in line]) == 1


async def test_a_registry_that_cannot_be_read_at_start_does_not_forget_the_house(world: World, tmp_path: Path) -> None:
    """The state file is the only memory a restart has, and a slow neighbour must not erase it.

    AfterTouch is a container that can still be starting while this one is already up. The device
    list is then unreadable, the service knows no speakers at all - and writing that answer into
    the file would forget the house permanently, one restart later.
    """
    options = _options(world, tmp_path)
    logs: list[str] = []

    async with _running(options, logs):
        await _both_wake(world)
        await eventually(lambda: believed(options) == (STUDIO_ID, HALLWAY_ID), "both boxes are in the zone")

    await world.registry.stop()

    async with _running(options, logs):
        await eventually(
            lambda: any("keeping the 0 speaker(s) already known" in line for line in logs),
            "the service could not read the device list at all",
        )
        assert believed(options) == (STUDIO_ID, HALLWAY_ID), "what the last run knew is still in the file"


async def test_a_box_that_moves_is_taken_back_at_the_address_it_moved_to(world: World, tmp_path: Path) -> None:
    """A box on a new lease must be re-joined there, or every later document goes to nobody.

    The zone holds a box by the address it joined at. When the registry reports a new one, the
    observer follows by itself and the zone does not: the member list the others hold names an
    address that is gone, and the dissolve at the end of the day goes there too - so the box that
    moved is left in a zone whose master has gone, which is the one outcome that needs a person.
    """
    options = _options(world, tmp_path)
    logs: list[str] = []
    moved = FakeSpeaker({}, host=MOVED_IP, device_id=STUDIO_ID, notify_port=world.notify_port)
    await moved.start()

    try:
        async with _running(options, logs) as service:
            await _both_wake(world)

            world.registry.body = devices_at({STUDIO_ID: MOVED_IP, HALLWAY_ID: HALLWAY_IP, CONSOLE_ID: CONSOLE_IP})
            await eventually(lambda: bool(joins(moved)), "the box was taken back at its new address")
            assert service.master is not None
            assert STUDIO_IP not in service.master.slaves, "and let go of at the old one"
    finally:
        await moved.stop()


# --- M2: the channel list the service owns ------------------------------------------------------


async def test_an_empty_channel_file_is_seeded_from_the_box_that_is_switched_on_first(
    world: World, tmp_path: Path
) -> None:
    """First start: there is no list, so it comes from the presets of the box somebody switches on.

    The user's rule of 2026-09-07, replacing a box named in the unit file: the house should not
    have to carry a name that nobody can later explain, and the box a person switches on is the
    box they are standing at.

    Presets 1 and 3 are set on it and 2 is not, which is the rule worth driving end to end: the
    seeded list holds channels 1 and 3, because the key on the box is the number in the room. The
    other box has a different preset, so a list that took ITS presets would be visible.
    """
    world.studio.presets = {
        1: _preset(f"{world.station_url}?c=1", "Superfly"),
        3: _preset(f"{world.station_url}?c=3", "Technikum"),
    }
    world.hallway.presets = {2: _preset(f"{world.station_url}?c=2", "The other box")}
    options = _options(world, tmp_path, seed=True)
    logs: list[str] = []

    async with _running(options, logs):
        await eventually(lambda: _channels_of(options).channels == (), "nothing is seeded before anybody wakes")
        await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
        await eventually(lambda: _channels_of(options).numbers_in_order() == ("1", "3"), "the list was seeded")
        await world.hallway.notify(now_playing_frame(device_id=HALLWAY_ID, source=RADIO))
        await asyncio.sleep(0.5)  # two registry polls: long enough for a second seeding to show

    seeded = _channels_of(options)
    assert seeded.numbers_in_order() == ("1", "3"), "the box that woke second did not re-seed"
    found = seeded.by_number("3")
    assert found is not None
    assert found.name == "Technikum"


async def test_a_box_with_no_presets_leaves_the_seeding_to_the_next_one_switched_on(
    world: World, tmp_path: Path
) -> None:
    """A box with nothing on its keys cannot seed anything, so it must not consume the chance."""
    world.studio.presets = {}
    world.hallway.presets = {2: _preset(f"{world.station_url}?c=2", "Technikum")}
    options = _options(world, tmp_path, seed=True)
    logs: list[str] = []

    async with _running(options, logs):
        await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
        await eventually(lambda: any("seeded 0" in line for line in logs), "the first box had nothing to give")
        assert _channels_of(options).channels == (), "and nothing was written"

        await world.hallway.notify(now_playing_frame(device_id=HALLWAY_ID, source=RADIO))
        await eventually(lambda: _channels_of(options).numbers_in_order() == ("2",), "the next one seeded it")


async def test_a_channel_file_that_exists_is_not_re_seeded(world: World, tmp_path: Path) -> None:
    """A change on one speaker must not silently rewrite the house's list."""
    options = _options(world, tmp_path, station_url=f"{world.station_url}?kept")
    world.studio.presets = {1: _preset(f"{world.station_url}?c=1", "Superfly")}
    logs: list[str] = []

    async with _running(options, logs):
        await _both_wake(world)

    kept = _channels_of(options)
    assert kept.numbers_in_order() == ("1",)
    found = kept.by_number("1")
    assert found is not None
    assert found.url.endswith("?kept"), "the existing list stands, presets or no presets"


async def test_the_remembered_channel_is_what_gets_played(world: World, tmp_path: Path) -> None:
    """A restart resumes the channel it was on, not the first one in the list."""
    options = _options(world, tmp_path, seed=True)
    save_channels(
        options.channel_file,
        ChannelList(
            channels=(
                Channel(number="1", name="One", kind=ChannelKind.RADIO, url=f"{world.station_url}?c=1"),
                Channel(number="3", name="Three", kind=ChannelKind.RADIO, url=f"{world.station_url}?c=3"),
            )
        ),
    )
    save_state(options.state_file, ZoneState(channel="3", members=()))
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        assert service.master is not None and service.master.station is not None
        assert service.master.station.playback_url.endswith("?c=3")


async def test_a_remembered_channel_that_is_gone_falls_back_to_the_lowest_and_says_so(
    world: World, tmp_path: Path
) -> None:
    """Somebody edits the file and deletes what was playing. The house must not go silent."""
    options = _options(world, tmp_path, seed=True)
    save_channels(
        options.channel_file,
        ChannelList(
            channels=(Channel(number="1", name="One", kind=ChannelKind.RADIO, url=f"{world.station_url}?c=1"),)
        ),
    )
    save_state(options.state_file, ZoneState(channel="3", members=()))
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        assert service.master is not None and service.master.station is not None
        assert service.master.station.playback_url.endswith("?c=1")

    assert any("3" in line and "channel" in line for line in logs), "and it says which channel was gone"


async def test_a_box_that_appears_later_can_still_seed_the_list(world: World, tmp_path: Path) -> None:
    """A box that was off at start must not need a restart of the service to be able to seed."""
    world.registry.body = devices_at({HALLWAY_ID: HALLWAY_IP})
    world.studio.presets = {1: _preset(f"{world.station_url}?c=1", "Superfly")}
    options = _options(world, tmp_path, seed=True)
    logs: list[str] = []

    async with _running(options, logs):
        await eventually(lambda: _channels_of(options).channels == (), "nothing was seeded while it was absent")
        world.registry.body = devices_at({STUDIO_ID: STUDIO_IP, HALLWAY_ID: HALLWAY_IP})
        # notify() waits for the service's observer to connect rather than racing it, so this
        # also waits out the registry poll that has to name the box before it is watched at all.
        await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
        await eventually(lambda: _channels_of(options).numbers_in_order() == ("1",), "and seeded once it woke")


async def test_a_box_is_asked_for_its_presets_only_once(world: World, tmp_path: Path) -> None:
    """A box that answered with nothing must not be asked again on every frame it sends."""
    world.studio.presets = {}
    options = _options(world, tmp_path, seed=True)
    logs: list[str] = []

    async with _running(options, logs):
        for _ in range(3):
            await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
        await eventually(lambda: any("seeded 0" in line for line in logs), "it said the box had no presets")
        await asyncio.sleep(0.8)  # several registry polls at 0.2 s

    assert len([line for line in logs if "seeded 0" in line]) == 1, "asked once, not once per frame"


# --- M2: dialling a channel from the room -------------------------------------------------------


def _dialable_world(world: World, tmp_path: Path, *, dial_window_s: float) -> ServiceOptions:
    """Two channels to dial between, and a window short enough for a test to wait out."""
    options = _options(world, tmp_path, seed=True, dial_window_s=dial_window_s)
    save_channels(
        options.channel_file,
        ChannelList(
            channels=(
                Channel(number="1", name="One", kind=ChannelKind.RADIO, url=f"{world.station_url}?c=1"),
                Channel(number="12", name="Twelve", kind=ChannelKind.RADIO, url=f"{world.station_url}?c=12"),
            )
        ),
    )
    return options


async def _forward_key(from_ip: str, key: str, state: str) -> None:
    """One key press as a member forwards it: a POST to the master's own face, from the box's
    address, carrying no device id at all - which is why the registry has to name the sender."""
    body = key_body(key, state)
    reader, writer = await asyncio.open_connection(MASTER, 8090, local_addr=(from_ip, 0))
    writer.write(f"POST /slaveMsg HTTP/1.1\r\nHost: x\r\nContent-Length: {len(body)}\r\n\r\n{body}".encode())
    assert b"<status>/slaveMsg</status>" in await asyncio.wait_for(reader.read(), 5.0)
    writer.close()


async def _tap_key(from_ip: str, key: str) -> None:
    """A key pressed and let go, which is the only way a real box ever reports one.

    A thumb acts on the RELEASE, because the same key carries the rotation when it is tapped and
    multiroom for that box when it is held, and only the time between the two tells them apart
    (``soundtouch_zonemaster.longpress``). On real hardware the pair is 291 to 444 ms apart; back to back
    here is a tap by any threshold.
    """
    await _forward_key(from_ip, key, KeyState.PRESS)
    await _forward_key(from_ip, key, KeyState.RELEASE)


async def _double_tap_key(from_ip: str, key: str) -> None:
    """Two taps of one key, the second pressed at once: a double tap by any dialling window.

    A thumb's single tap and its double tap mean two things (user, 2026-09-25): the single one hands
    the box the house volume, the double one changes the rotation.
    """
    await _tap_key(from_ip, key)
    await _tap_key(from_ip, key)


async def _hold_key(from_ip: str, key: str, *, threshold_s: float = HOLD_THRESHOLD_DEFAULT_S) -> None:
    """A key held past the hold threshold and then let go, in real time.

    Real time on purpose: the threshold is the one the service is RUNNING with, and reaching in to
    shorten it would be testing something other than what the house does. The release is sent
    because a real box sends one - the action has already happened by then, which is a thing worth
    having under test rather than assumed.
    """
    await _forward_key(from_ip, key, KeyState.PRESS)
    await asyncio.sleep(threshold_s + 0.2)
    await _forward_key(from_ip, key, KeyState.RELEASE)


async def _press_preset(speaker: FakeSpeaker, device_id: str, preset_id: int, *, hold_s: float = 0.2) -> None:
    """One preset press as a real box reports it: the selection, and the key going down and up.

    THREE frames, and each is there for a measured reason. The selection alone is not a press -
    that is exactly what the master's own station change looks like coming back from every slave,
    and reading it as one ran the flat's first zone into 28 station changes in 45 seconds
    (``soundtouch_zonemaster.presses``). And the touch is not one frame but two, 0.23 to 0.447 s
    apart on real hardware, because the window for the NEXT key is armed at the second of them.

    ``hold_s`` is shorter than any hold measured in the flat, to keep the suite quick; it only has
    to be above ``HOLD_FLOOR_S`` for the pair to read as one key action.
    """
    await speaker.notify(selection_frame(device_id=device_id, preset_id=preset_id))
    await speaker.notify(user_activity_frame(device_id=device_id))
    await asyncio.sleep(hold_s)
    await speaker.notify(user_activity_frame(device_id=device_id))


async def _wake_on_preset(speaker: FakeSpeaker, device_id: str, preset_id: int) -> None:
    """A box coming out of standby, in the order a real one does it.

    Measured 2026-09-07 over four wakes: the box names its preset 101 to 199 ms BEFORE it says it
    has left standby, and the user activity lands between the two. That order is what lets the
    service still know the box was asleep at the moment it has to decide what the selection meant.
    """
    await _press_preset(speaker, device_id, preset_id)
    await speaker.notify(now_playing_frame(device_id=device_id, source=RADIO))


def _playing(service: ZoneService) -> str:
    url = _station_url(service)
    assert url is not None, "the zone is playing nothing"
    return url


def _station_url(service: ZoneService) -> str | None:
    """What the zone is playing, or ``None`` while it plays nothing.

    The tolerant form, for the tests that watch a house come UP: a predicate that asserts cannot
    be waited on, it raises out of the wait instead of reporting not-yet.
    """
    master = service.master
    if master is None or master.station is None:
        return None
    return master.station.playback_url


async def test_a_two_digit_number_changes_the_channel_for_the_whole_zone(world: World, tmp_path: Path) -> None:
    """Two presses inside the window are ONE number, and the whole zone follows it."""
    options = _dialable_world(world, tmp_path, dial_window_s=0.5)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        assert _playing(service).endswith("?c=1"), "it starts on the lowest channel"

        await _press_preset(world.studio, STUDIO_ID, 1)
        await _press_preset(world.studio, STUDIO_ID, 2)
        await eventually(lambda: _playing(service).endswith("?c=12"), "the zone moved to channel 12")


async def test_an_intermediate_digit_touches_nothing(world: World, tmp_path: Path) -> None:
    """While the window is open the master does nothing at all - no source, no speaker touched.

    Measured 2026-09-06: twenty-two unacted presses left every slave on the master's stream, which
    is what multi-digit dialling rests on.
    """
    options = _dialable_world(world, tmp_path, dial_window_s=2.0)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        untouched = len(world.hallway.requests)

        await _press_preset(world.studio, STUDIO_ID, 1)
        await asyncio.sleep(0.4)

        assert _playing(service).endswith("?c=1"), "the station has not changed while the window is open"
        assert len(world.hallway.requests) == untouched, "and no speaker has been sent anything"


async def test_an_undefined_number_does_nothing_at_all(world: World, tmp_path: Path) -> None:
    """A mistyped sequence is harmless: wait a second, nothing happened, start again."""
    options = _dialable_world(world, tmp_path, dial_window_s=0.5)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        untouched = len(world.hallway.requests)

        await _press_preset(world.studio, STUDIO_ID, 6)
        await _press_preset(world.studio, STUDIO_ID, 6)
        await eventually(lambda: any("66" in line for line in logs), "it said the number was not there")
        await asyncio.sleep(0.3)

        assert _playing(service).endswith("?c=1"), "and it is still on the channel it was on"
        assert len(world.hallway.requests) == untouched, "and touched nobody"


async def test_a_selection_no_box_confirmed_is_our_own_voice_and_never_becomes_a_digit(
    world: World, tmp_path: Path
) -> None:
    """The defect the first live run found, from the outside: an echo must reach no dialler.

    A box reports what it is playing in exactly the frame a pressed preset produces, so the
    master's own station change comes back from every slave looking like a press. On 2026-09-07
    that answered itself 28 times in 45 seconds.

    The echo here is a bare selection of 1, and the press that follows it is a real 2. Read as a
    press, the echo makes the number 12, which is a channel this house HAS - so the defect moves
    the whole zone rather than saying anything. Dropped, the number is 2, which is not a channel,
    and the service says so and touches nothing.
    """
    options = _dialable_world(world, tmp_path, dial_window_s=0.5)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        assert _playing(service).endswith("?c=1"), "it starts on the lowest channel"

        await world.studio.notify(selection_frame(device_id=STUDIO_ID, preset_id=1))
        await _press_preset(world.studio, STUDIO_ID, 2)

        await eventually(lambda: any("dialled 2: no such channel" in line for line in logs), "the press was read as 2")
        assert _playing(service).endswith("?c=1"), "and the zone never moved to the 12 the echo would have made"


async def test_a_touch_stops_being_available_once_the_back_window_has_passed(world: World, tmp_path: Path) -> None:
    """The bound that keeps our own echo out, wired end to end.

    A touch may confirm the selection that follows it, because that is the order a SLAVE reports a
    press in (``soundtouch_zonemaster.presses``). What it may NOT do is still be lying there when our own
    station change comes back, which happens 0.7 to 1.4 s after the press that caused it - the
    dialling window plus the box's own delay. So a touch older than ``CONFIRM_BACK_WINDOW_S``
    confirms nothing, and the echo behind it is dropped rather than dialled.

    The stale touch here stands for the release of a press, and the selection behind it for the
    echo that press caused. If the two were paired, the house would read 1 and then the real press
    of 2 as the number 12 and change station to nothing at all.
    """
    options = _dialable_world(world, tmp_path, dial_window_s=0.5)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        assert _playing(service).endswith("?c=1"), "it starts on the lowest channel"

        await world.studio.notify(user_activity_frame(device_id=STUDIO_ID))
        await asyncio.sleep(CONFIRM_BACK_WINDOW_S + 0.05)
        await world.studio.notify(selection_frame(device_id=STUDIO_ID, preset_id=1))
        await _press_preset(world.studio, STUDIO_ID, 2)

        await eventually(lambda: any("dialled 2: no such channel" in line for line in logs), "only the 2 was read")
        assert _playing(service).endswith("?c=1"), "the stale touch never adopted the echo into a 12"


async def test_a_touch_confirms_the_selection_that_follows_it(world: World, tmp_path: Path) -> None:
    """The order a box reports a press in while it is a SLAVE, which is the room this is for.

    Measured 2026-09-07 on four presses made inside a playing zone: the touch arrives first and the
    preset 21 to 37 ms behind it, with nothing following. Reading only the confirmation behind a
    selection lost every one of them silently - no log line, no dial, a room whose buttons were
    dead while every other room worked.
    """
    options = _dialable_world(world, tmp_path, dial_window_s=0.5)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        assert _playing(service).endswith("?c=1")

        await world.studio.notify(user_activity_frame(device_id=STUDIO_ID))
        await world.studio.notify(selection_frame(device_id=STUDIO_ID, preset_id=2))

        await eventually(lambda: any("dialled 2: no such channel" in line for line in logs), "the press was read")


async def test_our_own_fade_is_not_a_person_touching_the_box(world: World, tmp_path: Path) -> None:
    """The touches a box reports for OUR volume writes confirm nothing.

    A real box answers every volume write with a ``userActivityUpdate``, the one frame that tells a
    press from our own station change coming back. Measured 2026-09-21: during the fade after a join
    Room1 sent eight of them, and one confirmed a selection nobody had made, so the house
    dialled a number from a press that never happened.

    The bare selection here is that echo, sent in the middle of the fade. Paired with a fade touch it
    reads as a press of 2, which this house has no channel for, and the service says so; left alone,
    as it must be, nothing is dialled at all.
    """
    options = _dialable_world(world, tmp_path, dial_window_s=0.5)
    logs: list[str] = []
    world.studio.volume = 27

    async with _running(options, logs):
        await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
        await eventually(lambda: len(_volume_writes(world.studio)) >= 3, "the fade was climbing", timeout=15.0)
        await world.studio.notify(selection_frame(device_id=STUDIO_ID, preset_id=2))
        await eventually(lambda: world.studio.volume == 27, "the box was put back where it was")
        # A deliberate bare sleep past the window: it establishes that nothing further is pending,
        # which is what makes the absence below mean something.
        await asyncio.sleep(1.5)

        dialled = [line for line in logs if "dialled" in line]
        assert dialled == [], f"a touch of our own fade was read as a person pressing: {dialled}"


async def test_a_person_pressing_during_our_fade_is_still_heard(world: World, tmp_path: Path) -> None:
    """The other half: our echoes are dropped ONE per write, never by the time they arrive in.

    A box that has just been taken in is the box somebody just switched on, and that person may
    press again while it is still climbing back to its volume. A window that dropped every touch
    near our writes swallowed exactly that press, and seven of the suite's press tests with it.
    """
    options = _dialable_world(world, tmp_path, dial_window_s=0.5)
    logs: list[str] = []
    world.studio.volume = 27

    async with _running(options, logs):
        await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
        await eventually(lambda: len(_volume_writes(world.studio)) >= 3, "the fade was climbing", timeout=15.0)
        await _press_preset(world.studio, STUDIO_ID, 2)

        await eventually(lambda: any("dialled 2: no such channel" in line for line in logs), "the press was read")


async def test_a_box_waiting_for_a_number_is_said_once_and_not_on_every_pass(world: World, tmp_path: Path) -> None:
    """A pass runs for every frame, so a line per pass is a wall of the same sentence.

    Measured 2026-09-20 at 17:11:08: eleven times "waiting, because a number is still being
    dialled" in 0.9 s, in exactly the moment a channel change can go wrong and its own lines are
    the ones worth reading. The wait is news when it starts, and not again until it changes.
    """
    options = _dialable_world(world, tmp_path, dial_window_s=1.0)
    logs: list[str] = []

    async with _running(options, logs):
        await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
        await eventually(lambda: len(joins(world.studio)) == 1, "the studio was taken into the zone")

        await _press_preset(world.studio, STUDIO_ID, 1)
        for _ in range(4):
            await world.hallway.notify(now_playing_frame(device_id=HALLWAY_ID, source=RADIO))
            await asyncio.sleep(0.05)
        await eventually(lambda: len(joins(world.hallway)) == 1, "the hallway was taken in once the number was done")

        waiting = [line for line in logs if "a number is still being dialled" in line]
        assert len(waiting) == 1, f"the wait was said on every pass: {waiting}"


async def _press_preset_as_a_slave(
    speaker: FakeSpeaker, device_id: str, preset_id: int, *, hold_s: float = 0.35
) -> None:
    """One preset press as a box that is a SLAVE reports it, which is the other way round.

    Measured over the ten presses made from inside a playing zone in the second live run: both
    touches arrive FIRST, 0.312 to 0.498 s apart, and the selection follows the second of them by
    21 to 37 ms. A box outside the zone does the opposite (``_press_preset``), and the room this
    feature exists for is the one that is playing.
    """
    await speaker.notify(user_activity_frame(device_id=device_id))
    await asyncio.sleep(hold_s)
    await speaker.notify(user_activity_frame(device_id=device_id))
    await speaker.notify(selection_frame(device_id=device_id, preset_id=preset_id))


async def test_a_four_digit_number_survives_a_thumb_slower_than_the_window(world: World, tmp_path: Path) -> None:
    """The defect the user reported in the flat on 2026-09-21, at the timing that produced it.

    Two of five four-digit numbers pressed that night came out as `111` and then a bare `3`. On the
    box's own forwarded channel the move from the `1` key to the `3` key measured 0.645 and 0.623 s
    against a 0.6 s window - so the window closed 17 ms and 28 ms before the last digit, the house
    dialled a number that is no channel, and the stray `3` moved every room.

    Nothing was lost and nothing was slow: the window was armed at the PRESS, so it charged a
    person for however long their thumb rested on the key. Held 0.35 s, a 0.65 s press-to-press gap
    is 0.3 s of idle time. Armed at the RELEASE, as it is now (user, 2026-09-21), the same pressing
    is comfortably one number at a 0.5 s window - and this test uses exactly that timing, so it
    fails the moment the window goes back to being armed at the press.
    """
    options = _options(world, tmp_path, seed=True, dial_window_s=0.5)
    save_channels(
        options.channel_file,
        ChannelList(
            channels=(
                Channel(number="1", name="One", kind=ChannelKind.RADIO, url=f"{world.station_url}?c=1"),
                Channel(number="1113", name="Book Three", kind=ChannelKind.RADIO, url=f"{world.station_url}?c=1113"),
            )
        ),
    )
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        assert _playing(service).endswith("?c=1"), "it starts on the lowest channel"

        for preset_id in (1, 1, 1, 3):
            await _press_preset_as_a_slave(world.studio, STUDIO_ID, preset_id)
            # 0.3 s of thinking between one key coming up and the next going down, which with the
            # 0.35 s hold above is the 0.65 s between presses that the flat measured.
            await asyncio.sleep(0.3)

        await eventually(lambda: any("dialled 1113" in line for line in logs), "the four presses were one number")
        await eventually(lambda: _playing(service).endswith("?c=1113"), "and the house moved to that channel")
        assert not [line for line in logs if "dialled 111:" in line], "it was never read as a three-digit number"


async def test_a_box_waking_into_a_playing_zone_joins_it_rather_than_choosing_for_the_house(
    world: World, tmp_path: Path
) -> None:
    """A wake and a press are the same frames, so the house decides which by what it is doing.

    The design says a waking box joins the running channel (Phase 1, Membership), and the user
    settled the rest on 2026-09-07: a box out of standby chooses only while nothing is playing.
    Otherwise POWER in one room would move every other room to whatever that box last had.
    """
    options = _dialable_world(world, tmp_path, dial_window_s=0.5)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
        await eventually(
            lambda: (_station_url(service) or "").endswith("?c=1"), "the house is playing the lowest channel"
        )

        await _wake_on_preset(world.hallway, HALLWAY_ID, 2)

        await eventually(lambda: any("woke on 2" in line for line in logs), "it read the wake as a wake")
        await eventually(lambda: len(joins(world.hallway)) == 1, "and took the box into the zone anyway")
        assert _playing(service).endswith("?c=1"), "and left the channel where the house had it"


async def test_a_box_waking_into_a_quiet_house_chooses_the_channel_it_names(world: World, tmp_path: Path) -> None:
    """The other half of that rule: with nothing playing, the box somebody switched on decides.

    It is the box they are standing at, and it is the only one saying anything, so what it names
    is what the house should come up on - the same reason the channel list is seeded from it.
    """
    options = _dialable_world(world, tmp_path, dial_window_s=0.5)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _wake_on_preset(world.studio, STUDIO_ID, 12)

        await eventually(
            lambda: (_station_url(service) or "").endswith("?c=12"), "the house came up on what the box named"
        )
    assert not any("woke on" in line for line in logs), "and nothing was refused as a wake"


async def test_a_dialled_channel_survives_a_restart(world: World, tmp_path: Path) -> None:
    """What was dialled is what a restart comes back on, which is what the state file is for."""
    options = _dialable_world(world, tmp_path, dial_window_s=0.5)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        await _press_preset(world.studio, STUDIO_ID, 1)
        await _press_preset(world.studio, STUDIO_ID, 2)
        await eventually(lambda: _playing(service).endswith("?c=12"), "the zone moved to channel 12")

    await eventually(
        lambda: load_state(options.state_file, log=lambda _kind, _text: None).channel == "12", "it was written down"
    )

    async with _running(options, logs) as second:
        # NOT _both_wake: the boxes already carry a join from the first run, so a check that only
        # counts joins is already satisfied and would assert on the run that has just ended.
        before = len(joins(world.studio))
        await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
        await eventually(lambda: len(joins(world.studio)) > before, "the studio was taken in by the NEW run")
        assert _playing(second).endswith("?c=12"), "and it comes back on it"


async def test_a_press_on_one_box_does_not_join_the_number_another_box_is_dialling(
    world: World, tmp_path: Path
) -> None:
    """Two people at two boxes are two numbers, not one interleaved one."""
    options = _dialable_world(world, tmp_path, dial_window_s=0.5)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)

        await _press_preset(world.studio, STUDIO_ID, 1)
        await _press_preset(world.hallway, HALLWAY_ID, 1)
        await _press_preset(world.studio, STUDIO_ID, 2)
        await eventually(lambda: _playing(service).endswith("?c=12"), "the studio dialled 12")

    assert not any("11" in line and "dial" in line for line in logs), "the two boxes never merged into 11"


async def test_next_steps_one_channel_and_counts_only_the_press(world: World, tmp_path: Path) -> None:
    """One press of next is ONE step, although the key arrives twice.

    Measured 2026-09-06: every key arrives as press AND release, 365 to 444 ms apart. That gap is
    INSIDE the dialling window, so a release counted as a press is not one step too many - it is a
    second step collected into the same jump, and one press would move two channels.
    """
    options = _dialable_world(world, tmp_path, dial_window_s=0.5)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        assert _playing(service).endswith("?c=1")

        await _forward_key(STUDIO_IP, KeyName.NEXT_TRACK, KeyState.PRESS)
        await _forward_key(STUDIO_IP, KeyName.NEXT_TRACK, KeyState.RELEASE)
        await eventually(lambda: _playing(service).endswith("?c=12"), "one press stepped one channel")

        assert not any("+2" in line for line in logs), "the release was not counted as a second press"


async def test_next_wraps_round_the_end_of_the_list(world: World, tmp_path: Path) -> None:
    """A listener can never get stuck at an end, so the two-channel list wraps back to the first."""
    options = _dialable_world(world, tmp_path, dial_window_s=0.5)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        await _forward_key(STUDIO_IP, KeyName.NEXT_TRACK, KeyState.PRESS)
        await eventually(lambda: _playing(service).endswith("?c=12"), "it stepped to the second channel")
        await _forward_key(STUDIO_IP, KeyName.NEXT_TRACK, KeyState.PRESS)
        await eventually(lambda: _playing(service).endswith("?c=1"), "and wrapped back to the first")


# --- M2: a held thumb steers the rotation ---------------------------------------------------------


def _rotation_world(world: World, tmp_path: Path, *, numbers: tuple[str, ...]) -> ServiceOptions:
    """A list of dialable channels, so a skip has something to skip over."""
    options = _options(world, tmp_path, seed=True, dial_window_s=0.5)
    save_channels(
        options.channel_file,
        ChannelList(
            channels=tuple(
                Channel(number=number, name=f"C{number}", kind=ChannelKind.RADIO, url=f"{world.station_url}?c={number}")
                for number in numbers
            )
        ),
    )
    return options


async def test_thumbs_down_takes_the_playing_channel_out_of_the_rotation(world: World, tmp_path: Path) -> None:
    """It is written into the channel file, and nothing in the house sounds different for it."""
    options = _rotation_world(world, tmp_path, numbers=("1", "12", "13"))
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        assert _playing(service).endswith("?c=1")

        await _hold_key(STUDIO_IP, KeyName.THUMBS_DOWN)
        await eventually(lambda: _channels_of(options).rotation_numbers() == ("12", "13"), "it was taken out")

        assert _playing(service).endswith("?c=1"), "and the zone is still on it"


async def test_a_single_thumbs_down_leaves_the_rotation_alone(world: World, tmp_path: Path) -> None:
    """The user's reason for the new gestures (2026-09-25): one stray touch took a channel out of what
    next and previous walk through, and nothing audible said so. A single tap now changes neither
    the rotation nor the group. Waited out past two windows, so a tap the service meant to read late
    would have acted by now."""
    options = _rotation_world(world, tmp_path, numbers=("1", "12", "13"))
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        await _tap_key(STUDIO_IP, KeyName.THUMBS_DOWN)
        await eventually(lambda: any("thumbed down once" in line for line in logs), "the tap was read")
        # A deliberate bare sleep: two windows, so a change read at the window has landed.
        await asyncio.sleep(2 * options.dial_window_s)

        assert _channels_of(options).rotation_numbers() == ("1", "12", "13"), "the rotation is as it was"
        assert STUDIO_IP in _slaves(service), "and the box is still in the zone"
    assert out_of_multiroom(options) == ()


async def test_a_double_thumb_acts_once_at_its_second_release(world: World, tmp_path: Path) -> None:
    """Every key arrives twice, 291 to 444 ms apart, and a double tap is two such pairs.

    The second RELEASE is what acts, so the second press alone must do nothing; setting a flag twice
    would hide either mistake, and the log does not, so the count of what it said is what this
    asserts."""
    options = _rotation_world(world, tmp_path, numbers=("1", "12", "13"))
    logs: list[str] = []

    async with _running(options, logs):
        await _both_wake(world)
        await _tap_key(STUDIO_IP, KeyName.THUMBS_DOWN)
        await _forward_key(STUDIO_IP, KeyName.THUMBS_DOWN, KeyState.PRESS)
        await asyncio.sleep(0.2)
        assert out_of_multiroom(options) == (), "the second press changed nothing"

        await _forward_key(STUDIO_IP, KeyName.THUMBS_DOWN, KeyState.RELEASE)
        await eventually(lambda: out_of_multiroom(options) == (STUDIO_ID,), "and its release took the box out")
        await asyncio.sleep(0.2)

    assert len([line for line in logs if "out of multiroom, on its own" in line]) == 1, "exactly once"
    assert _channels_of(options).rotation_numbers() == ("1", "12", "13"), "and the rotation was left alone"


async def test_next_skips_a_channel_a_thumbs_down_took_out(world: World, tmp_path: Path) -> None:
    """The listener is ON the channel they took out, which is where a thumbs down leaves them."""
    options = _rotation_world(world, tmp_path, numbers=("1", "12", "13"))
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        await _forward_key(STUDIO_IP, KeyName.NEXT_TRACK, KeyState.PRESS)
        await eventually(lambda: _playing(service).endswith("?c=12"), "it stepped to the second channel")

        await _hold_key(STUDIO_IP, KeyName.THUMBS_DOWN)
        await eventually(lambda: _channels_of(options).rotation_numbers() == ("1", "13"), "12 was taken out")

        await _forward_key(STUDIO_IP, KeyName.PREV_TRACK, KeyState.PRESS)
        await eventually(lambda: _playing(service).endswith("?c=1"), "previous from it went to the one before")
        await _forward_key(STUDIO_IP, KeyName.NEXT_TRACK, KeyState.PRESS)
        await eventually(lambda: _playing(service).endswith("?c=13"), "and next skipped over it")


async def test_thumbs_up_puts_the_channel_back_into_the_rotation(world: World, tmp_path: Path) -> None:
    """Dialling the number and thumbing up is the way back, with no editor and no restart."""
    options = _rotation_world(world, tmp_path, numbers=("1", "12", "13"))
    logs: list[str] = []

    async with _running(options, logs):
        await _both_wake(world)
        await _hold_key(STUDIO_IP, KeyName.THUMBS_DOWN)
        await eventually(lambda: _channels_of(options).rotation_numbers() == ("12", "13"), "it was taken out")

        await _hold_key(STUDIO_IP, KeyName.THUMBS_UP)
        await eventually(lambda: _channels_of(options).rotation_numbers() == ("1", "12", "13"), "and put back")


async def test_thumbs_down_on_the_last_channel_in_the_rotation_is_refused(world: World, tmp_path: Path) -> None:
    """An empty rotation would leave next and previous dead from every channel, which reads as
    the buttons being broken rather than as something somebody did."""
    options = _rotation_world(world, tmp_path, numbers=("1",))
    logs: list[str] = []

    async with _running(options, logs):
        await _both_wake(world)
        await _hold_key(STUDIO_IP, KeyName.THUMBS_DOWN)
        await eventually(lambda: any("last channel" in line for line in logs), "it said why it would not")

        assert _channels_of(options).rotation_numbers() == ("1",), "and left the list alone"


# --- M2: one room out of the group, by double-tapping a thumb on it -------------------------------


async def test_double_tapping_thumbs_down_takes_that_box_out_and_leaves_it_playing(
    world: World, tmp_path: Path
) -> None:
    """The room steps out of the group and keeps its music.

    Both halves matter and neither is enough. It is let go of the zone, which is what was asked
    for, AND it is handed the channel it was hearing on its own face - because a gesture that
    takes a room out of the group and silences it would read as having broken the speaker.

    What is asserted is what the BOX did, because that is what was wrong in the flat on 2026-09-24:
    the service dropped the box from its books and sent it nothing, so it stayed our slave and
    forwarded the ``/select`` back to us instead of playing it - the room kept the group's stream
    while every line in the journal said it had left (docs/measurements/2026-09-24-releasing-one-
    slave.md). A box is free only once it has been sent ``/removeZoneSlave``, and that has to
    reach it BEFORE the ``/select``.
    """
    options = _rotation_world(world, tmp_path, numbers=("1", "12", "13"))
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        assert STUDIO_IP in _slaves(service), "it starts in the zone"
        assert world.studio.zone_master == MASTER_ID, "and the box itself says whose slave it is"

        await _double_tap_key(STUDIO_IP, KeyName.THUMBS_DOWN)

        await eventually(lambda: STUDIO_IP not in _slaves(service), "the studio left the zone")
        await eventually(lambda: world.studio.played != [], "and played a station of its own")
        assert world.studio.played[0] == f"{world.station_url}?c=1", "the one it was on"
        assert world.studio.forwarded == [], "not one selection went back to the master"
        assert world.studio.zone_master is None, "it is nobody's slave any more"
        paths = world.studio.paths()
        assert paths.index("/removeZoneSlave") < paths.index("/select"), "released first, then given its station"
        released = world.studio.bodies_for("/removeZoneSlave")[0]
        assert f'master="{MASTER_ID}"' in released, "the release names our master"
        assert 'senderIsMaster="true"' in released
        assert f'<member ipaddress="{STUDIO_IP}">{STUDIO_ID}</member>' in released, "and the box it releases"
        assert HALLWAY_IP in _slaves(service), "and the rest of the house is untouched"
        assert world.hallway.bodies_for("/removeZoneSlave") == [], "which is sent nothing"
        assert _channels_of(options).rotation_numbers() == ("1", "12", "13"), "the rotation is not a hold's business"

    assert out_of_multiroom(options) == (STUDIO_ID,), "written down, so a restart cannot take it back"


async def test_our_own_select_coming_back_from_a_box_on_its_own_is_neither_a_press_nor_a_wake(
    world: World, tmp_path: Path
) -> None:
    """The ``/select`` that gives a released box its channel comes back as a press, and is not one.

    Measured 2026-09-24: a box outside the zone answers an API ``/select`` by naming the preset
    slot that holds the station and sending one touch - the shape of a person pressing that
    preset. Read as one, it dialled the same channel again, which sent the same ``/select`` again,
    every 0.65 s for 50 s in the flat. And because the release leaves the box asleep, that same
    echo is also the shape of a person SWITCHING IT ON, which is the way back into multiroom - so
    read as a press it would also undo the thumbs down that caused it.
    """
    options = _rotation_world(world, tmp_path, numbers=("1", "12", "13"))
    # As in the house, where channels 1 to 6 were seeded from the boxes' own presets: the channel
    # the box is handed IS one of its presets, so its echo names a dialable slot. With the slot
    # empty the echo names preset 0, which no number is made of, and the loop cannot happen.
    world.studio.presets[1] = _preset(f"{world.station_url}?c=1", "C1")
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        await _double_tap_key(STUDIO_IP, KeyName.THUMBS_DOWN)
        await eventually(lambda: world.studio.echoes >= 1, "the studio echoed the station it was given")

        # The echo is a whole press only once the dialling window after it has passed, and the loop
        # needed one window more to send the next /select - so three windows is past both.
        await asyncio.sleep(3 * options.dial_window_s)

        assert len(world.studio.bodies_for("/select")) == 1, "one /select, not a loop of them"
        assert world.studio.echoes == 1
        assert STUDIO_IP not in _slaves(service), "and the echo did not take the box back in"
        assert len(joins(world.studio)) == 1

    assert out_of_multiroom(options) == (STUDIO_ID,), "nor count as the box being switched on again"


async def test_a_box_on_its_own_that_dials_one_of_its_presets_is_sent_that_channel_once(
    world: World, tmp_path: Path
) -> None:
    """A preset dialled at a box outside the zone is one ``/select``, not a loop of them.

    The flat's loop exactly (2026-09-24 14:05:57 to 14:06:48): an awake box outside the zone dials
    2, the service sends it channel 2, the box echoes that as preset 2 with one touch, the echo
    completes to 2 one window later, and the service sends channel 2 again - every 0.65 s until
    somebody pressed another digit into a number no channel has.
    """
    options = _rotation_world(world, tmp_path, numbers=("1", "2", "12"))
    world.studio.presets[1] = _preset(f"{world.station_url}?c=1", "C1")
    world.studio.presets[2] = _preset(f"{world.station_url}?c=2", "C2")
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        await _double_tap_key(STUDIO_IP, KeyName.THUMBS_DOWN)
        await eventually(lambda: world.studio.echoes >= 1, "the studio is out and playing on its own")
        # Past the window its own echo would complete in, so the press below is the only one open.
        await asyncio.sleep(2 * options.dial_window_s)
        before = world.studio.echoes

        await _press_preset(world.studio, STUDIO_ID, 2)
        await eventually(lambda: world.studio.echoes > before, "it was sent channel 2 and echoed it")
        await asyncio.sleep(3 * options.dial_window_s)

        channel_2 = [body for body in world.studio.bodies_for("/select") if f"{world.station_url}?c=2" in body]
        assert len(channel_2) == 1, f"one /select of channel 2, not {len(channel_2)}"
        assert _playing(service).endswith("?c=1"), "and the house stayed where it was"


async def test_a_box_that_was_switched_out_is_still_out_after_a_restart(world: World, tmp_path: Path) -> None:
    """A decision somebody made standing in the room, and nothing they can see records it.

    A restart that quietly took the box back would undo it without anybody pressing anything, and
    the room would start playing the house's channel again on its own.
    """
    options = _rotation_world(world, tmp_path, numbers=("1", "12", "13"))
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        await _double_tap_key(STUDIO_IP, KeyName.THUMBS_DOWN)
        await eventually(lambda: STUDIO_IP not in _slaves(service), "the studio left the zone")

    await eventually(lambda: out_of_multiroom(options) == (STUDIO_ID,), "it was written down")

    async with _running(options, logs) as second:
        # NOT _both_wake: the boxes carry a join from the first run, so a check that only counts
        # joins is already satisfied and would assert on the run that has just ended. Both are
        # woken, because a box left in STANDBY does not belong to a zone whatever the file says,
        # and a studio that was never awake would pass this test without the exclusion doing
        # anything at all.
        studio_joins, hallway_joins = len(joins(world.studio)), len(joins(world.hallway))
        await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
        await world.hallway.notify(now_playing_frame(device_id=HALLWAY_ID, source=RADIO))

        # The studio's frame is queued FIRST and the queue is read by one worker, so the pass that
        # takes the hallway in has already seen the studio wake. Waiting on the hallway is
        # therefore a real barrier and not just a pause.
        await eventually(lambda: len(joins(world.hallway)) > hallway_joins, "the new run took the hallway in")
        assert believed(options) == (HALLWAY_ID,), "the studio is no part of the zone it rebuilt"
        assert len(joins(world.studio)) == studio_joins, "and it was never taken back in"
        assert STUDIO_IP not in _slaves(second)


async def test_a_box_that_is_out_of_multiroom_dials_for_itself_and_leaves_the_house_alone(
    world: World, tmp_path: Path
) -> None:
    """The point of stepping out: the room still reaches every channel in the list.

    Including the ones past the six a box can hold in its own presets, which is why the list is not
    six long. It arrives over the box's OWN notification socket - a preset press names its number
    there, which is how a waking box dials before it is in any zone - so this path does not depend
    on the box still forwarding anything to us.
    """
    options = _rotation_world(world, tmp_path, numbers=("1", "12", "13"))
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        await _double_tap_key(STUDIO_IP, KeyName.THUMBS_DOWN)
        await eventually(lambda: STUDIO_IP not in _slaves(service), "the studio left the zone")
        await eventually(lambda: world.studio.bodies_for("/select") != [], "and got the house's channel")
        # Waited on what lands LAST: the release puts the box into standby and the /select wakes
        # it, and a press read before the box has said it is playing is a press at a box that is
        # asleep - which is the way back into multiroom, not a number dialled on its own.
        await eventually(lambda: not service.policy.is_asleep(STUDIO_ID), "and said it is playing again")
        on_its_own = len(world.studio.bodies_for("/select"))

        await _press_preset(world.studio, STUDIO_ID, 1)
        await _press_preset(world.studio, STUDIO_ID, 3)

        await eventually(lambda: len(world.studio.bodies_for("/select")) > on_its_own, "it dialled 13 for itself")
        assert f'location="{world.station_url}?c=13"' in world.studio.bodies_for("/select")[-1]
        assert _playing(service).endswith("?c=1"), "and the house is still on the channel it was on"

    assert load_state(options.state_file, log=lambda _kind, _text: None).channel != "13", (
        "a box that is not in the zone must not move the number the zone comes back on"
    )


@pytest.mark.parametrize(
    "woken_on",
    [
        pytest.param(1, id="a-preset"),
        # The power key: a box switched on resumes whatever it had last and reports that as
        # preset 0, which is no digit - the second branch of the press, measured 2026-09-20.
        pytest.param(0, id="the-power-key"),
    ],
)
async def test_switching_a_box_that_is_out_back_on_brings_it_into_multiroom(
    world: World, tmp_path: Path, woken_on: int
) -> None:
    """The way back into multiroom is switching the box off and on again (user, 2026-09-24 14:20).

    Not the held thumbs up: a box that is really released reports a key only as an anonymous touch
    (measured 2026-09-24, Question 4), so it can never say which key was held. A wake it can say,
    and a box somebody switches on is a box somebody wants to hear the house.
    """
    options = _rotation_world(world, tmp_path, numbers=("1", "12", "13"))
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        await _double_tap_key(STUDIO_IP, KeyName.THUMBS_DOWN)
        await eventually(lambda: world.studio.echoes >= 1, "the studio is out and playing on its own")
        await asyncio.sleep(2 * options.dial_window_s)
        before = len(joins(world.studio))

        await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=SourceName.STANDBY))
        await _wake_on_preset(world.studio, STUDIO_ID, woken_on)

        await eventually(lambda: len(joins(world.studio)) > before, "switched on, it was taken back into the zone")
        assert STUDIO_IP in _slaves(service)

    assert out_of_multiroom(options) == (), "and the file does not hold it out any more"


async def test_double_tapping_thumbs_up_brings_back_a_box_the_release_did_not_reach(
    world: World, tmp_path: Path
) -> None:
    """A box the ``/removeZoneSlave`` never reached is still our slave, and still forwards its keys.

    That is the one case the held thumbs up still means anything for: a key reaches the master only
    as a ``/slaveMsg`` from a SLAVE, and a box that is really released sends none (measured
    2026-09-24). So the box here refuses the release, the way one dropping off the network does.
    """
    options = _rotation_world(world, tmp_path, numbers=("1", "12", "13"))
    world.studio.refuse = frozenset({"POST /removeZoneSlave"})
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        await _double_tap_key(STUDIO_IP, KeyName.THUMBS_DOWN)
        await eventually(lambda: any("removeZoneSlave" in line and "failed" in line for line in logs), "the release")
        assert world.studio.zone_master == MASTER_ID, "so it is still our slave"
        await eventually(lambda: STUDIO_IP not in _slaves(service), "the studio left the zone")
        before = len(joins(world.studio))

        await _double_tap_key(STUDIO_IP, KeyName.THUMBS_UP)

        await eventually(lambda: len(joins(world.studio)) > before, "and was taken back into the zone")
        assert STUDIO_IP in _slaves(service)

    assert out_of_multiroom(options) == (), "and the file does not hold it out any more"


async def test_a_hold_is_the_rotation_and_a_double_tap_is_multiroom_on_the_same_key(
    world: World, tmp_path: Path
) -> None:
    """One key, several gestures, told apart by how long it is down and how soon it comes again.

    The pair that matters: a hold must not take the box out of the zone, and a double tap must not
    touch the rotation. Getting either wrong is invisible in a test that only ever sends one of them.
    """
    options = _rotation_world(world, tmp_path, numbers=("1", "12", "13"))
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)

        await _hold_key(STUDIO_IP, KeyName.THUMBS_DOWN)
        await eventually(lambda: _channels_of(options).rotation_numbers() == ("12", "13"), "the hold was the rotation")
        assert STUDIO_IP in _slaves(service), "and left the box in the zone"
        assert out_of_multiroom(options) == ()

        await _double_tap_key(STUDIO_IP, KeyName.THUMBS_DOWN)
        await eventually(lambda: STUDIO_IP not in _slaves(service), "the double tap was multiroom")
        assert _channels_of(options).rotation_numbers() == ("12", "13"), "and left the rotation alone"


# --- House volume: a thumb tapped once, then volume (OPEN-WORK rank 191, user 2026-09-25) -------


def _faded_back(box: FakeSpeaker, level: int) -> bool:
    """Whether a join turned this box down and has since put it back at ``level``."""
    return 0 in box.volumes and box.volumes[-1] == level


def owed_volume(options: ServiceOptions) -> dict[str, int]:
    """What the state file says each box is owed, read the way a restart reads it."""
    return load_state(options.state_file, log=lambda _kind, _text: None).owed_volume


async def test_a_tapped_thumbs_up_then_volume_moves_every_box_in_the_zone_by_the_same_step(
    world: World, tmp_path: Path
) -> None:
    """The user's rule of 2026-09-24: +2 at the studio is +2 in the hallway, from the hallway's own
    level, so a quieter room stays quieter. The remote cannot send two keys at once (measured the same
    evening), so the thumb comes first and the studio's own volume REPORTS are the steps. Since
    2026-09-25 the thumb is TAPPED once; holding it is multiroom and nothing else.

    The first turn happens with no thumb held and must move nobody: it is the studio's own room. It
    is also the barrier for the rest - the reports that follow the thumb are read after it, in order,
    so a hallway that ends two steps up and not three proves the first one was left alone.
    """
    options = _options(world, tmp_path)
    logs: list[str] = []
    world.hallway.volume = 20

    async with _running(options, logs) as service:
        await _both_wake(world)
        await eventually(
            lambda: _faded_back(world.studio, 30) and _faded_back(world.hallway, 20), "both joins faded back up"
        )
        studio_writes = len(world.studio.volumes)

        await world.studio.turn_the_knob(33)

        await _tap_key(STUDIO_IP, KeyName.THUMBS_UP)
        await world.studio.turn_the_knob(35)
        await world.studio.turn_the_knob(37)
        await eventually(lambda: world.hallway.volume == 24, "the hallway moved by the two steps after the thumb")

        assert world.hallway.volumes[-2:] == [22, 24], "one write per step, each from where the last one left it"
        assert len(world.studio.volumes) == studio_writes, "the box the person turned is never written"
        assert world.console.requests == [], "and the Lifestyle console is never called"
        assert STUDIO_IP in _slaves(service), "a tapped thumbs up on a box in the zone leaves it there"
        assert out_of_multiroom(options) == ()


async def _hallway_after(world: World, gesture: Callable[[], Awaitable[None]]) -> int:
    """Turn the studio 30 -> 35 right after ``gesture``, then 35 -> 37 after a single thumbs up tap,
    and answer where the hallway (at 20) ends up.

    The second turn is the barrier and the control at once: it IS a house step, so the hallway moves
    and the wait ends on something that happened rather than on a guessed sleep, and the reports are
    read in order, so a hallway at 22 says the first turn moved nobody while 27 says it moved the
    house. A turn BACK would be no barrier at all: inside the same window it would undo the step it
    was meant to reveal.
    """
    await gesture()
    await world.studio.turn_the_knob(35)
    await _tap_key(STUDIO_IP, KeyName.THUMBS_UP)
    await world.studio.turn_the_knob(37)
    await eventually(lambda: world.hallway.volume != 20, "the control turn after a single tap moved the hallway")
    return world.hallway.volume


async def test_a_tapped_thumbs_down_also_hands_the_box_the_house_volume(world: World, tmp_path: Path) -> None:
    """Either thumb, tapped once, is the house volume (user, 2026-09-25): a single tap no longer
    touches the rotation, so both keys are free for it. Only the first turn after it counts; the
    turn back is inside the renewed window and moves the hallway back with it."""
    options = _options(world, tmp_path)
    logs: list[str] = []
    world.hallway.volume = 20

    async with _running(options, logs):
        await _both_wake(world)
        await eventually(
            lambda: _faded_back(world.studio, 30) and _faded_back(world.hallway, 20), "both joins faded back up"
        )
        await _tap_key(STUDIO_IP, KeyName.THUMBS_DOWN)
        await world.studio.turn_the_knob(35)
        await eventually(lambda: world.hallway.volume == 25, "the hallway moved with the studio")


async def test_a_held_thumbs_up_on_a_box_in_the_zone_hands_it_no_house_volume(world: World, tmp_path: Path) -> None:
    """A hold is the rotation and only the rotation (user, 2026-09-25), so the volume keys after it
    stay that room's own."""
    options = _options(world, tmp_path)
    logs: list[str] = []
    world.hallway.volume = 20

    async with _running(options, logs) as service:
        await _both_wake(world)
        await eventually(
            lambda: _faded_back(world.studio, 30) and _faded_back(world.hallway, 20), "both joins faded back up"
        )
        hallway = await _hallway_after(world, lambda: _hold_key(STUDIO_IP, KeyName.THUMBS_UP))

        assert hallway == 22, "only the turn after the single tap moved the hallway"
        assert any("held THUMBS_UP" in line for line in logs), "and it was read as the hold it was"
        assert STUDIO_IP in _slaves(service)


async def test_a_double_tap_leaves_the_volume_keys_to_the_room(world: World, tmp_path: Path) -> None:
    """A double tap BEGINS as a single tap, which hands the box the house volume at once rather than
    after a wait. The second tap takes it back, so a room that changes the rotation and then turns
    its own knob moves nobody else."""
    options = _rotation_world(world, tmp_path, numbers=("1", "12", "13"))
    logs: list[str] = []
    world.hallway.volume = 20

    async with _running(options, logs):
        await _both_wake(world)
        await eventually(
            lambda: _faded_back(world.studio, 30) and _faded_back(world.hallway, 20), "both joins faded back up"
        )
        hallway = await _hallway_after(world, lambda: _double_tap_key(STUDIO_IP, KeyName.THUMBS_UP))

        assert hallway == 22, "only the turn after the single tap moved the hallway"
        assert any("thumbed up twice" in line for line in logs), "and the double tap was read as one"


async def test_a_box_that_was_off_takes_the_step_it_missed_when_it_joins(world: World, tmp_path: Path) -> None:
    """A box in standby is never written - that could wake it - so it is OWED the step, across a
    restart, and its join fades it back up to its own level plus what it missed."""
    options = _options(world, tmp_path)
    logs: list[str] = []
    world.hallway.volume = 20

    async with _running(options, logs):
        await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
        await eventually(lambda: _faded_back(world.studio, 30), "the studio joined and faded back up")

        await _tap_key(STUDIO_IP, KeyName.THUMBS_UP)
        await world.studio.turn_the_knob(35)
        await eventually(lambda: owed_volume(options) == {HALLWAY_ID: 5}, "the hallway is owed the step")
        assert world.hallway.volumes == [], "a box that is off is never written"

    async with _running(options, logs):
        await world.hallway.notify(now_playing_frame(device_id=HALLWAY_ID, source=RADIO))
        await eventually(lambda: _faded_back(world.hallway, 25), "the hallway joined at its level plus the step")
        assert owed_volume(options) == {}, "and owes nothing any more"


async def test_a_box_owed_below_zero_joins_silent_and_is_not_faded(world: World, tmp_path: Path) -> None:
    """The house was turned right down while this box was off. It joins at zero, where the house is,
    rather than at a level it could not have been stepped to."""
    options = _options(world, tmp_path)
    logs: list[str] = []
    world.hallway.volume = 20
    save_state(options.state_file, ZoneState(owed_volume={HALLWAY_ID: -30}))

    async with _running(options, logs):
        await world.hallway.notify(now_playing_frame(device_id=HALLWAY_ID, source=RADIO))
        await eventually(lambda: len(joins(world.hallway)) == 1, "the hallway joined")
        await eventually(lambda: owed_volume(options) == {}, "what it owed was taken")
        await asyncio.sleep(MUTE_HOLD_S + FADE_S + 0.3)

        assert world.hallway.volumes == [0], "turned down once and never faded back up"


async def test_a_box_still_fading_in_ends_at_the_level_the_house_moved_it_to(world: World, tmp_path: Path) -> None:
    """A step that lands while a box is fading in moves the fade's TARGET rather than writing over
    the fade. The level it was fading to is then never written at all, which is what separates this
    from a fade that finished first and a write that followed it."""
    options = _options(world, tmp_path)
    logs: list[str] = []
    world.hallway.volume = 20
    world.hallway.slow["POST /volume"] = 0.15

    async with _running(options, logs):
        await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
        await eventually(lambda: _faded_back(world.studio, 30), "the studio joined and faded back up")
        await world.hallway.notify(now_playing_frame(device_id=HALLWAY_ID, source=RADIO))
        await eventually(lambda: len(joins(world.hallway)) == 1, "the hallway joined and is fading in")

        await _tap_key(STUDIO_IP, KeyName.THUMBS_UP)
        await world.studio.turn_the_knob(35)
        await eventually(lambda: _faded_back(world.hallway, 25), "the fade ended at the moved level")

        assert 20 not in world.hallway.volumes, "the level it was fading to before the step was never written"
        assert world.hallway.volumes[-2] > 20, (
            "and the climb itself was aimed at the moved level, not a jump at the end"
        )


async def test_a_box_fading_in_does_not_step_the_house_with_the_levels_we_set_it_to(
    world: World, tmp_path: Path
) -> None:
    """A box somebody just switched on is faded up by US, and it reports every level we set. With
    its thumbs up tapped in that moment, those reports must not come back as steps nobody made."""
    options = _options(world, tmp_path)
    logs: list[str] = []
    world.hallway.slow["POST /volume"] = 0.15

    async with _running(options, logs):
        await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
        await eventually(lambda: _faded_back(world.studio, 30), "the studio joined and faded back up")
        studio_writes = len(world.studio.volumes)
        await world.hallway.notify(now_playing_frame(device_id=HALLWAY_ID, source=RADIO))
        await eventually(lambda: len(joins(world.hallway)) == 1, "the hallway joined and is fading in")

        await _tap_key(HALLWAY_IP, KeyName.THUMBS_UP)
        await eventually(lambda: _faded_back(world.hallway, 30), "the hallway's fade finished")

        assert len(world.studio.volumes) == studio_writes, "the fade of one box moved no other box"


# --- M2: calibrating the dialling window on the person who dials ---------------------------------


async def _gesture(from_ip: str) -> None:
    """Next, previous, next, previous - the four presses that start a calibration.

    Alternating, so they sum to zero steps and nothing plays differently. Four TAPS rather than
    four presses: a person performing this lets go of each key, and since the gesture rule reads a
    key still down at the hold threshold as a HOLD, four presses with no releases are four held keys and
    not a gesture at all. That is not a quirk of the test - it is what the house would do to
    somebody who leant on the button - and the helper has to press the way a person does.
    """
    for key in (KeyName.NEXT_TRACK, KeyName.PREV_TRACK, KeyName.NEXT_TRACK, KeyName.PREV_TRACK):
        await _tap_key(from_ip, key)


async def test_the_gesture_starts_a_calibration_and_changes_no_channel(world: World, tmp_path: Path) -> None:
    """The whole point of this shape: it can be performed in the room without interrupting anyone."""
    options = _dialable_world(world, tmp_path, dial_window_s=0.5)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        assert _playing(service).endswith("?c=1")

        await _gesture(STUDIO_IP)
        await eventually(lambda: any("calibration" in line for line in logs), "it said a calibration began")
        await asyncio.sleep(1.0)  # two dialling windows: long enough for a stray jump to have acted

        assert _playing(service).endswith("?c=1"), "and the zone is still where it was"


async def test_a_digit_during_a_calibration_is_a_sample_rather_than_a_channel(world: World, tmp_path: Path) -> None:
    """The presses being measured must not dial, or the measurement would change the house."""
    options = _dialable_world(world, tmp_path, dial_window_s=0.5)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        await _gesture(STUDIO_IP)
        await eventually(lambda: any("calibration" in line for line in logs), "the calibration began")

        for n in range(5):
            await _press_preset(world.studio, STUDIO_ID, 1 + n % 2)
            await asyncio.sleep(0.3)
        await eventually(
            lambda: any("the window becomes" in line for line in logs), "it read what was pressed", timeout=12.0
        )

        assert _playing(service).endswith("?c=1"), "and nothing was dialled"

    assert not any("dialled" in line for line in logs), "the digits never reached the dialler"
    # Compared with what the calibration SAID, not with a fixed number: the window is measured on
    # the real gaps between the presses, and a slow machine stretches them (0.6 s, not 0.5 s, on
    # one starved cpu). What this test claims is that the measurement is the value stored.
    said = next(line for line in logs if "the window becomes" in line)
    measured = float(said.rsplit("the window becomes ", 1)[1].split(" s")[0])
    written = load_state(options.state_file, log=lambda _kind, _text: None)
    assert written.dial_window_s == pytest.approx(measured, abs=0.05), "and what it measured was written down"


async def test_a_key_a_calibration_swallows_is_said_by_name(world: World, tmp_path: Path) -> None:
    """Silence is right for the calibration and wrong for the person pressing.

    Measured 2026-09-20 at 22:37: two held thumbs fell into a running calibration, did nothing and
    were mentioned nowhere, so whoever stood in the room had no way to know why and kept pressing.
    """
    options = _dialable_world(world, tmp_path, dial_window_s=0.5)
    logs: list[str] = []

    async with _running(options, logs):
        await _both_wake(world)
        await _gesture(STUDIO_IP)
        await eventually(lambda: any("calibration started" in line for line in logs), "the calibration began")

        await _tap_key(STUDIO_IP, KeyName.THUMBS_UP)
        await eventually(
            lambda: any("THUMBS_UP does nothing while a calibration runs" in line for line in logs),
            "the swallowed key was said by name",
        )


async def test_a_calibrated_window_is_what_a_restart_dials_with(world: World, tmp_path: Path) -> None:
    """It outlives the run that measured it, which is the only reason to write it down.

    Proved by DIALLING with it rather than by the line the start writes. That line is written
    straight from the state file, so it says the window was read whether or not anything dials
    with it: the version of this that asserted the line alone stayed green with the assignment to
    the dialler deleted, and nothing else in the suite covered it.

    The two presses are 0.9 s apart, which is longer than the option this run was given and
    shorter than what the file remembers. On the calibrated 1.5 s they are one number, 12, which
    is a channel this house has; on the option's 0.5 s they are the numbers 1 and 2, and 2 is no
    channel at all, so the zone would sit where it started.
    """
    options = _dialable_world(world, tmp_path, dial_window_s=0.5)
    save_state(options.state_file, ZoneState(channel="1", members=(), dial_window_s=1.5))
    logs: list[str] = []

    async with _running(options, logs) as service:
        await eventually(lambda: any("1.5" in line and "window" in line for line in logs), "it started on 1.5 s")
        await _both_wake(world)
        assert _playing(service).endswith("?c=1"), "it starts on the lowest channel"

        await _press_preset(world.studio, STUDIO_ID, 1)
        await asyncio.sleep(0.9)
        await _press_preset(world.studio, STUDIO_ID, 2)
        await eventually(
            lambda: _playing(service).endswith("?c=12"),
            "the two presses were read as one number, which only the calibrated window makes them",
        )


async def _press_for(from_ip: str, key: str, *, seconds: float) -> None:
    """A key held down for exactly ``seconds`` and let go, in real time."""
    await _forward_key(from_ip, key, KeyState.PRESS)
    await asyncio.sleep(seconds)
    await _forward_key(from_ip, key, KeyState.RELEASE)


async def test_a_tap_slower_than_the_dialling_window_is_still_a_tap(world: World, tmp_path: Path) -> None:
    """OPEN-WORK rank 188: the window and the hold threshold are two numbers (user, 2026-09-24).

    The window measures the pause BETWEEN two keys and the threshold how long ONE key is down. With
    one number for both, the 685 ms this house's hand held an ordinary tap on 2026-09-21 was read as
    a hold against a 0.6 s window. Here the window is 0.5 s and the thumb is down 0.75 s, twice:
    under the old rule two holds, which take the playing channel out of the rotation; under the
    default threshold of 1.0 s a double tap, which takes the box out of multiroom. The box leaving
    is the positive half - asserting only that the rotation stayed put would pass for a press nobody
    read at all.
    """
    options = _rotation_world(world, tmp_path, numbers=("1", "12", "13"))
    assert options.dial_window_s == pytest.approx(0.5)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        assert _playing(service).endswith("?c=1")

        await _press_for(STUDIO_IP, KeyName.THUMBS_DOWN, seconds=0.75)
        await _press_for(STUDIO_IP, KeyName.THUMBS_DOWN, seconds=0.75)
        await eventually(lambda: out_of_multiroom(options) == (STUDIO_ID,), "it was read as a double tap")
        await eventually(lambda: STUDIO_IP not in _slaves(service), "and the box left the zone")

    assert _channels_of(options).rotation_numbers() == ("1", "12", "13"), "the rotation was left alone"


async def test_a_calibration_writes_the_hold_threshold_it_measured(world: World, tmp_path: Path) -> None:
    """The same presses give both numbers, and the hold is written down beside the window.

    Each digit is held 0.9 s, so the key-down time asks for 1.2 s, above the 1.0 s floor, while the
    pauses between them still ask for the window's floor - two different answers, which is the
    point: one number could not have been both. Not longer than 0.9 s: a preset's two touches are
    one press only up to ``presses.HOLD_CEILING_S`` apart, so a longer digit is no sample at all.
    """
    options = _dialable_world(world, tmp_path, dial_window_s=0.5)
    logs: list[str] = []

    async with _running(options, logs):
        await _both_wake(world)
        await _gesture(STUDIO_IP)
        await eventually(lambda: any("calibration" in line for line in logs), "the calibration began")

        for n in range(4):
            await _press_preset(world.studio, STUDIO_ID, 1 + n % 2, hold_s=0.9)
            await asyncio.sleep(0.3)
        await eventually(lambda: any("the hold becomes" in line for line in logs), "it read both", timeout=15.0)

    said = next(line for line in logs if "the hold becomes" in line)
    measured = float(said.rsplit("the hold becomes ", 1)[1].split(" s")[0])
    assert measured > HOLD_THRESHOLD_DEFAULT_S, "a hand that rests on its keys gets a longer threshold"
    written = load_state(options.state_file, log=lambda _kind, _text: None)
    assert written.hold_threshold_s == pytest.approx(measured), "and what it measured was written down"


async def test_a_calibrated_hold_threshold_is_what_a_restart_holds_with(world: World, tmp_path: Path) -> None:
    """It outlives the run that measured it. Proved by PRESSING, not by the startup line.

    The thumb is down 1.3 s, twice: past the default threshold of 1.0 s, so without the remembered
    1.8 s it would be two holds and move the rotation; with it, it is a double tap and takes the box
    out of multiroom instead.
    """
    options = _rotation_world(world, tmp_path, numbers=("1", "12", "13"))
    save_state(options.state_file, ZoneState(channel="1", members=(), dial_window_s=0.5, hold_threshold_s=1.8))
    logs: list[str] = []

    async with _running(options, logs) as service:
        await eventually(lambda: any("a key is held after 1.8 s" in line for line in logs), "it started on 1.8 s")
        await _both_wake(world)

        await _press_for(STUDIO_IP, KeyName.THUMBS_DOWN, seconds=1.3)
        await _press_for(STUDIO_IP, KeyName.THUMBS_DOWN, seconds=1.3)
        await eventually(lambda: out_of_multiroom(options) == (STUDIO_ID,), "it was read as a double tap")
        await eventually(lambda: STUDIO_IP not in _slaves(service), "and the box left the zone")

    assert _channels_of(options).rotation_numbers() == ("1", "12", "13"), "the rotation was left alone"


async def test_a_box_left_on_an_old_stream_is_put_back_by_the_next_pass(world: World, tmp_path: Path) -> None:
    """The safety net over rank 22, proved through the PASS rather than by calling the master.

    A station change is sent on ONE channel per box - twice would tell the box to stop the stream
    it has just been started on - so a superseded channel keeps the station it was told about. If
    the driven channel then ends, the box is driven on a channel the zone has left behind, and it
    plays that old stream until the stream stops: silence, with the new station's name on the
    display, which is what a person in the room notices first.

    The two channels are opened by the TEST and not by ``FakeSpeaker``, because a real box holds
    several at one address and the whole question is which of them the master drives.
    """
    options = _dialable_world(world, tmp_path, dial_window_s=0.5)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        assert _playing(service).endswith("?c=1"), "it starts on the lowest channel"

        older_reader, older_writer = await open_transport(MASTER)
        _newer_reader, newer_writer = await open_transport(MASTER)

        # The zone moves. Only the driven channel - the newer one - is told.
        await _press_preset(world.studio, STUDIO_ID, 1)
        await _press_preset(world.studio, STUDIO_ID, 2)
        await eventually(lambda: _playing(service).endswith("?c=12"), "the zone moved to channel 12")

        # And now the driven channel ends, which leaves the box driven on the older one.
        newer_writer.close()
        with contextlib.suppress(ConnectionError):
            await newer_writer.wait_closed()
        await eventually(
            lambda: any("driven channel ended" in line for line in logs),
            "the master noticed which stream the box is left on",
        )

        # Nobody calls anything here. The pass finds it and puts it back: STOP for the stream it
        # was left on, then the sequence onto the one the zone is playing.
        await eventually(
            lambda: any("was left on an old stream and was put back" in line for line in logs),
            "the pass put it back",
        )
        frames = await read_frames_but_not_pings(older_reader, 4)
        assert [f.typename for f in frames] == [
            "AudioServerMsgTransportControl",
            "AudioServerMsgSetURL",
            "AudioServerMsgTransportControl",
            "AudioServerMsgTransportControl",
        ], "the switch sequence, on the channel that was left behind"
        play = frames[3].payload_as(audio.AudioServerMsgTransportControl)
        assert play.control == audio.AudioServerMsgTransportControl.PLAY
        assert service.master is not None and service.master.station is not None
        assert play.url_id == service.master.station.url_id, "and onto the stream the zone is playing"
        older_writer.close()


async def test_a_box_that_hangs_up_in_the_middle_of_being_put_back_costs_the_house_nothing(
    world: World, tmp_path: Path
) -> None:
    """A channel that closes inside the put-back's own pause ends that channel, not the service.

    The crash of 2026-09-24 14:05:26 (OPEN-WORK rank 171), exactly: the pass put a box that had been
    left on an old stream back on the station, the first three messages of the switch went out, the
    box closed its channel during the one-second pause before PLAY, and the PLAY's ``drain`` raised
    ``ConnectionResetError`` through the pass and out of the service. systemd restarted it, and the
    house had no zone for ten seconds because one box hung up at the wrong moment.
    """
    options = _dialable_world(world, tmp_path, dial_window_s=0.5)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        older_reader, older_writer = await open_transport(MASTER)
        _newer_reader, newer_writer = await open_transport(MASTER)
        await _press_preset(world.studio, STUDIO_ID, 1)
        await _press_preset(world.studio, STUDIO_ID, 2)
        await eventually(lambda: _playing(service).endswith("?c=12"), "the zone moved to channel 12")
        newer_writer.close()
        with contextlib.suppress(ConnectionError):
            await newer_writer.wait_closed()

        # The put-back starts: STOP, SetURL, PAUSE - and the box hangs up before the PLAY.
        started = await read_frames_but_not_pings(older_reader, 3)
        assert [f.typename for f in started][1] == "AudioServerMsgSetURL", "the put-back was under way"
        older_writer.close()
        with contextlib.suppress(ConnectionError):
            await older_writer.wait_closed()

        # The house is still held: a press after the hang-up still moves the whole zone.
        await asyncio.sleep(1.5)
        await _press_preset(world.studio, STUDIO_ID, 1)
        await eventually(lambda: _playing(service).endswith("?c=1"), "the zone still answers a press")
        # And the hang-up really landed inside the switch, rather than before it began or after
        # its PLAY - either of which would pass the press above without the defect ever arising.
        assert any("gone in the middle of a station change" in line for line in logs)


# --- M3a: a channel whose sound comes from a stored MPD playlist ---------------------------------


@asynccontextmanager
async def _mpd(
    *,
    refuse: dict[str, str] | None = None,
    status_lines: list[str] | None = None,
    directories: dict[str, list[str]] | None = None,
    playlists: dict[str, list[str]] | None = None,
) -> AsyncGenerator[FakeMpd, None]:
    """An MPD on loopback for the life of one test, stopped however the test leaves it.

    It answers the shapes measured on 2026-09-10 and records every command line, which is what
    these tests assert on: what the service ASKED MPD is the contract, and a fake that answers OK
    to anything would answer OK to a misspelled verb too.
    """
    fake = FakeMpd(refuse=refuse, status_lines=status_lines, directories=directories, playlists=playlists)
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


def _with_mpd(options: ServiceOptions, fake: FakeMpd) -> ServiceOptions:
    """The same service, told where its MPD is. In the house it is the loopback and the default."""
    return replace(options, mpd_host=MPD_HOST, mpd_port=fake.port)


async def test_an_mpd_channel_is_loaded_before_the_master_is_pointed_at_the_stream(
    world: World, tmp_path: Path
) -> None:
    """The order is measured, not tidy: MPD's ``httpd`` port does not listen at all until its
    output first opens, so a master pointed at the stream first is refused and backs off - the
    wait doubling to 30 s - and the flat is silent for as long as that lasts.

    The station here stands in for MPD's own ``httpd`` output, which is what lets a test see the
    two events on one clock. What is real is the order, and only a clock can report it: a count of
    either side alone is the same whichever way round they happened.
    """
    async with _mpd() as fake:
        options = _with_mpd(_options(world, tmp_path, seed=True), fake)
        save_channels(
            options.channel_file,
            ChannelList(
                channels=(
                    Channel(
                        number="1",
                        name="Hoerbuecher",
                        kind=ChannelKind.MPD,
                        url=f"{world.station_url}?c=1",
                        mpd_entry="hoerbuecher",
                    ),
                )
            ),
        )
        logs: list[str] = []

        async with _running(options, logs) as service:
            await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
            await eventually(lambda: len(joins(world.studio)) == 1, "the studio was taken into the zone")

            assert _playing(service).endswith("?c=1"), "the zone is on the MPD channel"
            assert fake.seen[:4] == ["clear", "repeat 1", 'load "hoerbuecher"', "play 0"], fake.seen
            assert fake.first_command_at is not None, "mpd was never asked for anything"
            assert world.fetched_at, "the master never fetched the stream"
            assert fake.first_command_at < world.fetched_at[0], (
                "the master was pointed at the stream before mpd had been told to open its output"
            )


async def test_a_dialled_mpd_channel_is_loaded_before_the_house_is_moved_onto_it(world: World, tmp_path: Path) -> None:
    """Dialling is how a person chooses a channel, and it does not go through the pass: it starts
    the stream from its own line. So the MPD half has to sit in the one method both of them end
    in, or the feature works when a box is taken in and not when somebody presses the number.

    Channel 1 is RADIO on purpose. It is the negative control: it proves that what MPD was told
    came from the dial rather than from the house starting up, and that a radio channel still
    asks MPD for nothing at all.
    """
    async with _mpd() as fake:
        options = _with_mpd(_options(world, tmp_path, seed=True, dial_window_s=0.5), fake)
        save_channels(
            options.channel_file,
            ChannelList(
                channels=(
                    Channel(number="1", name="One", kind=ChannelKind.RADIO, url=f"{world.station_url}?c=1"),
                    Channel(
                        number="12",
                        name="Hoerbuecher",
                        kind=ChannelKind.MPD,
                        url=f"{world.station_url}?c=12",
                        mpd_entry="hoerbuecher",
                    ),
                )
            ),
        )
        logs: list[str] = []

        async with _running(options, logs) as service:
            await _both_wake(world)
            assert _playing(service).endswith("?c=1"), "it starts on the lowest channel, which is radio"
            assert fake.seen == [], "a radio channel asks mpd for nothing"

            await _press_preset(world.studio, STUDIO_ID, 1)
            await _press_preset(world.studio, STUDIO_ID, 2)
            await eventually(lambda: _playing(service).endswith("?c=12"), "the zone moved to channel 12")

            assert fake.seen[:4] == ["clear", "repeat 1", 'load "hoerbuecher"', "play 0"], fake.seen
            assert fake.first_command_at is not None, "mpd was never asked for anything"
            assert fake.first_command_at < world.fetched_at[-1], (
                "the house was moved onto the stream before mpd had been told to open its output"
            )


async def test_a_channel_naming_a_playlist_mpd_does_not_have_is_said_by_name_and_costs_no_zone(
    world: World, tmp_path: Path
) -> None:
    """A name MPD does not know is somebody's typo in the channel file, not a fault to die of.

    So it is said by name - the number AND the playlist, which is what an operator needs to find
    the line to fix - and the zone is held all the same. The house then hears one silent channel
    and every other channel, radio included, keeps working.
    """
    async with _mpd(refuse={"load": "No such playlist"}) as fake:
        options = _with_mpd(_options(world, tmp_path, seed=True), fake)
        save_channels(
            options.channel_file,
            ChannelList(
                channels=(
                    Channel(
                        number="1",
                        name="Hoerbuecher",
                        kind=ChannelKind.MPD,
                        url=f"{world.station_url}?c=1",
                        mpd_entry="gone",
                    ),
                )
            ),
        )
        logs: list[str] = []

        async with _running(options, logs) as service:
            await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
            await eventually(lambda: len(joins(world.studio)) == 1, "the studio was taken into the zone")

            assert any("'gone', which mpd does not have" in line for line in logs), logs
            assert any("channel 1 names playlist" in line for line in logs), logs
            assert _playing(service).endswith("?c=1"), "the zone is held regardless"
            assert believed(options) == (STUDIO_ID,)


async def test_a_connection_mpd_closed_is_replaced_before_the_next_exchange_not_written_into(
    world: World, tmp_path: Path
) -> None:
    """A connection MPD closed costs NOTHING, because the next exchange notices before it writes.

    MPD closes an idle control connection after 60 s, and a restart looks the same from here, so
    this is the ordinary case rather than a rare one: the service speaks to MPD only when somebody
    presses something. The client asks whether the connection is still MPD's before sending, so
    the keypress lands on a fresh one and nothing is lost.

    Checking FIRST rather than repairing afterwards is what makes that safe. Once a command's
    bytes are out, one MPD never read is indistinguishable from one it read and ran, and `next` is
    not idempotent - so a version that recovered by retrying would have to choose between losing
    the press and stepping twice.

    The count of CONNECTIONS is the evidence: from inside the protocol the bytes of the second
    connection look exactly like the first.
    """
    async with _mpd() as fake:
        options = _with_mpd(_options(world, tmp_path, seed=True, dial_window_s=0.5), fake)
        save_channels(
            options.channel_file,
            ChannelList(
                channels=(
                    Channel(number="1", name="One", kind=ChannelKind.RADIO, url=f"{world.station_url}?c=1"),
                    Channel(
                        number="12",
                        name="Hoerbuecher",
                        kind=ChannelKind.MPD,
                        url=f"{world.station_url}?c=12",
                        mpd_entry="hoerbuecher",
                    ),
                    Channel(
                        number="13",
                        name="Kindermusik",
                        kind=ChannelKind.MPD,
                        url=f"{world.station_url}?c=13",
                        mpd_entry="kindermusik",
                    ),
                )
            ),
        )
        logs: list[str] = []

        async with _running(options, logs) as service:
            await _both_wake(world)
            assert _playing(service).endswith("?c=1"), "it starts on the lowest channel, which is radio"

            await _press_preset(world.studio, STUDIO_ID, 1)
            await _press_preset(world.studio, STUDIO_ID, 2)
            await eventually(lambda: _playing(service).endswith("?c=12"), "the zone moved to channel 12")
            assert fake.connections == 1, "one channel needed one connection"

            # The idle timeout, which from out here is indistinguishable from MPD restarting.
            # Nothing tells the service either way.
            fake.drop_connections()

            await _press_preset(world.studio, STUDIO_ID, 1)
            await _press_preset(world.studio, STUDIO_ID, 3)
            await eventually(lambda: _playing(service).endswith("?c=13"), "the zone moved to channel 13")
            await eventually(lambda: fake.connections == 2, "a fresh connection was opened for it")
            assert fake.seen[-4:] == ["clear", "repeat 1", 'load "kindermusik"', "play 0"], fake.seen
            assert not any("did not take it" in line for line in logs), (
                "a connection mpd had already closed must be replaced before the exchange, not failed over"
            )


async def test_dialling_a_second_mpd_channel_moves_the_house_although_both_name_one_url(
    world: World, tmp_path: Path
) -> None:
    """Every MPD channel in the house names the SAME url, and it is not a mistake in the file.

    MPD has one ``httpd`` output, so what distinguishes two of its channels is the playlist they
    name and never the address the stream is fetched from. The channels above this one are
    written with an url apiece, which no real channel file has, and that is what let a guard
    comparing URLs pass every test while refusing the only move a person makes on an audiobook:
    measured in the flat 2026-09-21, eleven dials in a row, where going from one book to another
    answered "already playing it" and did nothing, and the only way through was a radio channel
    in between.

    What proves the house moved is what MPD was told and what the STATION saw. The url cannot
    say it - it is the same either way, which is the whole point.
    """
    async with _mpd() as fake:
        options = _with_mpd(_options(world, tmp_path, seed=True, dial_window_s=0.5), fake)
        one_output = f"{world.station_url}?c=mpd"
        save_channels(
            options.channel_file,
            ChannelList(
                channels=(
                    Channel(number="1", name="One", kind=ChannelKind.RADIO, url=f"{world.station_url}?c=1"),
                    Channel(
                        number="12",
                        name="Hoerbuecher",
                        kind=ChannelKind.MPD,
                        url=one_output,
                        mpd_entry="hoerbuecher",
                    ),
                    Channel(
                        number="13",
                        name="Kindermusik",
                        kind=ChannelKind.MPD,
                        url=one_output,
                        mpd_entry="kindermusik",
                    ),
                )
            ),
        )
        logs: list[str] = []

        async with _running(options, logs) as service:
            await _both_wake(world)
            assert _playing(service).endswith("?c=1"), "it starts on the lowest channel, which is radio"

            await _press_preset(world.studio, STUDIO_ID, 1)
            await _press_preset(world.studio, STUDIO_ID, 2)
            await eventually(
                lambda: fake.seen[-4:] == ["clear", "repeat 1", 'load "hoerbuecher"', "play 0"], "the first book"
            )
            fetched = len(world.fetches)

            await _press_preset(world.studio, STUDIO_ID, 1)
            await _press_preset(world.studio, STUDIO_ID, 3)
            await eventually(
                lambda: fake.seen[-4:] == ["clear", "repeat 1", 'load "kindermusik"', "play 0"],
                "the house moved to the other book",
            )
            await eventually(lambda: len(world.fetches) > fetched, "and the zone was moved onto it, not only mpd")
            assert not any("already playing it" in line for line in logs), (
                "two channels that share an url are still two channels"
            )


async def test_an_mpd_that_cannot_be_reached_at_all_is_said_by_name_and_costs_no_zone(
    world: World, tmp_path: Path
) -> None:
    """The other half of the one above, and the reason its assertion could be tightened.

    Replacing a closed connection removes the case where MPD is THERE and the socket is not. It
    does nothing for a daemon that is gone, which is a machine that needs attention rather than
    anything this service can mend - so that one is still said by name, once, and the stream is
    started anyway. The zone is holding every other room on every other channel and is not worth
    a daemon that this house may not even need.

    Without this test the only assertion that the failure path speaks at all would have gone when
    the one above stopped needing it.
    """
    async with _mpd() as fake:
        options = _radio_and_mpd(world, tmp_path, fake)
        logs: list[str] = []

        async with _running(options, logs) as service:
            await _both_wake(world)
            assert _playing(service).endswith("?c=1"), "it starts on the radio channel"

            # Not a closed connection: no daemon at all, so reopening cannot help either.
            await fake.stop()

            await _press_preset(world.studio, STUDIO_ID, 1)
            await _press_preset(world.studio, STUDIO_ID, 2)
            await eventually(lambda: _playing(service).endswith("?c=12"), "the zone moved onto it regardless")
            await eventually(lambda: any("did not take it" in line for line in logs), "it said mpd could not be asked")


def _radio_and_mpd(world: World, tmp_path: Path, fake: FakeMpd, *, end: ChannelEnd = ChannelEnd.WRAP) -> ServiceOptions:
    """A list with one channel of each kind, which is what a step has to choose between."""
    options = _with_mpd(_options(world, tmp_path, seed=True, dial_window_s=0.5), fake)
    save_channels(
        options.channel_file,
        ChannelList(
            channels=(
                Channel(number="1", name="One", kind=ChannelKind.RADIO, url=f"{world.station_url}?c=1"),
                Channel(
                    number="12",
                    name="Hoerbuecher",
                    kind=ChannelKind.MPD,
                    url=f"{world.station_url}?c=12",
                    mpd_entry="hoerbuecher",
                    end=end,
                ),
            )
        ),
    )
    return options


async def test_next_inside_an_mpd_channel_steps_the_file_and_not_the_channel(world: World, tmp_path: Path) -> None:
    """The key stays in the world it is pressed in. On a radio channel next is the next CHANNEL,
    and on a channel that is a playlist it is the next FILE - which is what a person listening to
    an audiobook means by it, and the only reading under which the key is usable there at all.

    The stream does not restart, because it is the same stream: MPD is playing something else
    behind the same httpd URL, so no room pays the 3.5 to 4 s of silence a station change costs.
    """
    async with _mpd(status_lines=PLAYING_AT_61_5) as fake:
        options = _radio_and_mpd(world, tmp_path, fake)
        logs: list[str] = []

        async with _running(options, logs) as service:
            await _both_wake(world)
            await _press_preset(world.studio, STUDIO_ID, 1)
            await _press_preset(world.studio, STUDIO_ID, 2)
            await eventually(lambda: _playing(service).endswith("?c=12"), "the zone is on the MPD channel")
            fetched = len(world.fetches)

            await _tap_key(STUDIO_IP, KeyName.NEXT_TRACK)
            await eventually(lambda: "play 3" in fake.seen, "mpd was asked for the file after the third")

            assert _playing(service).endswith("?c=12"), "the channel did not move"
            assert len(world.fetches) == fetched, "and the stream was not fetched again"


async def test_previous_inside_an_mpd_channel_steps_the_file_backwards(world: World, tmp_path: Path) -> None:
    """The other direction, because a branch written on one key is a branch that reads the other
    one as a channel step and moves the whole house out of the book somebody is listening to."""
    async with _mpd(status_lines=PLAYING_AT_61_5) as fake:
        options = _radio_and_mpd(world, tmp_path, fake)
        logs: list[str] = []

        async with _running(options, logs) as service:
            await _both_wake(world)
            await _press_preset(world.studio, STUDIO_ID, 1)
            await _press_preset(world.studio, STUDIO_ID, 2)
            await eventually(lambda: _playing(service).endswith("?c=12"), "the zone is on the MPD channel")

            await _tap_key(STUDIO_IP, KeyName.PREV_TRACK)
            await eventually(lambda: "play 1" in fake.seen, "mpd was asked for the file before the third")

            assert _playing(service).endswith("?c=12"), "the channel did not move"


async def _on_the_mpd_channel(world: World, service: ZoneService) -> None:
    """Both boxes awake and the zone dialled onto channel 12, the MPD one."""
    await _both_wake(world)
    await _press_preset(world.studio, STUDIO_ID, 1)
    await _press_preset(world.studio, STUDIO_ID, 2)
    await eventually(lambda: _playing(service).endswith("?c=12"), "the zone is on the MPD channel")


ON_THE_LAST_OF_5 = ["state: play", "song: 4", "elapsed: 10.000", "duration: 900.000", "playlistlength: 5"]
ON_THE_FIRST_OF_5 = ["state: play", "song: 0", "elapsed: 10.000", "duration: 900.000", "playlistlength: 5"]
RAN_OUT_WITH_5 = ["state: stop", "playlistlength: 5"]
"""What a real MPD 0.24.6 reports at the natural end with ``repeat`` off: stopped, queue kept
(``test_mpdclient.py``, against the binary)."""


@pytest.mark.parametrize("end", [ChannelEnd.WRAP, ChannelEnd.STOP])
async def test_next_on_the_last_file_goes_to_the_first(world: World, tmp_path: Path, end: ChannelEnd) -> None:
    """A press past the end wraps on EVERY channel (user, 2026-09-24). Before, it was a bare MPD
    ``next``, which stops at the end of the queue with ``repeat`` off, and the house went silent."""
    async with _mpd(status_lines=ON_THE_LAST_OF_5) as fake:
        options = _radio_and_mpd(world, tmp_path, fake, end=end)
        async with _running(options, []) as service:
            await _on_the_mpd_channel(world, service)
            loaded = len(fake.seen)
            await _tap_key(STUDIO_IP, KeyName.NEXT_TRACK)
            await eventually(lambda: "play 0" in fake.seen[loaded:], "mpd was asked for the first file")


@pytest.mark.parametrize("end", [ChannelEnd.WRAP, ChannelEnd.STOP])
async def test_previous_on_the_first_file_goes_to_the_last(world: World, tmp_path: Path, end: ChannelEnd) -> None:
    async with _mpd(status_lines=ON_THE_FIRST_OF_5) as fake:
        options = _radio_and_mpd(world, tmp_path, fake, end=end)
        async with _running(options, []) as service:
            await _on_the_mpd_channel(world, service)
            await _tap_key(STUDIO_IP, KeyName.PREV_TRACK)
            await eventually(lambda: "play 4" in fake.seen, "mpd was asked for the last file")


async def test_next_after_a_stop_channel_ran_out_starts_it_again(world: World, tmp_path: Path) -> None:
    """Somebody still in the room after the book ended presses next: the first file, not silence."""
    async with _mpd(status_lines=RAN_OUT_WITH_5) as fake:
        options = _radio_and_mpd(world, tmp_path, fake, end=ChannelEnd.STOP)
        async with _running(options, []) as service:
            await _on_the_mpd_channel(world, service)
            loaded = len(fake.seen)
            await _tap_key(STUDIO_IP, KeyName.NEXT_TRACK)
            await eventually(lambda: "play 0" in fake.seen[loaded:], "mpd was asked for the first file")


@pytest.mark.parametrize(("end", "repeat"), [(ChannelEnd.WRAP, "repeat 1"), (ChannelEnd.STOP, "repeat 0")])
async def test_a_channel_is_loaded_with_the_repeat_its_end_asks_for(
    world: World, tmp_path: Path, end: ChannelEnd, repeat: str
) -> None:
    async with _mpd() as fake:
        options = _radio_and_mpd(world, tmp_path, fake, end=end)
        async with _running(options, []) as service:
            await _on_the_mpd_channel(world, service)
            assert repeat in fake.seen


async def test_leaving_a_stop_channel_that_ran_out_forgets_the_place_so_it_starts_again(
    world: World, tmp_path: Path
) -> None:
    """The book played to its end. Coming back to the place written down BEFORE that would play a
    few seconds near the end and fall silent again, so the place is forgotten (user, 2026-09-24)."""
    async with _mpd(status_lines=PLAYING_AT_61_5) as fake:
        options = _radio_and_mpd(world, tmp_path, fake, end=ChannelEnd.STOP)
        async with _running(options, []) as service:
            await _on_the_mpd_channel(world, service)
            await _press_preset(world.studio, STUDIO_ID, 1)
            await eventually(lambda: _playing(service).endswith("?c=1"), "the house left the book")
            assert positions_of(options) == {"12": AT_61_5}, "precondition: a real place was written down"

            await _press_preset(world.studio, STUDIO_ID, 1)
            await _press_preset(world.studio, STUDIO_ID, 2)
            await eventually(lambda: _playing(service).endswith("?c=12"), "the house came back to the book")
            fake.status_lines = list(RAN_OUT_WITH_5)
            await _press_preset(world.studio, STUDIO_ID, 1)
            await eventually(lambda: _playing(service).endswith("?c=1"), "and left it after it ran out")
            await eventually(lambda: positions_of(options) == {}, "the place is forgotten")

            before = len(fake.seen)
            await _press_preset(world.studio, STUDIO_ID, 1)
            await _press_preset(world.studio, STUDIO_ID, 2)
            await eventually(lambda: _playing(service).endswith("?c=12"), "the house came back again")
            assert "play 0" in fake.seen[before:], "and the book starts from the beginning"


async def test_leaving_a_wrap_channel_that_says_it_stopped_keeps_the_place(world: World, tmp_path: Path) -> None:
    """The control for the one above: only a ``stop`` channel forgets. A ``wrap`` channel never runs
    out, so a stopped MPD with a queue there is something else, and a real place is worth more."""
    async with _mpd(status_lines=PLAYING_AT_61_5) as fake:
        options = _radio_and_mpd(world, tmp_path, fake, end=ChannelEnd.WRAP)
        async with _running(options, []) as service:
            await _on_the_mpd_channel(world, service)
            await _press_preset(world.studio, STUDIO_ID, 1)
            await eventually(lambda: _playing(service).endswith("?c=1"), "the house left the channel")
            assert positions_of(options) == {"12": AT_61_5}, "precondition: a real place was written down"

            await _press_preset(world.studio, STUDIO_ID, 1)
            await _press_preset(world.studio, STUDIO_ID, 2)
            await eventually(lambda: _playing(service).endswith("?c=12"), "the house came back")
            fake.status_lines = list(RAN_OUT_WITH_5)
            await _press_preset(world.studio, STUDIO_ID, 1)
            await eventually(lambda: _playing(service).endswith("?c=1"), "and left it again")

            assert positions_of(options) == {"12": AT_61_5}, "the real place is still there"


async def test_next_on_a_radio_channel_still_steps_the_channel(world: World, tmp_path: Path) -> None:
    """The control, and the one that matters: the list HOLDS an MPD channel, so a branch written
    too wide would swallow this step and the rotation would stop working for the radio channels
    as well. It passed before the branch existed and must go on passing after it."""
    async with _mpd(status_lines=PLAYING_AT_61_5) as fake:
        options = _radio_and_mpd(world, tmp_path, fake)
        logs: list[str] = []

        async with _running(options, logs) as service:
            await _both_wake(world)
            assert _playing(service).endswith("?c=1"), "it starts on the lowest channel, which is radio"

            await _tap_key(STUDIO_IP, KeyName.NEXT_TRACK)
            await eventually(lambda: _playing(service).endswith("?c=12"), "one press stepped one channel")

            assert not [line for line in fake.seen if line.startswith("play ") and line != "play 0"], (
                "a step on a radio channel is a CHANNEL step, not a file one"
            )


PLAYING_AT_61_5 = ["state: play", "song: 2", "elapsed: 61.500", "duration: 900.000", "playlistlength: 5"]
"""An MPD an hour into a book, in the shape a real one answers ``status`` with."""


def positions_of(options: ServiceOptions) -> dict[str, Place]:
    """Where in each MPD channel the state file says the house got to, read as a restart reads it."""
    return load_state(options.state_file, log=lambda _kind, _text: None).positions


AT_61_5 = Place(track=2, seconds=61.5)
"""The place :data:`PLAYING_AT_61_5` describes: its third file, a minute in."""


async def test_a_step_after_a_dropped_connection_reopens_instead_of_reporting_nothing_to_step(
    world: World, tmp_path: Path
) -> None:
    """A connection MPD closed must cost one keypress, never the feature.

    Measured in the flat on 2026-09-20, and this test is the one that was missing. MPD closes an
    idle control connection after 60 s and the service speaks to it only when somebody presses
    something, so the first step after a quiet minute failed - which is right, and the connection
    was thrown away, which is also right. What followed was not: every later step answered
    "nothing has reached mpd yet, so there is no file to step" and did nothing, because a
    connection thrown away and one never opened are the same ``None``. Three presses in a row
    changed nothing, the tracks that did change were MPD's queue running on by itself, and the
    sentence in the log was untrue.

    ``test_a_connection_mpd_dropped_is_thrown_away_rather_than_used_again`` does not cover this
    and its name does not say so: it recovers by DIALLING, which goes through ``_put_mpd_on``, the
    one path that reopens. The step path is reached only by a key.

    The count of CONNECTIONS is the evidence, as it is there: from inside the protocol the bytes
    of the second connection look exactly like the first.
    """
    async with _mpd(status_lines=PLAYING_AT_61_5) as fake:
        options = _radio_and_mpd(world, tmp_path, fake)
        logs: list[str] = []

        async with _running(options, logs) as service:
            await _both_wake(world)
            await _press_preset(world.studio, STUDIO_ID, 1)
            await _press_preset(world.studio, STUDIO_ID, 2)
            await eventually(lambda: _playing(service).endswith("?c=12"), "the zone is on the MPD channel")

            await _tap_key(STUDIO_IP, KeyName.NEXT_TRACK)
            await eventually(lambda: fake.seen.count("play 3") == 1, "the first step reached mpd")
            assert fake.connections == 1, "one channel and one step needed one connection"

            # The quiet minute, which from out here is MPD closing the connection under us.
            fake.drop_connections()

            await _tap_key(STUDIO_IP, KeyName.NEXT_TRACK)
            await eventually(lambda: fake.seen.count("play 3") == 2, "the step after it reached mpd too")
            await eventually(lambda: fake.connections == 2, "a fresh connection was opened for the step")

            assert _playing(service).endswith("?c=12"), "and the channel still did not move"
            assert not any("nothing has reached mpd yet" in line for line in logs), (
                "a channel whose playlist mpd is holding must never be reported as one it never got"
            )


async def test_leaving_an_mpd_channel_writes_down_how_far_into_it_the_house_got(world: World, tmp_path: Path) -> None:
    """MPD keeps no position per stored playlist - measured 2026-09-10, coming back to one gives
    song, elapsed and state as nothing, nothing and stop - so a book nobody writes down is a book
    that starts again from the beginning."""
    async with _mpd(status_lines=PLAYING_AT_61_5) as fake:
        options = _radio_and_mpd(world, tmp_path, fake)
        logs: list[str] = []

        async with _running(options, logs) as service:
            await _both_wake(world)
            await _press_preset(world.studio, STUDIO_ID, 1)
            await _press_preset(world.studio, STUDIO_ID, 2)
            await eventually(lambda: _playing(service).endswith("?c=12"), "the zone is on the MPD channel")

            await _press_preset(world.studio, STUDIO_ID, 1)
            await eventually(lambda: _playing(service).endswith("?c=1"), "the house left it for the radio one")
            await eventually(lambda: positions_of(options) == {"12": AT_61_5}, "the position was written down")


async def test_the_position_survives_a_restart_and_the_book_goes_on_where_it_stopped(
    world: World, tmp_path: Path
) -> None:
    """The whole of Task 5 from the outside: a run ends, another starts, and the book goes on.

    The resume is ONE ``seek``, naming the entry and the offset together. It was a command list
    of ``play`` then ``seekcur`` until 2026-09-21, when that sequence was measured segfaulting
    MPD 0.24.6 on a container host - see ``adapters/mpd/client.py`` and the upstream
    issue it names. A single ``seek`` cannot race the decoder, and it is still one exchange, so
    the flat never hears the top of the track.
    """
    async with _mpd(status_lines=PLAYING_AT_61_5) as fake:
        options = _radio_and_mpd(world, tmp_path, fake)
        logs: list[str] = []

        async with _running(options, logs) as first:
            await _both_wake(world)
            await _press_preset(world.studio, STUDIO_ID, 1)
            await _press_preset(world.studio, STUDIO_ID, 2)
            await eventually(lambda: _playing(first).endswith("?c=12"), "the zone is on the MPD channel")

        # The run has ended, so nothing is still on its way: the stand-down is in run()'s finally.
        assert positions_of(options) == {"12": AT_61_5}, "the stand-down wrote it down"
        before, joined = len(fake.seen), len(joins(world.studio))

        async with _running(options, logs) as second:
            await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
            # The JOIN is the later of the two: the pass asks MPD and then hands the box the zone
            # document, so a wait on what mpd was told would assert on something still on its way.
            await eventually(lambda: len(joins(world.studio)) > joined, "the studio was taken in by the NEW run")

            assert _playing(second).endswith("?c=12"), "the new run came back on the MPD channel"
            assert "seek 2 41.500" in fake.seen[before:], "and asked mpd for that file, a little before where it was"
            assert not [line for line in fake.seen[before:] if line.startswith("seekcur")], (
                "seekcur after a play is what segfaults mpd 0.24.6"
            )


async def test_a_stopped_mpd_does_not_overwrite_a_real_position_with_the_start_of_the_book(
    world: World, tmp_path: Path
) -> None:
    """A stopped MPD carries no ``elapsed`` key at all, and reading an absent key as 0.0 would
    write the start of the book over a real position with nothing to report - the channel would
    simply begin again next time, which is the one failure this feature can cause."""
    async with _mpd(status_lines=PLAYING_AT_61_5) as fake:
        options = _radio_and_mpd(world, tmp_path, fake)
        logs: list[str] = []

        async with _running(options, logs) as service:
            await _both_wake(world)
            await _press_preset(world.studio, STUDIO_ID, 1)
            await _press_preset(world.studio, STUDIO_ID, 2)
            await eventually(lambda: _playing(service).endswith("?c=12"), "the zone is on the MPD channel")
            await _press_preset(world.studio, STUDIO_ID, 1)
            # On the STATION rather than on the state file, because the file is written first: a
            # wait on it returns while the channel change is still on its way, and the presses
            # below then land inside the window still open here and become one long number.
            await eventually(lambda: _playing(service).endswith("?c=1"), "the house left the book")
            assert positions_of(options) == {"12": AT_61_5}, "a real position was written down"

            # Now MPD is stopped, which is what it looks like after somebody restarted it.
            fake.status_lines = ["state: stop"]
            await _press_preset(world.studio, STUDIO_ID, 1)
            await _press_preset(world.studio, STUDIO_ID, 2)
            await eventually(lambda: _playing(service).endswith("?c=12"), "the house came back to the book")
            await _press_preset(world.studio, STUDIO_ID, 1)
            await eventually(lambda: _playing(service).endswith("?c=1"), "and left it again")

            assert positions_of(options) == {"12": AT_61_5}, "the real position is still there"

            # And the house still works, which is the half the assertion above cannot see: a
            # position written as None rather than skipped leaves that same file untouched,
            # because the write raises on its way to disk and takes the dialling worker with it.
            # Measured: without this the guard could be deleted and this test still passed.
            await _press_preset(world.studio, STUDIO_ID, 1)
            await _press_preset(world.studio, STUDIO_ID, 2)
            await eventually(lambda: _playing(service).endswith("?c=12"), "the service is still dialling")


async def test_a_box_woken_onto_a_selection_that_is_no_preset_is_still_taken_in(world: World, tmp_path: Path) -> None:
    """PROBE (rank 19). A box switched on resumes whatever it last had, which need not be a preset.

    Measured in the flat 2026-09-20 at 08:10:05: Room1 was switched on and the only line the
    service wrote was ``ignoring '0', which no preset key can press``. A box reports a selection
    that is not in one of its six preset slots as ``<preset id="0">`` (four of them in
    ``research/captures/2026-09-07-live-run-2/observer.jsonl``), and what it had last was the
    zone's own stream from before the master let it go.

    The press is the proof that somebody is standing there. Here it is thrown away: the house is
    silent, so ``_may_choose_the_channel`` says yes and the press goes to the dialler, which
    cannot use the digit - and neither branch that marks a wake is reached.
    """
    options = _options(world, tmp_path)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await eventually(lambda: service.master is not None, "the switch is on and the master is up")
        assert _station_url(service) is None, "precondition: the house is playing nothing"
        assert service.policy.is_asleep(STUDIO_ID), "precondition: the box is in standby"

        await _press_preset(world.studio, STUDIO_ID, 0)

        await eventually(lambda: len(joins(world.studio)) == 1, "the box that was switched on was taken in")


async def test_an_awake_box_selecting_something_that_is_no_preset_is_left_alone(world: World, tmp_path: Path) -> None:
    """The other side of the rule above, and the reason it asks whether the box was ASLEEP.

    A selection that is no preset is also what a box reports when somebody switches it to
    Bluetooth while it is awake (four such frames in
    ``research/captures/2026-09-07-live-run-2/observer.jsonl``, all of them
    ``<preset id="0"><ContentItem source="BLUETOOTH">``). Taking that box into the zone would be
    the one thing the membership rule exists to prevent: music taken away from somebody standing
    in the room.

    It is a control rather than a RED - it passes with the rule and without it - and it is here so
    that a later, wider reading of "a press is a wake" cannot be made without something failing.
    """
    options = _options(world, tmp_path)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await eventually(lambda: service.master is not None, "the switch is on and the master is up")
        await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=AUX))
        await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
        await eventually(lambda: not service.policy.is_asleep(STUDIO_ID), "the box is awake, not in standby")

        await _press_preset(world.studio, STUDIO_ID, 0)

        # A deliberate bare sleep, not a wait for an event: a negative needs a quiet window, and
        # the wake this denies would be acted on by the very next pass.
        await asyncio.sleep(0.5)
        assert joins(world.studio) == [], "a box somebody switched to something else is not taken into the zone"


async def test_the_house_comes_back_to_the_file_it_left_and_not_the_first_one(world: World, tmp_path: Path) -> None:
    """Heard in the flat 2026-09-20 at 15:43, and it is what the feature's headline promises.

    The house was on the fifth file of a channel; the state file recorded 1.539 seconds and
    nothing else, and dialling the channel again played the FIRST file 1.5 seconds in. MPD names
    the queue position in the same ``status`` the offset is read from (``song``), so the entry was
    read and then dropped.

    The resume stays ONE exchange; what this test is about is which ENTRY it names. Since
    2026-09-21 that exchange is a single ``seek <songpos> <time>`` rather than a command list of
    ``play`` and ``seekcur``, because the list segfaults MPD 0.24.6 - so the entry is now the
    first argument of the seek rather than the argument of a play.
    """
    async with _mpd(status_lines=PLAYING_AT_61_5) as fake:
        options = _radio_and_mpd(world, tmp_path, fake)
        logs: list[str] = []

        async with _running(options, logs) as service:
            await _both_wake(world)
            await _press_preset(world.studio, STUDIO_ID, 1)
            await _press_preset(world.studio, STUDIO_ID, 2)
            await eventually(lambda: _playing(service).endswith("?c=12"), "the zone is on the MPD channel")

            await _press_preset(world.studio, STUDIO_ID, 1)
            await eventually(lambda: _playing(service).endswith("?c=1"), "the house left it for the radio one")
            before = len(fake.seen)

            await _press_preset(world.studio, STUDIO_ID, 1)
            await _press_preset(world.studio, STUDIO_ID, 2)
            await eventually(lambda: _playing(service).endswith("?c=12"), "the house came back to the MPD channel")
            await eventually(lambda: "seek 2 41.500" in fake.seen[before:], "the file and the offset were asked for")

            asked = [line for line in fake.seen[before:] if line.startswith(("seek ", "play "))]
            assert asked == ["seek 2 41.500"], "the file it left, the offset it left, and one exchange"
            assert "play 0" not in fake.seen[before:], "and never the first file"


async def test_a_held_step_key_acts_once_at_the_threshold_and_not_again_at_the_release(
    world: World, tmp_path: Path
) -> None:
    """The gesture rule from the outside, on the key that had no hold before it.

    Two things are asserted together because getting either wrong is invisible in the other. The
    hold ACTS - a key held past the threshold does something, rather than reading as a remote that
    missed the press - and it acts ONCE: the press collected a step on its way down, so a hold
    that did not drop it would move the house twice for one gesture. Since the threshold is longer
    than the dialling window, the step must also not act as a tap while the key is still down. The rotation here is two
    channels, so two steps would land back where it started and look exactly like nothing having
    happened at all.
    """
    options = _dialable_world(world, tmp_path, dial_window_s=0.5)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        await eventually(lambda: _station_url(service) is not None, "the house is playing")
        assert _playing(service).endswith("?c=1"), "precondition: the house is on the first channel"

        await _hold_key(STUDIO_IP, KeyName.NEXT_TRACK, threshold_s=options.hold_threshold_s)

        await eventually(lambda: _playing(service).endswith("?c=12"), "the held key stepped the channel")
        assert [line for line in logs if "held NEXT_TRACK" in line] != [], (
            "a live run has to be able to see which gesture the house read"
        )
        # A deliberate bare sleep, not a wait for an event: it establishes that nothing further is
        # pending, which is what makes the reading below mean one step rather than two.
        await asyncio.sleep(0.5 + 0.3)
        assert _playing(service).endswith("?c=12"), "one gesture is one step; two would return to the first"


async def test_a_step_key_tapped_slower_than_the_window_steps_once(world: World, tmp_path: Path) -> None:
    """A step key down longer than the window and shorter than the hold threshold is ONE tap.

    Its step is collected at the press, and the window can now close while the key is still down.
    Acting then read the key as a tap and, had it stayed down to the threshold, as a hold as well.
    Here it comes up at 0.75 s against a 0.5 s window and a 1.0 s threshold: one step, onto the
    second of two channels. Two would land back on the first and look like nothing happened.
    """
    options = _dialable_world(world, tmp_path, dial_window_s=0.5)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        await eventually(lambda: _station_url(service) is not None, "the house is playing")
        assert _playing(service).endswith("?c=1")

        await _press_for(STUDIO_IP, KeyName.NEXT_TRACK, seconds=0.75)
        await eventually(lambda: _playing(service).endswith("?c=12"), "the tap stepped the channel")
        # A deliberate bare sleep: past the threshold and a window after it, so a second step
        # would have acted by now.
        await asyncio.sleep(options.hold_threshold_s + 0.5)
        assert _playing(service).endswith("?c=12"), "one tap is one step"
    assert not any("held NEXT_TRACK" in line for line in logs), "and it was never read as a hold"


async def test_a_thumb_let_go_just_after_the_threshold_is_a_hold_and_not_a_tap(world: World, tmp_path: Path) -> None:
    """The gesture a person actually makes: held barely past the hold threshold, then let go at once.

    What this does NOT pin is the race between the loop and the release, although it was written
    for it: the loop re-checks every ``DIAL_TICK_S``, 50 ms, so it reports the hold before a
    release sent 50 ms after the threshold can arrive, and removing the guard that refuses a late
    release leaves this test green (mutation-verified 2026-09-20). The race is staged exactly in
    ``tests/test_longpress.py::TestWhoeverNoticesFirst``, where the clock is an argument; here the
    only honest claim is the end-to-end one - a thumb held just past the threshold takes a channel
    out of the rotation rather than reading as a tap.
    """
    options = _rotation_world(world, tmp_path, numbers=("1", "12", "13"))
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        await eventually(lambda: _station_url(service) is not None, "the house is playing")

        # Press, wait out the threshold, and release IMMEDIATELY - no pause for the loop to notice.
        await _forward_key(STUDIO_IP, KeyName.THUMBS_DOWN, KeyState.PRESS)
        await asyncio.sleep(options.hold_threshold_s + 0.05)
        await _forward_key(STUDIO_IP, KeyName.THUMBS_DOWN, KeyState.RELEASE)

        await eventually(
            lambda: _channels_of(options).rotation_numbers() == ("12", "13"), "the held thumb moved the rotation"
        )
        assert not any("thumbed down once" in line for line in logs), "and it was never read as a tap"


SLOW_START_S = 4.0
"""How long the slow station holds its body back, which is the wait a gesture must not sit behind.

Well inside ``FIRST_BYTES_TIMEOUT_S`` (10 s), so the station start is WAITING rather than giving
up, and long enough that half of it is still a generous ceiling for a gesture that acts at a
0.3 s window.
"""


async def test_a_gesture_acts_at_its_window_while_a_slow_station_is_still_starting(
    world: World, tmp_path: Path
) -> None:
    """A press must not wait for somebody else's server (OPEN-WORK rank 172).

    Measured in the flat 2026-09-20: a key held at 22:37:27.611 was due at its 0.6 s window and was
    acted on at 22:37:32.880, 4.67 s late, because a competing step five milliseconds earlier had
    put the loop inside a station start. In the room that is: hold a key, nothing happens, five
    seconds later the house jumps.

    Two constraints of ours caused it and BOTH have to go, which is why one test covers them: the
    pass lock was held across ``master.play``, so press N waited for press N-1; and the deadline
    loop awaited the action it dispatched, so press N was not even SEEN while N-1 was fetching.
    ``ZoneMaster.play`` was written for exactly this - it bumps a generation, takes its own lock
    AFTER the wait, and the older call gives up - and its docstring says so: "no press is made to
    wait ten seconds on another". We were re-imposing the wait it exists to avoid.

    The ceiling is half the slow station's delay: a run that acts at the window lands near 0.5 s,
    and one that sits behind the fetch cannot get in under 2.0 s however the machine is loaded. The
    window is the option's own FLOOR rather than a number typed here, which is as short as the
    record allows and keeps the margin at its widest.
    """
    slow, slow_url = await _station(os.urandom(200_000), first_byte_delay=SLOW_START_S)
    options = _options(world, tmp_path, seed=True, dial_window_s=WINDOW_FLOOR_S)
    save_channels(
        options.channel_file,
        ChannelList(
            channels=(
                Channel(number="1", name="Quick", kind=ChannelKind.RADIO, url=f"{world.station_url}?c=1"),
                Channel(number="2", name="Slow", kind=ChannelKind.RADIO, url=slow_url),
                Channel(number="3", name="Third", kind=ChannelKind.RADIO, url=f"{world.station_url}?c=3"),
            )
        ),
    )
    save_state(options.state_file, ZoneState(channel="1", members=()))
    logs: list[str] = []

    try:
        async with _running(options, logs) as service:
            await eventually(lambda: service.master is not None, "the switch is on and the master is up")
            await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
            await eventually(lambda: len(joins(world.studio)) == 1, "the box is in the zone on the quick channel")

            await _press_preset(world.studio, STUDIO_ID, 2)
            await eventually(
                lambda: any("dialled 2: Slow" in line for line in logs),
                "the slow channel was dialled, so the service is now inside its wait for first bytes",
            )

            await _press_preset(world.studio, STUDIO_ID, 3)
            await eventually(
                lambda: any("dialled 3: Third" in line for line in logs),
                "the next number acted at its own window rather than behind the slow station",
                timeout=SLOW_START_S / 2,
            )
    finally:
        slow.close()


# --- OPEN-WORK rank 11 step 3: a channel that plays a DIRECTORY ----------------------------------

BOOK_IN_DATABASE_ORDER = ["Buch/10.mp3", "Buch/2.mp3", "Buch/Bonus/01.mp3"]
"""What ``listall`` answers, in MPD's database order: 10 before 2, as measured."""
BOOK_IN_PLAY_ORDER = ["Buch/2.mp3", "Buch/10.mp3", "Buch/Bonus/01.mp3"]
PLAYING_THE_FIRST = ["state: play", "song: 0", "elapsed: 61.500", "duration: 900.000", "playlistlength: 3"]


def _radio_and_directory(world: World, tmp_path: Path, fake: FakeMpd, *, directory: str = "Buch") -> ServiceOptions:
    """Channel 1 a station, channel 12 a directory under MPD's music directory."""
    options = _with_mpd(_options(world, tmp_path, seed=True, dial_window_s=0.5), fake)
    save_channels(
        options.channel_file,
        ChannelList(
            channels=(
                Channel(number="1", name="One", kind=ChannelKind.RADIO, url=f"{world.station_url}?c=1"),
                Channel(
                    number="12",
                    name="Buch",
                    kind=ChannelKind.MPD,
                    url=f"{world.station_url}?c=12",
                    mpd_directory=directory,
                ),
            )
        ),
    )
    return options


def _from(seen: list[str], first: str) -> list[str]:
    """What mpd was told from ``first`` on, which is where one channel change begins."""
    assert first in seen, f"mpd was never told {first!r}: {seen}"
    return seen[seen.index(first) :]


async def test_a_directory_channel_is_queued_file_by_file_in_play_order(world: World, tmp_path: Path) -> None:
    """The order is ours (decision C): MPD lists 10 before 2, the channel plays 2 before 10."""
    async with _mpd(directories={"Buch": BOOK_IN_DATABASE_ORDER}) as fake:
        options = _radio_and_directory(world, tmp_path, fake)
        async with _running(options, []) as service:
            await _on_the_mpd_channel(world, service)
            await eventually(lambda: "play 0" in fake.seen, "mpd was told to play")

            assert _from(fake.seen, 'listall "Buch"')[:8] == [
                'listall "Buch"',
                "clear",
                "repeat 1",
                "command_list_begin",
                'add "Buch/2.mp3"',
                'add "Buch/10.mp3"',
                'add "Buch/Bonus/01.mp3"',
                "command_list_end",
            ]
            assert fake.queue == BOOK_IN_PLAY_ORDER


async def test_a_directory_mpd_does_not_have_is_said_by_name_and_costs_no_zone(world: World, tmp_path: Path) -> None:
    """A typo in the channel file, said as what it is - a DIRECTORY - and the zone still moves."""
    async with _mpd() as fake:
        options = _radio_and_directory(world, tmp_path, fake, directory="Bcuh")
        logs: list[str] = []
        async with _running(options, logs) as service:
            await _on_the_mpd_channel(world, service)
            await eventually(
                lambda: any("names directory 'Bcuh', which mpd does not have" in line for line in logs),
                "the missing directory was said by name",
            )


async def test_an_empty_directory_is_said_by_name_and_takes_the_old_queue_off(world: World, tmp_path: Path) -> None:
    async with _mpd(directories={"Buch": []}) as fake:
        options = _radio_and_directory(world, tmp_path, fake)
        logs: list[str] = []
        async with _running(options, logs) as service:
            await _on_the_mpd_channel(world, service)
            await eventually(
                lambda: any("directory 'Buch' holds no files mpd can play" in line for line in logs),
                "the empty directory was said by name",
            )
            assert _from(fake.seen, 'listall "Buch"')[1:3] == ["clear", "repeat 1"], "the old queue went"
            assert "play 0" not in fake.seen


async def test_leaving_a_directory_channel_remembers_the_file_by_name(world: World, tmp_path: Path) -> None:
    """The index alone would point at another chapter once somebody adds a file in front of it."""
    async with _mpd(status_lines=PLAYING_AT_61_5, directories={"Buch": BOOK_IN_DATABASE_ORDER}) as fake:
        options = _radio_and_directory(world, tmp_path, fake)
        async with _running(options, []) as service:
            await _on_the_mpd_channel(world, service)
            await _press_preset(world.studio, STUDIO_ID, 1)
            await eventually(lambda: _playing(service).endswith("?c=1"), "the house left it for the radio one")
            await eventually(
                lambda: positions_of(options) == {"12": Place(track=2, seconds=61.5, file="Buch/Bonus/01.mp3")},
                "the place was written down with the file's name",
            )


async def test_coming_back_to_a_directory_finds_the_file_by_name_after_one_was_added(
    world: World, tmp_path: Path
) -> None:
    """Left in 10.mp3, the SECOND file then; a 3.mp3 added since makes it the third."""
    grown = ["Buch/10.mp3", "Buch/2.mp3", "Buch/3.mp3"]
    async with _mpd(directories={"Buch": grown}) as fake:
        options = _radio_and_directory(world, tmp_path, fake)
        save_state(options.state_file, ZoneState(positions={"12": Place(track=1, seconds=61.5, file="Buch/10.mp3")}))
        async with _running(options, []) as service:
            await _on_the_mpd_channel(world, service)
            await eventually(lambda: "seek 2 41.500" in fake.seen, "the file was found where it is now")
            assert "seek 1 41.500" not in fake.seen, "and not at the index it had"


async def test_a_held_next_on_a_directory_channel_goes_to_the_next_directory(world: World, tmp_path: Path) -> None:
    """D: a held next is the first file of the next directory, here Buch/Bonus - not the next file."""
    async with _mpd(status_lines=PLAYING_THE_FIRST, directories={"Buch": BOOK_IN_DATABASE_ORDER}) as fake:
        options = _radio_and_directory(world, tmp_path, fake)
        logs: list[str] = []
        async with _running(options, logs) as service:
            await _on_the_mpd_channel(world, service)
            loaded = len(fake.seen)
            await _hold_key(STUDIO_IP, KeyName.NEXT_TRACK, threshold_s=options.hold_threshold_s)
            await eventually(lambda: "play 2" in fake.seen[loaded:], "mpd was asked for the next directory")
            assert "play 1" not in fake.seen[loaded:], "a held key is not a step to the next file"
            assert _playing(service).endswith("?c=12"), "and the channel did not move"


async def test_a_held_previous_from_inside_a_directory_goes_to_its_first_file(world: World, tmp_path: Path) -> None:
    """G: the CD player's back key. From the THIRD file of Buch a hold goes to its first, where a
    tap would go to the second - so the two gestures cannot be mistaken for each other here."""
    three = ["Buch/1.mp3", "Buch/2.mp3", "Buch/3.mp3", "Buch/Bonus/01.mp3"]
    async with _mpd(status_lines=PLAYING_AT_61_5, directories={"Buch": three}) as fake:
        options = _radio_and_directory(world, tmp_path, fake)
        async with _running(options, []) as service:
            await _on_the_mpd_channel(world, service)
            loaded = len(fake.seen)
            await _hold_key(STUDIO_IP, KeyName.PREV_TRACK, threshold_s=options.hold_threshold_s)
            await eventually(lambda: "play 0" in fake.seen[loaded:], "mpd was asked for the first file of Buch")
            assert "play 1" not in fake.seen[loaded:], "a held key is not a step to the previous file"


async def test_a_held_next_on_a_stored_playlist_steps_by_directory_too(world: World, tmp_path: Path) -> None:
    """D holds for a stored playlist as well: its entries come from directories too. From the
    first file of A a hold goes to B, where a tap would go to A's second file."""
    playlist = ["A/1.mp3", "A/2.mp3", "A/3.mp3", "B/1.mp3", "B/2.mp3"]
    async with _mpd(status_lines=PLAYING_THE_FIRST, playlists={"hoerbuecher": playlist}) as fake:
        options = _radio_and_mpd(world, tmp_path, fake)
        async with _running(options, []) as service:
            await _on_the_mpd_channel(world, service)
            loaded = len(fake.seen)
            await _hold_key(STUDIO_IP, KeyName.NEXT_TRACK, threshold_s=options.hold_threshold_s)
            await eventually(lambda: "play 3" in fake.seen[loaded:], "mpd was asked for B/1.mp3")
            assert "play 1" not in fake.seen[loaded:], "a held key is not a step to the next file"
