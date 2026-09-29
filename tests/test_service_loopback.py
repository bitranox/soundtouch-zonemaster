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
import gc
import itertools
import json
import os
import socket
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import pytest
from mpdfake import HOST as MPD_HOST
from mpdfake import FakeMpd
from registry_double import FakeRegistry, devices_at
from service_log import recording_into
from slow_store import SlowStore
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

from soundtouch_zonemaster.adapters.aftertouch.registry import DEVICES_PATH
from soundtouch_zonemaster.adapters.files.channel_file import save_channels
from soundtouch_zonemaster.adapters.files.house_store import SqlHouseStore
from soundtouch_zonemaster.adapters.files.state_file import LegacyState, save_state
from soundtouch_zonemaster.adapters.files.store_worker import StoreWorker
from soundtouch_zonemaster.adapters.soundtouch.orion import ORION_FALLBACK_PATH, OrionBase
from soundtouch_zonemaster.adapters.soundtouch.pb import audio
from soundtouch_zonemaster.adapters.soundtouch.reports import SlaveState
from soundtouch_zonemaster.adapters.soundtouch.speaker_http import SPEAKER_HTTP_TIMEOUT_S, http_get
from soundtouch_zonemaster.adapters.soundtouch.zone_master import ZoneMaster
from soundtouch_zonemaster.application.errors import StoreError
from soundtouch_zonemaster.application.options import ChannelPolicy, ServiceOptions
from soundtouch_zonemaster.application.zone_service.constants import (
    JOIN_RETRY_S,
    MUTE_HOLD_S,
    PORTS_BUSY_RETRY_S,
)
from soundtouch_zonemaster.application.zone_service.service import ZoneService
from soundtouch_zonemaster.application.zone_service.zone import STAND_DOWN_SAVE_S
from soundtouch_zonemaster.composition import build_production, hold_the_zone, open_house_store
from soundtouch_zonemaster.domain.channellist import Channel, ChannelList
from soundtouch_zonemaster.domain.dialling import WINDOW_DEFAULT_S, WINDOW_FLOOR_S
from soundtouch_zonemaster.domain.enums import ChannelEnd, ChannelKind, FrameKind, KeyName, KeyState, SourceName
from soundtouch_zonemaster.domain.events import SpeakerEvent
from soundtouch_zonemaster.domain.logfn import ERROR_KIND
from soundtouch_zonemaster.domain.longpress import HOLD_THRESHOLD_DEFAULT_S
from soundtouch_zonemaster.domain.membership import UNREACHABLE_TIMEOUT_S, WAKE_WINDOW_S
from soundtouch_zonemaster.domain.preferences import FADE_DEFAULT_S, PreferenceName, PreferenceSource
from soundtouch_zonemaster.domain.presses import CONFIRM_BACK_WINDOW_S
from soundtouch_zonemaster.domain.state import Place, ZoneState
from soundtouch_zonemaster.domain.zonexml import station_content_item

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from soundtouch_zonemaster.application.options import ChannelsExport, LegacyFiles
    from soundtouch_zonemaster.application.ports import (
        AddressOf,
        HouseStore,
        ServiceStore,
        SpeakerWatch,
        ZoneServicePorts,
    )
    from soundtouch_zonemaster.domain.logfn import LogFn
    from soundtouch_zonemaster.domain.preferences import PreferenceRow, PreferenceValue
    from soundtouch_zonemaster.domain.secret import Secret

MASTER = "127.0.0.10"
"""An address of the master's own, which nothing else in this file binds or connects from.

The master's ports are fixed and 40002 and 40003 lie inside the kernel's ephemeral range, so any
socket that takes an ephemeral port ON THE MASTER'S ADDRESS can be sitting on one when the master
starts: a fake server bound to port 0 there, or a client connection, whose source address is
127.0.0.1 for every destination in 127/8. The master then finds a port busy for the whole test
(OPEN-WORK rank 176, reproduced by narrowing the ephemeral range in a private network namespace).
"""
STATION_IP = "127.0.0.1"
"""The fake radio station, beside the registry and MPD fakes and away from :data:`MASTER`."""
MASTER_ID = "5EB0CE000001"

STUDIO_ID, STUDIO_IP = "AABBCC000010", "127.0.0.2"
HALLWAY_ID, HALLWAY_IP = "AABBCC000011", "127.0.0.3"
CONSOLE_ID, CONSOLE_IP = "AABBCC000012", "127.0.0.4"
"""The three devices of the shipped fixture, moved onto addresses a test can reach. The third is
the Lifestyle console, and the service must leave it alone without being told its address."""
CONSOLE_NAME = "Bose Cinema"
"""What the registry fixture calls the console, which is how the probe line names it."""

MOVED_IP = "127.0.0.5"
"""Where the studio turns up after a reboot onto a new lease, in the one test that moves it."""

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
        probe.bind((STUDIO_IP, 0))
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
        with contextlib.suppress(OSError):
            await writer.wait_closed()

    server = await asyncio.start_server(handle, STATION_IP, 0)
    return server, f"http://{STATION_IP}:{server.sockets[0].getsockname()[1]}/live"


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

    The database is where the service keeps everything; the channel file written here, and any
    state or switch file a test writes before its first start, is what that start imports into
    it, the way a house upgrading from the files would.
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
        database=str(tmp_path / "zonemaster.sqlite"),
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


def _legacy(path: Path | None) -> Path:
    """One of the three old files, which ``_options`` always names: an import source now.

    Written BEFORE a test's first start, each is what the service imports into its empty
    database, which keeps every test that sets up a list or a state that way working unchanged.
    """
    assert path is not None, "_options names all three old files"
    return path


def _state_file(options: ServiceOptions) -> Path:
    return _legacy(options.state_file)


def _channel_file(options: ServiceOptions) -> Path:
    return _legacy(options.channel_file)


def _switch_file(options: ServiceOptions) -> Path:
    return _legacy(options.switch_file)


def _store_of(options: ServiceOptions) -> SqlHouseStore:
    """A READER on the service's database, which the service's writer lock does not block."""
    store = SqlHouseStore(options.database, log=lambda _kind, _text: None)
    store.open(exclusive=False)
    return store


def _state_of(options: ServiceOptions) -> ZoneState:
    """What a restart starts from, read out of the database the way a restart reads it."""
    store = _store_of(options)
    try:
        return store.load_state()
    finally:
        store.close()


def _preferences_of(options: ServiceOptions) -> dict[str, str]:
    """The stored preferences as name -> JSON text, read the way a restart reads them."""
    store = _store_of(options)
    try:
        return {row.name: row.text for row in store.load_preferences()}
    finally:
        store.close()


def _channels_of(options: ServiceOptions) -> ChannelList:
    """The channel list as the service left it, read the way a restart reads it."""
    store = _store_of(options)
    try:
        return store.load_channels()
    finally:
        store.close()


def _switch_of(options: ServiceOptions) -> bool:
    """Whether the database says the house is on, read the way the service reads it."""
    store = _store_of(options)
    try:
        return store.is_on()
    finally:
        store.close()


def _flip(options: ServiceOptions, *, on: bool) -> None:
    """What the service's switch command does, on the database a service may be running on."""
    store = _store_of(options)
    try:
        store.set_switch(on=on)
    finally:
        store.close()


def _set_preference(options: ServiceOptions, name: PreferenceName, value: PreferenceValue) -> None:
    """What `prefs set` does, on the database a service may be running on."""
    store = _store_of(options)
    try:
        store.set_preference(name, value, source=PreferenceSource.CLI)
    finally:
        store.close()


def _unset_preference(options: ServiceOptions, name: PreferenceName) -> None:
    """What `prefs unset` does."""
    store = _store_of(options)
    try:
        store.unset_preference(name)
    finally:
        store.close()


@asynccontextmanager
async def _running(
    options: ServiceOptions, logs: list[str], *, ports: ZoneServicePorts | None = None
) -> AsyncGenerator[ZoneService, None]:
    """The service as the unit runs it, ended the way SIGINT ends it: cancelled, then cleaned up.

    ``ports`` is the production wiring unless a test hands in its own, built from that wiring with
    one port replaced at the seam (:func:`_relaying`, for instance).
    """
    service = ZoneService(
        options,
        log=recording_into(logs),
        ports=ports if ports is not None else build_production().zone_ports,
    )
    task = asyncio.create_task(service.run())
    try:
        yield service
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class _Frames:
    """Every frame the observers put on the reader's queue, and a clock step applied on the way.

    A plain class rather than a dataclass, so the list is typed without a factory pyright reads
    as a list of nothing in particular.
    """

    def __init__(self) -> None:
        self.put: list[SpeakerEvent] = []
        """Each event as it went onto the queue, re-stamped. Appended in the same step as the put."""
        self.clock_step_s = 0.0
        """Added to ``received_at`` of every frame put while it is set: the wall clock stepped by it."""


class _Relay(asyncio.Queue[SpeakerEvent]):
    """One observer's view of the reader's queue: each frame re-stamped, written down, and passed on.

    Both ways of adding an item are overridden, ``put`` (what the observer calls today) and
    ``put_nowait``, so the relay's own queue never holds anything: a frame that stayed in it would
    be a frame nothing reads, and a test waiting for it would time out naming nothing about why.
    The put into the real queue never suspends (the queue is unbounded), so a test that sees a
    frame in :attr:`_Frames.put` and the reader's queue empty knows the reader has taken it - and
    read it to the end, because the reader does nothing between two ``get`` calls that could let
    another task run.
    """

    def __init__(self, into: asyncio.Queue[SpeakerEvent], frames: _Frames) -> None:
        super().__init__()
        self._into = into
        self._frames = frames

    async def put(self, item: SpeakerEvent) -> None:
        self.put_nowait(item)

    def put_nowait(self, item: SpeakerEvent) -> None:
        stamped = replace(item, received_at=item.received_at + self._frames.clock_step_s)
        self._frames.put.append(stamped)
        self._into.put_nowait(stamped)


def _relaying(frames: _Frames) -> ZoneServicePorts:
    """The production wiring with every observer putting its frames through a :class:`_Relay`.

    The seam is the ``watch_speaker`` port, which is handed the reader's queue: nothing inside the
    service is replaced, and the observer is the real one reading a real WebSocket.
    """
    production = build_production().zone_ports

    def watch(
        device_id: str,
        /,
        *,
        address_of: AddressOf,
        events: asyncio.Queue[SpeakerEvent],
        log: LogFn,
        policy: ChannelPolicy,
    ) -> SpeakerWatch:
        return production.watch_speaker(
            device_id, address_of=address_of, events=_Relay(events, frames), log=log, policy=policy
        )

    return replace(production, watch_speaker=watch)


async def test_the_relay_passes_on_a_frame_put_without_waiting_as_well() -> None:
    """Either way of putting reaches the reader, re-stamped and written down.

    The observer awaits ``put`` today. Were it to switch to ``put_nowait``, a relay that overrode
    only ``put`` would keep the frame in its OWN queue, which nothing reads: every test built on it
    would time out waiting for a frame the service never saw, and name nothing about why.
    """
    into: asyncio.Queue[SpeakerEvent] = asyncio.Queue()
    frames = _Frames()
    frames.clock_step_s = 5.0
    relay = _Relay(into, frames)
    event = SpeakerEvent(received_at=1.0, speaker=STUDIO_IP, device_id=STUDIO_ID, kind="nowPlayingUpdated", frame="")

    relay.put_nowait(event)
    await relay.put(event)

    expected = replace(event, received_at=6.0)
    assert frames.put == [expected, expected], "both puts were written down, re-stamped"
    assert [into.get_nowait(), into.get_nowait()] == [expected, expected], "and both reached the reader's queue"
    assert relay.empty(), "nothing stayed behind in the relay's own queue"


