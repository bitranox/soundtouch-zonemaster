"""Numbers, steps and the calibration: what a completed press sequence does to the house.

Sixth in the chain. It sits above the zone because a completed number changes what the whole zone
is playing, and below the key reading because reading a key must never wait for a speaker: what is
fast (writing down what was pressed) happens there, and what is slow (talking to boxes) happens
here, on a worker of its own.

That worker sleeps to the earliest pending deadline and re-reads it after every wake, because a
digit arriving during the sleep MOVES it - which is what makes four digits 0.9 s apart one number
instead of four.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from ...domain.enums import ChannelKind, KeyName
from .constants import DIAL_TICK_S, MIN_CHANNELS_TO_STEP
from .zone import ZoneReconcile

if TYPE_CHECKING:
    from ...domain.channellist import Channel
    from ...domain.dialling import DigitIgnored
    from ...domain.longpress import Hold
    from ...domain.presses import Press
    from ..ports import ZoneMasterPort

__all__ = ["Dialling"]


STEP_OF: dict[str, int] = {KeyName.NEXT_TRACK: 1, KeyName.PREV_TRACK: -1}
"""Which key is a step and in which direction. A key absent from this is not a step."""

ROTATION_OF: dict[str, bool] = {KeyName.THUMBS_UP: True, KeyName.THUMBS_DOWN: False}
"""Which thumb puts the playing channel into the rotation and which takes it out.

