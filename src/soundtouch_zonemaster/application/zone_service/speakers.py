"""Who the speakers are, and the one observer each of them gets.

Third in the chain. Everything here answers "which boxes exist and where", which is a different
question from "which of them belong in the zone" - that one is the membership rule's, and it is
asked one class further up.

A speaker already known is never forgotten here. AfterTouch's discovery cycle is what makes the
device list fresh, not ours, so a device missing from a later read is a gap in ITS view rather
than evidence a box has gone; membership has its own reachability timeout and that is the one
allowed to drop a speaker.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import replace
from typing import TYPE_CHECKING

from ..errors import RegistryError
from .channels import ChannelBook

if TYPE_CHECKING:
    from ...domain.events import SpeakerEvent
    from ...domain.speakers import Speaker

__all__ = ["SpeakerBook"]


class SpeakerBook(ChannelBook):
    """The device list, the observers over it, and the names everything else logs by."""

    async def _poll_the_registry(self) -> None:
        """Ask who the speakers are again, for ever: a new box must not need a restart.

        Every poll period, or at once when a console has just been allowed. A box listed for the
        first time is asked what it is playing here, as every box is at start, so that its first
        wake reads as one.
        """
        while True:
            await self._wait_for_the_next_registry_read()
            await self._read_the_registry()
            await self._seed_the_channels()
            await self._ask_the_speakers_what_they_are_playing()
            self._wanted.set()

    async def _wait_for_the_next_registry_read(self) -> None:
        """Sleep out the poll period, unless a read is asked for sooner.

        ``asyncio.wait`` rather than ``wait_for``: it never raises on its timeout, so there is no
        TimeoutError handler here to swallow a cancel that races the deadline (the trap the
        dialling worker's loop describes), and the waiter is cancelled on the way out either way.
        """
        asked = asyncio.ensure_future(self._registry_wanted.wait())
        try:
            await asyncio.wait({asked}, timeout=self.options.registry_poll_s)
        finally:
            asked.cancel()

    def _read_the_registry_now(self) -> None:
        """Have the registry read at once rather than at the next poll: a console has just been allowed.

        The registry read is where a console is let into the speaker book and watched, and a box
        new to the book is asked what it is playing right after it, which is what places it -
        asleep, so its next frame is a wake, or already on the house's stream, so it belongs at
        once. A console the book already holds (allowed earlier in the run, taken off, allowed
        again) needs neither: it has been watched all along, and what it said while it was off the
        list was recorded like anything else it says - which is why a take asks for this only for a
        console the book does not hold yet.
        """
        self._registry_wanted.set()

    def _heard_from(self, address: str) -> None:
        """A slave reported on its transport channel, so it is there. Liveness, never membership."""
        for device_id, joined_at in self._joined.items():
            if joined_at == address:
                self.policy.seen(device_id, at=time.time())
                return

    async def _read_the_registry(self) -> None:
        """Learn who the speakers are. A speaker already known is never forgotten here.

        AfterTouch's discovery cycle is what makes that list fresh, not ours, so a device missing
        from a later read is a gap in ITS view rather than evidence that a box has gone. Membership
        has its own reachability timeout, and that is the one allowed to drop a speaker.
        """
        # Cleared BEFORE the read, so a console allowed while it is in flight asks for another one
        # rather than being answered by a read that may have missed it.
        self._registry_wanted.clear()
        skipped: list[str] = []
        try:
            speakers = await self.ports.fetch_speakers(
                self.options.registry_url, log=lambda _kind, text: skipped.append(text)
            )
        except RegistryError as exc:
            self.log("registry", f"{exc}; keeping the {len(self._speakers)} speaker(s) already known")
            return
        self._note_the_skipped(skipped)
        for speaker in speakers:
            if speaker.is_console and speaker.device_id not in self._preferences.consoles_allowed:
                continue
            self._remember(speaker)

    def _note_the_skipped(self, skipped: list[str]) -> None:
        """Say which device-list entries were skipped when that changes, and never in between.

        A placeholder AfterTouch writes for a half-discovered device stays for as long as that
        device is on the network, so the same skip used to be logged on every poll - measured
        about 120 lines an hour. The whole set is repeated when it changes, so one line block
        always describes the list as it is now.
        """
        now = frozenset(skipped)
        if now == self._last_reported.skipped_in_registry:
            return
        self._last_reported.skipped_in_registry = now
        if not now:
            self.log("registry", "the device list has no unusable entry any more")
        for text in skipped:
            self.log("registry", text)

    def _remember(self, speaker: Speaker) -> None:
        """Take one entry of the device list, and start watching a box that is new to us."""
        known = self._speakers.get(speaker.device_id)
        self._speakers[speaker.device_id] = speaker
        if known is None:
            self.log("registry", f"{speaker.name} ({speaker.device_id}) at {speaker.ip}")
            self._watch(speaker.device_id)
            self._not_asked_yet.add(speaker.device_id)
        elif known.ip != speaker.ip:
            self.log("registry", f"{speaker.name} is at {speaker.ip} now")

    def _watch(self, device_id: str) -> None:
        """One observer per speaker, each holding its own channel and reconnecting on its own."""
        observer = self.ports.watch_speaker(
            device_id,
            address_of=self._address_of,
            events=self.events,
            log=self.log,
            policy=self.options.channel_policy,
        )
        self._observers[device_id] = asyncio.create_task(observer.run())

    async def _address_of(self, device_id: str) -> str | None:
        """Where a box is right now, or ``None`` while the registry does not list it."""
        speaker = self._speakers.get(device_id)
        return speaker.ip if speaker is not None else None

    async def _stop_watching(self) -> None:
        for task in self._observers.values():
            task.cancel()
        if self._observers:
            await asyncio.gather(*self._observers.values(), return_exceptions=True)
        self._observers.clear()

    async def _ask_the_speakers_what_they_are_playing(self) -> None:
        """Ask every box not asked yet, once, so that a wake later reads as a wake.

        Everything else the service knows arrives in a frame a box CHOSE to send. One that was
        already in standby has sent none, so without this its next frame - somebody switching it
        on - says "playing its own radio", which on its own is a reason to stay out. Asked once,
        they are all placed, and a box that does not answer keeps whatever is remembered about it.

        At start that is every box. After it, it is a box the registry has just listed for the
        first time, or a console just allowed: unasked, either would be watched with nothing known
        about it, and taken in only on its SECOND wake.

        All at once, and each answer is taken the moment it arrives: a box slow to answer - up to
        the HTTP timeout - must not hold back what another box has already said.
        """
        # In the order the registry listed them, which is the order the start has always asked in.
        asked = [speaker for device_id, speaker in self._speakers.items() if device_id in self._not_asked_yet]
        self._not_asked_yet.clear()
        await asyncio.gather(*(self._ask_what_it_is_playing(speaker) for speaker in asked))

    async def _ask_what_it_is_playing(self, speaker: Speaker) -> None:
        """Ask one box, and take its answer unless the box has said something newer itself since.

        An answer is what the box said when the question ARRIVED, and it can be seconds old by the
        time it is read. A frame the box sent in between is newer, and applied after it the stale
        answer is worse than none: a box somebody switched off while its answer was on the way
        reads as standby-then-radio, which is a wake, and the house would switch it back on.
        """
        asked_at = time.time()
        event = await self.ports.ask_now_playing(speaker.ip, speaker.device_id)
        if event is None:
            self.log("probe", f"{speaker.name} did not answer; what is remembered about it stands")
            return
        if self._source_named_at.get(speaker.device_id, -math.inf) >= asked_at:
            self.log(
                "probe",
                f"{speaker.name} answered {event.source}, but it has named a source itself since it was "
                "asked; what it said last stands",
            )
            return
        self.log("probe", f"{speaker.name}: {event.source}")
        self._noted_switched_on(event)
        self.policy.observe(event)
        # A pass now rather than once every box asked with it has answered: an answer can put a
        # box in the zone (it plays the house's stream), and the pass is what acts on that.
        self._wanted.set()

    def _a_frame_named_a_source(self, event: SpeakerEvent) -> None:
        """Note when a box last said what it is playing in a frame of its own: newer than any answer asked before it."""
        if event.device_id and event.source is not None:
            self._source_named_at[event.device_id] = event.received_at

    def _named(self, event: SpeakerEvent) -> SpeakerEvent:
        """A forwarded key press names its speaker by ADDRESS; the registry turns that into an id.

        The body carries no device id at all, so the HTTP face cannot fill one in, and an event
        without one tells the policy nothing - not even that the box that sent it is alive.
        """
        if event.device_id:
            return event
        for speaker in self._speakers.values():
            if speaker.ip == event.speaker:
                return replace(event, device_id=speaker.device_id)
        return event
