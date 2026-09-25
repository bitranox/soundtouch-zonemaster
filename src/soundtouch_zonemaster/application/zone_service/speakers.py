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
        """Ask who the speakers are again, for ever: a new box must not need a restart."""
        while True:
            await asyncio.sleep(self.options.registry_poll_s)
            await self._read_the_registry()
            await self._seed_the_channels()
            self._wanted.set()

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
            if speaker.is_console and speaker.device_id not in self.options.consoles_allowed:
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
        """Ask every box once, at start, so that a wake later reads as a wake.

        Everything else the service knows arrives in a frame a box CHOSE to send. One that was
        already in standby has sent none, so without this its next frame - somebody switching it
        on - says "playing its own radio", which on its own is a reason to stay out. Asked once,
        they are all placed, and a box that does not answer keeps whatever is remembered about it.
        """
        asked = list(self._speakers.values())
        answers: list[SpeakerEvent | None] = list(
            await asyncio.gather(*(self.ports.ask_now_playing(one.ip, one.device_id) for one in asked))
        )
        for speaker, event in zip(asked, answers, strict=True):
            if event is None:
                self.log("probe", f"{speaker.name} did not answer; what is remembered about it stands")
                continue
            self.log("probe", f"{speaker.name}: {event.source}")
            self._noted_switched_on(event)
            self.policy.observe(event)

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
