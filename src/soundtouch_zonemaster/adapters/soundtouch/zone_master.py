"""The zone master: owns the station, the slaves, and the servers that bind them together.

Placement lives in ``placement`` and every document a speaker sees is built in ``zonexml``; what
is left here is the zone itself - who is in it, what it is playing, and the listeners that let a
speaker reach it.
"""

from __future__ import annotations

import asyncio
import errno
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ...application.errors import PortsBusyError
from ...domain import zonexml
from ...domain.enums import SpeakerPath
from ...domain.station import Station, StationRequest
from . import connections, xmlmodels
from .clock import now_us, serve_clock
from .http_api import HttpApi, serve_http
from .pb import audio_data
from .placement import JoinPlanner, JoinSlot, StreamKey
from .source import RingBuffer, StreamSource
from .speaker_http import http_get, http_post
from .xmlread import attribute_anywhere, parse

if TYPE_CHECKING:
    from collections.abc import Callable

    from ...domain.events import SpeakerEvent
    from ...domain.logfn import LogFn
    from .reports import SlaveState

__all__ = [
    "BIND_ATTEMPTS",
    "BIND_RETRY_WAIT_S",
    "FIRST_BYTES_POLL_S",
    "FIRST_BYTES_TIMEOUT_S",
    "Slave",
    "SlaveTransports",
    "ZoneMaster",
]

BIND_ATTEMPTS = 3
"""How many times :meth:`ZoneMaster.start` tries for the ports before it gives up.

Deliberately SMALL, and the reason is who owns the long wait. The three ports the protocol fixes -
40002, 40003 and 40005 - all sit inside the kernel's ephemeral range on the machine this runs on
(32768 to 60999, read 2026-09-07), so an outbound connection can be holding one when the zone
starts; measured that evening, switching the service on failed three times before the fourth
attempt bound. Waiting that out HERE would be wrong: the service calls this while holding the lock
that serialises a pass, so a ten-second wait here is ten seconds in which the house cannot react to
anything. The service gives up the pass instead and comes back on the next one, a second later,
which is an unbounded retry that blocks nothing.

What is left for this loop is the collision a master makes with ITSELF: a datagram transport's
close is scheduled rather than immediate (see :meth:`stop`), so standing down and coming straight
back can meet its own clock socket. Three attempts a tenth of a second apart covers that and
nothing else.
"""

BIND_RETRY_WAIT_S = 0.1
"""How long between those attempts. One loop turn plus room, which is what the scheduled close needs."""


FIRST_BYTES_TIMEOUT_S = 10.0
"""How long :meth:`ZoneMaster.play` waits for a station's first bytes before starting anyway.

Generous because a station is somebody else's server on somebody else's radio link. Named and
overridable per call so a test can reach the give-up branch without waiting it out; nothing in
the program passes anything but the default.
"""

FIRST_BYTES_POLL_S = 0.1
"""How often that wait looks at the source. Fine enough that the give-up is not visibly late."""


@dataclass
class Slave:
    ip: str
    device_id: str
    transport: connections.TransportConnection | None = None