def _read_to_the_end(service: ZoneService, frames: _Frames, device_id: str, kind: str) -> bool:
    """Whether the reader has taken a frame of ``kind`` from ``device_id`` and done with it."""
    put = any(event.device_id == device_id and event.kind == kind for event in frames.put)
    return put and service.events.empty()


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
    """Who the database says the zone belongs to, read the way a restart reads it."""
    return _state_of(options).members


def out_of_multiroom(options: ServiceOptions) -> tuple[str, ...]:
    """Which boxes the database says a person switched out, read the way a restart reads it."""
    return _state_of(options).out_of_multiroom


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
    save_state(_state_file(options), LegacyState(state=ZoneState(channel="1", members=())))
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
    save_state(_state_file(options), LegacyState(state=ZoneState(channel="1", members=())))
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
        _channel_file(options),
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
            lambda: _state_of(options).muted == {},
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
        muted = _state_of(options).muted
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
        muted = _state_of(options).muted
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
    save_state(_state_file(options), LegacyState(state=ZoneState(muted={STUDIO_ID: 22})))
    world.studio.volume = 0

    async with _running(options, []):
        await eventually(lambda: world.studio.volume == 22, "the box was turned back up on start-up")
        await eventually(
            lambda: _state_of(options).muted == {},
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
    _switch_file(options).write_text("off\n", encoding="utf-8")
    save_state(_state_file(options), LegacyState(state=ZoneState(muted={STUDIO_ID: 22})))
    world.studio.volume = 0

    async with _running(options, []):
        await eventually(lambda: world.studio.volume == 22, "the box was turned back up with the house off")
        await eventually(
            lambda: _state_of(options).muted == {},
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
    _switch_file(options).write_text("off\n", encoding="utf-8")
    save_state(_state_file(options), LegacyState(state=ZoneState(muted={STUDIO_ID: 22})))
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
            lambda: _state_of(options).muted == {},
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
    save_state(_state_file(options), LegacyState(state=ZoneState(muted={STUDIO_ID: 22})))
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
                lambda: _state_of(options).muted == {},
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

        _flip(options, on=False)
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
    _flip(options, on=True)
    studio_joins, hallway_joins = len(joins(world.studio)), len(joins(world.hallway))

    async with _running(options, logs):
        await eventually(lambda: len(joins(world.studio)) > studio_joins, "the studio was taken back into the zone")
        await eventually(lambda: believed(options) == (STUDIO_ID,), "the file was corrected to what is true now")
        assert len(joins(world.hallway)) == hallway_joins, "a box that went to standby is not taken back"


def _two_radio_channels(world: World) -> ChannelList:
    """Two stations on the world's own server, which is what a house's hand-built list looks like."""
    return ChannelList(
        channels=(
            Channel(number="1", name="One", kind=ChannelKind.RADIO, url=f"{world.station_url}?c=1"),
            Channel(number="2", name="Two", kind=ChannelKind.RADIO, url=f"{world.station_url}?c=2"),
        )
    )


async def test_a_first_start_takes_the_old_files_into_the_database_and_a_restart_reads_it_back(
    world: World, tmp_path: Path
) -> None:
    """The upgrade path on the real wiring: the old files go in once, and never again.

    The first start finds an empty database beside a channel file and a switch file, so both are
    imported and set aside as ``.imported``. The second start finds the switch file written again,
    as an operator who never heard of the database would write it, and must leave it unread:
    the database already holds a switch, and whatever wrote that is newer than any file.
    """
    options = _options(world, tmp_path, seed=True)
    two = _two_radio_channels(world)
    save_channels(_channel_file(options), two)
    _switch_file(options).write_text("off\n", encoding="utf-8")
    logs: list[str] = []

    async with _running(options, logs):
        # The import is one synchronous step before any worker starts, so the line it writes is
        # also the moment the renames are done.
        await eventually(lambda: any("imported 2 channel(s)" in line for line in logs), "the channel import")
        await eventually(
            lambda: any("zone.switch" in line and "imported the switch" in line for line in logs),
            "the switch import",
        )

    assert not _channel_file(options).exists(), "the imported file is not left where it was read"
    assert _channel_file(options).with_name("channels.json.imported").exists()
    assert _channels_of(options) == two
    assert _switch_of(options) is False, "the switch came over as it was: off"

    _switch_file(options).write_text("on\n", encoding="utf-8")
    logs.clear()

    async with _running(options, logs):
        await eventually(
            lambda: any("zone.switch" in line and "not imported" in line for line in logs),
            "the second start refusing the switch file BY NAME, not any 'not imported' line",
        )

    assert _switch_of(options) is False, "a file written after the import changes nothing"
    assert _switch_file(options).exists(), "and it is left in place for a person to find"
    assert _channels_of(options) == two, "the list the first start imported is the one a restart reads"


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
        with contextlib.suppress(OSError):
            await writer.wait_closed()

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


async def test_relative_orion_presets_seed_the_list_and_play_through_the_registry_s_base(
    world: World, tmp_path: Path
) -> None:
    """The form AfterTouch now writes presets in, from the preset key to the station's socket.

    ``/station?data=...`` is relative: a speaker completes it with the LOCAL_INTERNET_RADIO base
    its service registry names, and the master has to do the same before it can fetch a byte. The
    list keeps the location as the box stored it, because that is what a box is handed again.
    """
    relative = "/station?data=eyJuYW1lIjoiT3Jpb24ifQ%3D%3D"
    station_base = world.station_url.removesuffix("/live")
    world.registry.bodies["/bmx/registry/v1/services"] = json.dumps(
        {"bmx_services": [{"id": {"name": "LOCAL_INTERNET_RADIO"}, "baseUrl": f"{station_base}/orion"}]}
    )
    world.studio.presets = {1: _preset(relative, "Orion")}
    options = _options(world, tmp_path, seed=True)
    logs: list[str] = []

    async with _running(options, logs):
        await _both_wake(world)
        await eventually(lambda: _channels_of(options).numbers_in_order() == ("1",), "the list was seeded")
        await eventually(
            lambda: any(f"GET /orion{relative} " in fetch for fetch in world.fetches),
            "the station was fetched at the registry's base plus the relative location",
        )

    seeded = _channels_of(options).by_number("1")
    assert seeded is not None
    assert seeded.url == relative, "the channel keeps the location exactly as the preset stored it"
    assert "/bmx/registry/v1/services" in world.registry.paths


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
        _channel_file(options),
        ChannelList(
            channels=(
                Channel(number="1", name="One", kind=ChannelKind.RADIO, url=f"{world.station_url}?c=1"),
                Channel(number="3", name="Three", kind=ChannelKind.RADIO, url=f"{world.station_url}?c=3"),
            )
        ),
    )
    save_state(_state_file(options), LegacyState(state=ZoneState(channel="3", members=())))
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
        _channel_file(options),
        ChannelList(
            channels=(Channel(number="1", name="One", kind=ChannelKind.RADIO, url=f"{world.station_url}?c=1"),)
        ),
    )
    save_state(_state_file(options), LegacyState(state=ZoneState(channel="3", members=())))
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


@pytest.mark.parametrize(
    "first_listed_answers_last",
    [pytest.param(True, id="first-listed-answers-last"), pytest.param(False, id="first-listed-answers-first")],
)
@pytest.mark.parametrize("listed_mid_run", [pytest.param(False, id="at-start"), pytest.param(True, id="mid-run")])
async def test_the_list_is_seeded_from_the_box_listed_first_whichever_answered_first(
    world: World, tmp_path: Path, listed_mid_run: bool, first_listed_answers_last: bool
) -> None:
    """Two boxes already on, asked together: the one the registry lists first seeds, whoever replies first.

    Neither was switched on in front of the service, so which of them was on "first" is not
    something either answer can say; the registry's order decides, as it did before each answer
    was taken as it arrived. When the first-listed box answers LAST, a list that follows the
    replies would come from the other box - the "which radio replied fastest" the seeding's own
    docstring names as the defect. When it answers FIRST, the later answer must not be put ahead
    of it: a rule that sends every answer to the front of what its round has placed so far agrees
    with the registry only while the replies come in reverse order.

    At start nothing seeds before every answer is in anyway, so that arm pins the ORDER the
    answers are written down in. Mid-run - a first registry read that failed, say - each answer
    asks for a pass the moment it lands, so that arm pins that no pass seeds while a box asked in
    the same round has still to answer.
    """
    entries = _listed_mid_run(world, STUDIO_ID, HALLWAY_ID) if listed_mid_run else None
    world.studio.now_playing = now_playing_document(device_id=STUDIO_ID, source=RADIO)
    world.hallway.now_playing = now_playing_document(device_id=HALLWAY_ID, source=RADIO)
    world.studio.presets = {
        1: _preset(f"{world.station_url}?c=1", "Superfly"),
        3: _preset(f"{world.station_url}?c=3", "Technikum"),
    }
    world.hallway.presets = {2: _preset(f"{world.station_url}?c=2", "The other box")}
    slow, fast = (world.studio, "Bose Hallway") if first_listed_answers_last else (world.hallway, "Bose Studio")
    released = asyncio.Event()
    slow.held["/now_playing"] = released
    options = _options(world, tmp_path, seed=True)
    logs: list[str] = []

    async with _running(options, logs):
        try:
            if entries is not None:
                await eventually(lambda: DEVICES_PATH in world.registry.paths, "the start read the registry")
                world.registry.body = json.dumps(entries)
            await eventually(lambda: "/now_playing" in slow.paths(), "the slow box was asked what it plays")
            await eventually(lambda: _said(logs, f"probe: {fast}: {RADIO}"), "the other box answered first")
            registry_lines = [line for line in logs if line.startswith("registry: Bose ")]
            assert registry_lines[0].startswith("registry: Bose Studio"), f"the control: listed first {registry_lines}"
            # A seeding that was going to run on the first answer runs within a loop turn or two
            # on loopback; this is margin over it.
            await asyncio.sleep(0.5)
            assert _channels_of(options).channels == (), "seeded while a box of the same round had still to answer"
        finally:
            released.set()
        await eventually(lambda: _channels_of(options).numbers_in_order() == ("1", "3"), "the studio seeded it")


class _AsksASecondRound(ZoneService):
    """The service, with a way for a test to start a second round of "what are you playing".

    Nothing in the service can do that today - the start asks one round and the registry poll,
    which only runs after the start, asks the next - so this is the one way to reach the guard
    that keeps it so, and it goes through the same method both of them call.
    """

    async def ask_a_second_round(self) -> None:
        await self._ask_the_speakers_what_they_are_playing()


