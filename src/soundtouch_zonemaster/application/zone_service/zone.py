"""The pass: make the zone look like what the policy says, and nothing else anywhere.

Fifth in the chain, and the heart of the service. Every event, every registry read and every flip
of the switch ends in the same question - who should be in the zone now - answered in one place
under one lock. There is no second path that joins or drops a speaker.

Nothing is taken from anybody. A box that stops being a member by itself is dropped WITHOUT being
sent anything, because the document that takes a box out of a zone is the one that puts it into
standby (E6). A box a person takes out of multiroom is the exception: it is released on the wire and
handed its channel. Only the switch and the end of the service dissolve, and those are deliberate.

The switch worker and the pass loop are here too, rather than with the run that starts them: what
they do is ask for a pass, and the reason the loop may come back unasked is a busy listening port,
which is this class's state.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

from ...domain import zonexml
from ...domain.enums import ChannelEnd, ChannelKind
from ...domain.logfn import ERROR_KIND
from ...domain.mpd import entry_after
from ...domain.playorder import directory_jump, play_order
from ...domain.state import Place
from ...domain.station import StationRequest
from ..errors import MpdError, NotInMpdError, PortsBusyError
from .constants import JOIN_RETRY_S, wait_for_the_next_pass_s
from .volume import VolumeGuard

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ...domain.channellist import Channel
    from ..ports import MpdControlPort, ZoneMasterPort

__all__ = ["ZoneReconcile"]

MPD_TIMEOUT_S = 5.0
"""How long MPD is given to take a channel before the pass gives up on it and plays anyway.

Bounded because the pass holds its lock across this call, which is the same reason every
call to a speaker under that lock is bounded: a daemon on a host that has gone away answers
neither yes nor no, and a pass that waits for it stops holding the house. Generous for what
it covers - a loopback exchange of three short lines - because being wrong the other way
costs a channel, and being wrong this way costs the zone."""

OWN_SELECT_TAIL_S = 1.0
"""How long after one of our ``/select`` calls returns its echo may still arrive.

