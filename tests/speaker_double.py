"""A SoundTouch box on loopback: the slice of one the master and the service actually talk to.

The master's HTTP calls take an address, so a small server on loopback IS a speaker as far as they
are concerned. Nothing is monkeypatched, and what a test asserts on is the bytes a real box would
have to parse. It lives here rather than in one test file because three of them drive it: the one
that checks call by call what the master says, the one that runs the whole command, and the one
that runs the whole service.

A box has TWO faces, and the service needs both. The HTTP one on 8090 is what the master calls;
the notification WebSocket on its own port is what the service listens to, and it is the only
place a box says anything about itself while it is NOT in the zone. A double that has only the
first can be joined to a zone but can never tell the service it went to AUX.

The frames it sends are the ones the M0 run recorded, with the ids substituted - never retyped.
A parser that agrees with a retyped frame and not with the stored bytes is the failure this
avoids, and the substitution keeps every other attribute exactly as the speaker sent it.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING

from websockets.asyncio.server import Server, ServerConnection, serve

from soundtouch_zonemaster.adapters.soundtouch import ipc
from soundtouch_zonemaster.adapters.soundtouch.observer import SUBPROTOCOL
from soundtouch_zonemaster.domain.enums import SourceName

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "RECORDED_BOX",
    "RECORDED_MASTER",
    "SPEAKER_PORT",
    "FakeSpeaker",
    "key_bodies",
    "key_body",
    "now_playing_document",
    "now_playing_frame",
    "now_playing_frames",
    "open_transport",
    "read_frames",
    "read_frames_but_not_pings",
    "selection_frame",
    "user_activity_frame",
    "volume_frame",
]

SPEAKER_PORT = 8090
"""Where a speaker answers. It is hardcoded in the code under test because a real box has no other
one, which is also why a master and a double on the same host need two addresses between them."""

FIXTURES = Path(__file__).parent / "fixtures"
NOW_PLAYING_FIXTURE = FIXTURES / "notification-nowplaying.txt"
KEY_FIXTURE = FIXTURES / "slavemsg-keydata.txt"
VOLUME_FIXTURE = FIXTURES / "notification-volume.txt"

RECORDED_VOLUME_BOX = "AABBCC0000A1"
"""The speaker the recorded volume frame came from; substituted like ``RECORDED_BOX``."""

XML_HEAD = '<?xml version="1.0" encoding="UTF-8" ?>'
"""Written out rather than imported from ``zonexml``: what a real box sends is not ours to change,
and a double that borrows the code's own constant cannot notice when the code changes it."""

RECORDED_BOX = "AABBCC0000A3"
"""The speaker the two recorded frames came from; substituted for whichever box a test is using."""

RECORDED_MASTER = "AABBCC000001"
"""The master named INSIDE the recorded playing frame, which is what makes it ours rather than the
box's own radio. Substituted separately, because the two ids are what the whole rule turns on."""


def now_playing_frames() -> tuple[str, str]:
    """The two real ``nowPlayingUpdated`` frames of the M0 run: standby, then the master's stream."""
    written = NOW_PLAYING_FIXTURE.read_text(encoding="utf-8").splitlines()
    lines = [ln for ln in written if ln and not ln.startswith("#")]
    assert len(lines) == 2, f"expected the standby frame and the playing frame, got {len(lines)}"
    return lines[0], lines[1]