async def test_a_second_round_while_one_is_out_is_refused_out_loud(world: World, tmp_path: Path) -> None:
    """Two rounds out at once would silently misplace each other's boxes, so a second is refused.

    A round's boxes keep the place in ``_switched_on`` the list had reached when the round opened,
    as a POSITION. A second round opening while the first is out would insert ahead of it and move
    it, and the channel list would then be seeded from the wrong box with nothing to show why. Only
    the order of the two callers keeps them apart today, so the rule is enforced where a round
    opens: said at ERROR, nobody asked, the round that is out undisturbed.
    """
    released = asyncio.Event()
    world.studio.held["/now_playing"] = released
    options = _options(world, tmp_path)
    logs: list[str] = []
    service = _AsksASecondRound(options, log=recording_into(logs), ports=build_production().zone_ports)
    task = asyncio.create_task(service.run())
    try:
        await eventually(lambda: "/now_playing" in world.studio.paths(), "the start's round is out")
        asked_before = len(world.hallway.paths())
        await asyncio.wait_for(service.ask_a_second_round(), timeout=2.0)

        refused = [line for line in logs if line.startswith(f"{ERROR_KIND}: ") and "second round" in line]
        assert len(refused) == 1, logs
        assert len(world.hallway.paths()) == asked_before, "the refused round asked nobody"
        released.set()
        await eventually(lambda: _said(logs, "probe: Bose Studio: "), "the round that was out still finished")
    finally:
        released.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.parametrize(
    "frame_before_second_answer",
    [
        pytest.param(False, id="second-answer-then-frame"),
        pytest.param(True, id="frame-then-second-answer"),
    ],
)
async def test_a_box_switched_on_while_a_round_is_out_comes_after_that_round_s_boxes(
    world: World, tmp_path: Path, frame_before_second_answer: bool
) -> None:
    """A round's boxes stand together where the round opened; a frame during it goes after them.

    The hallway and the console are listed mid-run, both already on, and asked together; the
    studio, known and asleep since the start, is switched on by a person while their answers are
    on the wire. The channel list is empty, so whichever box stands first seeds it. The round's
    boxes were on before the question went out, which is before the studio's frame, so the
    hallway - listed first of its round - must seed in both arms.

    What differs between the arms is only which the reader took first, the console's answer or
    the studio's frame, and that is reply speed. A rule that places each answer relative to the
    answers already taken, and a frame at the end, gave [hallway, console, studio] when the
    console's answer came first and [studio, hallway, console] when the frame did, so the studio
    seeded in exactly the arm where the console was slow. The hallway's answer is held until both
    others are in, in both arms.
    """
    entries = _listed_mid_run(world, HALLWAY_ID, CONSOLE_ID)
    world.hallway.now_playing = now_playing_document(device_id=HALLWAY_ID, source=RADIO)
    world.console.now_playing = now_playing_document(device_id=CONSOLE_ID, source=RADIO)
    world.hallway.presets = {
        1: _preset(f"{world.station_url}?c=1", "Superfly"),
        3: _preset(f"{world.station_url}?c=3", "Technikum"),
    }
    world.console.presets = {2: _preset(f"{world.station_url}?c=2", "The console")}
    world.studio.presets = {4: _preset(f"{world.station_url}?c=4", "The studio")}
    hallway_released, console_released = asyncio.Event(), asyncio.Event()
    world.hallway.held["/now_playing"] = hallway_released
    world.console.held["/now_playing"] = console_released
    options = replace(_options(world, tmp_path, seed=True), consoles_allowed=(CONSOLE_ID,))
    logs: list[str] = []
    frames = _Frames()

    async def the_console_answers() -> None:
        console_released.set()
        await eventually(lambda: _said(logs, f"probe: Bose Cinema: {RADIO}"), "the console's answer was taken")

    async with _running(options, logs, ports=_relaying(frames)) as service:
        try:
            await eventually(lambda: _said(logs, f"probe: Bose Studio: {SourceName.STANDBY}"), "the studio was asleep")
            world.registry.body = json.dumps(entries)
            await eventually(lambda: "/now_playing" in world.hallway.paths(), "the hallway was asked what it plays")
            await eventually(lambda: "/now_playing" in world.console.paths(), "the console was asked what it plays")
            if not frame_before_second_answer:
                await the_console_answers()
            await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
            await eventually(
                lambda: _read_to_the_end(service, frames, STUDIO_ID, "nowPlayingUpdated"),
                "the reader read the studio switching on while the round was out",
            )
            if frame_before_second_answer:
                await the_console_answers()
            assert not hallway_released.is_set(), "the control: the hallway's answer was still on the wire"
            assert _channels_of(options).channels == (), "seeded while a box of the round had still to answer"
        finally:
            hallway_released.set()
            console_released.set()
        await eventually(lambda: _said(logs, f"probe: Bose Hallway: {RADIO}"), "the hallway's answer was taken")
        await eventually(lambda: _channels_of(options).channels != (), "the list was seeded once the round was over")
        assert _channels_of(options).numbers_in_order() == ("1", "3"), "the hallway, first of its round, seeded it"


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
        _channel_file(options),
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
    with contextlib.suppress(OSError):
        await writer.wait_closed()


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
        _channel_file(options),
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

    await eventually(lambda: _state_of(options).channel == "12", "it was written down")

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
        _channel_file(options),
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

    assert _state_of(options).channel != "13", (
        "a box that is not in the zone must not move the number the zone comes back on"
    )


ORION_RELATIVE = "/station?data=eyJuYW1lIjoiT3Jpb24ifQ%3D%3D"
"""A channel as AfterTouch writes a preset: the base is left to whoever plays it."""


def _patient_locations(service_url: str, /, *, log: LogFn) -> OrionBase:
    """The production resolver with a registry deadline no test waits out: a held read stays held."""
    return OrionBase(service_url, log=log, timeout_s=60.0)


def _registry_reads_running() -> bool:
    """Whether a read of the bmx registry is still running anywhere on this loop."""
    return any(
        getattr(task.get_coro(), "__qualname__", "") == "OrionBase._base"
        for task in asyncio.all_tasks()
        if not task.done()
    )


def _orion_world(world: World, tmp_path: Path, *, relative: tuple[str, ...]) -> ServiceOptions:
    """Channels 1, 12 and 13, the ones named in ``relative`` written as a RELATIVE Orion location."""
    options = _options(world, tmp_path, seed=True, dial_window_s=0.5)
    save_channels(
        _channel_file(options),
        ChannelList(
            channels=tuple(
                Channel(
                    number=number,
                    name=f"C{number}",
                    kind=ChannelKind.RADIO,
                    url=ORION_RELATIVE if number in relative else f"{world.station_url}?c={number}",
                )
                for number in ("1", "12", "13")
            )
        ),
    )
    return options


async def _out_of_multiroom_and_awake(world: World, service: ZoneService) -> None:
    """The studio double-tapped out of the group, released, handed its channel, and playing again."""
    await _double_tap_key(STUDIO_IP, KeyName.THUMBS_DOWN)
    await eventually(lambda: STUDIO_IP not in _slaves(service), "the studio left the zone")
    await eventually(lambda: world.studio.bodies_for("/select") != [], "and got the house's channel")
    await eventually(lambda: not service.policy.is_asleep(STUDIO_ID), "and said it is playing again")


def _station_name(service: ZoneService) -> str | None:
    """The NAME of what the zone plays: a relative channel's playback url is the same for all of them."""
    master = service.master
    return master.station.name if master is not None and master.station is not None else None


async def test_a_relative_channel_reaches_every_box_absolute_from_the_run_s_first_station(
    world: World, tmp_path: Path
) -> None:
    """What a speaker is SENT is absolute only against a base the registry has already named.

    The service reads the registry in the background as it starts, so by the time a box is
    switched on the base is known, and the run's FIRST station already shows its slaves the
    absolute location. Before that read existed, building that first item was what started it,
    and every run began with its slaves shown the location as stored. The barrier is the read's
    own log line, never a station: that is the precondition the rule states, and in the house a
    person switches a box on seconds or hours after the start, not milliseconds.

    From then on each document a box is sent carries the absolute location too: the item of the
    next channel the zone plays, the ``/select`` that hands a released box its channel, and the
    ``/select`` of a number dialled on a box out of multiroom. One registry read serves the whole
    run, the zone's own fetch included.
    """
    station_base = world.station_url.removesuffix("/live")
    world.registry.bodies["/bmx/registry/v1/services"] = json.dumps(
        {"bmx_services": [{"id": {"name": "LOCAL_INTERNET_RADIO"}, "baseUrl": f"{station_base}/orion"}]}
    )
    absolute = f"{station_base}/orion{ORION_RELATIVE}"
    options = _orion_world(world, tmp_path, relative=("1", "13"))
    logs: list[str] = []

    async with _running(options, logs) as service:
        await eventually(
            lambda: _said(logs, f"bmx registry names {RADIO} at {station_base}/orion"),
            "the start read the bmx registry without a station asking for it",
        )
        await _both_wake(world)
        await eventually(lambda: _station_name(service) == "C1", "the zone plays channel 1")
        # What a slave reads back from the master's own HTTP face: the item the zone is playing.
        first = await http_get(MASTER, "/now_playing")
        assert f'location="{absolute}"' in first, "the run's first station is shown to its slaves absolute"
        await eventually(
            lambda: any(f"GET /orion{ORION_RELATIVE} " in fetch for fetch in world.fetches),
            "the zone fetched it at the base the registry names",
        )

        await _press_preset(world.studio, STUDIO_ID, 1)
        await _press_preset(world.studio, STUDIO_ID, 3)
        await eventually(lambda: _station_name(service) == "C13", "the zone moved to channel 13")
        shown = await http_get(MASTER, "/now_playing")
        assert f'location="{absolute}"' in shown, "the next item the zone shows its slaves is absolute"

        await _out_of_multiroom_and_awake(world, service)
        assert world.studio.played[0] == absolute, "the released box was handed the absolute location"

        on_its_own = len(world.studio.bodies_for("/select"))
        await _press_preset(world.studio, STUDIO_ID, 1)
        await eventually(lambda: len(world.studio.bodies_for("/select")) > on_its_own, "it dialled 1 for itself")
        assert world.studio.played[-1] == absolute, "and a number dialled on its own is sent absolute too"

    selections = [body for box in (world.studio, world.hallway) for body in box.bodies_for("/select")]
    assert selections, "the control: something was selected at all"
    assert not any(f'location="{ORION_RELATIVE}"' in body for body in selections), "no /select went out relative"
    stored = _channels_of(options).by_number("1")
    assert stored is not None
    assert stored.url == ORION_RELATIVE, "the channel list keeps the location as it was written"
    assert world.registry.paths.count("/bmx/registry/v1/services") == 1, "one registry read served them all"


async def test_a_relative_channel_is_sent_as_stored_when_the_registry_cannot_say(world: World, tmp_path: Path) -> None:
    """A box out of multiroom dials a relative channel while the registry names no Orion base.

    The ``/select`` carries the location exactly as the channel stores it, for the speaker to
    complete through its own registry. It must NOT carry the fallback base: that is a guess about
    the service's layout, built from ``[registry] url``, and the shipped value of that is the
    loopback - a box handed it asks ITSELF for the station, stays silent, and a box that stores
    it keeps it for good. Channel 1 is absolute so the zone itself needs no registry.
    """
    options = _orion_world(world, tmp_path, relative=("13",))
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        await _out_of_multiroom_and_awake(world, service)
        on_its_own = len(world.studio.bodies_for("/select"))
        await _press_preset(world.studio, STUDIO_ID, 1)
        await _press_preset(world.studio, STUDIO_ID, 3)
        await eventually(lambda: len(world.studio.bodies_for("/select")) > on_its_own, "it dialled 13 for itself")
        assert world.studio.played[-1] == ORION_RELATIVE, "sent as stored"
        await eventually(
            lambda: "/bmx/registry/v1/services" in world.registry.paths,
            "and the registry was asked, so a later document could be absolute had it answered",
        )

    selections = [body for box in (world.studio, world.hallway) for body in box.bodies_for("/select")]
    assert not any(ORION_FALLBACK_PATH in body for body in selections), "no box was handed the fallback base"
    assert any("as stored" in line for line in logs), "the log says the location went out as stored"