Measured 2026-09-24 on Room1: the selection landed before the call returned both times, and the
touch 41 ms and 398 ms after it - the second with the box just released, which is the case this
service creates. A second is 2.5 times the slow one. It only bounds how long a claim waits for an
echo that never comes; each claim hands over one frame and is then spent.
"""


class ZoneReconcile(VolumeGuard):
    """The zone, the one pass that changes it, and the two loops that ask for one."""

    async def _watch_the_switch(self) -> None:
        """Off stands the service down; on builds the zone again. Nothing else is touched."""
        async with contextlib.aclosing(self.switch.watch()) as switch:
            async for on in switch:
                self._on = on
                self._wanted.set()

    async def _reconcile_when_asked(self) -> None:
        """One pass at a time, and another as soon as anything happened while one was running."""
        while True:
            await self._wait_to_be_asked()
            self._wanted.clear()
            await self._reconcile()

    async def _wait_to_be_asked(self) -> None:
        """Wait for something to happen - or, while a port is busy, come back without being asked.

        A busy port is the one state nothing else reports a change out of: the switch has not moved,
        sleeping boxes say nothing, and the registry poll is half a minute away. Every other reason
        a pass is wanted arrives as an event, so this waits normally the rest of the time.
        """
        waiting_for = wait_for_the_next_pass_s(ports_busy=self._last_reported.ports_busy)
        if waiting_for is None:
            await self._wanted.wait()
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wanted.wait(), timeout=waiting_for)

    async def _reconcile(self) -> None:
        """Make the zone look like what the policy says. Every path ends here and nowhere else.

        The lock is what keeps two events from taking the same box on twice; it is held across
        calls to the speakers, which is why each of those has a timeout and a box that refuses is
        then left alone for a while.
        """
        # Before the lock: it is the slow half (six HTTP reads of one box) and it only ever runs
        # while there is no list at all, so it must not hold up a pass that is holding the house.
        await self._seed_the_channels()
        async with self._lock:
            # FIRST in the pass, and that is a rule rather than an ordering. Two of the paths
            # below give the whole pass up - the switch off, a listening port held by somebody
            # else - and while this call stood under them a box whose mute outlived its join
            # stayed silently at zero until the house was switched on again or the service
            # restarted (measured 2026-09-18, OPEN-WORK rank 41). At the top, a return added
            # anywhere in this method is below it by construction, so the omission cannot be
            # written. It needs no zone and no master, which is what lets it be here at all.
            #
            # One path is still above it and RAISES rather than returns: a refused seeding ends
            # the pass loop and with it the service, and the start-up restore is what puts those
            # volumes back on the next run.
            await self._put_back_any_volume_we_took_away()
            believed = self._who_belongs()
            # Only when the registry has answered at least once. At start it may not have - the
            # neighbour is a container that can still be coming up - and target_members over no
            # speakers is an empty answer that would write the house out of the file, which is the
            # only memory a restart has.
            if self._speakers:
                self._write_down(believed)
            if not self._on:
                await self._stand_down()
                return
            try:
                master = await self._zone()
            except PortsBusyError as busy:
                # Costs this pass and nothing else. _wait_to_be_asked brings the next one back in
                # PORTS_BUSY_RETRY_S, so the retry is the loop itself and the observers, the registry
                # and the membership state all survive - which is what dying here destroyed
                # (measured in the flat 2026-09-07 22:07).
                self._note_the_ports(busy)
                return
            self._note_the_ports(None)
            await self._follow_a_move(master)
            await self._let_go(master, believed)
            await self._take_in(master, believed)
            await self._put_back_what_the_zone_left_behind(master)
            if not self._joined:
                await master.stop_station()

    def _note_the_ports(self, busy: PortsBusyError | None) -> None:
        """Say it when the ports go busy and when they come back, and never in between.

        A pass runs about once a second, so a line per pass would be the same endless stream a
        reader has to scroll past to find a real fault. Only the two edges are news.
        """
        if (busy is not None) == self._last_reported.ports_busy:
            return
        self._last_reported.ports_busy = busy is not None
        if busy is None:
            self.log("master", "the ports are free again; taking the zone")
        else:
            self.log("master", f"a port is busy, so there is no zone this pass: {busy}")

    def _who_belongs(self) -> frozenset[str]:
        """The policy's answer right now, less the boxes somebody switched out by hand.

        The exclusion is taken off HERE and not inside the policy, because the policy decides from
        what the speakers said and nothing else, and a double-tapped thumb is a house setting like the
        switch. Taking it off at the one place the answer is formed makes the whole pass agree: the
        box is let go, it is not taken in again, and it is not written down as a member either.
        """
        return self.policy.target_members(list(self._speakers.values())) - self._out_of_multiroom

    async def _follow_a_move(self, master: ZoneMasterPort) -> None:
        """Let go of a box that is on a new address, so it can be taken back at the new one.

        The zone holds a box by the address it joined at, and the observer does not: it asks the
        registry again on every attempt, so it follows a move by itself. Without this the member
        list the others hold names an address that is gone, and the dissolve goes there too - which
        leaves the box that moved in a zone whose master has gone.
        """
        for device_id, address in list(self._joined.items()):
            speaker = self._speakers.get(device_id)
            if speaker is None or speaker.ip == address:
                continue
            self.log("zone", f"{speaker.name} moved to {speaker.ip}; taking it back there")
            self._joined.pop(device_id)
            await master.slave_left(address)

    async def _zone(self) -> ZoneMasterPort:
        """The running master, started if the switch has just come on."""
        if self.master is not None:
            return self.master
        master = self.ports.open_zone_master(
            bind_ip=self.options.bind_ip,
            device_id=self.options.device_id,
            log=self.log,
            events=self.events,
            slave_heard=self._heard_from,
            # M2 REPLACES the select path. A forwarded preset press carries the ContentItem and no
            # number, so acting on it would switch the zone to the pressed preset on the FIRST
            # digit - which is the opposite of dialling, where an intermediate digit does nothing.
            # The number comes from the box's own notification channel instead.
            ignore_selects=True,
        )
        # Held BEFORE it is started: start() binds four listeners one after another, so a failure
        # or a cancel in the middle would otherwise leave the bound ones with nothing holding them
        # and nothing able to close them.
        self.master = master
        try:
            await master.start()
        except BaseException:
            self.master = None
            await master.stop()
            raise
        return master

    async def _let_go(self, master: ZoneMasterPort, believed: frozenset[str]) -> None:
        """Drop the boxes that are no longer ours - sending nothing to a box that left by itself.

        A box on AUX is one somebody is listening to, and the document that would take it out of
        the zone is the one that puts it into standby. So it is sent nothing at all: it stops being
        a member, and nothing is sent to the boxes that stay either - a zone document costs a
        playing box about four seconds of silence, measured 2026-09-08 (``master.slave_left``).

        A box a PERSON took out of multiroom is the one exception, and it is asked by the flag
        rather than by the reason it left. That box is still our slave, and a slave plays nothing
        it is told to play by itself - it forwards it to us - so it is released on the wire first
        and handed its channel after (``master.release``, measured 2026-09-24).
        """
        for device_id in sorted(self._joined.keys() - believed):
            address = self._joined.pop(device_id)
            if device_id in self._out_of_multiroom:
                self.log("zone", f"{self._name(device_id)} is out of multiroom; releasing it")
                await master.release(address)
                await self._on_its_own(device_id, address)
                continue
            self.log("zone", f"{self._name(device_id)} is not ours any more; leaving it as it is")
            await master.slave_left(address)

    async def _on_its_own(self, device_id: str, address: str) -> None:
        """Hand a box a person switched out of multiroom the channel it was hearing.

        Only for a box a PERSON took out, and only once it has been released. A box that left by
        itself went to AUX or Bluetooth and somebody is listening to what it chose, so selecting a
        station on it is the one thing that must never happen.

        Taking a room out of the group should not stop the music in that room, and the box can
        fetch the channel with nothing else running: the url is the local service's own playback
        address, the same one that stands in the box's presets. The release has just put the box
        into STANDBY, and this ``/select`` wakes it (measured 2026-09-24, Questions 5 and 6: sent at
        once, it plays). From here it plays on its own, out of step with the house, which is what
        stepping out means.

        A failure is logged and left. The box is out of the zone either way, and the next thing the
        person presses is the retry.
        """
        channel = self._the_channel_to_play()
        if channel is None:
            self.log("zone", f"{self._name(device_id)} is out of multiroom; nothing in the list to leave it playing")
            return
        try:
            await self._select_on_its_own(device_id, address, channel)
        except Exception as exc:  # noqa: BLE001 - the next press is the retry, so this must not raise
            self.log(
                "zone", f"{self._name(device_id)}: {channel.name} could not be started on it ({type(exc).__name__})"
            )
            return
        self.log("zone", f"{self._name(device_id)} is on its own now, playing {channel.name}")

    async def _select_on_its_own(self, device_id: str, address: str, channel: Channel) -> None:
        """Every ``/select`` this service sends, marked so the box's answer is not read as a person.

        A box outside the zone answers one by naming the preset slot that holds the station and
        sending one touch, which is a pressed preset in every respect but the count of touches
        (measured 2026-09-24). Read as a press it dialled the same channel and sent this again,
        every 0.65 s for 50 s in the flat; and on a box the release has just put to sleep it reads
        as the box being switched ON, which is the way back into multiroom. So each call is owed
        one selection and one touch, claimed by ``_claimed_as_our_selection`` and
        ``_claimed_as_our_echo`` - owed from BEFORE the send, because the selection arrives before
        the call returns.
        """
        touch, selection = self._owed.owe_touch(device_id), self._owed.owe_selection(device_id)
        try:
            await self.ports.select_station(address, url=channel.url, name=channel.name)
        finally:
            touch.until = selection.until = time.time() + OWN_SELECT_TAIL_S

    def _claimed_as_our_selection(self, device_id: str, *, at: float) -> bool:
        """Whether a selection this box reported at ``at`` is the echo a ``/select`` of ours is owed."""
        return self._owed.claim_selection(device_id, at=at)

    async def _take_in(self, master: ZoneMasterPort, believed: frozenset[str]) -> None:
        """Take in every box that belongs and is not in yet, all of them at once.

        Nothing arrives while a number is still open, because the channel is not decided yet and
        there is nothing correct to start. Measured in the fifth live run 2026-09-08 (OPEN-WORK
        rank 31): a box that named its own source 0.19 s after the press became a member the
        ordinary way, and the pass that took it in started the REMEMBERED channel; the number
        completed 0.4 s later naming a different one, and every box was switched over. Two stations
        fetched for a press that named ONE channel, and each room already playing pays a stop and a
        re-buffer of 3.5 to 4 s for it (``master.slave_left``).

        The wait is bounded by the dialling window and by nothing else - at most
        :data:`soundtouch_zonemaster.domain.dialling.WINDOW_CEILING_S` - because a deadline is armed at the press and
        only a further press moves it. ``deadline`` covers steps as well as digits, and a step
        changes the channel exactly as a number does.
        """
        arriving = sorted(device for device in believed - self._joined.keys() if self._may_try(device))
        if self._dialler.deadline() is not None:
            self._note_the_wait(arriving)
            return
        self._note_the_wait([])
        if not arriving:
            return
        await self._play_the_channel(master)
        # That wait lasts as long as somebody else's server takes to answer, up to ten seconds,
        # and the house can change its mind inside it. So the question is asked again before
        # anything is SENT: a box taken over after it went to AUX is dropped again on the next
        # pass without being told - which is right for a box that left by itself, and would leave
        # this one playing our stream for good with the input somebody just chose gone.
        still: frozenset[str] = self._who_belongs() if self._on else frozenset()
        for device_id in sorted(set(arriving) - still):
            self.log("zone", f"{self._name(device_id)} changed its mind while the station started")
        arriving = [device_id for device_id in arriving if device_id in still]
        if arriving:
            await asyncio.gather(*(self._take_one(master, device_id) for device_id in arriving))

    def _note_the_wait(self, waiting: list[str]) -> None:
        """Say which boxes wait for a number when that changes, and never in between.

        A pass runs for every frame a box sends, and while somebody dials that is several a
        second: measured 2026-09-20, eleven identical lines in 0.9 s, burying the lines about the
        channel change they were waiting for. An empty list ends the wait without a line.
        """
        now = frozenset(waiting)
        if now == self._last_reported.waiting_on_a_number:
            return
        self._last_reported.waiting_on_a_number = now
        if now:
            self.log("zone", f"{self._names(waiting)}: waiting, because a number is still being dialled")

    async def _put_back_what_the_zone_left_behind(self, master: ZoneMasterPort) -> None:
        """Move a box that is on a stream the zone has left onto the one it is playing.

        The safety net over rank 22: a box can end up driven on a channel a station change never
        reached, and it plays the old stream until that stream stops - then falls silent with the
        new station's name on its display, which is what a person in the room notices first.

        It runs HERE, in the pass, and that is the whole reason it is safe. The pass is the one
        place that talks to speakers and it is serialised, so this cannot race the switch it is
        repairing; the master's own switch lock is the second half of that. Doing it from the
        report path instead would fire on every report a box sends, needing a guard against
        re-setting a box already being moved, and doing it in the close handler would need an
        await inside a teardown that may itself be being cancelled.

        After ``_take_in``, because a box arriving in this same pass is joined to the right station
        by that and has nothing to be put back onto.
        """
        for address in master.slaves_left_on_an_old_stream():
            if await master.put_back_on_the_station(address):
                self.log("zone", f"{self._name_of(address)} was left on an old stream and was put back")

    def _may_try(self, device_id: str) -> bool:
        """Whether a box that refused earlier may be tried again yet (:data:`JOIN_RETRY_S`)."""
        refused = self._refused.get(device_id)
        return refused is None or time.monotonic() - refused >= JOIN_RETRY_S

    async def _take_one(self, master: ZoneMasterPort, device_id: str) -> None:
        """One box into the zone, without the room hearing what it was playing on its own.

        A box that has just been switched on plays its OWN last station while it waits to be taken
        in - measured 2026-09-08, 4.7 s of it, and the user heard it. So it is turned down to zero
        first, joined, and faded back up to the level it was on once it has let go of that station.

        Its failure is still its own and reaches nobody else, and the volume is put back on every
        path out of here: a box that refuses to join is turned up again at once, because it keeps
        playing its own station and a silent speaker is worse than the wrong one.
        """
        speaker = self._speakers[device_id]
        level = await self._turn_it_down(speaker)
        try:
            await master.add_slave(speaker.ip)
        except Exception as exc:  # noqa: BLE001 - a box that will not join is not the others' problem
            self._refused[device_id] = time.monotonic()
            # The retry is named here because it is the only place it can be seen. A pass that
            # skips a box inside its quiet period returns before every log line it has, so the
            # skip itself is silent - and a line per pass would be a line a second. One line, at
            # the moment it happens, saying what will happen next.
            self.log(
                "zone",
                f"{speaker.name} ({speaker.ip}) did not join: {type(exc).__name__}: {exc}"
                f"; not tried again for {JOIN_RETRY_S:.0f} s",
            )
            await self._turn_it_back_up(speaker, level, fade=False)
            return
        self._refused.pop(device_id, None)
        self._joined[device_id] = speaker.ip
        self.log("zone", f"{speaker.name} joined; {len(self._joined)} in the zone")
        await self._turn_it_back_up(speaker, level, fade=True)

    async def _play_the_channel(self, master: ZoneMasterPort) -> None:
        """Start the stream of the channel the zone is on, if it is not already running.

        Lazily on purpose: a zone nobody is in must not pull a radio station all night.
        """
        if master.station is not None:
            return
        channel = self._the_channel_to_play()
        if channel is None:
            self.log("channels", "no channel to play; nothing has been seeded and the list is empty")
            return
        await self._start_the_channel(master, channel)

    def _start_the_channel_soon(self, master: ZoneMasterPort, channel: Channel) -> None:
        """Start a channel WITHOUT making the next gesture wait for it (OPEN-WORK rank 172).

        ``master.play`` waits up to ten seconds for somebody else's server, and it is written so
        that several presses may be inside it at once: it bumps a generation, takes its own lock
        AFTER that wait, and a call that finds a newer one gives up and stops the source it made.
        Its docstring says it outright - "no press is made to wait ten seconds on another". Awaiting
        it from the dialling loop puts exactly that wait back in front of every other deadline the
        loop holds, which is what made a key held in the flat on 2026-09-20 act 4.67 s late.

        The task is kept in a set while it runs, because asyncio holds only a weak reference to a
        task and one nobody else holds can be collected mid-flight. Anything it raises is said in
        one line here rather than left to the loop's default handler, which reports it at whatever
        unrelated moment it happens to notice.
        """
        task = asyncio.create_task(self._start_the_channel(master, channel))
        self._starting.add(task)
        task.add_done_callback(self._starting.discard)
        task.add_done_callback(self._say_if_starting_a_channel_failed)

    def _say_if_starting_a_channel_failed(self, task: asyncio.Task[None]) -> None:
        """One line for a channel start that raised. A cancel is how a stop ends it, not news."""
        if task.cancelled():
            return
        failure = task.exception()
        if failure is not None:
            self.log(ERROR_KIND, f"starting a channel: {type(failure).__name__}: {failure}")

    async def _stop_starting_channels(self) -> None:
        """End any channel start still in flight, so a stop does not wait out a dead station."""
        starting = tuple(self._starting)
        for task in starting:
            task.cancel()
        await asyncio.gather(*starting, return_exceptions=True)

    async def _start_the_channel(self, master: ZoneMasterPort, channel: Channel) -> None:
        """Put one channel on the zone: whatever produces its sound first, then the stream itself.

        The ONE place a channel is started, and that is a rule rather than a tidy-up. A channel
        reaches the zone two ways - the pass starts one when it takes a box in, and a dialled
        number starts one from its own line - so a kind that needs something done first, as an MPD
        channel does, would otherwise have to be remembered in both. Forgetting one of them is
        silent: the feature works when a box is switched on and not when somebody presses the
        number, which is the half a person uses. Here, a third caller cannot forget what it does
        not have to remember.
        """
        await self._remember_where_mpd_is()
        if channel.kind is ChannelKind.MPD:
            await self._put_mpd_on(channel)
        station = await master.play(
            StationRequest(
                playback_url=channel.url,
                name=channel.name,
                content_item_xml=zonexml.station_content_item(url=channel.url, name=channel.name),
            )
        )
        if station is not None:
            # Only the call that WON writes it, which is why the answer is read rather than
            # discarded. Two starts can be inside ``play`` at once and the slow one routinely
            # finishes last; it comes back None, and a line that wrote the number regardless would
            # leave the house recorded as being on the channel that lost.
            self._on_air = channel.number

    def _the_mpd_connection(self) -> MpdControlPort:
        """The one control connection, opened the first time anything here needs it.

        Every caller comes through this, and that is a rule rather than a tidy-up. A connection
        thrown away after a failure and a connection never opened are the same ``None``, so a
        caller that reads the field for itself has to guess which of the two it is looking at.
        On 2026-09-20 in the flat one of them guessed wrong: after MPD closed an idle connection,
        every file step answered "nothing has reached mpd yet" and did nothing, for a channel MPD
        was holding at the time, until somebody dialled a channel and the other path reopened.

        Whether MPD has been GIVEN a channel is a different question with its own field,
        :attr:`_mpd_channel`, and that is the one a caller should be asking.

        Still lazy: a house with nothing but radio channels calls this never and opens nothing.
        """
        if self._mpd is None:
            self._mpd = self.ports.open_mpd(self.options.mpd_host, self.options.mpd_port, self.log)
        return self._mpd

    async def _put_mpd_on(self, channel: Channel) -> None:
        """Ask MPD for this channel's playlist BEFORE the master is pointed at the stream.

        The order is measured rather than tidy: MPD's ``httpd`` port does not listen at all until
        its output first opens, so a master pointed at the stream first is refused and backs off -
        the wait doubling to 30 s - and the flat is silent for as long as that lasts.

        **Nothing here raises.** A playlist MPD does not have is somebody's typo in the channel
        file, and a daemon that will not answer is a machine that needs attention; neither is worth
        the zone, which is holding every other room on every other channel. So it is said by name,
        once, and the stream is started anyway: that channel is silent with its name on the
        display, and the radio channels keep working. A connection that failed is thrown away
        rather than kept, because it would go on failing and every later channel change would
        report a fault that was over.
        """
        try:
            async with asyncio.timeout(MPD_TIMEOUT_S):
                await self._load(self._the_mpd_connection(), channel)
        except NotInMpdError:
            self.log("mpd", f"channel {channel.number} names {_what_it_plays(channel)}, which mpd does not have")
        except (MpdError, OSError) as exc:
            await self._mpd_would_not(f"channel {channel.number}", exc)
        else:
            # Only once MPD has taken it: this is what the position is written against later, and
            # a channel recorded here that MPD never loaded would put one book's place on another.
            self._mpd_channel = channel.number

    async def _load(self, mpd: MpdControlPort, channel: Channel) -> None:
        """A stored playlist is one ``load``; a directory is listed, put in OUR order, and queued.

        The directory's order is the domain's (OPEN-WORK rank 11, decision C), which is why the
        files are listed and added one by one instead of MPD being handed the directory. Its place
        is found by the file's NAME in the files as they are now, because a file added since the
        house left would otherwise move the index onto another chapter.
        """
        if not channel.mpd_directory:
            await mpd.play_entry(channel.mpd_entry, place=self._where_to_come_back_to(channel.number), end=channel.end)
            return
        files = play_order(await mpd.files_under(channel.mpd_directory))
        if not files:
            self.log("mpd", f"channel {channel.number}: {_what_it_plays(channel)} holds no files mpd can play")
        place = self._where_to_come_back_to(channel.number, files=files)
        await mpd.play_files(files, place=place, end=channel.end)

    def _where_to_come_back_to(self, number: str, *, files: Sequence[str] | None = None) -> Place | None:
        """The remembered place, stepped back by the overlap, or nothing for a channel never left.

        With ``files``, the place is first found in them by name (``Place.found_in``): a directory
        channel's files can have changed since, a stored playlist's are not read to save a round
        trip, and its index is what it has always been trusted by.

        The overlap is applied HERE and not where the place is written down, so that what is
        recorded stays where the house actually stopped: the setting can then be changed, or set
        to zero, without anything already written meaning something else.
        """
        place = self._positions.get(number)
        if place is not None and files is not None:
            place = place.found_in(files)
        return place.resumed(rewind_s=self.options.mpd_rewind_s) if place is not None else None

    async def _step_inside_the_channel(self, channel: Channel, steps: int, *, by_directory: bool = False) -> None:
        """Move by whole files INSIDE an MPD channel, which is what next and previous mean there.

        Called with the pass's lock held, exactly like :meth:`_put_mpd_on`, because both speak on
        the one connection this service keeps and two coroutines writing to it would interleave
        their commands into something MPD reads as neither.

        The entry is worked out here and NAMED (``play N``), never left to MPD's ``next`` and
        ``previous``: those stop at the end of the queue when ``repeat`` is off, and a press past
        either end wraps on every channel (user, 2026-09-24) - a ``stop`` channel included, because
        its ``stop`` is for the natural end, when nobody is there. Three taps are one jump of three,
        so it is one ``status`` and one ``play`` under one bound, whatever the count.

        A step is not a channel change, so nothing is started here and the stream is not touched -
        the same httpd URL carries whatever MPD plays next, and no room pays the silence a station
        change costs. Nothing raises, for the reason :meth:`_put_mpd_on` says.

        ``by_directory`` is a HELD key (OPEN-WORK rank 11, decisions D, G, H): the first file of the
        next directory, or of this one when previous is held from inside it. It reads the queue's
        paths, so it works on a stored playlist the same as on a directory channel.
        """
        if self._mpd_channel is None:
            self.log("mpd", f"channel {channel.number}: nothing has reached mpd yet, so there is no file to step")
            return
        try:
            async with asyncio.timeout(MPD_TIMEOUT_S):
                mpd = self._the_mpd_connection()
                status = await mpd.status()
                if by_directory:
                    target = directory_jump(await mpd.queue_files(), current=status.song, steps=steps)
                else:
                    target = entry_after(current=status.song, length=status.playlist_length, steps=steps)
                if target is None:
                    self.log("mpd", f"channel {channel.number}: mpd holds no queue, so there is no file to step to")
                    return
                await mpd.play_at(target)
        except (MpdError, OSError) as exc:
            await self._mpd_would_not(f"channel {channel.number}", exc)

    async def _remember_where_mpd_is(self) -> None:
        """Write down how far into its channel MPD has got, before the house leaves it.

        Called wherever a channel is about to be replaced and on the way out, and it is idempotent:
        it takes the channel MPD was holding and leaves none behind, so a second call with nothing
        in between writes nothing. That is what lets the stand-down call it on every pass with the
        switch off without a file write each time.

        **An absent ``elapsed`` is not a position of zero, and an absent ``song`` is not the first
        track.** A stopped MPD carries neither key at all (measured 2026-09-10), and recording
        either as zero would put the start of the book over a real place in it with nothing to
        report - the channel would simply begin again next time, which is the one failure this
        feature can cause. So a status missing either half writes nothing: half a place is not a
        place, and the one that is already written down is better than half a new one.

        **The one exception is a ``stop`` channel that ran out** (user, 2026-09-24): stopped with
        its queue still loaded, which a real MPD 0.24.6 was measured to leave at the natural end.
        The place written down before that is somewhere near the end, and coming back to it would
        play a few seconds and fall silent again, so it is forgotten and the book starts over. A
        ``wrap`` channel never runs out, so the same status there is something else and a real
        place is kept.
        """
        number, self._mpd_channel = self._mpd_channel, None
        if number is None:
            return
        try:
            async with asyncio.timeout(MPD_TIMEOUT_S):
                status = await self._the_mpd_connection().status()
        except (MpdError, OSError) as exc:
            await self._mpd_would_not(f"channel {number}", exc)
            return
        if status.ran_out() and self._stops_at_its_end(number):
            self._forget_where_it_was(number)
            return
        if status.elapsed is None or status.song is None:
            return
        file = await self._the_file_at(number, status.song)
        self._positions[number] = Place(track=status.song, seconds=status.elapsed, file=file)
        self._save_the_state()

    async def _the_file_at(self, number: str, track: int) -> str | None:
        """The path of that queue entry, for a directory channel, whose order can change under it.

        Nothing for a stored playlist, whose place is trusted by its index and costs no extra
        exchange; and nothing when MPD will not say, because a place with its index is still better
        than none - it is written down anyway rather than lost with the name.
        """
        channel = self._channels.by_number(number)
        if channel is None or not channel.mpd_directory:
            return None
        try:
            async with asyncio.timeout(MPD_TIMEOUT_S):
                queue = await self._the_mpd_connection().queue_files()
        except (MpdError, OSError) as exc:
            await self._mpd_would_not(f"channel {number}", exc)
            return None
        return queue[track] if 0 <= track < len(queue) else None

    def _stops_at_its_end(self, number: str) -> bool:
        """Whether that channel falls silent at its natural end rather than starting again."""
        channel = self._channels.by_number(number)
        return channel is not None and channel.end is ChannelEnd.STOP

    def _forget_where_it_was(self, number: str) -> None:
        """Drop a channel's place, so it starts from the beginning the next time it is dialled."""
        if self._positions.pop(number, None) is None:
            return
        self.log("mpd", f"channel {number} ran out, so it starts from the beginning next time")
        self._save_the_state()

    async def _mpd_would_not(self, what: str, exc: Exception) -> None:
        """Say what MPD did not do, and throw the connection away so the next attempt is fresh.

        One place, because everything this service asks of MPD fails the same way and for the same
        reasons, and a policy written twice is a policy that will be changed once. TimeoutError is
        an OSError, so the bound on each call arrives here with everything else.
        """
        where = f"{self.options.mpd_host}:{self.options.mpd_port}"
        self.log("mpd", f"{what}: mpd at {where} did not take it ({type(exc).__name__}: {exc})")
        await self._forget_the_mpd_connection()

    async def _forget_the_mpd_connection(self) -> None:
        """Drop a connection that failed, so the next channel change opens a fresh one.

        Closing can fail on a socket that is already gone, and this runs inside the handling of
        the failure that brought us here - so a raise would replace a channel nobody can hear with
        a service nobody can use.
        """
        mpd, self._mpd = self._mpd, None
        if mpd is None:  # pragma: no cover - only reachable if something cleared it in between
            return
        with contextlib.suppress(Exception):
            await mpd.close()

    async def _stand_down(self) -> None:
        """Let the house alone: dissolve the zone, stop the stream, and give up the ports.

        Idempotent, and the same thing whether the switch went off or the service is ending. From
        a speaker's side those are one event: the zone is over, and it is on its own again.
        """
        await self._remember_where_mpd_is()
        await self._stop_fading()
        await self._stop_house_writes()
        master, self.master = self.master, None
        self._joined.clear()
        if master is None:
            return
        self.log("zone", "standing down: dissolving the zone")
        await master.dissolve()
        await master.stop()


def _what_it_plays(channel: Channel) -> str:
    """The playlist or the directory an MPD channel names, the way a log line says it."""
    if channel.mpd_directory:
        return f"directory {channel.mpd_directory!r}"
    return f"playlist {channel.mpd_entry!r}"
