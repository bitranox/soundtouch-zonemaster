"""The service itself: what has to be true before the first event, and the workers after it.

The top of the chain, and the smallest class in it. Holding a house is five workers over the seven
classes below, so what is left here is starting them and the order of the start-up - which is not
arbitrary at any step: the state comes back before any volume is put back and the registry before
it again, because a level is remembered against a device id and only the registry says where that
is; the channel file is read before the registry so an unusable one stops the service here rather
than once a speaker wakes; and the first pass runs only after every box has been asked what it is
playing.

The stand-down is in a ``finally``. Leaving a real speaker in a zone whose master has gone is the
one outcome that needs a person to undo it by hand, which is why the unit ends this with SIGINT.
"""

from __future__ import annotations

import asyncio
import time

from .keys import KeyReading

__all__ = ["ZoneService"]


class ZoneService(KeyReading):
    """The service: one zone, the speakers that belong in it, and the switch above both."""

    async def run(self) -> None:
        """Hold the house until cancelled; the stand-down is in a ``finally``, as in the prototype.

        Leaving a real speaker in a zone whose master has gone is the one outcome that needs a
        person to undo it by hand. That is why the unit ends this with SIGINT rather than SIGTERM:
        the default handling of a TERM does not run this block.
        """
        workers: list[asyncio.Task[None]] = []
        try:
            # Inside the try, because starting up is where the zone is FIRST taken: a stop that
            # lands in here - systemd stopping a service it has just started, or the first read
            # of a slow registry - would otherwise leave speakers in a zone and the ports bound.
            await self._start_up()
            workers = [
                asyncio.create_task(self._watch_the_switch()),
                asyncio.create_task(self._poll_the_registry()),
                asyncio.create_task(self._read_what_the_speakers_say()),
                asyncio.create_task(self._reconcile_when_asked()),
                asyncio.create_task(self._complete_dialled_numbers()),
            ]
            await asyncio.gather(*workers)
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            # Before the stand-down, which stops the master: a start still inside its wait would
            # otherwise be holding a source that the dissolve is about to take the zone out from
            # under, and it would go on fetching a station nobody is listening to.
            await self._stop_starting_channels()
            await self._stop_watching()
            await self._stand_down()

    async def _start_up(self) -> None:
        """Everything that has to be true before the first event is read."""
        state = self.ports.load_state(self.options.state_file, log=self.log)
        self._channel = state.channel
        self._believed = state.members
        self._muted = dict(state.muted)
        self._owed_volume = dict(state.owed_volume)
        self._out_of_multiroom = set(state.out_of_multiroom)
        # Where each MPD channel had got to. Nothing is said about it here: it is read the
        # moment somebody dials one of those channels, and a line per start about numbers
        # nobody has asked for would push the two that DO need saying off a short screen.
        self._positions = dict(state.positions)
        if state.out_of_multiroom:
            # Said out loud at every start. A box that is out of multiroom looks exactly like one
            # the zone cannot reach, and the difference is a decision somebody made days ago.
            self.log("zone", f"out of multiroom, from the last run: {self._names(state.out_of_multiroom)}")
        if state.dial_window_s is not None:
            # A calibration was measured on a person in this house, so it outranks the option,
            # which is a number somebody typed for a house rather than for anybody in it.
            self._calibrated_window_s = state.dial_window_s
            self._the_window_is_now(state.dial_window_s)
            self.log("dial", f"the dialling window is {state.dial_window_s:.1f} s, calibrated in an earlier run")
        if state.hold_threshold_s is not None:
            # Measured by the same calibration on the same presses, and restored on its own: a
            # house calibrated before the threshold was a number of its own has a window and no hold.
            self._calibrated_hold_s = state.hold_threshold_s
            self._the_hold_is_now(state.hold_threshold_s)
            self.log("dial", f"a key is held after {state.hold_threshold_s:.1f} s, calibrated in an earlier run")
        self.policy.restore(state.members, at=time.time())
        self.log("state", f"{len(state.members)} member(s) remembered from the last run")
        # Before anything reads the registry: an unusable channel file must stop the service here,
        # not once a speaker wakes and there is nothing to play.
        self._channels = self.ports.load_channels(self.options.channel_file, log=self.log)
        await self._read_the_registry()
        # AFTER the registry, not before it: the state file remembers a device id and a level, and
        # the address to send that level to is what the registry answers. Called any earlier it
        # walks the same notes and skips every one of them for want of a speaker, while reading
        # like the thing that undoes a service killed mid-join. It has to be here rather than left
        # to the pass, because the pass gives up above its own call to this when the switch is off
        # or the ports are busy - so with the house switched off a box would stay at zero.
        await self._put_back_any_volume_we_took_away()
        await self._seed_the_channels()
        await self._ask_the_speakers_what_they_are_playing()
        self._on = self.switch.is_on()
        await self._reconcile()