async def test_a_number_dialled_while_an_earlier_one_waits_on_the_registry_is_the_one_that_plays(
    world: World, tmp_path: Path
) -> None:
    """The later number wins, however long the registry keeps the earlier one's station waiting.

    Channel starts run as concurrent tasks and ``play`` takes its generation on entry, so a start
    that waited for anything BEFORE ``play`` - the registry, for the item the zone shows its slaves -
    let a number dialled after it reach ``play`` first and then lose to it: the house played 12
    while it recorded 13. The registry is held here from the start - the start's own read of it is
    what 12's fetch queues behind - until 13 plays, and released only then, which is exactly the
    order that inverted the two.
    """
    station_base = world.station_url.removesuffix("/live")
    world.registry.bodies["/bmx/registry/v1/services"] = json.dumps(
        {"bmx_services": [{"id": {"name": "LOCAL_INTERNET_RADIO"}, "baseUrl": f"{station_base}/orion"}]}
    )
    answer = asyncio.Event()
    world.registry.held["/bmx/registry/v1/services"] = answer
    options = _orion_world(world, tmp_path, relative=("12",))
    logs: list[str] = []
    # The read the start begins is the one 12's fetch queues behind, so it must outlast the whole
    # test rather than give up after the shipped two seconds and let 12 through on the fallback.
    ports = replace(build_production().zone_ports, open_locations=_patient_locations)

    try:
        async with _running(options, logs, ports=ports) as service:
            await _both_wake(world)
            await eventually(lambda: _station_name(service) == "C1", "the zone plays channel 1")

            await _press_preset(world.studio, STUDIO_ID, 1)
            await _press_preset(world.studio, STUDIO_ID, 2)
            await eventually(lambda: _said(logs, "dialled 12: C12"), "12 was dialled")
            await eventually(
                lambda: "/bmx/registry/v1/services" in world.registry.paths, "and the registry is being read"
            )
            assert not _said(logs, "relative location ->"), "the control: 12's fetch is still waiting on it"
            await _press_preset(world.studio, STUDIO_ID, 1)
            await _press_preset(world.studio, STUDIO_ID, 3)
            await eventually(lambda: _station_name(service) == "C13", "the later number plays")
            assert not answer.is_set(), "the control: 13 got there while the registry still held 12"

            answer.set()
            await eventually(
                lambda: _said(logs, "a newer select won, dropping C12"),
                "the earlier number gave up once the registry let it through",
                timeout=15.0,
            )
            assert _station_name(service) == "C13", "and the house still plays the later one"
    finally:
        answer.set()

    assert _state_of(options).channel == "13", "the number the house records is the one it plays"


async def test_stopping_the_service_ends_a_registry_read_still_waiting_on_an_answer(
    world: World, tmp_path: Path
) -> None:
    """A stop does not wait out the bmx registry, and leaves no read of it running behind it.

    The start reads the registry in the background, and a registry that took the connection and
    said nothing holds that read for as long as its deadline - which is stretched here past the
    test, so what ends the read can only be the stop. A read left behind would go on holding a
    connection to a neighbour after the service has gone.
    """
    answer = asyncio.Event()
    world.registry.held["/bmx/registry/v1/services"] = answer
    options = _options(world, tmp_path)
    logs: list[str] = []
    ports = replace(build_production().zone_ports, open_locations=_patient_locations)

    try:
        async with _running(options, logs, ports=ports):
            await eventually(
                lambda: "/bmx/registry/v1/services" in world.registry.paths, "the start is reading the registry"
            )
            assert _registry_reads_running(), "the control: the read is still waiting when the stop comes"
        assert not _registry_reads_running(), "a registry read outlived the service"
    finally:
        answer.set()


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
    return _state_of(options).owed_volume


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
    save_state(_state_file(options), LegacyState(state=ZoneState(owed_volume={HALLWAY_ID: -30})))

    async with _running(options, logs):
        await world.hallway.notify(now_playing_frame(device_id=HALLWAY_ID, source=RADIO))
        await eventually(lambda: len(joins(world.hallway)) == 1, "the hallway joined")
        await eventually(lambda: owed_volume(options) == {}, "what it owed was taken")
        await asyncio.sleep(MUTE_HOLD_S + FADE_DEFAULT_S + 0.3)

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
    written = float(_preferences_of(options)["dialling.window_s"])
    assert written == pytest.approx(measured, abs=0.05), "and what it measured was written down"


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
    save_state(_state_file(options), LegacyState(state=ZoneState(channel="1", members=()), dial_window_s=1.5))
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
    written = float(_preferences_of(options)["dialling.hold_threshold_s"])
    assert written == pytest.approx(measured), "and what it measured was written down"


async def test_a_calibrated_hold_threshold_is_what_a_restart_holds_with(world: World, tmp_path: Path) -> None:
    """It outlives the run that measured it. Proved by PRESSING, not by the startup line.

    The thumb is down 1.3 s, twice: past the default threshold of 1.0 s, so without the remembered
    1.8 s it would be two holds and move the rotation; with it, it is a double tap and takes the box
    out of multiroom instead.
    """
    options = _rotation_world(world, tmp_path, numbers=("1", "12", "13"))
    save_state(
        _state_file(options),
        LegacyState(state=ZoneState(channel="1", members=()), dial_window_s=0.5, hold_threshold_s=1.8),
    )
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
            _channel_file(options),
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
            _channel_file(options),
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
            _channel_file(options),
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
            _channel_file(options),
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
            _channel_file(options),
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
        _channel_file(options),
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
    return _state_of(options).positions


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


class _StoreThatCanFail:
    """A real store, wrapped so ``save_state`` or ``load_preferences`` can be made to fail once armed.

    Everything else goes straight to the real store underneath. ``save_state`` is the one call
    ``_stand_down`` makes through ``_remember_where_mpd_is`` before it dissolves the zone;
    ``load_preferences`` is what the preference watch reads every switch poll.
    """

    def __init__(self, real: HouseStore) -> None:
        self._real = real
        self.saving_state_fails = False
        self.saves_to_fail = 0
        """How many of the NEXT saves fail, each once, before the store writes again."""
        self.reading_preferences_fails = False
        self.save_gate: threading.Event | None = None
        """When set, every save waits for this event first: a database that stopped answering."""
        self.refused_saves = 0
        """How many saves this store refused: each is one failed write, to be said exactly once."""
        self.closed = threading.Event()
        self.where = real.where

    def open(self, *, exclusive: bool, create: bool = True) -> None:
        self._real.open(exclusive=exclusive, create=create)

    def close(self) -> None:
        self._real.close()
        self.closed.set()

    def import_legacy(self, files: LegacyFiles) -> None:
        self._real.import_legacy(files)

    def load_state(self) -> ZoneState:
        return self._real.load_state()

    def save_state(self, state: ZoneState) -> None:
        if self.save_gate is not None:
            self.save_gate.wait()
        if self.saving_state_fails or self.saves_to_fail > 0:
            self.saves_to_fail = max(0, self.saves_to_fail - 1)
            self.refused_saves += 1
            message = "simulated: the house database refused the write"
            raise StoreError(message)
        self._real.save_state(state)

    def load_channels(self) -> ChannelList:
        return self._real.load_channels()

    def save_channels(self, channels: ChannelList) -> None:
        self._real.save_channels(channels)

    def export_channels(self, path: Path) -> ChannelsExport:
        return self._real.export_channels(path)

    def import_channels(self, path: Path) -> ChannelList:
        return self._real.import_channels(path)

    def is_on(self) -> bool:
        return self._real.is_on()

    def set_switch(self, *, on: bool) -> bool:
        return self._real.set_switch(on=on)

    def load_preferences(self) -> tuple[PreferenceRow, ...]:
        if self.reading_preferences_fails:
            message = "simulated: the house database could not be read"
            raise StoreError(message)
        return self._real.load_preferences()

    def set_preference(
        self, name: PreferenceName, value: PreferenceValue, *, source: PreferenceSource
    ) -> PreferenceRow | None:
        return self._real.set_preference(name, value, source=source)

    def unset_preference(self, name: PreferenceName) -> PreferenceRow | None:
        return self._real.unset_preference(name)


def _ports_with_a_store_that_can_fail(
    created: list[_StoreThatCanFail], *, stop_bound_s: float | None = None
) -> ZoneServicePorts:
    """The production ports, except that the store the service opens is the wrapper above.

    Injected at the ``open_store`` port, which is exactly the seam the service already takes a
    ``HouseStore`` through, never a monkeypatch of the store's own internals. ``stop_bound_s``
    shortens how long the worker's close waits for a call that never ends, through the worker's
    own constructor at the ``off_the_loop`` port.
    """

    def _open_store(database: str, *, password: Secret | None, log: LogFn) -> HouseStore:
        store = _StoreThatCanFail(open_house_store(database, password=password, log=log))
        created.append(store)
        return store

    production = build_production().zone_ports
    if stop_bound_s is None:
        return replace(production, open_store=_open_store)

    def _off_the_loop(store: HouseStore, /, *, log: LogFn) -> ServiceStore:
        return StoreWorker(store, log=log, stop_bound_s=stop_bound_s)

    return replace(production, open_store=_open_store, off_the_loop=_off_the_loop)


@asynccontextmanager
async def _running_with_a_store_that_can_fail(
    options: ServiceOptions, logs: list[str]
) -> AsyncGenerator[tuple[ZoneService, _StoreThatCanFail], None]:
    """The real wiring, except the store the service opens is the wrapper above."""
    created: list[_StoreThatCanFail] = []
    ports = _ports_with_a_store_that_can_fail(created)
    service = ZoneService(options, log=recording_into(logs), ports=ports)
    task = asyncio.create_task(service.run())
    try:
        await eventually(lambda: len(created) == 1, "the service opened its store")
        yield service, created[0]
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_a_stop_that_lands_inside_the_dissolve_still_dissolves_the_zone_and_frees_the_ports(
    world: World, tmp_path: Path
) -> None:
    """A stop that arrives while the switch-off is still telling the boxes must finish the job.

    The dissolve is one HTTP call per box, sent one after another, and a real box takes tens of
    milliseconds to answer each - so a SIGINT right after ``switch off`` (which is how a deploy
    stops the unit) lands inside it. Whatever the stop interrupted, the box that was not told yet
    must still be told, and the master's four listeners must still be closed: a master nobody
    holds any more keeps its ports until the process exits, and the boxes it never reached stay in
    a zone whose master has gone.
    """
    options = _options(world, tmp_path)
    logs: list[str] = []

    async with _running(options, logs):
        await _both_wake(world)
        for box in (world.studio, world.hallway):
            box.slow["POST /setZone"] = 1.0
        _flip(options, on=False)
        await eventually(
            lambda: len(dissolves(world.studio)) + len(dissolves(world.hallway)) == 1,
            "the first box has the dissolve and the service is waiting for its answer",
        )
    # The stop has run to its end here: _running waited for the service task.
    assert len(dissolves(world.studio)) >= 1, "the studio was told the zone is over"
    assert len(dissolves(world.hallway)) >= 1, "the hallway was told the zone is over"
    await nothing_answers_on(MASTER, 8090, timeout=2.0)
    await nothing_answers_on(MASTER, 40002, timeout=2.0)