The user's decision of 2026-09-07 over two other shapes for the thumbs. The design had them set
and clear a favourite mark that nothing anywhere ever read; this reads it on every step. Since
2026-09-25 it takes a HOLD, because one stray touch changed the rotation for the whole house and
nothing audible said so. The same map says which way a double tap moves a box: up is in, down out.
"""


class Dialling(ZoneReconcile):
    """What a press, a dialled number, a step and a calibration do once the pressing stops.

    What a press MEANS lives here rather than with the frames that carry it, because this is
    the class that drains the pairing - on the worker that can act at a moment no frame arrived
    at, which is where a selection nobody confirmed is finally dropped. The reader above writes
    each frame down and asks for the same drain, so that an ordinary press is still decided the
    instant its confirmation lands rather than at the next tick.
    """

    async def _complete_dialled_numbers(self) -> None:
        """Sleep to the earliest pending number's deadline, then read whatever is complete.

        It sleeps rather than polls, and it re-reads the deadline after every wake, because a digit
        arriving during that sleep MOVES it: that is what makes four digits 0.9 s apart one number
        instead of four. Reading events stays where it is - the lesson from M1 is that the task
        which waits must not be the task which reads.
        """
        while True:
            await self._tell_the_house_a_calibration_began()
            deadline = self._earliest_deadline()
            if deadline is None:
                # Nothing is being dialled: sleep until a digit lands, which costs nothing.
                await self._dialled.wait()
                self._dialled.clear()
                continue
            waiting = deadline - time.time()
            if waiting > 0:
                # A short slice rather than one sleep to the deadline, because a digit arriving
                # during it MOVES that deadline and the loop has to see the new one. The slice
                # only runs while somebody is mid-number, so it is a fraction of a second after a
                # keypress and never while the house is idle.
                #
                # Deliberately NOT asyncio.wait_for: it turns a cancellation that races its own
                # deadline into TimeoutError, so a handler catching TimeoutError swallows the
                # cancel and the task outlives its loop. Measured here on 2026-09-07 - the suite
                # hung in pytest-asyncio's loop finalizer, waiting for exactly that task.
                await asyncio.sleep(min(waiting, DIAL_TICK_S))
                continue
            now = time.time()
            # Before everything else it feeds. A selection whose confirmation never came is dropped
            # here, and a press whose confirmation came late becomes a digit here - so draining it
            # after the dialler would let the number complete without that digit, which is the
            # whole defect this was built for.
            self._read_what_the_presses_now_say(at=now)
            await self._read_the_calibration(at=now)
            # Before the steps, because a hold drops what the same gesture collected on its way
            # down: drained the other way round, the tap would act and the hold act again.
            for device_id, hold in self._longpresses.due(at=now):
                await self._a_held_key(device_id, hold)
            for device_id, number in self._dialler.due(at=now, still_pressing=self._presses.pending):
                await self._dialled_number(device_id, number)
            for device_id, steps in self._dialler.steps_due(at=now, still_stepping=self._stepping):
                await self._stepped(device_id, steps)

    def _read_what_the_presses_now_say(self, *, at: float) -> None:
        """Act on every press the rule can now report, in the order the keys went down."""
        for device_id, press in self._presses.due(at=at):
            self._a_press(device_id, press)

    def _a_press(self, device_id: str, press: Press) -> None:
        """One press, decided: a digit for the house, or a box asking to be let into the zone."""
        if press.asked.asleep and device_id in self._out_of_multiroom:
            self._switched_back_on(device_id)
        if not press.asked.may_choose_the_channel:
            # Asleep, by construction: that is the only answer that gets here. The press is what
            # makes it a member, rather than the nowPlayingUpdated it sends when it has started
            # its OWN station - which is 0.17 to 4.45 s later and is the sound the person did not
            # want (measured 2026-09-08, three wakes). A pass is asked for because nothing else
            # will: the box has said nothing a pass reacts to.
            self.policy.woke(device_id, at=press.pressed_at)
            self.log("dial", f"{self._name(device_id)} woke on {press.preset_id}: joining the zone")
            self._wanted.set()
            return
        ignored = self._a_digit(
            device_id, str(press.preset_id), pressed_at=press.pressed_at, released_at=press.released_at
        )
        if ignored is None or not press.asked.asleep:
            return
        # The same press, down the other branch: a box switched on resumes whatever it had last,
        # and that need not be one of its six presets - it reports such a selection as preset 0,
        # measured in the flat 2026-09-20 at 08:10 where the box had the zone's own stream from
        # before the master let it go. The digit is unusable, so no number will ever complete to
        # mark the wake, and without this the strongest evidence there is - somebody pressed a
        # key - is thrown away and the box waits for a source frame that may never come.
        self.policy.woke(device_id, at=press.pressed_at)
        self.log("dial", f"{self._name(device_id)} woke on no preset of its own: joining the zone")
        self._wanted.set()

    def _switched_back_on(self, device_id: str) -> None:
        """A box that is out of multiroom was switched on: it is back in (user, 2026-09-24 14:20).

        The way back cannot be a key. A box that is really released reports its keys only as
        anonymous touches (measured 2026-09-24), so it can never say that a thumb was held - but a
        wake it can say, and somebody switching a box on wants to hear the house. Only the flag is
        cleared here; the press itself then goes on to be the wake it is.

        Our own ``/select`` wakes a released box too, with frames of exactly this shape. It never
        gets here, because each one is claimed as our echo before it can become a press
        (``_select_on_its_own``).
        """
        self._out_of_multiroom.discard(device_id)
        self._save_the_state()
        self.log("zone", f"{self._name(device_id)} was switched on: back into multiroom")

    def _may_choose_the_channel(self, device_id: str) -> bool:
        """Whether a press from this box may set the channel for the whole house.

        A box that is WAKING chooses only while nothing is playing (user, 2026-09-07). Waking and
        pressing a preset are the same thing on the wire - both are a selection out of standby
        with a user activity behind it - so one rule has to serve both, and the two halves of it
        are the two rooms it happens in. In a quiet flat the box somebody switched on is the only
        box saying anything, and what it names is what the house should play. In a flat already
        playing, the design says a waking box joins the running channel (Phase 1, Membership), and
        a POWER-on in Room6 must not move three other rooms to whatever Room6 last had.

        The cost is named rather than hidden: a preset pressed at a sleeping box while the house
        plays joins the zone instead of choosing, and the same press once the box is awake dials.
        """
        if not self.policy.is_asleep(device_id):
            return True
        master = self.master
        return master is None or master.station is None

    def _a_digit(self, device_id: str, digit: str, *, pressed_at: float, released_at: float) -> DigitIgnored | None:
        """One preset press: a calibration sample while one is running, a dialled digit otherwise.

        A press from a box that is not the one being calibrated is dropped rather than dialled. A
        channel change in the middle of a measurement would be confusing in both rooms, and the
        measurement is over in seconds.

        It answers what the dialler could not use, because that is the one case where nothing else
        will speak for the press: a number completing is what marks a waking box as a member, and
        a digit no preset key can press completes nothing. A calibration sample answers nothing
        either way - the person is standing at the box being measured, and a press there is not a
        box asking to be taken into the zone.

        **The two moments are not interchangeable, and both are real.** The window for the next
        key is armed at the RELEASE of this one (user, 2026-09-21), because a window armed at the
        press charges a person for however long their thumb rests on the key: a measured 0.645 s
        between two presses in the flat is only about 0.31 s of idle time, and a 0.6 s window split
        the number anyway. The calibration is handed both, because what it measures is that same
        idle time - the gap from one key coming up to the next going down.
        """
        if self._calibration.is_running():
            if not self._calibration.press(device_id, pressed_at=pressed_at, released_at=released_at):
                self.log("dial", f"{self._name(device_id)} pressed {digit} while another box is calibrating")
            self._dialled.set()
            return None
        # A digit is not a step key, so it breaks a half-finished gesture: the four presses have
        # to be consecutive or a person who dials mid-thought loses the next key they press.
        self._gesture.forget(device_id)
        ignored = self._dialler.digit(device_id, digit, at=released_at)
        if ignored is None:
            # One line per DIGIT and not only per completed number, because the number is the one
            # thing that cannot say which half went wrong. Measured in the flat 2026-09-21: the
            # house read the four-digit 1111 out of a burst the master had only three forwarded
            # selects for, and nothing recorded whether a press had reached the dialler twice or a
            # forward had been dropped - the only line either way was the number it ended in.
            self.log("dial", f"{self._name(device_id)}: pressed {digit}")
        else:
            # The dialler said this itself until narration came out of ``domain``. The text is
            # its own, word for word, and so is the moment it is written: before the worker is
            # woken, which is where the old call sat.
            self.log("dial", ignored.said)
        self._dialled.set()
        return ignored

    def _earliest_deadline(self) -> float | None:
        """The nearest of the four things this loop waits for, or nothing while it waits for none.

        Two of them are not waiting for another frame to arrive, which is why they are deadlines at
        all. A key still DOWN is one: a box sends nothing while a key is held, so the moment a
        press becomes a hold is a moment nothing announces. An unconfirmed SELECTION is the other:
        our own voice coming back is a frame the box never follows with anything, and without a
        deadline it would sit in the table until that box next spoke - two and a half minutes, in
        the first live run.
        """
        pending = [
            when
            for when in (
                self._dialler.deadline(still_pressing=self._presses.pending, still_stepping=self._stepping),
                self._calibration.deadline(),
                self._longpresses.deadline(),
                self._presses.deadline(),
            )
            if when is not None
        ]
        return min(pending) if pending else None

    def _stepping(self, device_id: str) -> bool:
        """Whether a step key on that box is still down and may yet become a hold."""
        return any(self._longpresses.undecided(device_id, key) for key in STEP_OF)

    async def _tell_the_house_a_calibration_began(self) -> None:
        """Say it the only way a flat with no screen can hear: the playing channel starts again.

        The break in the music IS the message - there is nothing else to say it with, and it needs
        no machinery of its own, being the call a channel change already makes.
        """
        if not self._calibration.needs_announcing():
            return
        self._calibration.announced()
        async with self._lock:
            master = self.master
            if master is not None:
                await self._play_the_channel(master)

    async def _read_the_calibration(self, *, at: float) -> None:
        """End a calibration whose quiet is up, and take the number if it produced one.

        The channel starts again whichever way it went, because the person is standing there
        waiting to hear that it is over, and a refusal they cannot hear is a refusal they will
        answer by pressing more.
        """
        deadline = self._calibration.deadline()
        if deadline is None or deadline > at:
            return
        result = self._calibration.finish(at=at)
        self.log("dial", f"calibration: {result.said}")
        if result.window_s is not None:
            self._the_window_is_now(result.window_s)
            self._calibrated_window_s = result.window_s
        if result.hold_s is not None:
            self._the_hold_is_now(result.hold_s)
            self._calibrated_hold_s = result.hold_s
        if result.window_s is not None or result.hold_s is not None:
            self._save_the_state()
        async with self._lock:
            master = self.master
            if master is not None:
                await self._play_the_channel(master)

    async def _dialled_number(self, device_id: str, number: str) -> None:
        """One completed number: the channel for the WHOLE zone, or nothing at all.

        A number that is not in the list does nothing and says so, which is the design's rule and
        what makes a mistyped sequence harmless - wait a second, nothing happened, start again.
        Without a display that is the friendliest error handling available.
        """
        channel = self._channels.by_number(number)
        if channel is None:
            self.log("dial", f"{self._name(device_id)} dialled {number}: no such channel, doing nothing")
            return
        self.log("dial", f"{self._name(device_id)} dialled {number}: {channel.name}")
        master = await self._book_the_dialled_number(device_id, channel, number)
        if master is not None:
            # OUTSIDE the lock, and dispatched rather than awaited. Holding the pass lock across
            # a station start makes press N wait for press N-1, which is the one thing
            # ``ZoneMaster.play`` was written not to do; awaiting it here makes the dialling loop
            # blind to every other deadline while it fetches. Both were measured in the flat on
            # 2026-09-20 as a key acting 4.67 s after its window (OPEN-WORK rank 172).
            self._start_the_channel_soon(master, channel)

    async def _book_the_dialled_number(self, device_id: str, channel: Channel, number: str) -> ZoneMasterPort | None:
        """Everything a completed number changes here, under the lock and without waiting on anyone.

        Returns the master when the channel still has to be STARTED, and None when it does not -
        the box is out of multiroom and dials only for itself, nobody is holding the house, the
        zone is empty, or the zone is already on that channel. Split out so that the lock covers
        the bookkeeping and nothing else: the start that follows waits on somebody else's server.
        """
        async with self._lock:
            if device_id in self._out_of_multiroom:
                await self._dialled_on_its_own(device_id, channel)
                return None
            self._channel = number
            self._save_the_state()
            self._take_in_the_box_that_dialled(device_id)
            # Every completed number asks for a pass, whether or not it starts anything here. A
            # zone that is still empty is left alone on purpose one branch down, and _take_in also
            # declines to start a station while a number is open - so without this the box that
            # dialled would wait for some unrelated event to bring a pass around.
            self._wanted.set()
            master = self.master
            if master is None:
                # Nobody is holding the house, so there is nothing to switch; the number is
                # remembered and the switch coming on will start it.
                return None
            if not master.slaves:
                # The same reason one line up, for the case that actually happens: a box WAKING
                # dials before any pass has taken it in, so the zone is still empty. Starting the
                # station here breaks the rule _play_the_channel states - a zone nobody is in must
                # not pull a radio station - and the pass then stops it again as "nothing left in
                # the zone". Measured in the flat 2026-09-08 02:09: one channel fetched three
                # times in 3.7 s when two boxes were switched on three seconds apart. The churn is
                # audible through _may_choose_the_channel, which lets a waking box pick the
                # channel only while master.station is None: every stop reopened that door, so the
                # second box's wake was read as a channel change too, and the box already in the
                # zone was stopped on one stream and re-buffered onto the next. The number is
                # remembered above, and _take_in starts it once, when there is somebody to hear it.
                return None
            if master.station is not None and self._on_air == number:
                # Dialling the channel that is already playing is not a change. Without this, an
                # awake box pressing the preset it is already listening to restarts the stream and
                # stumbles every room in the house.
                #
                # By NUMBER, and that is the whole rule rather than a tidier spelling of the same
                # one. This compared the station's URL against the channel's until 2026-09-21, and
                # every MPD channel names the same ``httpd`` output - so it read a move from one
                # audiobook to the next as the same channel pressed twice and did nothing, while
                # still writing the new number into the state file. Eleven dials in the flat that
                # night: every MPD-to-MPD one refused, and the only way through was a radio channel
                # in between.
                self.log("dial", f"{self._name(device_id)} dialled {number}: already playing it")
                return None
            return master

    def _take_in_the_box_that_dialled(self, device_id: str) -> None:
        """A box that dialled belongs in the zone from now, awake or asleep.

        It is deliberately HERE rather than at the press, and that half is unchanged: the channel
        is only known once the number is complete. Marking at the press instead would let a pass
        take the box in first and start the house on the REMEMBERED channel, and the dial
        completing a moment later would move every box to the dialled one, which costs each of
        them a stop and a re-buffer of 3.5 to 4 s (``master.slave_left``, measured 2026-09-08).
        The window this waits out is the dialling window, at most two seconds.

        **An AWAKE box is taken in too** (user, 2026-09-20; this used to return early on one that
        was not asleep). The dial reaches us from the box's own ``nowSelectionUpdated``, so by the
        time we act it is ALREADY playing that station on its own. The choice was therefore never
        between playing and staying silent - it was between playing in step with the house and
        playing alone, and alone is what a listener hears as an echo from the next room. Measured
        in the flat that morning: an awake box dialled, every OTHER room moved to its station, and
        the room whose button had been pressed stayed outside on its own copy.

        This does not reopen the churn that the empty-zone rule in :meth:`_dialled_number` guards
        (one station fetched three times in 3.7 s). The mark happens BEFORE the pass, so the pass
        takes the box in and starts the station once, instead of a station being started into a
        zone nobody is in and stopped again.
        """
        self.policy.woke(device_id, at=time.time())

    async def _a_held_key(self, device_id: str, hold: Hold) -> None:
        """A key still down when the hold threshold passed, acted on at that moment.

        Said out loud first, because a held key and a tapped one produce the same line further
        down and a live run has to be able to tell which gesture the house read.

        A held STEP key on an MPD channel moves by DIRECTORY rather than by file (OPEN-WORK rank
        11, decisions D, G, H); on a station there are no directories, and it does what a tap does
        (decision F), because a held key doing nothing would read as a remote that missed the
        press. What it must not do is act TWICE -
        the press collected a step on its way down - so what was collected is dropped and this one
        action replaces it. It cannot have acted already: a step waits while its key is undecided
        (``_stepping``), because the dialling window can close before the threshold does. The
        gesture run is broken for the same reason: four alternating presses are a calibration only
        while all four are taps.
        """
        self.log("dial", f"{self._name(device_id)} held {hold.key} for {hold.seconds:.1f} s")
        if hold.key in ROTATION_OF:
            # A held thumb is the rotation (user, 2026-09-25): a hold cannot happen by brushing a
            # key, which is what took channels out by accident when a single tap did this.
            self._thumbed(device_id, in_rotation=ROTATION_OF[hold.key])
            return
        if hold.key not in STEP_OF:
            return
        self._dialler.forget_steps(device_id)
        self._gesture.forget(device_id)
        await self._stepped(device_id, STEP_OF[hold.key], held=True)

    def _thumb_tapped(self, device_id: str, key: str, *, pressed_at: float, released_at: float) -> None:
        """A thumb tapped once hands this box the house volume; tapped twice, it moves the box in or out.

        The user's split of 2026-09-25, made so that one stray touch can no longer change the
        rotation or the group. Either thumb serves for the volume: the remote cannot send two keys
        at once (OPEN-WORK rank 191), so the thumb comes first and the volume keys follow it.

        The single tap opens the window at once rather than once the double tap has been ruled
        out, because the first volume report arrives 0.22 to 0.55 s after the thumb comes up -
        inside any dialling window. A second tap therefore closes it again before it acts.
        """
        name, verb = self._name(device_id), "up" if ROTATION_OF[key] else "down"
        if self._dialler.thumb(device_id, key, pressed_at=pressed_at, released_at=released_at) == 1:
            self.log("dial", f"{name} thumbed {verb} once")
            if device_id in self._joined:
                self._house_volume.open(device_id, at=released_at)
                self.log("volume", f"{name}: the volume keys set the whole house now")
            return
        self._house_volume.close(device_id)
        self._in_or_out(device_id, key, said=f"{name} thumbed {verb} twice")

    def _in_or_out(self, device_id: str, key: str, *, said: str) -> None:
        """A double-tapped thumb switches THIS box into or out of multiroom.

        The user's gesture of 2026-09-08 was a hold; since 2026-09-25 it is a double tap, and the
        hold is the rotation. Up is in and down is out, which is the rotation's meaning of the same
        two keys carried over: a thumbs down is always less of this, a thumbs up more of it. At a
        box already in, a double thumbs up is said and left alone.

        Only the flag is set here. It is read by ``_who_belongs`` on the next pass, which is the one
        place membership is decided and the only place allowed to talk to a speaker - so a box on
        its way out is let go there, and handed the channel it was hearing, rather than from the
        reader.
        """
        wants_in = ROTATION_OF[key]
        was_out = device_id in self._out_of_multiroom
        if wants_in != was_out:
            self.log("zone", f"{said}: it is already {'in' if wants_in else 'out of'} multiroom")
            return
        if wants_in:
            self._out_of_multiroom.discard(device_id)
            self.log("zone", f"{said}: back into multiroom")
        else:
            self._out_of_multiroom.add(device_id)
            self.log("zone", f"{said}: out of multiroom, on its own from here")
        self._save_the_state()
        self._wanted.set()

    async def _stepped(self, device_id: str, steps: int, *, held: bool = False) -> None:
        """Next or previous, by however many presses were collected into one jump.

        It steps the ROTATION in dialling order and wraps, so a listener can never get stuck at an
        end and a channel a thumbs down took out is passed over.

        **The button stays in the world it was pressed in**, and there are two of them now. On a
        channel that is a stored MPD playlist the key means the next FILE, which is what somebody
        listening to an audiobook means by it and the only reading that makes the key usable
        there; on every other kind it means the next channel. The branch is deliberately the
        FIRST thing here, because everything below it is about the rotation and none of it applies.
        """
        # From what is PLAYING rather than from what was dialled: nothing has been dialled on a
        # fresh start, and the zone is on the lowest channel there is rather than on nothing.
        playing = self._the_channel_to_play()
        if playing is not None and playing.kind is ChannelKind.MPD:
            await self._step_inside(device_id, playing, steps, by_directory=held)
            return
        rotation = self._channels.rotation_numbers()
        moved = self._channels.step(playing.number if playing is not None else None, steps)
        if moved is None or len(rotation) < MIN_CHANNELS_TO_STEP:
            self.log("dial", f"{self._name(device_id)} stepped {steps:+d}: there is nowhere to step to")
            return
        was = playing.number if playing is not None else "nothing"
        self.log("dial", f"{self._name(device_id)} stepped {steps:+d}: {was} to {moved}")
        await self._dialled_number(device_id, moved)

    async def _step_inside(self, device_id: str, channel: Channel, steps: int, *, by_directory: bool) -> None:
        """Page through the files of the MPD channel the zone is on, without changing the channel.

        Under the pass's lock, which is where every other speaker-facing thing this class does
        ends up: the same one connection carries the channel change, so two of these running at
        once would interleave their commands on it.

        Nothing about the ZONE changes - no station, no document, no speaker is sent anything -
        so this costs the rooms nothing at all. That is the whole reason the key can mean this
        here: a channel step costs every room 3.5 to 4 s of silence, and a file step costs none.
        """
        unit = "directory" if by_directory else "file"
        self.log("dial", f"{self._name(device_id)} stepped {steps:+d} {unit} inside {channel.number}: {channel.name}")
        async with self._lock:
            await self._step_inside_the_channel(channel, steps, by_directory=by_directory)

    async def _dialled_on_its_own(self, device_id: str, channel: Channel) -> None:
        """A box that is out of multiroom dials for itself and for nobody else.

        This is what switching one out is for: the house keeps playing what it was playing, and the
        room that stepped out still reaches every channel in the list - including the ones past the
        six a box can hold in its own presets, which is the whole reason the list is not six long.
        It needs no zone and no master stream, because the channel's url is the local service's own
        playback address.

        The house's remembered channel is deliberately NOT written. That number says what the ZONE
        is on, and a box that is not in the zone must not move it - otherwise a room on its own
        would decide what every other room hears at the next restart.
        """
        speaker = self._speakers.get(device_id)
        if speaker is None:
            return
        try:
            await self._select_on_its_own(device_id, speaker.ip, channel)
        except Exception as exc:  # noqa: BLE001 - the next press is the retry, so this must not raise
            self.log("dial", f"{speaker.name}: {channel.name} could not be started on it ({type(exc).__name__})")
            return
        self.log("dial", f"{speaker.name} is out of multiroom; playing {channel.name} on its own")
