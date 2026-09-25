"""The house's channels: seeding the list, choosing from it, and editing its rotation.

Second in the chain, above the bare state and below everything that talks to a speaker. It owns
the list itself and the three questions asked of it - which box seeds it, which channel the zone
is on, and whether a channel is in the rotation the step keys walk.

Narration that used to live in ``domain`` is written here. ``seed_from_presets`` returns the lines
it would have said, in order, and :meth:`ChannelBook._say` writes them under the kind the old code
used, so a log from this service reads exactly as it did.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...domain.channellist import PresetStation, SeedingError, seed_from_presets
from ...domain.enums import SourceName
from .constants import PRESET_KEYS
from .state import ServiceState

if TYPE_CHECKING:
    from ...domain.channellist import Channel
    from ...domain.events import SpeakerEvent
    from ...domain.speakers import Speaker

__all__ = ["ChannelBook"]


class ChannelBook(ServiceState):
    """The channel list this house dials, and everything that decides what is in it."""

    def _thumbed(self, device_id: str, *, in_rotation: bool) -> None:
        """A thumb press: the playing channel leaves the rotation, or comes back into it.

        The last channel in the rotation is refused rather than taken out. An empty rotation leaves
        next and previous dead from every channel, which reads as the buttons being broken rather
        than as something somebody did - and the way back would be an editor.
        """
        name, verb = self._name(device_id), "up" if in_rotation else "down"
        # The channel the zone is ON, which on a fresh start is the lowest one rather than the
        # nothing that was dialled. A thumb is about what a person is hearing.
        channel = self._the_channel_to_play()
        if channel is None:
            self.log("dial", f"{name} thumbed {verb}: nothing is playing that is in the list")
            return
        if channel.in_rotation == in_rotation:
            self.log("dial", f"{name} thumbed {verb} {channel.number}: it already sits where that puts it")
            return
        if not in_rotation and self._channels.rotation_numbers() == (channel.number,):
            self.log("dial", f"{name} thumbed down {channel.number}: refused, it is the last channel there is")
            return
        self._channels = self._channels.with_rotation(channel.number, in_rotation=in_rotation)
        self.ports.save_channels(self.options.channel_file, self._channels)
        where = "back in the rotation" if in_rotation else "out of the rotation"
        self.log("dial", f"{name} thumbed {verb} {channel.number}: {where}")

    async def _seed_the_channels(self) -> None:
        """Take places 1 to 6 off the first box that was switched on, while there is no list.

        The user's rule of 2026-09-07, replacing a box named in the unit file. Every box in this
        house carries the same presets, so naming one decided nothing about the CONTENT of the
        list - only which box had to be reachable at the first start, which is a fact about the
        radio rather than about the house.

        A box is asked once and never again: one with nothing on its keys leaves the chance to the
        next box switched on, rather than consuming it or being asked on every frame it sends.

        Serialised on its own lock. Reading a box's presets is six sequential HTTP GETs, each with
        an eight-second timeout, and this is awaited from two tasks that do not wait for each other
        - the pass and the thirty-second registry poll. Without the lock a poll tick landing inside
        one of those fetches saw an empty list, picked the NEXT box, and whichever of the two boxes
        answered LAST wrote its own list over the other's, in memory and on disk, with no log line
        anywhere saying two had been asked. It also decided the list by which radio replied fastest
        rather than by which box was switched on first, which is the rule this function states.
        """
        if self._channels.channels:
            return
        async with self._seeding:
            # Re-read INSIDE the lock: the caller that held it may have just filled the list, and
            # the guard above was answered before the wait.
            if self._channels.channels:
                return
            speaker = self._first_unasked_speaker()
            if speaker is None:
                return
            self._asked_for_presets.add(speaker.device_id)
            try:
                report = seed_from_presets(await self._presets_of(speaker))
            except SeedingError as refused:
                # The lines it said while walking the presets were LOGGED by the old code before
                # it raised. They are written here and the refusal goes on, because dropping them
                # would lose lines from exactly the failure somebody is trying to read.
                self._say(refused.said)
                raise
            self._say(report.said)
            seeded = report.seeded
            if not seeded.channels:
                self.log("channels", f"{speaker.name} has no presets; the next box switched on gets the chance")
                return
            self._channels = seeded
            self.ports.save_channels(self.options.channel_file, seeded)

    def _first_unasked_speaker(self) -> Speaker | None:
        """The earliest box seen out of standby that has not been asked for its presets yet."""
        for device_id in self._switched_on:
            if device_id in self._asked_for_presets:
                continue
            speaker = self._speakers.get(device_id)
            if speaker is not None:
                return speaker
        return None

    def _noted_switched_on(self, event: SpeakerEvent) -> None:
        """Remember, in order, which boxes have been seen out of standby.

        Fed from BOTH ways the service learns a source: the frames a box sends, and the one probe
        at start that asks every box what it is playing. A box that was already on when the
        service started is switched on as far as the house is concerned, and it is the one a
        person is standing at.
        """
        if event.source is None or event.source == SourceName.STANDBY or not event.device_id:
            return
        if event.device_id not in self._switched_on:
            self._switched_on.append(event.device_id)

    async def _presets_of(self, speaker: Speaker) -> dict[int, PresetStation | None]:
        """The six preset keys of one box, with ``None`` where a key is not set.

        The conversion happens here because ``channellist`` is not allowed to know what HTTP is,
        and the service is the one place allowed to import both sides of it.
        """
        presets: dict[int, PresetStation | None] = {}
        for number in range(1, PRESET_KEYS + 1):
            try:
                request = await self.ports.read_preset(speaker.ip, number)
            except Exception as exc:  # noqa: BLE001 - a key that is not set and a box that did not answer read alike here
                self.log("channels", f"{speaker.name} preset {number}: {type(exc).__name__}: {exc}")
                presets[number] = None
                continue
            presets[number] = PresetStation(name=request.name, url=request.playback_url)
        return presets

    def _the_channel_to_play(self) -> Channel | None:
        """The channel the zone is on: what was dialled, or the lowest there is.

        A remembered number that is no longer in the list is what happens when somebody edits the
        file and deletes what was playing. Falling back to the lowest keeps the house audible and
        says which number went missing, rather than leaving a zone playing nothing.
        """
        if self._channel is not None:
            wanted = self._channels.by_number(self._channel)
            if wanted is not None:
                return wanted
            self.log("channels", f"channel {self._channel} is not in the list any more; taking the lowest instead")
        numbers = self._channels.numbers_in_order()
        return self._channels.by_number(numbers[0]) if numbers else None

    def _say(self, lines: tuple[str, ...]) -> None:
        """Write out what the seeding would have said, in the order it would have said it.

        ``domain`` narrates nothing since the rebuild, so ``seed_from_presets`` hands back the
        lines and this is the caller that writes them. The kind is ``channels``, which is the kind
        the old code used at exactly these points.
        """
        for line in lines:
            self.log("channels", line)