async def test_a_store_that_cannot_save_state_does_not_stop_the_dissolve(world: World, tmp_path: Path) -> None:
    """A StoreError while remembering where MPD was must not abort the stand-down: the speakers
    still hear the zone dissolve, and the failure is logged rather than swallowed or left to leave
    the house bound to a master that is already gone."""
    async with _mpd(status_lines=PLAYING_AT_61_5) as fake:
        options = _radio_and_mpd(world, tmp_path, fake)
        logs: list[str] = []

        async with _running_with_a_store_that_can_fail(options, logs) as (service, store):
            await _both_wake(world)
            await _press_preset(world.studio, STUDIO_ID, 1)
            await _press_preset(world.studio, STUDIO_ID, 2)
            await eventually(lambda: _playing(service).endswith("?c=12"), "the zone is on the MPD channel")

            store.saving_state_fails = True
            _flip(options, on=False)

            await eventually(
                lambda: len(dissolves(world.studio)) == 1 and len(dissolves(world.hallway)) == 1,
                "both boxes were told the zone is over although the state write failed",
            )
            await eventually(lambda: service.master is None, "the master is gone, not merely idle")
            await eventually(
                lambda: any("could not remember where mpd was" in line for line in logs),
                "the failed write was logged rather than swallowed",
            )
        assert positions_of(options) == {}, "the position that failed to save is not there afterwards"
        # Every failed write said ONCE: by the one who knows what it was for when somebody awaited
        # it, and generically only when nobody did. A generic line as well would put two errors in
        # the log for one write, and the second tells the reader nothing.
        assert sum("could not remember where mpd was" in line for line in logs) == 1, logs
        assert len(_failed_saves_said(logs)) == store.refused_saves, logs


def _failed_saves_said(logs: list[str]) -> list[str]:
    """Every line saying a state save failed, whoever said it: the caller, the service, or the store."""
    saying = (
        "could not remember where mpd was, so the house dissolves without it",
        "the house state was not saved",
        "save_state failed after its caller had stopped waiting",
        "fading back up failed",
    )
    return [line for line in logs if any(said in line for said in saying)]


STAND_DOWN_WITHIN_S = STAND_DOWN_SAVE_S + 3.0
"""How long a stop may take while the house database hangs: the stand-down's bounded save, the
worker's shortened close bound below, and the dissolve itself, with room for a busy machine."""


async def test_a_database_that_hangs_at_the_stop_does_not_keep_the_zone_from_being_dissolved(
    world: World, tmp_path: Path
) -> None:
    """The stop is bounded however long the database takes: the speakers are waiting on it.

    A PostgreSQL host in a network black hole keeps a call in ``recv`` for minutes, and nothing on
    the loop can interrupt a call on the database thread. The stand-down's own save - where MPD had
    got to - queues behind it, and it came BEFORE the dissolve, so the dissolve waited too; then the
    close waited, and the process could not exit. Here the database stops answering just before the
    stop, and the stop must still tell both boxes, close the master's ports and end the run within
    a bound. A save is not lost by giving up on it: it stays queued, and here the database then
    refuses every one that was waiting, each of which is said exactly once - by the worker when
    whoever asked had already stopped waiting, as the stand-down has.
    """
    async with _mpd(status_lines=PLAYING_AT_61_5) as fake:
        options = _radio_and_mpd(world, tmp_path, fake)
        logs: list[str] = []
        created: list[_StoreThatCanFail] = []
        service = ZoneService(
            options, log=recording_into(logs), ports=_ports_with_a_store_that_can_fail(created, stop_bound_s=0.5)
        )
        task = asyncio.create_task(service.run())
        gate = threading.Event()
        try:
            await eventually(lambda: len(created) == 1, "the service opened its store")
            (store,) = created
            await _both_wake(world)
            await _press_preset(world.studio, STUDIO_ID, 1)
            await _press_preset(world.studio, STUDIO_ID, 2)
            await eventually(lambda: _playing(service).endswith("?c=12"), "the zone is on the MPD channel")

            store.save_gate = gate
            store.saving_state_fails = True
            started = time.monotonic()
            task.cancel()
            done, _pending = await asyncio.wait({task}, timeout=STAND_DOWN_WITHIN_S + 5.0)
            took = time.monotonic() - started
        finally:
            gate.set()
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        assert task in done, f"the stop was still waiting on the database after {took:.1f} s"
        assert took < STAND_DOWN_WITHIN_S, f"the stop took {took:.1f} s"
        assert len(dissolves(world.studio)) >= 1, "the studio was told the zone is over"
        assert len(dissolves(world.hallway)) >= 1, "the hallway was told the zone is over"
        await nothing_answers_on(MASTER, 8090, timeout=2.0)
        assert any("could not remember where mpd was" in line for line in logs), logs

        await eventually(store.closed.is_set, "the close queued behind the stuck save ran once it ended")
        assert store.refused_saves >= 1, "the control: the stuck saves really did fail once let through"
        assert len(_failed_saves_said(logs)) == store.refused_saves, logs


async def test_a_save_that_fails_at_the_end_of_a_fade_is_said_once_in_the_house_log(
    world: World, tmp_path: Path
) -> None:
    """The fade runs on a task nobody awaits, so whatever its last save raises must be said by it.

    A fade that reaches the top writes the box's level down as put back, and that save is awaited:
    a store that refuses it raises into the fade task. Nothing retrieves that task - the stop only
    waits for fades still running - so the failure used to surface only as asyncio's own "Task
    exception was never retrieved", at whatever moment the task was collected, in a log that is not
    the house's. The box is at its level either way; what the house log needs is the one line.
    """
    complaints: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: complaints.append(context))
    options = _options(world, tmp_path)
    logs: list[str] = []
    world.studio.volume = 24
    try:
        async with _running_with_a_store_that_can_fail(options, logs) as (_service, store):
            await _wake_on_preset(world.studio, STUDIO_ID, 1)
            # Armed once the climb has begun: the join's own saves - the level before the mute, and
            # who the zone belongs to - are behind it by then, so the save refused is the fade's last.
            await eventually(lambda: len(_volume_writes(world.studio)) >= 2, "the climb has begun")
            store.saves_to_fail = 1
            await eventually(lambda: store.refused_saves == 1, "the fade's last save was refused")
            assert world.studio.volume == 24, "the control: the fade reached the top before its save failed"
            await eventually(lambda: _failed_saves_said(logs) != [], "the failed save was said")
            gc.collect()
            for _ in range(3):
                await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous)
    assert len(_failed_saves_said(logs)) == store.refused_saves == 1, logs
    assert complaints == [], f"asyncio had nothing of its own to report: {complaints}"


STALL_S = 0.2
"""How long every call to the slow store below sleeps: most of ten frame periods, so a loop that
waited for one would miss its clock and its frames by a margin no scheduler could explain."""


@asynccontextmanager
async def _running_on_a_slow_store(
    options: ServiceOptions, logs: list[str], *, stall_s: float
) -> AsyncGenerator[tuple[ZoneService, SlowStore], None]:
    """The real wiring, except the store the service opens is a :class:`SlowStore` over the real one.

    Injected at the ``open_store`` port, the seam the service takes its store through, so what the
    service does with it - which thread, in which order, and whether the loop waits - is the
    production wiring's answer and not the test's.
    """
    created: list[SlowStore] = []

    def _open_store(database: str, *, password: Secret | None, log: LogFn) -> HouseStore:
        store = SlowStore(open_house_store(database, password=password, log=log), stall_s=stall_s)
        created.append(store)
        return store

    ports = replace(build_production().zone_ports, open_store=_open_store)
    service = ZoneService(options, log=recording_into(logs), ports=ports)
    task = asyncio.create_task(service.run())
    try:
        await eventually(lambda: len(created) == 1, "the service opened its store")
        yield service, created[0]
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _beat(gaps: list[float]) -> None:
    """Turn on the loop every few milliseconds and write down how long each turn really took.

    A task of the service's own loop, so it can only run when nothing else holds that loop: a gap
    far longer than the sleep is the loop having been kept from turning, which is exactly what the
    zone's clock and frames would feel.
    """
    last = time.monotonic()
    while True:
        await asyncio.sleep(0.005)
        now = time.monotonic()
        gaps.append(now - last)
        last = now


async def test_the_loop_keeps_turning_while_the_house_database_is_slow(world: World, tmp_path: Path) -> None:
    """No database call runs on the event loop, on either backend (backlog rank 199).

    The loop that serves the house also times the zone: the clock on UDP 40005 and the frames on
    TCP 40003 are answered from it. Measured 2026-09-29, a state save on PostgreSQL took up to
    10.75 ms and one on SQLite, with ``synchronous = FULL``, stalled for 20 to 24 ms - most of a
    frame period, on that loop. Every call here sleeps far longer than either, so the gap it would
    leave in the heartbeat cannot be mistaken for scheduling noise.
    """
    options = _options(world, tmp_path)
    logs: list[str] = []
    gaps: list[float] = []
    beating = asyncio.create_task(_beat(gaps))
    try:
        async with _running_on_a_slow_store(options, logs, stall_s=STALL_S) as (_service, store):
            # One box waking is enough: the pass that takes it writes down who the zone belongs
            # to, which is a state save, and the switch and the preferences are polled throughout.
            await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
            await eventually(
                lambda: bool(store.named("save_state")) and len(store.named("is_on")) >= 2,
                "the service saved its state and polled the switch, both through the slow store",
            )
    finally:
        beating.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beating
    assert store.stalls >= 5, "the control: the database really was slow, call after call"
    assert gaps, "the control: the heartbeat ran"
    assert max(gaps) < STALL_S / 2, f"the loop stood still for {max(gaps) * 1000:.0f} ms while the database worked"


async def test_every_database_call_the_service_makes_runs_on_one_thread_that_ends_with_the_run(
    world: World, tmp_path: Path
) -> None:
    """One thread, and never the loop's: open first, every read and write, close last, all there.

    One, because a single worker is what keeps two saves in the order they were asked for and keeps
    a SQLite connection on the thread that made it; and it must be gone once the run has ended, or
    every restart in a long-lived process would leave one behind.
    """
    options = _options(world, tmp_path)
    logs: list[str] = []
    loop_thread = threading.current_thread()

    async with _running_on_a_slow_store(options, logs, stall_s=0.0) as (_service, store):
        await _both_wake(world)
        await eventually(
            lambda: bool(store.named("save_state")) and len(store.named("is_on")) >= 2,
            "the service saved its state and polled the switch",
        )

    names = [call.name for call in store.calls]
    assert names[0] == "open", names[:3]
    assert names[-1] == "close", names[-3:]
    threads = {call.thread for call in store.calls}
    assert len(threads) == 1, sorted(thread.name for thread in threads)
    (worker,) = threads
    assert worker is not loop_thread, "a database call ran on the event loop"
    assert not worker.is_alive(), "the database thread outlived the run"


async def test_a_write_still_queued_when_the_service_stops_is_written_before_the_database_is_closed(
    world: World, tmp_path: Path
) -> None:
    """A stop must not lose what the house decided just before it.

    A double thumbs down is read on the reader, which may not wait, so its save is queued rather
    than awaited - and here that save is slow enough to still be running when the stop has done
    everything else and comes to close the database. The run ends only once it has landed, and
    every save queued behind it: a restart must find the box still out.
    """
    options = _rotation_world(world, tmp_path, numbers=("1", "12", "13"))
    logs: list[str] = []

    async with _running_on_a_slow_store(options, logs, stall_s=0.0) as (_service, store):
        await _both_wake(world)
        # Both fades have put their boxes back and saved that, so the next save is the reader's.
        await eventually(lambda: not _state_of(options).muted, "the joins have finished writing")
        store.save_stalls = [1.5]
        readers_save = len(store.named("save_state"))
        await _double_tap_key(STUDIO_IP, KeyName.THUMBS_DOWN)
        await eventually(
            lambda: any("out of multiroom, on its own from here" in line for line in logs), "the double tap was read"
        )
        assert STUDIO_ID not in out_of_multiroom(options), "the control: the write is still on its way at the stop"

    assert out_of_multiroom(options) == (STUDIO_ID,), "the queued write reached the database before it was closed"
    assert store.calls[-1].name == "close", [call.name for call in store.calls[-3:]]
    # The READER's save, not a later one that happens to carry the same flag: it is the first save
    # asked for after the tap, and it was taken before the pass let the studio go, so the studio is
    # still one of the members it records. A pass's save after the release would not list it.
    first_after_the_tap = store.saved[readers_save]
    assert first_after_the_tap.out_of_multiroom == (STUDIO_ID,), first_after_the_tap
    assert STUDIO_ID in first_after_the_tap.members, "the first save after the tap was the reader's own"