@dataclass
class SlaveTransports:
    """Every live transport channel, and which one of a box's channels the master drives.

    A box holds MORE than one at a time. Measured in the flat on 2026-09-07 (the run recorded in
    ``research/captures/2026-09-07-live-run-2``): during a burst of membership changes ALL FOUR
    boxes reached two live transport connections, and a box that opened a second one kept the
    first for between 0.5 and 9 seconds. A registry of one connection per address cannot hold
    that, and the way it failed was silent: the OLDER channel ending removed the address, taking
    the LIVE channel out of the registry with it, so the next station switch skipped that box
    while it went on reporting the stream it had been left on. Room4 fell silent at 21:04 that
    evening with the new station's name on its display, and the master had the evidence in every
    ``slave-state`` line it wrote (OPEN-WORK rank 22).

    So a channel is forgotten by IDENTITY and never by address, and the newest live channel of a
    box is the one control is sent on - the box opened it, so it is the one it means.
    """

    _by_peer: dict[str, list[connections.TransportConnection]] = field(
        default_factory=dict[str, list[connections.TransportConnection]]
    )

    def add(self, tc: connections.TransportConnection) -> None:
        """Take a channel that has just connected; it becomes the one its box is driven by."""
        self._by_peer.setdefault(tc.conn.peer, []).append(tc)

    def forget(self, tc: connections.TransportConnection) -> None:
        """Forget THIS channel and no other. Identity, because two channels of one box compare
        equal on their fields and only one of them has ended."""
        peer = tc.conn.peer
        live = [other for other in self._by_peer.get(peer, []) if other is not tc]
        if live:
            self._by_peer[peer] = live
        else:
            self._by_peer.pop(peer, None)

    def driven_for(self, peer: str) -> connections.TransportConnection | None:
        """The channel this box is driven on, or None while it has none."""
        live = self._by_peer.get(peer)
        return live[-1] if live else None

    def driven(self) -> list[connections.TransportConnection]:
        """One channel per box: what a station switch is sent on.

        One and not all of them, because a switch is a STOP followed by a PLAY - sending it twice
        over two channels of the same box would tell it to stop the stream it has just started.
        """
        return [live[-1] for live in self._by_peer.values() if live]

    def peers(self) -> list[str]:
        """The boxes that have a live channel right now."""
        return list(self._by_peer)


