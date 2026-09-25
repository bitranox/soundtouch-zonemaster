"""The channels the house can dial, and the numbers a person can actually press.

A channel number is a string over the six preset keys, so the alphabet is 1 to 6 and nothing else.
There is no 7, 8, 9 or 0 on the remote to press, which is why no number containing one is ever
handed out: a channel nobody can type would be worse than no channel, because the list would say
it is there. There is no upper bound on how many channels there may be.

Counting in that alphabet is bijective base 6 - 1 to 6, then 11 to 16, 21 to 26 and on to 66, then
111 upwards - and :func:`nth_number` and :func:`ladder_index` walk it in the two directions. They
are inverses, which is the property worth testing rather than the handful of values somebody
thought of.

**Repeated digits are dialable, and that is measured.** On 2026-09-07 a box announced a preset
press even when it selected what was already selected, twice over, so "1", "11" and "111" are three
different numbers a person can really type
(``docs/measurements/2026-09-07-first-live-run.md``, question 5). Had it gone the other way the
ladder here would have had to skip every number with two equal digits side by side, and the
thumbs-up rule would have had to hand out something other than 11 as its seventh number.

Nothing in this module reaches a speaker, a file or a clock. What a completed number DOES is the
service's, and where the list is stored is the channel file's.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .enums import ChannelEnd, ChannelKind

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "DIGITS",
    "Channel",
    "ChannelList",
    "ChannelNumberError",
    "PresetStation",
    "SeedingError",
    "SeedingReport",
    "a_number_a_key_can_press",
    "a_url_the_master_can_fetch",
    "dialable",
    "ladder_index",
    "nth_number",
    "seed_from_presets",
]

DIGITS = "123456"
"""The whole alphabet: one character per preset key, in the order the keys sit in."""


class ChannelNumberError(ValueError):
    """A number no preset key can press, or a place in the ladder that does not exist.

    A ``ValueError`` so that a pydantic validator raising it reports the way every other refused
    field does, rather than escaping as an error the model layer does not know about.
    """


def dialable(number: str) -> bool:
    """Whether a person could press this number on the six preset keys, and nothing else.

    The empty string is not a number: it is what a buffer holds before anybody has pressed
    anything, and treating it as dialable would make "no digits yet" look like a channel.
    """
    return bool(number) and all(character in DIGITS for character in number)


def a_number_a_key_can_press(number: str) -> str:
    """``number`` back, or the refusal a number no key can press earns.

    It returns the value rather than a bool so that the channel file's field validator can be the
    one line the pydantic validator it replaces was, and so that both callers - the record and the
    file - raise one refusal with one message.
    """
    if not dialable(number):
        raise ChannelNumberError(f"not a number the keys can press: {number!r}")
    return number


def a_url_the_master_can_fetch(url: str) -> str:
    """``url`` back, or the refusal anything the master cannot pull over HTTP earns.

    A bare path is the case worth refusing by name: it reads like something that could work, and
    the design keeps local files behind a media root that does not exist yet. That refusal used to
    sit on the service's ``--station-url``; it moved here with the channel list, because this is
    where the URL a person can type now lives.
    """
    if not url.startswith(("http://", "https://")):
        raise ChannelNumberError(f"a channel is fetched over http or https, not {url!r}")
    return url


def ladder_index(number: str) -> int:
    """Where a number sits in dialling order, counting from 1.

    Dialling order is the only order these numbers have. Sorted as text they read
    "1", "11", "111", "2", which puts channel 111 before channel 2 and is exactly the mistake a
    plain ``sorted()`` makes here.
    """
    if not dialable(number):
        raise ChannelNumberError(f"not a number the keys can press: {number!r}")
    index = 0
    for character in number:
        index = index * 6 + int(character)
    return index


def nth_number(n: int) -> str:
    """The nth number in dialling order. The inverse of :func:`ladder_index`.

    There is no zeroth number, because the ladder counts the numbers a person can press and the
    shortest of those is one digit long.
    """
    if n < 1:
        raise ChannelNumberError(f"there is no number {n} in the ladder; it counts from 1")
    digits: list[str] = []
    while n:
        # A remainder of 0 means the LAST digit of the alphabet rather than a carry, which is what
        # separates bijective counting from ordinary base 6 and is why there is no digit 0 here.
        remainder = n % 6 or 6
        digits.append(str(remainder))
        n = (n - remainder) // 6
    return "".join(reversed(digits))


def _plainly_under_the_music_directory(path: str) -> bool:
    """Relative, with no ``..`` and no empty segment, so it names one place inside MPD's tree.

    MPD would refuse the first two only when the number is DIALLED, which is the house silent
    with the reason in a log; an empty segment (``a//b``, a trailing ``/``) is a typo that MPD
    reads its own way. Refused here, all three stop the channel file from loading instead.
    """
    return not path.startswith("/") and all(segment not in {"", ".."} for segment in path.split("/"))


@dataclass(frozen=True, slots=True, kw_only=True)
class Channel:
    """One entry of the house's channel list: a number, a name, and where the sound comes from."""

    number: str
    """The digits pressed to reach it. Refused at construction if a key cannot produce them."""

    name: str
    kind: ChannelKind
    url: str
    mpd_entry: str = ""
    """The stored playlist MPD is asked to load, for a channel whose kind is ``MPD``.

    Empty for every other kind. An MPD channel names this OR :attr:`mpd_directory`, exactly one.
    They are checked TOGETHER with the kind in ``__post_init__`` because they are one fact: a
    channel that names nothing to play and a channel that names something nothing will read are
    dialled the same way and both end in silence, with no line saying why.

    A stored playlist and not a library directory, on purpose. MPD's ``load`` takes stored playlists
    only, so accepting a directory here would mean guessing between ``load`` and ``add`` from the
    name, and a name that is legal as both would be resolved silently. That is why a directory has
    its own field.
    """

    mpd_directory: str = ""
    """A directory under MPD's ``music_directory`` that an MPD channel plays, subdirectories included.

    Relative, the way :attr:`mpd_entry` is a name under ``playlist_directory``: MPD knows one tree,
    and whether it is filled by a bind mount, a network mount or a copy is nothing this service can
    see. The files play in :func:`~soundtouch_zonemaster.domain.playorder.play_order`, which is
    ours rather than MPD's database order (OPEN-WORK rank 11, user 2026-09-24).
    """

    end: ChannelEnd = ChannelEnd.WRAP
    """What an MPD channel does when it runs out by itself: start again, or stop and forget.

    ``WRAP`` when the file says nothing, because every MPD channel written before the field existed
    fell silent at its end, and that silence is what the field was added to end. ``STOP`` is
    refused on any other kind: a station has no end, and the word would mislead whoever reads the
    file next.
    """

    in_rotation: bool = True
    """Whether next and previous land on it. Thumbs down takes it out, thumbs up puts it back.

    A channel out of the rotation keeps its number and is still dialled by pressing it, which is
    what makes a stray thumbs down recoverable from the room rather than only in this file.

    It replaced a ``favourite`` flag that nothing ever read (user, 2026-09-07): a stored mark with
    no reader can be wrong for years without anything behaving differently.
    """

    def __post_init__(self) -> None:
        """Refuse a channel no key can reach, or one the master cannot fetch.

        Refused at construction rather than at the point of dialling, so an unreachable channel
        cannot be written into the file in the first place. A bare path is the case worth refusing
        by name: it reads like something that could work, and the design keeps local files behind a
        media root that does not exist yet. That refusal used to sit on the service's
        ``--station-url``; it moved here with the channel list, because this is where the URL a
        person can type now lives.

        The checks run in FIELD ORDER and the first failure raises, where the pydantic model this
        replaced collected both and reported a count. The file boundary is unaffected: the channel
        file keeps a pydantic model calling these same two functions once per field, so its
        ``unusable (N problem(s))`` counts are unchanged. The
        difference is visible only to a caller constructing a Channel with two bad fields at once,
        and ``tests/test_boundary_golden.py`` states it against the recorded old behaviour.
        """
        a_number_a_key_can_press(self.number)
        a_url_the_master_can_fetch(self.url)
        self._the_entry_belongs_to_an_mpd_channel_and_to_no_other()

    def _the_entry_belongs_to_an_mpd_channel_and_to_no_other(self) -> None:
        """Kind and playlist are one fact, so the mismatch is refused rather than represented.

        The second half is the one that would survive a review: an MPD entry on a RADIO channel
        plays perfectly well, because the url is the whole answer for that kind, and lies to the
        next reader about where the sound comes from.
        """
        if self.kind is ChannelKind.MPD:
            self._an_mpd_channel_names_one_thing_to_play()
            return
        if self.mpd_entry:
            raise ChannelNumberError(f"channel {self.number} is {self.kind} and carries an MPD playlist")
        if self.mpd_directory:
            raise ChannelNumberError(f"channel {self.number} is {self.kind} and carries an MPD directory")
        if self.end is ChannelEnd.STOP:
            raise ChannelNumberError(f"channel {self.number} is {self.kind} and has no end to stop at")

    def _an_mpd_channel_names_one_thing_to_play(self) -> None:
        """A playlist or a directory, never both: with both, which of them plays would be a guess."""
        if not self.mpd_entry and not self.mpd_directory:
            raise ChannelNumberError(f"channel {self.number} is an MPD channel and names no playlist and no directory")
        if self.mpd_entry and self.mpd_directory:
            raise ChannelNumberError(f"channel {self.number} names a playlist and a directory; it plays one of them")
        if self.mpd_directory and not _plainly_under_the_music_directory(self.mpd_directory):
            raise ChannelNumberError(
                f"channel {self.number}: {self.mpd_directory!r} is not a path under mpd's music directory"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelList:
    """Every channel the house has, and the questions asked of them.

    Frozen, and every change returns a new list: the service holds one of these while a reconcile
    may be running, and a list that changed underneath a pass would answer two different things to
    one decision.
    """

    channels: tuple[Channel, ...] = ()

    def by_number(self, number: str) -> Channel | None:
        """The channel that number reaches, or ``None``.

        ``None`` rather than a raise because the design's answer to an undefined number is that
        nothing happens at all: it is a person mistyping, not an error condition.
        """
        for channel in self.channels:
            if channel.number == number:
                return channel
        return None

    def numbers_in_order(self) -> tuple[str, ...]:
        """Every number, in dialling order, which is what next and previous step through."""
        return tuple(sorted((channel.number for channel in self.channels), key=ladder_index))

    def lowest_free_number(self) -> str:
        """The lowest number that can be DIALLED and is not taken.

        The user's rule, 2026-09-06: with 1 to 6 taken the next number handed out is 11 and never
        7. It walks the ladder rather than the counting numbers, so what comes back is always
        something somebody can press.
        """
        taken = {channel.number for channel in self.channels}
        n = 1
        while nth_number(n) in taken:
            n += 1
        return nth_number(n)

    def rotation_numbers(self) -> tuple[str, ...]:
        """The numbers next and previous walk: dialling order, minus what a thumbs down took out."""
        return tuple(
            channel.number
            for channel in sorted(self.channels, key=lambda one: ladder_index(one.number))
            if channel.in_rotation
        )

    def step(self, current: str | None, steps: int) -> str | None:
        """The number ``steps`` away from ``current``, or ``None`` when there is nowhere to go.

        It walks the rotation and wraps, so a listener can never get stuck at an end. Two cases the
        obvious index-and-add misses, and both are ordinary:

        A current channel that is NOT in the rotation is where a thumbs down leaves the listener -
        on the channel they just took out. It is placed by how many rotation entries sit before it,
        so next goes to the one after it and previous to the one before it, rather than to an end.

        A current number that is not in the list at all is what a hand-edited file leaves behind;
        it counts as sitting before the first channel.
        """
        rotation = self.rotation_numbers()
        if not rotation:
            return None
        if current in rotation:
            return rotation[(rotation.index(current) + steps) % len(rotation)]
        before = self._how_many_come_before(current)
        # Stepping FORWARD from between two entries lands on the next one, which is `before`
        # itself, so one step is already taken by being between them; stepping back is not.
        offset = before + steps - 1 if steps > 0 else before + steps
        return rotation[offset % len(rotation)]

    def _how_many_come_before(self, number: str | None) -> int:
        """How many channels in the rotation sit before ``number`` in dialling order.

        A number that is not in the list at all counts as sitting before everything, so stepping
        forward from it lands on the first channel. That is what a hand-edited file leaves behind,
        and it matches what the service plays in the same situation - the lowest there is.
        """
        if number is None or self.by_number(number) is None:
            return 0
        return sum(1 for one in self.rotation_numbers() if ladder_index(one) < ladder_index(number))

    def with_rotation(self, number: str, *, in_rotation: bool) -> ChannelList:
        """This list with that channel in or out of the rotation; unchanged when it is not there."""
        channel = self.by_number(number)
        if channel is None or channel.in_rotation == in_rotation:
            return self
        return self.with_channel(replace(channel, in_rotation=in_rotation))

    def with_channel(self, channel: Channel) -> ChannelList:
        """This list plus that channel, replacing whatever held its number.

        Replacing rather than appending: two entries with one number is a list that answers a
        press differently depending on which is found first.
        """
        kept = tuple(one for one in self.channels if one.number != channel.number)
        return ChannelList(channels=(*kept, channel))

    def without_number(self, number: str) -> ChannelList:
        """This list without that number; unchanged when it was not there."""
        return ChannelList(channels=tuple(one for one in self.channels if one.number != number))


@dataclass(frozen=True, slots=True, kw_only=True)
class PresetStation:
    """One preset read off a speaker, reduced to what a channel needs from it.

    A record rather than a ``tuple[str, str]`` for the reason ``Options.speakers`` is a NamedTuple:
    both fields are strings, and no checker can tell the name from the URL once they are
    positional.

    It is this module's own rather than ``source.StationRequest`` because the layers contract makes
    ``source`` and ``channellist`` independent - and because seeding has no business knowing what
    HTTP is. The service does the conversion; it is allowed to import both.
    """

    name: str
    url: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SeedingReport:
    """What seeding produced, and the lines the caller writes about it.

    The lines are carried rather than written here, because nothing in ``domain`` narrates. They
    are in the order they were said, all of one kind (``channels``), so a caller that writes them
    out in order reproduces exactly what the old service logged at this point.
    """

    seeded: ChannelList
    said: tuple[str, ...]


class SeedingError(ChannelNumberError):
    """A preset seeding could not turn into a channel, carrying what was said before it.

    The refusal itself is :class:`ChannelNumberError` with the message that channel raised, so a
    caller sees the same refusal as before. It carries ``said`` because the holes ahead of the bad
    preset were LOGGED by the old code as it walked, before the raise - dropping them would lose
    lines from exactly the failure a person is trying to read.
    """

    def __init__(self, said: tuple[str, ...], because: ChannelNumberError) -> None:
        super().__init__(str(because))
        self.said = said


def seed_from_presets(presets: Mapping[int, PresetStation | None]) -> SeedingReport:
    """Places 1 to 6 from one speaker's presets, at first start and never again by itself.

    A preset that is not set leaves a HOLE: a box with presets 1 and 3 seeds channels 1 and 3, not
    1 and 2. The key on the box is the number in the room, and shifting the others up would break
    exactly the promise that makes seeding from presets worth doing - that one press means the same
    thing whether this service is running or not.
    """
    seeded: list[Channel] = []
    said: list[str] = []
    for number, preset in sorted(presets.items()):
        if preset is None:
            said.append(f"preset {number}: no preset there, so channel {number} stays free")
            continue
        try:
            seeded.append(Channel(number=str(number), name=preset.name, kind=ChannelKind.RADIO, url=preset.url))
        except ChannelNumberError as exc:
            raise SeedingError(tuple(said), exc) from exc
    said.append(f"seeded {len(seeded)} channel(s) from presets")
    return SeedingReport(seeded=ChannelList(channels=tuple(seeded)), said=tuple(said))