async def test_a_save_the_reader_could_not_wait_for_is_said_when_it_fails_and_the_house_goes_on(
    world: World, tmp_path: Path
) -> None:
    """A double thumbs down is read on the reader, which queues its save and goes on.

    So a save that fails there has nobody to raise into. It must be said - a lost write is the one
    thing worth a line - and it must not take the service with it: the decision is still held,
    and the next save writes the whole state again.
    """
    options = _rotation_world(world, tmp_path, numbers=("1", "12", "13"))
    logs: list[str] = []

    async with _running_with_a_store_that_can_fail(options, logs) as (service, store):
        await _both_wake(world)
        # Both fades have put their boxes back and saved that, so the next save is the reader's.
        await eventually(lambda: not _state_of(options).muted, "the joins have finished writing")
        store.saves_to_fail = 1
        await _double_tap_key(STUDIO_IP, KeyName.THUMBS_DOWN)

        await eventually(
            lambda: any(line.startswith(f"{ERROR_KIND}: the house state was not saved") for line in logs),
            "the failed save was said",
        )
        await eventually(lambda: STUDIO_IP not in _slaves(service), "the house went on and let the studio go")
        assert service.master is not None, "the zone is still held"
        await eventually(
            lambda: out_of_multiroom(options) == (STUDIO_ID,), "the next save wrote the decision the failed one lost"
        )


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
        _channel_file(options),
        ChannelList(
            channels=(
                Channel(number="1", name="Quick", kind=ChannelKind.RADIO, url=f"{world.station_url}?c=1"),
                Channel(number="2", name="Slow", kind=ChannelKind.RADIO, url=slow_url),
                Channel(number="3", name="Third", kind=ChannelKind.RADIO, url=f"{world.station_url}?c=3"),
            )
        ),
    )
    save_state(_state_file(options), LegacyState(state=ZoneState(channel="1", members=())))
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
        _channel_file(options),
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
        save_state(
            _state_file(options),
            LegacyState(state=ZoneState(positions={"12": Place(track=1, seconds=61.5, file="Buch/10.mp3")})),
        )
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


async def test_a_console_allowed_in_the_house_database_is_taken_in_when_it_wakes(world: World, tmp_path: Path) -> None:
    """A stored preference decides from the first pass: the console joins, although no config file names it."""
    options = _options(world, tmp_path)
    assert options.consoles_allowed == (), "the control: nothing but the stored row allows it"
    store = _store_of(options)
    try:
        store.set_preference(PreferenceName.CONSOLES, (CONSOLE_ID,), source=PreferenceSource.CLI)
    finally:
        store.close()
    logs: list[str] = []

    async with _running(options, logs):
        await eventually(
            lambda: any(f"consoles allowed into the zone: {CONSOLE_ID}, set by cli" in line for line in logs),
            "it said so",
        )
        await world.console.notify(now_playing_frame(device_id=CONSOLE_ID, source=RADIO))
        await eventually(lambda: len(joins(world.console)) == 1, "the console was taken into the zone")


# --- Live preferences: the house database read again every switch poll --------------------------


def _said(logs: list[str], text: str) -> bool:
    return any(text in line for line in logs)


async def test_a_window_set_while_the_house_runs_decides_the_next_number(world: World, tmp_path: Path) -> None:
    """2.0 s from the options, 0.5 s from the house database a moment later: 1 then 2 a second apart
    are two numbers now, and 2 is no channel."""
    options = _dialable_world(world, tmp_path, dial_window_s=2.0)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        _set_preference(options, PreferenceName.WINDOW, 0.5)
        await eventually(lambda: _said(logs, "the dialling window is 0.5 s, set by cli"), "it took it")
        await _press_preset(world.studio, STUDIO_ID, 1)
        await asyncio.sleep(1.0)
        await _press_preset(world.studio, STUDIO_ID, 2)
        await eventually(lambda: _said(logs, "dialled 2: no such channel"), "two numbers, not one")
        assert _playing(service).endswith("?c=1")


async def test_a_window_changed_mid_number_waits_until_that_number_is_read(world: World, tmp_path: Path) -> None:
    """The number somebody is typing is read on the window it began with.

    The change is taken in while the first digit is open - the line saying so is the barrier - and
    the second digit then lands a second later: inside the 2.0 s the number began with, outside the
    0.5 s it would have if the dialler had been handed the new window at once.
    """
    options = _dialable_world(world, tmp_path, dial_window_s=2.0)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        await _press_preset(world.studio, STUDIO_ID, 1)
        _set_preference(options, PreferenceName.WINDOW, 0.5)
        await eventually(lambda: _said(logs, "the dialling window is 0.5 s, set by cli"), "it took it")
        await asyncio.sleep(1.0)
        await _press_preset(world.studio, STUDIO_ID, 2)
        await eventually(lambda: _playing(service).endswith("?c=12"), "still one number, 12")


async def test_a_hold_changed_while_a_key_is_down_decides_the_next_hold(world: World, tmp_path: Path) -> None:
    """The key somebody is holding is decided on the threshold it went down with.

    2.0 s from the options, 1.0 s from the house database while the first key is down - the line
    saying so is the barrier - and that key comes up at 1.5 s: a tap on the 2.0 s it began with,
    where the new 1.0 s would already have made it a hold. On a station a held step moves the
    channel as a tap does, so the "held" line is what tells the two apart, and the step is what
    shows the release was read at all. The next key, down for the same 1.5 s, is a hold.

    The step arriving also orders the second press after the hand-over: the dialling worker hands
    the numbers over on its very next turn after booking the step, and the channel only shows as
    playing once a separate start has reached the station.
    """
    options = replace(_dialable_world(world, tmp_path, dial_window_s=WINDOW_DEFAULT_S), hold_threshold_s=2.0)
    down_for_s = 1.5
    held = f"held {KeyName.NEXT_TRACK} for"
    logs: list[str] = []
    loop = asyncio.get_running_loop()

    async with _running(options, logs) as service:
        await _both_wake(world)
        assert _playing(service).endswith("?c=1")
        pressed_at = loop.time()
        await _forward_key(STUDIO_IP, KeyName.NEXT_TRACK, KeyState.PRESS)
        _set_preference(options, PreferenceName.HOLD, 1.0)
        await eventually(lambda: _said(logs, "a key is held after 1.0 s, set by cli"), "it took it")
        assert loop.time() < pressed_at + 1.0, "the control: taken in while the key could not yet be a hold"
        await asyncio.sleep(pressed_at + down_for_s - loop.time())
        await _forward_key(STUDIO_IP, KeyName.NEXT_TRACK, KeyState.RELEASE)
        await eventually(lambda: _playing(service).endswith("?c=12"), "the key that was down stepped")
        assert not _said(logs, held), "and it was a tap, on the 2.0 s it went down with"

        await _forward_key(STUDIO_IP, KeyName.NEXT_TRACK, KeyState.PRESS)
        await asyncio.sleep(down_for_s)
        await _forward_key(STUDIO_IP, KeyName.NEXT_TRACK, KeyState.RELEASE)
        await eventually(lambda: _said(logs, held), "the next key held as long is a hold, on the new 1.0 s")


async def test_a_console_taken_off_the_list_leaves_the_zone(world: World, tmp_path: Path) -> None:
    """Allowed only by a stored row, and let go once that row is gone: nothing else changed.

    Nothing else asks for a pass either, so the pass that lets it go is the one the preference
    asks for. The registry poll, which asks for one every time it runs, is pushed out past the
    wait. Every level the fade sets comes back as a ``volumeUpdated`` and, 50 ms after the answer,
    a touch, and each frame asks for a pass - so the row is removed only once the box has sent the
    last of them and the service has had a moment to read it. Waiting on the last LEVEL alone left
    the trailing touch in flight: it raced the preference poll, and when it landed second its pass
    let the console go on the preference's behalf, so the case passed without the take's request.
    """
    options = replace(_options(world, tmp_path), registry_poll_s=30.0)
    left_within_s = 5.0
    assert left_within_s < options.registry_poll_s, "the control: the registry poll cannot ask for the pass"
    _set_preference(options, PreferenceName.CONSOLES, (CONSOLE_ID,))
    logs: list[str] = []

    async with _running(options, logs) as service:
        await world.console.notify(now_playing_frame(device_id=CONSOLE_ID, source=RADIO))
        await eventually(lambda: CONSOLE_IP in _slaves(service), "the console joined")
        await eventually(lambda: _faded_back(world.console, 30), "and its fade finished")
        await eventually(lambda: not world.console.answering, "and the box sent the last frame its fade caused")
        # A frame already sent is read within a loop turn on loopback; this is margin over that
        # turn, far under the poll that would otherwise ask, so the house is quiet when the row goes.
        await asyncio.sleep(0.2)
        _unset_preference(options, PreferenceName.CONSOLES)
        await eventually(
            lambda: CONSOLE_IP not in _slaves(service),
            "and left once it was no longer allowed",
            timeout=left_within_s,
        )


class _PassCountingMaster(ZoneMaster):
    """The real master, counting the passes that reach it.

    Every pass that holds a zone asks the master which boxes a station change left behind, whoever
    is in the zone and whatever else the pass does, so that call is one tick per pass. A pass that
    changes nothing is otherwise invisible from outside, which is the point of it being idempotent.
    """

    passes = 0

    def slaves_left_on_an_old_stream(self) -> list[str]:
        self.passes += 1
        return super().slaves_left_on_an_old_stream()


@asynccontextmanager
async def _running_counting_passes(
    options: ServiceOptions, logs: list[str]
) -> AsyncGenerator[tuple[ZoneService, Callable[[], int]], None]:
    """The real wiring, with the master built through the ``open_zone_master`` port as the counter above."""
    ports = replace(build_production().zone_ports, open_zone_master=_PassCountingMaster)
    service = ZoneService(options, log=recording_into(logs), ports=ports)

    def passes() -> int:
        master = service.master
        return master.passes if isinstance(master, _PassCountingMaster) else 0

    task = asyncio.create_task(service.run())
    try:
        yield service, passes
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _quiet(passes: Callable[[], int], *, for_s: float = 0.3, within_s: float = 5.0) -> int:
    """Wait until no pass has run for ``for_s``, and answer how many had run by then."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within_s
    seen, since = passes(), loop.time()
    while loop.time() - since < for_s:
        assert loop.time() < deadline, f"the house never went quiet: {passes()} passes"
        await asyncio.sleep(0.02)
        if passes() != seen:
            seen, since = passes(), loop.time()
    return seen


def _questions(logs: list[str], name: str) -> int:
    """How many times a box's answer to "what are you playing" was read, taken or dropped alike."""
    return sum(line.startswith(f"probe: {name}") for line in logs)