@dataclass
class ZoneMaster:
    bind_ip: str
    device_id: str
    log: LogFn
    encryption: audio_data.AudioServerMsgAcceptAudioData.EncryptionType = audio_data.AudioServerMsgAcceptAudioData.NONE
    bind_attempts: int = BIND_ATTEMPTS
    """Overridable so a test can ask for a bound it is willing to wait for."""
    bind_retry_wait_s: float = BIND_RETRY_WAIT_S
    """Overridable for the same reason, and for the same tests."""
    ignore_selects: bool = False
    """Accept a slave's select and do nothing with it.

    A measurement flag, not a feature: it is how a run answers what a speaker does when its key
    press does not change the zone, which is the case every intermediate digit of a dialled
    channel number will be.
    """
    events: asyncio.Queue[SpeakerEvent] | None = None
    """Where a key press forwarded by a slave goes.

    ``None`` is the prototype run, which has nobody behind it to read one; the service passes the
    queue its observers already feed, so both ways a speaker can say something end up in ONE
    stream.
    """
    slave_heard: Callable[[str], None] | None = None
    """Called with the ADDRESS of a slave that has just reported on its transport channel.

    Liveness and never membership: it says something is at the other end of a channel we opened,
    which is all a report proves. The service uses it to keep a quiet member from ageing out.
    """
    slaves: dict[str, Slave] = field(default_factory=dict[str, Slave])
    transports: SlaveTransports = field(default_factory=SlaveTransports)
    station: Station | None = None
    sources: dict[int, StreamSource] = field(default_factory=dict[int, StreamSource])
    _next_url_id: int = 1
    _play_generation: int = 0
    """Counts calls to :meth:`play`, so an overtaken one can recognise that it lost."""
    _switch_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Held only for the switch itself, never for the wait that precedes it."""
    _http_api: HttpApi | None = None
    """Kept from :meth:`start`, because shutting down means stopping ITS background work too."""
    _servers: list[asyncio.AbstractServer | asyncio.DatagramTransport] = field(
        default_factory=list[asyncio.AbstractServer | asyncio.DatagramTransport]
    )
    planner: JoinPlanner = field(init=False)

    def __post_init__(self) -> None:
        # The planner reaches streams through source_for, so it can read one and never add one.
        self.planner = JoinPlanner(log=self.log, source_for=self.source_for)

    # --- lifecycle ---------------------------------------------------------------------------

    async def start(self) -> None:
        """Take the four listeners, waiting out a port somebody else is holding.

        The ports are the protocol's and cannot be moved, and on this machine all three of the
        numbered ones fall inside the kernel's ephemeral range, so any outbound connection can be
        sitting on one when the zone starts. That is transient - a connection ends - so it is worth
        waiting for; measured in the flat on 2026-09-07, three attempts failed and the fourth bound.

        A busy port must not be fatal, because the caller is a service that is watching speakers and
        holding a house: it can come back on its next pass, and dying loses the observers, the
        registry and everything the membership rule has learned. Anything OTHER than a busy port is
        raised at once - an address that does not exist does not become free.

        The waiting here is deliberately short and is NOT the answer to a port somebody else holds;
        see :data:`BIND_ATTEMPTS` for who owns that wait. Reporting is the caller's too: a service
        that retries once a second must not write a line every second, which is why this raises
        with the detail rather than logging it.
        """
        for attempt in range(1, self.bind_attempts + 1):
            try:
                await self._take_the_listeners()
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE:
                    raise
                await self._give_the_listeners_back()
                if attempt == self.bind_attempts:
                    raise PortsBusyError(f"{self.bind_ip}: still busy after {attempt} attempt(s): {exc}") from exc
                await asyncio.sleep(self.bind_retry_wait_s)
            else:
                self.log(
                    "master", f"listening on {self.bind_ip}: udp/40005 tcp/40002 tcp/40003 tcp/8090 as {self.device_id}"
                )
                return

    async def _take_the_listeners(self) -> None:
        """Bind all four, in the order a slave meets them. Each one is appended as it succeeds.

        Appending as it goes is what makes a failure recoverable: the ones already bound are in
        ``_servers`` and :meth:`_give_the_listeners_back` can close them.
        """
        self._servers.append(await serve_clock(self.bind_ip, 40005, self.log))
        self._servers.append(
            await connections.serve_transport(self.bind_ip, self.log, self._on_transport, self.transport_closed)
        )
        self._servers.append(
            await connections.serve_data(self.bind_ip, self.log, self.source_for, self.encryption, self.base_for)
        )
        self._http_api = HttpApi(self, self.log, events=self.events)
        self._servers.append(await serve_http(self.bind_ip, self._http_api, self.log))

    async def _give_the_listeners_back(self) -> None:
        """Undo a partial bind, so the next attempt meets the world and not this master.

        Without it the retry fails on the ports this master is holding itself, and the log names
        the wrong port. The yield is the same one :meth:`stop` needs and for the same reason: a
        datagram transport's close is scheduled, so the clock's socket is bound until the loop
        takes its next turn.
        """
        if self._http_api is not None:
            await self._http_api.aclose()
            self._http_api = None
        for s in self._servers:
            s.close()
        self._servers.clear()
        await asyncio.sleep(0)

    async def stop(self) -> None:
        """Close every listener and stop every source.

        Deliberately NOT ``await s.wait_closed()``. On an asyncio Server that waits for every
        in-flight CONNECTION HANDLER as well as the listening socket, and a slave holds its
        transport and data connections open for as long as it is in the zone - so awaiting it
        here waits for a speaker to leave, which is not what shutting down means. Measured
        2026-09-06: adding it hung the loopback end-to-end test until it was killed.

        ``close()`` releases the listening socket itself, which is the part a restart needs.
        The per-connection ``wait_closed()`` in the channel and HTTP handlers is the bounded
        one, and that is where it belongs.
        """
        if self._http_api is not None:
            await self._http_api.aclose()
        for s in self._servers:
            s.close()
        self._servers.clear()
        # A datagram transport's close is SCHEDULED, not immediate: it goes through call_soon, so
        # the clock's UDP socket is still bound until the loop takes its next turn. One yield is
        # what makes "stopped" mean the ports are free - a service stands down and comes back
        # whenever somebody flips the switch, and the master after it would die on bind.
        await asyncio.sleep(0)
        for src in self.sources.values():
            await src.stop()

    # --- station ---------------------------------------------------------------------------------

    async def play(
        self, request: StationRequest, *, first_bytes_timeout_s: float = FIRST_BYTES_TIMEOUT_S
    ) -> Station | None:
        """Start fetching a station and switch the slaves to it; None if a newer press won.

        Two presses can be inside this method at once, because every ``/slaveMsg`` select starts
        its own task and nothing outside here orders them. The wait for the first bytes lasts up to
        ten seconds, so the call that started FIRST routinely finishes LAST, and unordered it would
        install its station over the newer one and stop the newer one's source on the way out.
        What a listener hears is the zone falling back to the station they pressed EARLIER.

        Two mechanisms, one job each. The generation number decides who wins: a call that finds a
        newer one has started while it waited gives up, stops the source it created and returns
        None, so the last press is the one that plays. The lock makes the switch itself atomic,
        because sending two different stations to the same transports at once is incoherent in any
        order. The lock is taken AFTER the wait and never around it, so no press is made to wait
        ten seconds on another, and the announcement is left outside it because that talks to every
        slave over HTTP.
        """
        self._play_generation += 1
        generation = self._play_generation
        station = Station(self._next_url_id, request.playback_url, request.name, request.content_item_xml)
        self._next_url_id += 1
        source = StreamSource(station, RingBuffer(), self.log)
        source.start(now_us)
        self.sources[station.url_id] = source
        # Wait for the first bytes so t0_us exists before any slave is told to PLAY at it.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + first_bytes_timeout_s
        while source.t0_us is None and loop.time() < deadline:
            await asyncio.sleep(FIRST_BYTES_POLL_S)
        # Read it ONCE here: it is set exactly once per source, and a local keeps the rest of this
        # method from asking a question whose answer is already known.
        t0_us = source.t0_us
        if t0_us is None:
            self.log(
                "master", f"url_id={station.url_id}: no bytes after {first_bytes_timeout_s:.0f} s, continuing anyway"
            )
            t0_us = now_us()
            source.begin_at(t0_us)
        async with self._switch_lock:
            if generation != self._play_generation:
                self.log("master", f"url_id={station.url_id}: a newer select won, dropping {station.name}")
                self.sources.pop(station.url_id, None)
                await source.stop()
                return None
            old = self.station
            self.station = station
            if old is not None:
                # Every connected transport channel is switched, registered slave or not: a slave
                # whose channel is up is playing us, whatever the registry thinks. They all start
                # the new stream together at its byte 0.
                for peer in self.transports.peers():
                    self.planner.assign(
                        StreamKey(peer, station.url_id), JoinSlot(source.timeline.t0_us, source.timeline.t0_byte)
                    )
                # Only channels that have JOINED. A switch is a STOP and a PLAY, and a channel
                # still inside its join sequence has been told neither its clock master nor its
                # first URL - only the join carries those - so switching it would leave a box
                # holding a stream it cannot place. It is waiting for this very lock, and it
                # joins on the station this call is installing as soon as this releases it.
                await asyncio.gather(
                    *(tc.switch(station, t0_us) for tc in self.transports.driven() if tc.current is not None),
                    return_exceptions=True,
                )
                old_src = self.sources.pop(old.url_id, None)
                if old_src:
                    await old_src.stop()
                self.planner.drop_stream(old.url_id)
        await self._announce_selection(station)
        return station

    async def select(self, content_item_xml: str, *, origin: str) -> None:
        """A slave pressed a preset: switch the whole zone to it (what a real master does)."""
        if self.ignore_selects:
            self.log("master", f"selection from {origin}: ignoring (--ignore-selects)")
            return
        request = xmlmodels.station_request(content_item_xml)
        if request is None:
            self.log("master", f"selection from {origin}: no location in {content_item_xml[:100]}")
            return
        self.log("master", f"selection from {origin}: {request.name}")
        await self.play(request)

    def source_for(self, stream_id: int) -> StreamSource | None:
        return self.sources.get(stream_id)

    # --- placement, as the channels reach it ------------------------------------------------------

    async def base_for(self, peer: str, stream_id: int) -> int | None:
        """The absolute byte a slave's data channel starts at, planned when its PLAY was sent."""
        return await self.planner.base_for(peer, stream_id)

    async def plan_join(self, peer: str, source: StreamSource) -> JoinSlot:
        """Decide a slave's PLAY time and first byte for ``source`` (REPORT.md S7 rule)."""
        return await self.planner.plan_join(peer, source)

    def on_slave_state(self, report: SlaveState) -> None:
        """Place a slave's PLAYING report against the zone; the ``sync`` line is the instrument.

        It is also the only proof that a member is still there, so it is passed on as liveness
        before it is placed: a box playing our stream has nothing to say on its own notification
        channel and says nothing at all.
        """
        if self.slave_heard is not None:
            self.slave_heard(report.peer)
        self.planner.on_slave_state(report)

    # --- slaves ----------------------------------------------------------------------------------

    async def add_slave(self, ip: str) -> None:
        info = await http_get(ip, "/info")
        root = parse(info)
        device_id = attribute_anywhere(root, "deviceID") if root is not None else None
        if device_id is None:
            raise RuntimeError(f"{ip}: no deviceID in /info")
        # Recorded BEFORE the push, because the document being pushed names the whole zone, this
        # box included - and taken back out again if the push fails. An unguarded record survives
        # a box that refused, and then nothing removes it: the caller never counted it as joined,
        # while every later announcement and the dissolve still reach it. A box that was never in
        # the zone would be sent the document that puts a real speaker into standby.
        self.slaves[ip] = Slave(ip=ip, device_id=device_id)
        try:
            await self._push_zone_to(ip)
        except Exception:
            self.slaves.pop(ip, None)
            raise

    async def _push_zone_to(self, ip: str) -> None:
        body = zonexml.zone_body(
            device_id=self.device_id, bind_ip=self.bind_ip, members=self.slaves.values(), sender_is_master=True
        )
        out = await http_post(ip, "/setZone", body)
        self.log("master", f"setZone -> {ip}: {out.strip()[-60:]}")

    async def slave_left(self, ip: str) -> None:
        """Drop one slave. The boxes that stay are told NOTHING, and that is the point.

        This used to push the smaller member list to everyone who remained, so that no box kept a
        document naming a speaker that had gone. Measured on the hardware on 2026-09-08, twice and
        once by ear, that correction is the most expensive thing the zone does: a slave STOPS,
        reconnects and re-buffers on any ``/setZone`` - 3.553 s in one room, 4.081 s in the other -
        and it does so even when the document is byte-identical to the one it already holds, which
        is precisely what the box that stayed was being sent. ``add_slave`` inserts the arriving box
        before pushing, so a box that joined alone holds a list naming only itself, and a leave that
        empties the zone back down to it rebuilds exactly that document.

        What the push bought was nothing anybody can hear. No box holds a correct member list in the
        first place - each carries one naming only itself - and the house still plays in sync,
        because the audio path hangs on the ``master`` field, which every box agrees about. So a box
        that joined before the leaver may now keep a member entry for a speaker that has gone; it
        costs a stale room in the app's zone view and nothing in the audio.

        A leave is therefore bookkeeping, and bookkeeping cannot fail on an unplugged speaker.
        """
        if ip not in self.slaves:
            return
        self.slaves.pop(ip)
        self.log("master", f"{ip} left the zone; {len(self.slaves)} slave(s) remain")

    async def release(self, ip: str) -> None:
        """Tell one slave it is free, then drop it the way :meth:`slave_left` does.

        :meth:`slave_left` sends the box nothing, and for a box that left BY ITSELF that is right.
        For a box the house takes out it is not: measured 2026-09-24, such a box stays our slave -
        transport open, still in sync, ``/getZone`` naming us - and forwards every ``/select`` and
        every preset press to the master instead of playing it. The message that frees it is the
        one a Bose master sends a leaving slave, ``/removeZoneSlave`` with the slave as the only
        member; the box answers with its own ``removeZoneSlave``, closes its transport and goes to
        STANDBY (docs/measurements/2026-09-24-releasing-one-slave.md). The OTHER boxes are sent
        nothing, for the reason :meth:`slave_left` gives.

        A failed send is logged and not raised: the house has decided the box is out either way,
        and a box the message did not reach is still our slave and still forwards its keys, which
        is the one way back it then has.
        """
        slave = self.slaves.get(ip)
        if slave is None:
            return
        body = zonexml.zone_body(device_id=self.device_id, bind_ip=self.bind_ip, members=[slave], sender_is_master=True)
        try:
            out = await http_post(ip, SpeakerPath.REMOVE_ZONE_SLAVE, body)
            self.log("master", f"removeZoneSlave -> {ip}: {out.strip()[-60:]}")
        except Exception as exc:  # noqa: BLE001 - the box is out of the house's zone whatever it answers
            self.log("master", f"removeZoneSlave -> {ip} failed ({type(exc).__name__}); it may still be our slave")
        await self.slave_left(ip)

    async def stop_station(self) -> None:
        """Stop fetching, and keep listening. What an empty zone pulls is bandwidth nobody hears.

        Not the same as :meth:`stop`: a run that ends closes everything, while a service whose
        zone has emptied keeps its listeners up so the next box that wakes finds a master, and
        simply stops paying for a stream. The placement books go with the stream they belonged to.
        """
        if self.station is None:
            return
        self.log("master", f"nothing left in the zone; stopping {self.station.name}")
        for url_id, source in list(self.sources.items()):
            await source.stop()
            self.planner.drop_stream(url_id)
        self.sources.clear()
        self.station = None

    async def dissolve(self) -> None:
        """Send every slave an empty zone; a real slave goes to standby on that (E6).

        Slave-driven work is stopped FIRST. A preset press that arrived a moment ago is still in
        ``play()``'s wait for its station's first bytes, and finishing after the dissolve would
        send a speaker the switch sequence for a zone that no longer exists - it would start
        playing again with nobody expecting it. Dissolving is the end of the zone, so it is also
        the end of anything a slave asked for.
        """
        if self._http_api is not None:
            await self._http_api.aclose()
        body = zonexml.dissolve_body(device_id=self.device_id, bind_ip=self.bind_ip)
        for ip in list(self.slaves):
            try:
                await http_post(ip, "/setZone", body)
                self.log("master", f"dissolved zone at {ip}")
            except Exception as exc:  # noqa: BLE001 - keep dissolving the others
                self.log("master", f"dissolve {ip}: {exc!r}")
        self.slaves.clear()

    async def _on_transport(self, tc: connections.TransportConnection) -> None:
        """Place a channel a box has just opened, on the station the zone is on.

        Under the switch lock, and everything it decides is read INSIDE it, for the reason
        :meth:`put_back_on_the_station` gives: ``plan_join`` really waits - up to ``RATE_WAIT_S``
        for a rate the playing slaves have not reported yet - and a press can move the zone inside
        that wait. Reading the station on both sides of it told the box to play the NEW station at
        a time and a byte planned in the OLD stream's ring, and recorded that byte under the new
        stream's key, where the data channel then served from it.

        This is the third of the three join paths, and the only one that was not doing this.
        """
        slave = self.slaves.get(tc.conn.peer)
        if slave is None:
            self.log("master", f"transport from unknown {tc.conn.peer}; accepting anyway")
        else:
            slave.transport = tc
        self.transports.add(tc)
        tc.on_state = self.on_slave_state
        async with self._switch_lock:
            station = self.station
            if station is None:
                raise connections.CannotServeError("no station selected yet")
            source = self.sources.get(station.url_id)
            if source is None:
                raise connections.CannotServeError(f"no source for url_id={station.url_id}")
            slot = await self.plan_join(tc.conn.peer, source)
            self.planner.assign(StreamKey(tc.conn.peer, station.url_id), slot)
            await tc.join(station, slot.t0_us)

    def slaves_left_on_an_old_stream(self) -> list[str]:
        """Boxes whose driven channel was never put on the station the zone is playing.

        What we TOLD each channel, not what its box reports back. A report lags a switch by a
        second or so, so comparing against it would need a tolerance nobody has measured; the
        channel's own ``current`` needs none - it is exactly the set of channels a switch did not
        reach. Today one thing puts a channel in that set: a switch goes to ONE channel per box, so
        a superseded channel stays where it was, and if the driven one then ends, the box is driven
        on a channel the zone has left behind (OPEN-WORK rank 22).

        It does NOT catch a box that was told and did not obey. That needs the reported ``url_id``
        and a tolerance for the lag, and the lag has never been measured.
        """
        if self.station is None:
            return []
        left: list[str] = []
        for peer in self.transports.peers():
            driven = self.transports.driven_for(peer)
            if driven is not None and driven.current is not self.station:
                left.append(peer)
        return left

    async def put_back_on_the_station(self, peer: str) -> bool:
        """Move a box that was left behind onto the stream the zone is playing. True if it moved.

        A LATE JOINER and not a co-starter: the others have been playing for a while, so the
        placement comes from :meth:`plan_join` (REPORT.md S7) rather than from the stream's own
        start. Under the switch lock, and everything it decides is re-read inside it, because a
        press can move the zone while this is waiting for that lock - putting a box on a station
        that has just been replaced is the defect this exists to fix, committed twice over.
        """
        async with self._switch_lock:
            station = self.station
            driven = self.transports.driven_for(peer)
            if station is None or driven is None or driven.current is station:
                return False
            source = self.sources.get(station.url_id)
            if source is None:
                return False
            slot = await self.plan_join(peer, source)
            self.planner.assign(StreamKey(peer, station.url_id), slot)
            return await driven.switch(station, slot.t0_us)

    def transport_closed(self, tc: connections.TransportConnection) -> None:
        """One channel has ended. What the BOX still has decides what is forgotten with it.

        The placement books belong to the box and not to the channel, so they are dropped only
        once the box has no channel left; dropping them on any close threw away the placement of
        a channel that was still carrying audio.

        A box whose driven channel ends while an older one is still live keeps that older one, and
        the older one is on whatever station it was last told about. Said out loud when the two
        differ, because that box is playing the wrong stream and will fall silent when the stream
        it is on stops - re-setting it is rank 22's remaining half and is not done here.
        """
        peer = tc.conn.peer
        driven_before = self.transports.driven_for(peer)
        self.transports.forget(tc)
        driven = self.transports.driven_for(peer)
        slave = self.slaves.get(peer)
        if slave is not None and slave.transport is tc:
            slave.transport = driven
        if driven is None:
            self.planner.drop_peer(peer)
            return
        if driven_before is tc and driven.current is not self.station:
            on = "nothing" if driven.current is None else f"url_id={driven.current.url_id}"
            zone = "nothing" if self.station is None else f"url_id={self.station.url_id}"
            self.log("master", f"{peer}: driven channel ended; the one left is on {on} and the zone on {zone}")

    async def _announce_selection(self, station: Station) -> None:
        sel = zonexml.selection_message(station)
        note = zonexml.now_playing_notification(device_id=self.device_id, bind_ip=self.bind_ip, station=station)
        for ip in list(self.slaves):
            for path, body in (("/masterMsg", sel), ("/notification", note)):
                try:
                    await http_post(ip, path, body)
                except Exception as exc:  # noqa: BLE001 - display-only traffic
                    self.log("master", f"{path} -> {ip}: {exc!r}")

    # --- XML the slaves ask for ------------------------------------------------------------------

    def zone_xml(self) -> str:
        return zonexml.zone_xml(device_id=self.device_id, members=self.slaves.values())

    def info_xml(self) -> str:
        return zonexml.info_xml(device_id=self.device_id, bind_ip=self.bind_ip)

    def now_playing_xml(self) -> str:
        return zonexml.now_playing_xml(device_id=self.device_id, station=self.station)