def key_bodies() -> list[str]:
    """The eight ``/slaveMsg`` bodies the flat sent on 2026-09-06, comments dropped."""
    return [line for line in KEY_FIXTURE.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")]


def key_body(key: str, state: str) -> str:
    """The one recorded body for that key in that state, or fail saying it is not in the fixture.

    ``str`` rather than the enums, so a test may ask for a spelling the enums do not carry; every
    member of ``KeyName`` and ``KeyState`` is a ``str`` and passes straight in.
    """
    wanted = [body for body in key_bodies() if f'state="{state}"' in body and f">{key}<" in body]
    assert len(wanted) == 1, f"expected exactly one recorded body for {key} {state}, got {len(wanted)}"
    return wanted[0]


def now_playing_frame(*, device_id: str, source: str, owner: str | None = None) -> str:
    """One box's ``nowPlayingUpdated`` frame, built from a recorded one by substitution.

    ``owner`` is the device id inside the ``<nowPlaying>`` element: the box itself when it is
    playing its own, the master when the stream is ours. It defaults to the box, because a box
    naming somebody else is the special case and it should have to be asked for.
    """
    standby, playing = now_playing_frames()
    if source == SourceName.STANDBY:
        return standby.replace(RECORDED_BOX, device_id)
    frame = playing.replace(RECORDED_MASTER, owner if owner is not None else device_id)
    frame = frame.replace(RECORDED_BOX, device_id)
    return frame.replace(f'source="{SourceName.LOCAL_INTERNET_RADIO}"', f'source="{source}"')


def volume_frame(*, device_id: str, level: int) -> str:
    """One box's ``volumeUpdated`` frame at a level, built from the recorded one by substitution.

    Recorded 2026-09-24 (``notification-volume.txt``): Room1 at 12, with the target and the actual
    level equal, as every frame of that capture carried them.
    """
    written = VOLUME_FIXTURE.read_text(encoding="utf-8").splitlines()
    lines = [ln for ln in written if ln and not ln.startswith("#")]
    assert len(lines) == 1, f"expected the one recorded volume frame, got {len(lines)}"
    frame = lines[0].replace(RECORDED_VOLUME_BOX, device_id)
    return frame.replace(">12<", f">{level}<")


def selection_frame(*, device_id: str, preset_id: int) -> str:
    """One box's ``nowSelectionUpdated`` frame: the ONE place a preset NUMBER ever appears.

    The forwarded press on ``/slaveMsg`` carries the ContentItem and no number, which is why the
    service holds a WebSocket to every box at all. Shaped after the frames recorded on 2026-09-07
    (``research/captures/2026-09-07-repeat-preset/observe.jsonl``), where five presses of 1, 1, 2,
    2, 1 produced five of these - including both repeats, which is what makes a repeated digit a
    dialable number.
    """
    return (
        f'<updates deviceID="{device_id}"><nowSelectionUpdated><preset id="{preset_id}">'
        f'<ContentItem source="{SourceName.LOCAL_INTERNET_RADIO}" type="stationurl" '
        f'location="http://192.0.2.1/preset{preset_id}" sourceAccount="" isPresetable="true">'
        f"<itemName>Preset {preset_id}</itemName></ContentItem>"
        "</preset></nowSelectionUpdated></updates>"
    )


def user_activity_frame(*, device_id: str) -> str:
    """What a box sends when a HUMAN touched it, and the whole of it - it carries nothing else.

    Recorded 17 times in the live run of 2026-09-07 and identical every time apart from the device
    id. It is what separates a pressed preset from the master's own station change coming back, so
    a test that sends a selection without one is sending an echo (``soundtouch_zonemaster.presses``).
    """
    return f'<userActivityUpdate deviceID="{device_id}" />'


def now_playing_document(*, device_id: str, source: str, owner: str | None = None) -> str:
    """What ``GET /now_playing`` answers: the same element the notification carries, unwrapped.

    Cut out of the frame rather than written a second time. A real box has one answer to the
    question and reports it over both faces, so a double whose two faces could disagree would let
    a service pass a test it would fail in the flat.
    """
    frame = now_playing_frame(device_id=device_id, source=source, owner=owner)
    return XML_HEAD + frame[frame.index("<nowPlaying ") : frame.index("</nowPlayingUpdated>")]


_PING = "AudioServerMsgSlavePingMsg"
"""The keepalive both channels carry; it says nothing about what the master decided."""


async def read_frames(reader: asyncio.StreamReader, n: int, timeout: float = 8.0) -> list[ipc.Frame]:
    """The next ``n`` IPC frames off a transport or data channel, reassembled from the stream."""
    splitter = ipc.FrameSplitter()
    out: list[ipc.Frame] = []
    while len(out) < n:
        data = await asyncio.wait_for(reader.read(65536), timeout)
        assert data, "the master closed the connection"
        out += [ipc.decode_frame(b) for b in splitter.feed(data)]
    return out


async def read_frames_but_not_pings(reader: asyncio.StreamReader, n: int, timeout: float = 8.0) -> list[ipc.Frame]:
    """The next ``n`` frames that say something, with the keepalive pings dropped.

    A channel is pinged every couple of seconds whatever else is happening, so any sequence a test
    waits several seconds for has pings scattered through it. Asserting on the raw order makes such
    a test fail on the keepalive rather than on what it is about.

    ``timeout`` bounds this LOOP as well as each read under it, and that is what keeps a regression
    reportable. The pings arrive whatever else happens, so they satisfy every single read: a master
    that stops saying the thing under test leaves this loop reading keepalives for ever, and an
    unbounded loop HANGS the suite instead of reddening it. A hang names nothing, so the deadline is
    asserted inside the loop and the failure says how far the sequence got.
    """
    said: list[ipc.Frame] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while len(said) < n:
        assert loop.time() < deadline, f"only {len(said)} of {n} frames in {timeout}s: {[f.typename for f in said]}"
        said += [frame for frame in await read_frames(reader, 1, timeout) if frame.typename != _PING]
    return said


async def open_transport(bind: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a transport channel and read the join sequence the master answers with.

    A test opens this ITSELF rather than through :class:`FakeSpeaker`, because a real box opens
    several at once from one address and the interesting cases are about which of them the master
    drives - so the test has to hold each one separately.
    """
    reader, writer = await asyncio.open_connection(bind, 40002)
    await read_frames(reader, 3)
    return reader, writer


class FakeSpeaker:
    """The slice of a SoundTouch box the master talks to: /info, /presets, and three POSTs.

    Raw HTTP rather than a framework, because what is being checked is the bytes a real box would
    receive. It records every request so a test can assert on the document the master built.

    With ``notify_port`` it also serves the notification WebSocket, and then it is a whole box as
    far as the service is concerned: it can be joined, and it can say what it is playing.
    """

    def __init__(
        self,
        presets: Mapping[int, str],
        *,
        host: str = "127.0.0.1",
        device_id: str = "AABBCC000002",
        notify_port: int | None = None,
        refuse: frozenset[str] = frozenset(),
    ) -> None:
        self.refuse = refuse
        """Paths this box drops the connection on, the way one that is going off the air does.

        A bare path (``/volume``) drops every method; ``"POST /volume"`` drops only that one, which
        is what a box answering a read and failing a write looks like - the case that separates
        "its level could not be read" from "it could not be turned down", two branches whose
        difference is whether the mute note is left behind for a later pass.
        """
        self.slow: dict[str, float] = {}
        """How long this box takes to answer a path, keyed the way ``refuse`` is.

        A real box answers over a network and takes tens of milliseconds over it; on loopback a
        call returns inside one. A test about what else may happen WHILE the service is waiting for
        a box therefore has no window to aim at unless the box is slowed, and slowing the box is
        the honest half of that - the service really is inside its own ``await`` the whole time.
        ``"POST /volume"`` wins over a bare ``"/volume"``, so one method can be slowed alone.
        """
        self.host = host
        self.device_id = device_id
        self.presets = dict(presets)
        """The ContentItem each preset number holds, as a box stores it."""
        self.requests: list[tuple[str, str, str]] = []
        """(method, path, body) in arrival order."""
        self.volume = 30
        """Where the box's own volume knob is. A real box answers ``/volume`` with it and moves it
        on a POST, and the service both READS it (to know what to put back) and WRITES it (to hide
        the box's own audio while it is taken in), so a double that answered a flat OK would make
        that whole path inert while every test still passed."""
        self.volumes: list[int] = []
        """Every level this box was set to, in order."""
        self.touch_on_volume = True
        """Whether a volume write comes back as a touch, the way it does on a real box.

        Measured 2026-09-21 in ``burst-as-a-slave-frames.json``: every volume the service set during
        a fade came back as a ``volumeUpdated`` AND a ``userActivityUpdate``, the frame that otherwise
        means a person touched the box. On by default because a real box never skips it: a silent
        double hides the phantom press those touches produce, and it also hides that they keep a box
        heard - the fade alone holds a box alive for over two seconds after its join.
        """
        self.now_playing = now_playing_document(device_id=device_id, source=SourceName.STANDBY)
        """What it answers a ``/now_playing`` GET with. A box that nobody has touched is in standby."""
        self.zone_master: str | None = None
        """Whose slave the box is: the master named by the last ``/setZone`` that listed members.

        It decides what a ``/select`` does, which is the defect of 2026-09-24 (docs/measurements/
        2026-09-24-releasing-one-slave.md): a box that is a slave FORWARDS a selection to its master
        instead of playing it, and dropping a box from the master's books sends it nothing, so it
        stays a slave. Only a ``/removeZoneSlave`` sent to the box frees it here, as on the wire."""
        self.played: list[str] = []
        """Every station location the box played ON ITS OWN, in order - what the room heard."""
        self.forwarded: list[str] = []
        """Every ``/select`` body it forwarded because it was somebody's slave at the time."""
        self.echo_on_select = True
        """Whether a ``/select`` that the box plays comes back the way it does on a real box.

        Measured 2026-09-24 (Question 5 of the record above): a box woken by an API ``/select``
        names the preset slot holding that station BEFORE the HTTP answer returns, one
        ``userActivityUpdate`` follows 57 ms after it, and ``nowPlayingUpdated`` with its own id
        follows that. It is the shape of a person pressing the preset, and reading it as one made
        the service answer its own ``/select`` for 50 s in the flat. On by default because the box
        never skips it."""
        self.echoes = 0
        """How many ``/select`` echoes the box has sent, so a test can wait for the last one."""
        self.notify_port = notify_port
        self.connections = 0
        """How many times something opened the notification channel; a reconnect counts again."""
        self._server: asyncio.AbstractServer | None = None
        self._notify: Server | None = None
        self._watchers: set[ServerConnection] = set()
        self._callers: set[asyncio.StreamWriter] = set()
        """Whoever is connected to the HTTP face right now, so switching the box off can drop them."""

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, SPEAKER_PORT)
        if self.notify_port is not None:
            self._notify = await serve(self._watch, self.host, self.notify_port, subprotocols=[SUBPROTOCOL])

    async def stop(self) -> None:
        """Both faces go together: a box that is off is off, and that is what unreachable means.

        Whoever is still connected is dropped BEFORE the wait. ``wait_closed`` waits for every
        client transport to be gone, so one caller that vanished without closing its socket makes
        it endless - and that is exactly what a cancelled HTTP call leaves behind: measured
        2026-09-07, httpx reported its client closed while the connection stayed open, and this
        teardown then held the whole service suite for ever instead of failing. A box switched off
        at the wall does not wait for its callers either.
        """
        if self._notify is not None:
            self._notify.close()
            await self._notify.wait_closed()
            self._notify = None
        if self._server is not None:
            self._server.close()
            for caller in list(self._callers):
                caller.close()
            await self._server.wait_closed()
            self._server = None

    async def notify(self, frame: str, *, timeout: float = 5.0) -> None:
        """Send one frame to whoever is watching, waiting for a watcher rather than racing it.

        A frame sent before the service's observer has connected reaches nobody, and the test that
        follows then fails somewhere else entirely - so waiting is part of sending here.
        """
        await self._watcher(timeout)
        for connection in list(self._watchers):
            await connection.send(frame)

    def bodies_for(self, path: str) -> list[str]:
        """Every body that arrived at one path, in order."""
        return [body for _method, got, body in self.requests if got == path]

    def paths(self) -> list[str]:
        """Every path that was asked for, in arrival order."""
        return [path for _method, path, _body in self.requests]

    async def _watcher(self, timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not self._watchers:
            assert asyncio.get_running_loop().time() < deadline, f"{self.host}: nothing opened the notification channel"
            await asyncio.sleep(0.02)

    async def _watch(self, connection: ServerConnection) -> None:
        self.connections += 1
        self._watchers.add(connection)
        try:
            await connection.wait_closed()
        finally:
            self._watchers.discard(connection)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._callers.add(writer)
        try:
            await self._answer_one(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionError):
            # The box was switched off under this caller, or the caller went away mid-request.
            # Neither is an error worth a line: a real box drops its callers the same way.
            pass
        finally:
            self._callers.discard(writer)
            writer.close()

    async def _answer_one(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        request_line, *header_lines = head.decode("utf-8", "replace").split("\r\n")
        method, path, _version = request_line.split(" ", 2)
        length = next(
            (int(line.split(":", 1)[1]) for line in header_lines if line.lower().startswith("content-length:")), 0
        )
        body = (await reader.readexactly(length)).decode("utf-8", "replace") if length else ""
        self.requests.append((method, path, body))
        if path in self.refuse or f"{method} {path}" in self.refuse:
            # Before the write is applied, because a box that drops the connection did not MOVE.
            # Recording it first made a refused write look like one the box had accepted, so a test
            # reading `volumes` could not tell the two apart.
            writer.close()
            return
        if method == "POST" and path == "/volume":
            # A real box MOVES when it is told to, so a later read answers the new level. A double
            # that recorded the write without moving would let a test pass over a service that read
            # a volume it had itself just replaced.
            asked = re.search(r"<volume>(\d+)</volume>", body)
            if asked is not None:
                self.volume = int(asked.group(1))
                self.volumes.append(self.volume)
        plays = await self._take_the_zone_document(method, path, body)
        # After the box has MOVED and before it answers, so that the order of `volumes` stays the
        # order the requests arrived in however differently two paths are slowed.
        held = self.slow.get(f"{method} {path}", self.slow.get(path, 0.0))
        if held:
            await asyncio.sleep(held)
        payload = self._answer(path).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(payload), payload))
        await writer.drain()
        writer.close()
        if method == "POST" and path == "/removeZoneSlave":
            # A released box is asleep 48 ms after it answers and says so (Question 6).
            await self._tell_the_watchers(now_playing_frame(device_id=self.device_id, source=SourceName.STANDBY))
        if plays is not None and self.echo_on_select:
            await self._echo_the_select()
        if method == "POST" and path == "/volume":
            # Every level a real box moves to is reported, whoever moved it (measured 2026-09-21 and
            # 2026-09-24). The house-volume rule reads these, and a double that stayed silent would
            # let our own writes look like a box nobody had touched.
            await self._tell_the_watchers(volume_frame(device_id=self.device_id, level=self.volume))
        if self.touch_on_volume and method == "POST" and path == "/volume":
            # AFTER the answer, which is the harder case: the recorded touch lands up to 0.25 s
            # either side of its volume frame, so the service has to recognise one that arrives
            # once its own write has already returned.
            await asyncio.sleep(0.05)
            await self._tell_the_watchers(user_activity_frame(device_id=self.device_id))

    async def turn_the_knob(self, level: int) -> None:
        """A person tapping volume at the box: the level moves and the box says so, as recorded.

        Measured 2026-09-24 on Room1: one tap is a touch, the ``volumeUpdated`` about 35 ms later,
        and a second touch when the key comes up. Not written to ``volumes``, which is what the
        SERVICE set the box to; a person's hand is the other thing that moves it.
        """
        self.volume = level
        await self.notify(user_activity_frame(device_id=self.device_id))
        await asyncio.sleep(0.035)
        await self.notify(volume_frame(device_id=self.device_id, level=level))
        await asyncio.sleep(0.05)
        await self.notify(user_activity_frame(device_id=self.device_id))

    async def _take_the_zone_document(self, method: str, path: str, body: str) -> str | None:
        """Become or stop being a slave, or play a ``/select``; answer the location it played.

        Only what the service's release depends on. An EMPTY ``/setZone`` frees the box too, but
        what it does beyond that (standby, E6) is not modelled: the tests that dissolve a zone
        assert on the document the box received, not on what it went on to play.
        """
        if method != "POST":
            return None
        if path == "/setZone":
            master = re.search(r'master="([^"]*)"', body)
            self.zone_master = master.group(1) if master is not None and "<member" in body else None
        elif path == "/removeZoneSlave":
            self.zone_master = None
            self.now_playing = now_playing_document(device_id=self.device_id, source=SourceName.STANDBY)
        elif path == "/select":
            return await self._select(body)
        return None

    async def _select(self, body: str) -> str | None:
        """Forward the selection as a slave does, or play it and name its preset slot at once.

        The slot is named BEFORE the answer, because that is when a real box names it (Question 5:
        26 ms after the send, 16 ms before the answer returned) - which is why whoever sends a
        ``/select`` has to expect the echo before its own call has returned.
        """
        if self.zone_master is not None:
            self.forwarded.append(body)
            return None
        location = re.search(r'location="([^"]*)"', body)
        played = location.group(1) if location is not None else ""
        self.played.append(played)
        if self.echo_on_select:
            slot = next((n for n, item in self.presets.items() if f'location="{played}"' in item), 0)
            await self._tell_the_watchers(selection_frame(device_id=self.device_id, preset_id=slot))
        return played

    async def _echo_the_select(self) -> None:
        """The rest of a played ``/select``'s echo: one touch, then the box naming its own station."""
        await asyncio.sleep(0.057)
        await self._tell_the_watchers(user_activity_frame(device_id=self.device_id))
        await asyncio.sleep(0.2)
        self.now_playing = now_playing_document(device_id=self.device_id, source=SourceName.LOCAL_INTERNET_RADIO)
        await self._tell_the_watchers(
            now_playing_frame(device_id=self.device_id, source=SourceName.LOCAL_INTERNET_RADIO)
        )
        self.echoes += 1

    async def _tell_the_watchers(self, frame: str) -> None:
        """Send a frame the box sends BY ITSELF - to nobody, when nothing is watching."""
        for connection in list(self._watchers):
            await connection.send(frame)

    def _answer(self, path: str) -> str:
        if path == "/volume":
            return (
                f"<volume><targetvolume>{self.volume}</targetvolume>"
                f"<actualvolume>{self.volume}</actualvolume><muteenabled>false</muteenabled></volume>"
            )
        if path == "/info":
            return f'<info deviceID="{self.device_id}"><name>Fake</name></info>'
        if path == "/presets":
            items = "".join(f'<preset id="{n}">{item}</preset>' for n, item in sorted(self.presets.items()))
            return f"<presets>{items}</presets>"
        if path == "/now_playing":
            return self.now_playing
        return "<status>OK</status>"


class RecordingMaster:
    """A ``MasterPort`` that answers fixed documents and remembers what it was asked to do.

    The HTTP face needs a master behind it, and a test of the face itself must not depend on the
    zone a real one happens to hold. Three test files already carry a copy of this shape; this one
    is the shared home for the next caller, and merging the three is OPEN-WORK rather than a change
    smuggled in here - the golden corpus was recorded against the exact documents one of them
    returns.
    """

    device_id = "5EB0CE000001"

    def __init__(self) -> None:
        self.selected: list[tuple[str, str]] = []
        self.left: list[str] = []

    def info_xml(self) -> str:
        return "<info/>"

    def now_playing_xml(self) -> str:
        return "<nowPlaying/>"

    def zone_xml(self) -> str:
        return "<zone/>"

    async def select(self, content_item_xml: str, *, origin: str) -> None:
        self.selected.append((content_item_xml, origin))

    async def slave_left(self, ip: str) -> None:
        self.left.append(ip)