async def test_a_preference_take_asks_for_a_pass_only_when_the_console_list_moved(world: World, tmp_path: Path) -> None:
    """Who belongs is the only thing a preference changes that the pass acts on.

    A fade, a rewind, a window or a hold is read where it is used, so a take that moves all four
    asks for no pass. A take that moves the console list asks for one, and that pass is what lets a
    console go (``test_a_console_taken_off_the_list_leaves_the_zone``). The console is allowed from
    the start, so it is in the speaker book: taking it off the list, and allowing it again, asks for
    no registry read - which is the one other thing that would ask for a pass, and the read count
    shows it did not run. The registry poll is pushed out past every wait here, and no box says
    anything.
    """
    options = replace(_options(world, tmp_path), registry_poll_s=30.0)
    _set_preference(options, PreferenceName.CONSOLES, (CONSOLE_ID,))
    logs: list[str] = []

    def reads() -> int:
        return world.registry.paths.count(DEVICES_PATH)

    async with _running_counting_passes(options, logs) as (_service, passes):
        await eventually(lambda: passes() > 0, "the start ran its pass")
        before = await _quiet(passes)
        _set_preference(options, PreferenceName.FADE, 1.0)
        _set_preference(options, PreferenceName.REWIND, 5.0)
        _set_preference(options, PreferenceName.WINDOW, 0.5)
        _set_preference(options, PreferenceName.HOLD, 1.5)
        for line in (
            "a joining box fades in over 1.0 s, set by cli",
            "an MPD channel starts 5 s back, set by cli",
            "the dialling window is 0.5 s, set by cli",
            "a key is held after 1.5 s, set by cli",
        ):
            await eventually(lambda line=line: _said(logs, line), f"taken in: {line}")
        assert await _quiet(passes) == before, "a take that moved no console asked for a pass"

        read_at_start = reads()
        _unset_preference(options, PreferenceName.CONSOLES)
        await eventually(lambda: _said(logs, "consoles allowed into the zone: none"), "the list was taken in")
        await eventually(lambda: passes() > before, "and the take that moved it asked for a pass", timeout=5.0)
        before = await _quiet(passes)

        _set_preference(options, PreferenceName.CONSOLES, (CONSOLE_ID,))
        await eventually(lambda: sum("consoles allowed into the zone: " in line for line in logs) >= 3, "allowed again")
        await eventually(lambda: passes() > before, "which asked for a pass too", timeout=5.0)
        await _quiet(passes)
        assert reads() == read_at_start, "a console the speaker book already holds needs no registry read"


async def test_a_console_put_on_the_list_while_the_house_runs_is_watched_and_joins_when_it_wakes(
    world: World, tmp_path: Path
) -> None:
    """The other direction, without a restart: a console nobody allowed at start is not even watched.

    The take that allows it asks for a registry read at once rather than at the next poll - the
    registry read is where watching is decided - and the box is asked what it is playing, the
    question every box is asked at start. So its FIRST frame after that is read against standby:
    "radio" is a wake, and it is taken in, with no standby frame sent to it first. The poll is
    pushed out past every wait, so it cannot be what found the console.

    The probe line is the barrier before the wake, because a frame read before the answer lands
    would be overwritten by it; in the house the wake comes long after the question.
    """
    options = replace(_options(world, tmp_path), registry_poll_s=30.0)
    within_s = 5.0
    assert within_s < options.registry_poll_s, "the control: the registry poll cannot be what finds it"
    logs: list[str] = []

    async with _running(options, logs) as service:
        await eventually(lambda: _said(logs, f"({HALLWAY_ID}) at {HALLWAY_IP}"), "the start read the registry")
        assert not _said(logs, f"({CONSOLE_ID}) at {CONSOLE_IP}"), "the control: not watched while not allowed"
        _set_preference(options, PreferenceName.CONSOLES, (CONSOLE_ID,))
        await eventually(lambda: _said(logs, f"({CONSOLE_ID}) at {CONSOLE_IP}"), "watched at once", timeout=within_s)
        await eventually(
            lambda: _said(logs, f"{CONSOLE_NAME}: {SourceName.STANDBY}"),
            "and asked what it is playing",
            timeout=within_s,
        )
        await world.console.notify(now_playing_frame(device_id=CONSOLE_ID, source=RADIO))
        await eventually(lambda: CONSOLE_IP in _slaves(service), "and taken in on its first wake", timeout=within_s)
        reads = [path for path in world.registry.paths if path == DEVICES_PATH]
        assert len(reads) == 2, f"one read at start and one for the take, not a read per loop turn: {len(reads)}"
        # The take's read asked only the box new to the book: a box asked at start is not asked again.
        assert _questions(logs, CONSOLE_NAME) == 1, "the console was asked once"
        assert _questions(logs, "Bose Hallway") == 1, "and a box asked at start was not asked again"


async def test_a_console_put_on_the_list_while_it_plays_the_house_stream_is_taken_in_at_once(
    world: World, tmp_path: Path
) -> None:
    """Awake when it is allowed, and already playing the house's stream: taken in without a wake.

    The answer to the question it is asked is what places it, exactly as at start, and a box
    playing OUR stream belongs by the membership rule. Before a take asked for a registry read
    and asked the box, it waited for the next poll to be watched at all and then for a frame it
    had no reason to send. A console awake on a station of its own is NOT this case: as any box on
    its own station does, it stays out until it goes to standby and is switched on again, or
    somebody dials a channel on it.
    """
    world.console.now_playing = now_playing_document(device_id=CONSOLE_ID, source=RADIO, owner=MASTER_ID)
    options = replace(_options(world, tmp_path), registry_poll_s=30.0)
    within_s = 5.0
    assert within_s < options.registry_poll_s, "the control: the registry poll cannot be what finds it"
    logs: list[str] = []

    async with _running(options, logs) as service:
        await eventually(lambda: _said(logs, f"({HALLWAY_ID}) at {HALLWAY_IP}"), "the start read the registry")
        assert not joins(world.console), "the control: not taken in while not allowed"
        _set_preference(options, PreferenceName.CONSOLES, (CONSOLE_ID,))
        await eventually(lambda: CONSOLE_IP in _slaves(service), "taken in at once", timeout=within_s)


async def test_a_box_the_registry_adds_after_the_start_is_taken_in_on_its_first_wake(
    world: World, tmp_path: Path
) -> None:
    """Any box first listed mid-run is asked what it is playing, as every box is at start.

    Without the question its first frame is the first thing the service hears from it, and a
    frame saying "radio" alone reads as a box on its own station rather than one just switched
    on - so a box added to the registry mid-run was not taken in on its first wake, only on the
    one after. No standby frame is sent to it here: its answer is what says it was asleep.
    """
    entries = json.loads(devices_at({STUDIO_ID: STUDIO_IP, HALLWAY_ID: HALLWAY_IP, CONSOLE_ID: CONSOLE_IP}))
    world.registry.body = json.dumps([entry for entry in entries if entry["device_id"] != HALLWAY_ID])
    options = _options(world, tmp_path)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await eventually(lambda: _said(logs, f"({STUDIO_ID}) at {STUDIO_IP}"), "the start read the registry")
        assert not _said(logs, f"({HALLWAY_ID}) at"), "the control: the hallway is not listed yet"
        world.registry.body = json.dumps(entries)
        await eventually(lambda: _said(logs, f"({HALLWAY_ID}) at {HALLWAY_IP}"), "watched from the next registry read")
        await eventually(lambda: _said(logs, f"Bose Hallway: {SourceName.STANDBY}"), "and asked what it is playing")
        await world.hallway.notify(now_playing_frame(device_id=HALLWAY_ID, source=RADIO))
        await eventually(lambda: HALLWAY_IP in _slaves(service), "and taken in on its first wake", timeout=5.0)
        # Two more reads: the poll reads, then asks, then waits, so the question that followed the
        # first of them has been answered by the time the second starts.
        read = world.registry.paths.count(DEVICES_PATH)
        await eventually(lambda: world.registry.paths.count(DEVICES_PATH) >= read + 2, "the poll went on reading")
        assert _questions(logs, "Bose Hallway") == 1, "the late box was asked once, not on every read"
        assert _questions(logs, "Bose Studio") == 1, "and a box asked at start was not asked again"


def _listed_mid_run(world: World, *late: str) -> list[dict[str, object]]:
    """Leave ``late`` out of the device list the start reads, and answer the whole list to hand back later."""
    entries: list[dict[str, object]] = json.loads(
        devices_at({STUDIO_ID: STUDIO_IP, HALLWAY_ID: HALLWAY_IP, CONSOLE_ID: CONSOLE_IP})
    )
    world.registry.body = json.dumps([entry for entry in entries if entry["device_id"] not in late])
    return entries


def _answered(logs: list[str], name: str, source: str) -> bool:
    """Whether the service has done with a box's answer, whether it took it or dropped it."""
    return any(line.startswith("probe: ") and name in line and source in line for line in logs)


@pytest.mark.parametrize(
    "clock_step_s",
    [
        pytest.param(0.0, id="one-clock"),
        # The wall clock set back an hour between the question and the frame - an NTP correction,
        # or somebody setting the date. The frame then carries a time EARLIER than the moment the
        # question went out, and a rule comparing the two read the stale answer as the newer word.
        pytest.param(-3600.0, id="the-clock-stepped-back"),
    ],
)
async def test_an_answer_the_box_contradicted_on_the_way_is_dropped_and_leaves_it_off(
    world: World, tmp_path: Path, clock_step_s: float
) -> None:
    """A box switched off while its answer is on the wire stays off: the frame is the newer word.

    The hallway is listed mid-run, awake on a station of its own, and asked what it is playing;
    the box takes its answer when the question arrives and the body is held back on the wire.
    Meanwhile somebody switches it off and it says STANDBY. Read after that frame, the stale
    "radio" is standby-then-radio, which is a wake, and the pass took the box in - switching back
    on a box a person had just switched off. The answer is dropped instead, because the box has
    named a source in a frame of its own since the question went out.

    "Since" is the ORDER the reader took things in, never a comparison of two wall-clock readings:
    the second arm stamps the STANDBY frame an hour before the question, which is what a clock set
    back in between produces, and the answer must still be dropped.

    The last step is the liveness pair: the same box, really switched on afterwards, IS taken in,
    so nothing unrelated kept it out while the answer was being refused.
    """
    entries = _listed_mid_run(world, HALLWAY_ID)
    world.hallway.now_playing = now_playing_document(device_id=HALLWAY_ID, source=RADIO)
    released = asyncio.Event()
    world.hallway.held["/now_playing"] = released
    options = _options(world, tmp_path)
    logs: list[str] = []
    frames = _Frames()

    async with _running(options, logs, ports=_relaying(frames)) as service:
        try:
            await eventually(lambda: _said(logs, f"({STUDIO_ID}) at {STUDIO_IP}"), "the start read the registry")
            world.registry.body = json.dumps(entries)
            await eventually(lambda: "/now_playing" in world.hallway.paths(), "the hallway was asked what it plays")
            frames.clock_step_s = clock_step_s
            await world.hallway.notify(now_playing_frame(device_id=HALLWAY_ID, source=SourceName.STANDBY))
            await eventually(lambda: service.policy.is_asleep(HALLWAY_ID), "the service read it switching off")
            # Only that frame: after the step every reading, the service's and the observer's
            # alike, is on the new clock, and unshifted they agree with each other again.
            frames.clock_step_s = 0.0
        finally:
            released.set()
        await eventually(lambda: _answered(logs, "Bose Hallway", RADIO), "the held answer arrived")
        assert service.policy.is_asleep(HALLWAY_ID), "the stale answer overwrote the box switching itself off"
        # A pass that took the stale answer runs within a loop turn or two on loopback; this is
        # margin over it, so a join that was going to happen has happened.
        await asyncio.sleep(0.5)
        assert not joins(world.hallway), "a box somebody switched off was taken into the zone"

        await world.hallway.notify(now_playing_frame(device_id=HALLWAY_ID, source=RADIO))
        await eventually(lambda: HALLWAY_IP in _slaves(service), "the control: a real wake takes it in", timeout=5.0)


