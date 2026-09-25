"""Turning a box down while it is taken in, and making sure it always comes back up.

Fourth in the chain. A box that has just been switched on plays its OWN last station while it
waits to be taken into the zone - measured 2026-09-08, 4.7 s of it, and the user heard it - so it
is turned down to zero first, joined, and faded back to the level it was on once it has let go.

The one failure this feature can cause is a speaker left silently at zero, which reads as broken
hardware rather than as a service that stopped halfway. That is why the level is written to the
state file BEFORE the box is muted, why every path out of a join puts it back, why a cancelled
fade still puts it back in one step, and why a level that could not be read means NOTHING WAS
DONE rather than a guess.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

from ...domain.housevolume import owe, stepped
from .constants import FADE_S, FADE_STEPS, MUTE_HOLD_S
from .speakers import SpeakerBook

if TYPE_CHECKING:
    from ...domain.speakers import Speaker

__all__ = ["VolumeGuard"]

OWN_TOUCH_TAIL_S = 0.5
"""How long after one of our volume writes returns its touch may still arrive.

Measured 2026-09-21 in ``burst-as-a-slave-frames.json``: each write came back as a ``volumeUpdated``
and a ``userActivityUpdate``, the touch landing up to 0.25 s on EITHER side of its volume frame, so
the tail is twice that. It only bounds how long a write waits for an echo that never comes; a write
whose echo arrived stops claiming touches at once.
"""


class VolumeGuard(SpeakerBook):
    """Every volume this service takes away, and the several ways it gives them back."""

    async def _write_volume(self, speaker: Speaker, level: int) -> None:
        """Every volume this service sets, marked so the box's answer is not read as a person.

        A real box answers each write with a ``userActivityUpdate``, the frame that tells a press
        from our own station change coming back (``domain/presses.py``). Measured 2026-09-21: one of
        the touches a join produced confirmed a selection nobody made, and the house dialled it. So
        each write is owed exactly one touch, from the moment it is sent until ``OWN_TOUCH_TAIL_S``
        after it returns, and ``_claimed_as_our_echo`` hands it over when it arrives.
        """
        owed = self._owed.owe_touch(speaker.device_id)
        try:
            await self.ports.set_volume(speaker.ip, level)
        finally:
            owed.until = time.time() + OWN_TOUCH_TAIL_S

    def _claimed_as_our_echo(self, device_id: str, *, at: float) -> bool:
        """Whether a touch this box reported at ``at`` is the echo a command of ours is owed.

        A volume write is owed one, and so is a ``/select`` (``_select_on_its_own``).

        ONE touch per write and no more, which is what keeps a person pressing in the middle of a
        fade from being swallowed with it. A time window alone did swallow them: a box that has
        just been taken in is the box somebody just switched on, and that person may well press
        again at once. A human touch that lands while a write is still owed its echo is taken in
        the echo's place, and the echo behind it then counts as the human one - the NUMBER of
        touches a person made comes through either way, which is what a press is read from.
        """
        return self._owed.claim_touch(device_id, at=at)

    async def _turn_it_down(self, speaker: Speaker) -> int:
        """Take one box to zero, and answer the level it was on so it can be put back.

        ``-1`` is the answer that means NOTHING WAS DONE, and every caller has to treat it that
        way. It covers three cases that are one decision: the box did not answer, it answered
        without a level, or it was already at zero. A volume that could not be read is a volume
        that could not be put back, and this must never leave a speaker somewhere it did not
        choose to be - so it declines to mute rather than risk that.

        The level is written to the state file BEFORE the box is muted. A service that dies in
        between then still finds it on the next start; one that wrote afterwards would not.
        """
        try:
            level = await self.ports.read_volume(speaker.ip)
        except Exception as exc:  # noqa: BLE001 - joining at its own volume is the safe failure
            self.log("zone", f"{speaker.name}: volume not read ({type(exc).__name__}), joining as it is")
            return -1
        if level < 0:
            return -1
        owed = self._owed_volume.pop(speaker.device_id, 0)
        target = self._with_what_it_missed(speaker, level, owed)
        if target <= 0:
            await self._join_silent(speaker, level, owed)
            return -1
        self._muted[speaker.device_id] = target
        self._save_the_state()
        try:
            await self._write_volume(speaker, 0)
        except Exception as exc:  # noqa: BLE001 - same failure, and the note must come back out
            self._muted.pop(speaker.device_id, None)
            self._owe_again(speaker, owed)
            self.log("zone", f"{speaker.name}: could not be turned down ({type(exc).__name__}), joining as it is")
            return -1
        return target

    def _with_what_it_missed(self, speaker: Speaker, level: int, owed: int) -> int:
        """The level a joining box is put back to: its own, moved by the house steps it missed.

        Taken at the join because the join already turns the box down and fades it back up, so the
        steps land without a jump the room can hear and without a write to a box that was asleep.
        """
        if not owed:
            return level
        target = stepped(level, owed)
        self.log("zone", f"{speaker.name} missed {owed:+d} of house volume while it was off: {level} to {target}")
        return target

    async def _join_silent(self, speaker: Speaker, level: int, owed: int) -> None:
        """A box whose level, with what it missed, is nothing: it joins at zero and is not faded.

        The house was turned right down while it was off, so zero is where the house is. A box
        already at zero is left alone, as it always was.
        """
        if level <= 0:
            if owed:
                self._save_the_state()
            return
        try:
            await self._write_volume(speaker, 0)
        except Exception as exc:  # noqa: BLE001 - it joins at its own level, and still owes the steps
            self._owe_again(speaker, owed)
            self.log("zone", f"{speaker.name}: could not be turned down ({type(exc).__name__}), joining as it is")
            return
        self._save_the_state()

    def _owe_again(self, speaker: Speaker, owed: int) -> None:
        """Put back steps a join took and then could not apply, so the next join takes them."""
        if owed:
            self._owed_volume = owe(self._owed_volume, [speaker.device_id], owed)
        self._save_the_state()

    async def _turn_it_back_up(self, speaker: Speaker, level: int, *, fade: bool) -> None:
        """Put one box back where it was: at once, or in steps on a task of its own.

        ``fade`` is false on the path where the box did NOT join. It is still playing its own
        station, so every moment at zero is a moment somebody is missing, and it is put back here
        and now rather than in steps.

        The fading path does NOT run here. It waits out the moment a box needs to let go of its own
        station and then climbs, which together is over two seconds - and this is called from
        inside the pass, which holds the lock that everything else in the service waits on. Doing
        it inline made a join stall every other thing the house wanted; the suite noticed before the
        house did, going from 55 to 158 seconds.
        """
        if level <= 0:
            return
        if not fade:
            await self._put_one_back(speaker, level)
            return
        self._fading[speaker.device_id] = asyncio.create_task(
            self._fade_one_back_up(speaker, level), name=f"fade {speaker.device_id}"
        )

    async def _fade_one_back_up(self, speaker: Speaker, level: int) -> None:
        """Wait out the box's own station, then climb from zero to where it was.

        The ``except`` is the whole safety of this: cancelled - by a stand-down, by the service
        ending - it still puts the box back in one step, because a speaker left at zero reads as
        broken hardware and nobody would connect it to a zone that was dissolved. The last step is
        INSIDE it too, so a cancel that lands on that step is answered the same way rather than
        leaving the box wherever the climb had got to.

        That last step is also inside the ``try``, which is what keeps the entry in ``_fading``
        until it has returned. Popped first, the pass would see a mute with no fade running and
        rescue it - and the level it writes is the one this was about to write, so the room sounds
        right and the only trace is ``_put_back_any_volume_we_took_away`` saying a join did not
        finish. That line is the single signal a service ever died mid-join, and the eighth
        listening run of 2026-09-08 spent it on a run where nothing had.

        A box that stops answering mid-climb ends the climb and drops to the single step below,
        which is the module's rule for every other call to a speaker: log it and let the next pass
        be the retry. Unguarded, that raised out of a task nothing ever retrieves - the ``finally``
        has already taken it out of ``_fading``, so not even ``_stop_fading`` waits on it - and
        asyncio printed a traceback of its own to a logger that is not this service's, which is
        exactly where somebody looking for a real fault starts reading.
        """
        try:
            await asyncio.sleep(MUTE_HOLD_S)
            for step in range(1, FADE_STEPS):
                # The target is read from the note at every step rather than held from the start:
                # a house-volume step that lands mid-fade moves the note, and the climb then ends
                # where the house now is instead of where it was when the box was switched on.
                target = self._muted.get(speaker.device_id, level)
                try:
                    await self._write_volume(speaker, round(target * step / FADE_STEPS))
                except Exception as exc:  # noqa: BLE001 - the step below is the retry, so this must not raise
                    self.log("zone", f"{speaker.name}: fade stopped at step {step} ({type(exc).__name__})")
                    break
                await asyncio.sleep(FADE_S / FADE_STEPS)
            await self._put_one_back(speaker, level)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self._put_one_back(speaker, level)
            raise
        finally:
            self._fading.pop(speaker.device_id, None)

    async def _put_one_back(self, speaker: Speaker, level: int) -> None:
        """One box to one level, and the note removed once it is there.

        A failure leaves the note in ``self._muted`` on purpose rather than logging and forgetting.
        The next pass finds it and tries again, which is the only thing standing between a box that
        stopped answering mid-fade and a speaker that stays silent until somebody notices.

        "A later pass will" is a promise the CALLER keeps: it holds only while the note is still
        there for a later pass to find. A fade that let go of its ``_fading`` entry before calling
        this would let the pass rescue the box and take the note out first, and then the line would
        be naming nobody.

        The note is what is put back when there is one, and ``level`` only when there is none: a
        house-volume step may have moved the note since the caller read it.
        """
        level = self._muted.get(speaker.device_id, level)
        try:
            await self._write_volume(speaker, level)
        except Exception as exc:  # noqa: BLE001 - the next pass is the retry, so this must not raise
            self.log("zone", f"{speaker.name}: volume not put back ({type(exc).__name__}); a later pass will")
            return
        self._muted.pop(speaker.device_id, None)
        self._save_the_state()

    def _house_stepped(self, source_id: str, step: int) -> None:
        """Move every other box by the step a person just made at ``source_id`` (rank 191).

        Every box the registry lists is one of three things, and each takes the step its own way:

        - a box with a volume NOTE is being joined, faded in, or waiting to be put back, so the note
          moves and whatever puts it back puts back the moved level;
        - a box in the zone is written, by one task per box so the steps of a held key queue up;
        - any other box is off or elsewhere, and is OWED the step until it next joins.

        A box out of multiroom takes nothing: it left the house on purpose. The box the person
        turned is never written - its own hand already moved it. Nothing here talks to a speaker,
        because it runs on the reader, which must never wait.
        """
        noted: list[str] = []
        written: list[str] = []
        owed: list[str] = []
        for device_id in self._speakers:
            if device_id == source_id or device_id in self._out_of_multiroom:
                continue
            if device_id in self._muted:
                self._muted[device_id] = stepped(self._muted[device_id], step)
                noted.append(device_id)
            elif device_id in self._joined:
                self._house_steps[device_id] = self._house_steps.get(device_id, 0) + step
                self._write_the_house_steps(device_id)
                written.append(device_id)
            else:
                owed.append(device_id)
        self._owed_volume = owe(self._owed_volume, owed, step)
        if noted or owed:
            self._save_the_state()
        self.log(
            "volume",
            f"house {step:+d} from {self._name(source_id)}: {self._names(written) or 'nobody'} now"
            + (f", {self._names(noted)} as it comes in" if noted else "")
            + (f", {self._names(owed)} when it joins" if owed else ""),
        )

    def _write_the_house_steps(self, device_id: str) -> None:
        """Start the one writer for this box, unless it is already running and will take the step."""
        running = self._house_writers.get(device_id)
        if running is not None and not running.done():
            return
        self._house_writers[device_id] = asyncio.create_task(
            self._house_writes(device_id), name=f"house volume {device_id}"
        )

    async def _house_writes(self, device_id: str) -> None:
        """Write this box's queued steps, each from where the last one left it.

        The first step starts from the level the box last reported, or from a read when it has
        reported none; every step after that starts from what THIS task wrote, because a report of
        our own write can still be on its way while the next step is taken. A box that does not
        answer drops what it had queued and says so: the next turn of the knob is the retry.
        """
        written: int | None = None
        try:
            while step := self._house_steps.pop(device_id, 0):
                speaker = self._speakers.get(device_id)
                if speaker is None:
                    return
                base = written if written is not None else self._house_volume.level_of(device_id)
                if base is None:
                    base = await self.ports.read_volume(speaker.ip)
                if base < 0:
                    self.log("volume", f"{speaker.name}: volume not read, left where it is")
                    return
                written = stepped(base, step)
                await self._write_volume(speaker, written)
        except Exception as exc:  # noqa: BLE001 - the next turn of the knob is the retry
            self._house_steps.pop(device_id, None)
            self.log("volume", f"{self._name(device_id)}: house volume not set ({type(exc).__name__})")
        finally:
            self._house_writers.pop(device_id, None)

    async def _stop_house_writes(self) -> None:
        """End every house-volume write in flight; what is left unwritten is simply not written."""
        writers = list(self._house_writers.values())
        for task in writers:
            task.cancel()
        if writers:
            await asyncio.wait(writers)
        self._house_steps.clear()

    async def _stop_fading(self) -> None:
        """End every fade in flight, each putting its box back on the way out.

        Waited on with ``asyncio.wait`` rather than awaited directly: awaiting a task that has just
        been cancelled re-raises its CancelledError here, and this runs on the path that stands the
        house down, where swallowing that would be worse than the fade it is ending.
        """
        fading = list(self._fading.values())
        for task in fading:
            task.cancel()
        if fading:
            await asyncio.wait(fading)

    async def _put_back_any_volume_we_took_away(self) -> None:
        """Turn up every box this service muted and did not turn up again.

        Two callers, and they are the two ways the mute can outlive its join: start-up, where the
        map came out of the state file and the service may have died mid-join, and the pass, where
        a box stopped answering while it was being faded. In the pass it is the FIRST thing done,
        which is what keeps it below nothing: two paths there give the whole pass up, the switch
        off and a listening port held by somebody else, and under either of them a note nobody
        cleared meant a speaker at zero until the house came back on. Being first is also what
        keeps it from fighting a mute THIS pass is about to make.

        A fade left running by an EARLIER pass is the other half, and the ordering says nothing
        about that one: what covers it is the ``_fading`` guard below, which lets a box alone while
        its fade is still live. The two halves only meet because the fade holds its entry until its
        last step has returned - without that there is a moment where the box has a note and no
        fade, and rescuing it there reports a join that did not finish about one that did.

        It needs no zone and no master, which is why it is here and why the volume calls are module
        functions: a stood-down service still has to be able to give a speaker its volume back.
        """
        for device_id, level in list(self._muted.items()):
            speaker = self._speakers.get(device_id)
            fading = self._fading.get(device_id)
            if speaker is None or (fading is not None and not fading.done()):
                continue
            try:
                await self._write_volume(speaker, level)
            except Exception as exc:  # noqa: BLE001 - the next pass tries again
                self.log("zone", f"{speaker.name}: volume still not put back ({type(exc).__name__})")
                continue
            self.log("zone", f"{speaker.name}: volume put back to {level} after a join that did not finish")
            self._muted.pop(device_id, None)
            self._save_the_state()