async def test_a_frame_that_names_no_source_leaves_an_answer_on_the_way_standing(world: World, tmp_path: Path) -> None:
    """A touch says nothing about what a box plays, so the answer read after it is still news.

    Only a frame in which the box NAMES its source is newer than an answer to "what are you
    playing". A ``userActivityUpdate`` is a person touching the box and carries no source at all,
    and if it counted, a box somebody walked past while its answer was on the wire would be left
    unplaced - read as awake - and its next wake would not be taken as one.

    The hallway is listed mid-run and asleep; its answer is held back after the headers, it sends
    a touch, and the answer is released only once the reader has read that touch to the end. The
    answer must be TAKEN, and the liveness pair shows the placing mattered: the box's first wake
    afterwards takes it in.
    """
    entries = _listed_mid_run(world, HALLWAY_ID)
    released = asyncio.Event()
    world.hallway.held["/now_playing"] = released
    options = _options(world, tmp_path)
    logs: list[str] = []
    frames = _Frames()

    async with _running(options, logs, ports=_relaying(frames)) as service:
        try:
            await eventually(lambda: _said(logs, f"({STUDIO_ID}) at {STUDIO_IP}"), "the start read the registry")
            world.registry.body = json.dumps(entries)
            await eventually(lambda: "/now_playing" in world.hallway.paths(), "the hallway was asked what it plays")
            await world.hallway.notify(user_activity_frame(device_id=HALLWAY_ID))
            await eventually(
                lambda: _read_to_the_end(service, frames, HALLWAY_ID, FrameKind.USER_ACTIVITY_UPDATE),
                "the reader read the touch before the answer arrived",
            )
        finally:
            released.set()
        await eventually(lambda: _answered(logs, "Bose Hallway", SourceName.STANDBY), "the held answer arrived")
        assert _said(logs, f"probe: Bose Hallway: {SourceName.STANDBY}"), "the answer was taken, not dropped"
        assert service.policy.is_asleep(HALLWAY_ID), "and it placed the box: asleep"

        await world.hallway.notify(now_playing_frame(device_id=HALLWAY_ID, source=RADIO))
        await eventually(lambda: HALLWAY_IP in _slaves(service), "so its first wake takes it in", timeout=5.0)


async def test_each_answer_is_taken_as_it_arrives_and_a_slow_box_holds_up_no_other(
    world: World, tmp_path: Path
) -> None:
    """Two boxes listed at once, one slow to answer: the other is placed without waiting for it.

    The console plays the house's stream, which by the membership rule is a reason to be in the
    zone the moment its answer is read. It was read only once EVERY box asked had answered, so a
    box slow to answer - up to the HTTP timeout, eight seconds - held back every box asked with
    it. That happens at start as well as mid-run, whenever the first registry read failed.
    """
    entries = _listed_mid_run(world, HALLWAY_ID, CONSOLE_ID)
    world.console.now_playing = now_playing_document(device_id=CONSOLE_ID, source=RADIO, owner=MASTER_ID)
    released = asyncio.Event()
    world.hallway.held["/now_playing"] = released
    options = replace(_options(world, tmp_path), consoles_allowed=(CONSOLE_ID,))
    logs: list[str] = []

    async with _running(options, logs) as service:
        try:
            await eventually(lambda: _said(logs, f"({STUDIO_ID}) at {STUDIO_IP}"), "the start read the registry")
            world.registry.body = json.dumps(entries)
            await eventually(lambda: "/now_playing" in world.hallway.paths(), "the hallway was asked what it plays")
            await eventually(lambda: CONSOLE_IP in _slaves(service), "the console was taken in", timeout=5.0)
            assert not released.is_set(), "the control: the hallway's answer was still on the wire"
        finally:
            released.set()
        await eventually(lambda: _said(logs, f"Bose Hallway: {SourceName.STANDBY}"), "and the hallway's came later")


async def test_a_stored_preference_that_is_not_usable_is_named_once_and_ignored(world: World, tmp_path: Path) -> None:
    """A row nobody can use is said once, and the configured window goes on deciding.

    Another preference is set AFTER it, so the rows the service reads change while the bad one stays
    - which is the read that would name it a second time. The window is 2.0 s from the options, so
    1 then 2 a second apart reading as ONE number shows the configured value is what runs, rather
    than the default (0.8 s) or the floor (0.5 s), either of which splits it.
    """
    options = _dialable_world(world, tmp_path, dial_window_s=2.0)
    logs: list[str] = []

    async with _running(options, logs) as service:
        await _both_wake(world)
        with contextlib.closing(sqlite3.connect(options.database)) as raw, raw:
            raw.execute("INSERT INTO preference VALUES ('dialling.window_s', '\"fast\"', 'cli', '')")
        await eventually(
            lambda: _said(logs, 'dialling.window_s = "fast" in the house database is ignored'), "it was named"
        )
        _set_preference(options, PreferenceName.FADE, 1.0)
        await eventually(lambda: _said(logs, "a joining box fades in over 1.0 s, set by cli"), "the next row was read")
        assert sum("is ignored" in line for line in logs) == 1, "once, not on every read"

        await _press_preset(world.studio, STUDIO_ID, 1)
        await asyncio.sleep(1.0)
        await _press_preset(world.studio, STUDIO_ID, 2)
        await eventually(lambda: _playing(service).endswith("?c=12"), "one number, on the configured window")
        assert not _said(logs, "no such channel")


async def test_a_stored_number_nothing_can_read_costs_one_value_and_not_the_house(world: World, tmp_path: Path) -> None:
    """Two rows the rule once let raise: a 401-digit integer no float can hold, there at the start,
    and a 5000-digit literal the JSON decoder refuses, written while the service runs. Each is named
    once, cut short in the log, and the service goes on: a preference set afterwards is still taken
    in, which is the watch that ``OverflowError`` or ``ValueError`` out of it would have ended."""
    options = _options(world, tmp_path)
    _set_preference(options, PreferenceName.REWIND, 10**400)
    logs: list[str] = []

    async with _running(options, logs):
        at_start = f"mpd.rewind_s = 1{'0' * 79}... in the house database is ignored (refused: mpd.rewind_s is too"
        await eventually(lambda: _said(logs, at_start), "the row there at the start was named")
        with contextlib.closing(sqlite3.connect(options.database)) as raw, raw:
            raw.execute("INSERT INTO preference VALUES ('dialling.hold_threshold_s', ?, 'cli', '')", ("1" * 5000,))
        mid_run = f"dialling.hold_threshold_s = {'1' * 80}... in the house database is ignored (not JSON)"
        await eventually(lambda: _said(logs, mid_run), "the row written mid-run was named")
        _set_preference(options, PreferenceName.FADE, 1.0)
        await eventually(lambda: _said(logs, "a joining box fades in over 1.0 s, set by cli"), "the watch goes on")
        assert all(len(line) < 300 for line in logs if "is ignored" in line), "a row is quoted cut short"


async def test_a_rewind_with_a_fraction_is_logged_as_it_was_set(world: World, tmp_path: Path) -> None:
    """2.5 s is what runs, so 2.5 s is what the line says - not the "2 s" a whole-second format
    rounded it down to."""
    options = _options(world, tmp_path)
    logs: list[str] = []

    async with _running(options, logs):
        _set_preference(options, PreferenceName.REWIND, 2.5)
        await eventually(lambda: _said(logs, "an MPD channel starts 2.5 s back, set by cli"), "it was taken in")


def _gaps_from(box: FakeSpeaker, first: int) -> list[float]:
    """The time between each volume write from the ``first``-th on and the write after it."""
    times = box.volumes_at[first:]
    return [later - earlier for earlier, later in itertools.pairwise(times)]


async def test_a_fade_changed_while_a_box_climbs_keeps_its_pace_and_the_next_fade_takes_the_new_one(
    world: World, tmp_path: Path
) -> None:
    """The climb in progress keeps the length it started with; the next join uses the new one.

    2.4 s over eight steps is a write every 0.3 s. The change to 0.0 is taken in while the studio
    is on its way up - the line saying so is the barrier - and every write the studio receives AFTER
    that is still 0.3 s from the one before it. The hallway, joining once the change is in, is put
    back in one write: 0.0 means no climb at all (the design, and ``FADE_FLOOR_S``), not eight steps
    with no pause between them.
    """
    options = _options(world, tmp_path)
    _set_preference(options, PreferenceName.FADE, 2.4)
    step_s = 2.4 / 8
    logs: list[str] = []

    async with _running(options, logs):
        await world.studio.notify(now_playing_frame(device_id=STUDIO_ID, source=RADIO))
        await eventually(lambda: any(0 < level < 30 for level in world.studio.volumes), "the studio began to climb")
        _set_preference(options, PreferenceName.FADE, 0.0)
        await eventually(lambda: _said(logs, "a joining box fades in over 0.0 s, set by cli"), "it took it")
        taken_at = len(world.studio.volumes)
        await eventually(lambda: _faded_back(world.studio, 30), "the studio's climb finished")
        after = _gaps_from(world.studio, taken_at)
        assert len(after) >= 3, f"too few writes left to say anything: {world.studio.volumes}"
        assert min(after) >= step_s * 0.8, f"the climb in progress was re-timed: {after}"

        await world.hallway.notify(now_playing_frame(device_id=HALLWAY_ID, source=RADIO))
        await eventually(lambda: _faded_back(world.hallway, 30), "the hallway's level was put back")
        assert world.hallway.volumes == [0, 30], "a fade of 0.0 puts the level back in one write, not a climb"


async def test_a_preference_read_that_fails_keeps_what_is_in_use_and_says_so_once_each_way(
    world: World, tmp_path: Path
) -> None:
    """A database that cannot be read is said once when it starts failing and once when it answers.

    The failure is an error and is logged as one, beside every other failure; the recovery is not,
    and is a preference line. A value set while the reads fail is not taken in until one succeeds -
    and is then, which is the proof that the watch outlived the failure rather than ending on it.
    """
    options = _options(world, tmp_path)
    failing = f"{ERROR_KIND}: "
    kept = "keeping the preferences already in use"
    logs: list[str] = []

    async with _running_with_a_store_that_can_fail(options, logs) as (_service, store):
        await eventually(lambda: _said(logs, "member(s) remembered from the last run"), "it started")
        store.reading_preferences_fails = True
        await eventually(lambda: _said(logs, kept), "the failure was said")
        _set_preference(options, PreferenceName.FADE, 1.0)
        await asyncio.sleep(options.switch_poll_s * 6)
        said_failing = [line for line in logs if kept in line]
        assert len(said_failing) == 1, "once, not every poll"
        assert said_failing[0].startswith(failing), f"and as an error: {said_failing[0]}"
        assert not _said(logs, "fades in over 1.0 s"), "nothing was taken from a read that failed"

        store.reading_preferences_fails = False
        await eventually(lambda: _said(logs, "a joining box fades in over 1.0 s, set by cli"), "taken in on recovery")
        assert [line for line in logs if "the house database answers again" in line] == [
            "prefs: the house database answers again"
        ]
