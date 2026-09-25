"""Everything the two programs need from the world outside them, named as callables.

Most of these are a **callable Protocol** rather than a class with methods, because that is what
the old code actually reached for: ten module-level functions and three constructors. Naming each
one on its own keeps the substitution the same size as the thing being substituted - a test that
wants a different volume reader supplies a different function, not an object implementing every
method it does not care about. The ones that ARE objects - the switch, one speaker's watch, MPD's
control connection and the two views of a master - are objects because that is what the adapter
hands back.

They are bundled into two records, one per program, so a constructor takes ONE argument rather
than one per port, and so that a new port cannot be forgotten at a call site: adding a field to
:class:`ZoneServicePorts` breaks every place that builds one, which is the composition root and
the doubles in the tests, and nowhere else.

**Every type these mention is a domain or application type.** That is the whole point of the file:
``application`` may not import ``adapters``, so the service is handed functions whose SHAPES are
declared here and whose bodies live at the edge. It is also why the encryption a prototype run
asks for is the domain enum and not the protobuf value the wire carries - the mapping between them
belongs to the adapter that sends it.

The conformance check that each real adapter satisfies its port is in ``composition``, under
``TYPE_CHECKING``: this file cannot import the adapters to make it, and the composition root is
the one place that already names both sides.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import asyncio
    from collections.abc import AsyncGenerator, Mapping, Sequence
    from pathlib import Path

    from ..domain.channellist import ChannelList
    from ..domain.enums import ChannelEnd, Encryption
    from ..domain.events import SpeakerEvent
    from ..domain.logfn import LogFn
    from ..domain.mpd import MpdStatus
    from ..domain.speakers import Speaker
    from ..domain.state import Place, ZoneState
    from ..domain.station import Station, StationRequest
    from .options import ChannelPolicy, Options, ServiceOptions

__all__ = [
    "AddressOf",
    "AskNowPlaying",
    "FetchSpeakers",
    "LoadChannels",
    "LoadState",
    "MpdControlPort",
    "OpenMpdControl",
    "OpenPrototypeMaster",
    "OpenSwitch",
    "OpenZoneMaster",
    "PrototypeMaster",
    "PrototypePorts",
    "ReadPreset",
    "ReadVolume",
    "RunService",
    "RunZone",
    "SaveChannels",
    "SaveState",
    "SelectStation",
    "SetVolume",
    "SpeakerWatch",
    "StationSource",
    "SwitchReader",
    "WatchSpeaker",
    "ZoneMasterPort",
    "ZonePort",
    "ZoneServicePorts",
]


# --- the files --------------------------------------------------------------------------------


class LoadState(Protocol):
    """Read what a restart starts from. A document nobody can make sense of starts it empty."""

    def __call__(self, path: Path, *, log: LogFn) -> ZoneState: ...


class SaveState(Protocol):
    """Write it down, atomically enough that a power cut cannot leave half of it."""

    def __call__(self, path: Path, state: ZoneState) -> None: ...


class LoadChannels(Protocol):
    """Read the house's channel list. Missing is empty; unusable is a refusal, never empty."""

    def __call__(self, path: Path, *, log: LogFn) -> ChannelList: ...


class SaveChannels(Protocol):
    """Write the list back, in the shape a person can read and repair."""

    def __call__(self, path: Path, channels: ChannelList) -> None: ...


class SwitchReader(Protocol):
    """The switch as the service uses it: read it now, or follow it for as long as we run."""

    def is_on(self) -> bool: ...

    def watch(self) -> AsyncGenerator[bool, None]: ...


class OpenSwitch(Protocol):
    """Open the switch over one path. Built once, in the constructor, and then watched."""

    def __call__(self, path: Path, *, log: LogFn, poll_s: float = ...) -> SwitchReader: ...


# --- the speakers, and the service next door --------------------------------------------------


class FetchSpeakers(Protocol):
    """Who the speakers are, from AfterTouch's own device list.

    Raises ``RegistryError`` when the read fails or when NO entry in the list is usable. A list
    with some usable entries yields those, and ``log`` names each one dropped - a half-discovered
    device is normal in a discovery result and must not cost the house the speakers beside it.
    """

    def __call__(self, base_url: str = ..., *, log: LogFn | None = ...) -> Awaitable[tuple[Speaker, ...]]: ...


AddressOf = Callable[[str], "Awaitable[str | None]"]
"""Where a box is right now, or ``None`` while the registry does not list it.

Asked again on every reconnect rather than captured once, which is how an observer follows a box
that moved to a new lease without anybody restarting anything.
"""


class SpeakerWatch(Protocol):
    """One box's notification channel, held open and reconnected for as long as it runs."""

    async def run(self) -> None: ...


class WatchSpeaker(Protocol):
    """Start watching one box. The service holds the returned object's ``run`` as a task."""

    def __call__(
        self,
        device_id: str,
        /,
        *,
        address_of: AddressOf,
        events: asyncio.Queue[SpeakerEvent],
        log: LogFn,
        policy: ChannelPolicy,
    ) -> SpeakerWatch: ...


class ReadVolume(Protocol):
    """One box's volume, as a number between 0 and 100."""

    def __call__(self, ip: str, /) -> Awaitable[int]: ...


class SetVolume(Protocol):
    """Put one box at one level."""

    def __call__(self, ip: str, level: int, /) -> Awaitable[None]: ...


class SelectStation(Protocol):
    """Start one station on one box, without a zone and without a master stream."""

    def __call__(self, ip: str, /, *, url: str, name: str) -> Awaitable[None]: ...


class AskNowPlaying(Protocol):
    """What one box says it is playing, or ``None`` when it did not answer."""

    def __call__(self, ip: str, device_id: str, /) -> Awaitable[SpeakerEvent | None]: ...


class ReadPreset(Protocol):
    """What sits on one of a box's six preset keys. Raises when the key is not set."""

    def __call__(self, ip: str, number: int, /) -> Awaitable[StationRequest]: ...


# --- the daemon that holds the house's own music ------------------------------------------------


class MpdControlPort(Protocol):
    """MPD as the SERVICE drives it: put this playlist on, and move about inside it.

    Fewer methods than the client's whole verb list, because a port is what the caller needs and
    not what the adapter can do. Reading a position back arrives here when something reads it.
    """

    async def play_entry(self, entry: str, *, place: Place | None = ..., end: ChannelEnd) -> None:
        """Load one stored playlist and play it, from the start or from where the house left it.

        A place is a track and an offset together, because neither half is a place on its own.
        ``end`` decides what MPD does when the queue runs out by itself, and it is said on every
        load because MPD holds one such setting for the whole daemon.

        Raises :class:`~soundtouch_zonemaster.application.errors.NotInMpdError` when the
        channel names a playlist MPD does not have, which is a mistake in the channel file rather
        than a fault; any other refusal and every way the daemon can be unreachable arrive as
        :class:`~soundtouch_zonemaster.application.errors.MpdError` or an ``OSError``.
        """

    async def play_files(self, files: Sequence[str], *, place: Place | None = ..., end: ChannelEnd) -> None:
        """Build the queue from these files in THIS order, and play it from the start or a place.

        The order is the caller's, because it is the domain's rule and not MPD's database order. No
        files empties the queue and plays nothing. It fails the way :meth:`play_entry` does.
        """

    async def files_under(self, directory: str) -> tuple[str, ...]:
        """Every file MPD can play under that directory, subdirectories included, in MPD's order.

        Raises :class:`~soundtouch_zonemaster.application.errors.NotInMpdError` for a directory MPD
        does not have, which is a mistake in the channel file rather than a fault.
        """
        ...

    async def queue_files(self) -> tuple[str, ...]:
        """The path of every entry in the queue, in queue order: what a held key steps by."""
        ...

    async def play_at(self, position: int) -> None:
        """Play one entry of the queue, counting from zero, which is where a press lands.

        The caller names the entry rather than asking for the next one, because a press past
        either end wraps on every channel and MPD's own ``next`` stops there when ``repeat`` is
        off. It fails the way :meth:`play_entry` does.
        """

    async def status(self) -> MpdStatus:
        """What MPD is doing, with every key a stopped one omits left as ``None`` rather than 0.

        It is asked two things: how far into the current entry MPD has got, before the house
        leaves the channel, and where in the queue it is, before a press moves it. The record is a
        domain one, so both sides of this port may name it.
        """
        ...

    async def close(self) -> None:
        """Drop the connection.

        Here because the service needs it, not because the adapter has it: a connection that has
        failed once goes on failing, so the caller throws it away and opens a fresh one at the
        next channel change rather than reporting a dead socket for the rest of the day.
        """


class OpenMpdControl(Protocol):
    """Open a control connection to MPD.

    Built on the FIRST MPD channel the house plays and then kept, rather than in the constructor:
    a house with nothing but radio channels never opens one, and a service that connected at start
    would report a daemon it does not need as a fault.
    """

    def __call__(self, host: str, port: int, log: LogFn) -> MpdControlPort: ...


# --- the zone itself ----------------------------------------------------------------------------


class ZoneMasterPort(Protocol):
    """The master as the SERVICE drives it: lifecycle, membership, and the station.

    ``slaves`` is read for one question only - is the zone empty - so it is a read-only mapping
    of whatever the adapter keeps per member, and this file deliberately does not name that type.
    """

    station: Station | None

    @property
    def slaves(self) -> Mapping[str, object]: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def play(self, request: StationRequest) -> Station | None: ...

    async def stop_station(self) -> None: ...

    async def add_slave(self, ip: str) -> None: ...

    async def slave_left(self, ip: str) -> None: ...

    async def release(self, ip: str) -> None: ...

    async def dissolve(self) -> None: ...

    def slaves_left_on_an_old_stream(self) -> list[str]: ...

    async def put_back_on_the_station(self, peer: str) -> bool: ...


class OpenZoneMaster(Protocol):
    """Build the master the service holds. Not started here: the service starts it under its lock.

    Six parameters because a constructor's parameter list is what this mirrors: the port exists so
    a type checker can compare the two, and a shorter one would stop describing the thing. Bundling
    them into a record would move the same six one call further out and change the service body,
    which is the one thing this milestone's split may not do.
    """

    def __call__(  # noqa: PLR0913 - see above: this signature IS ZoneMaster's, mirrored
        self,
        *,
        bind_ip: str,
        device_id: str,
        log: LogFn,
        events: asyncio.Queue[SpeakerEvent],
        slave_heard: Callable[[str], None],
        ignore_selects: bool,
    ) -> ZoneMasterPort: ...


class ZonePort(Protocol):
    """What the prototype's run loop needs from the master it is holding.

    Named for the same reason ``http_api.MasterPort`` is: these three are the whole orchestration
    of a run, and without a seam the only way to reach them was to replace the function that calls
    them, which tests what the stand-in does rather than what the code does.
    """

    station: Station | None

    async def add_slave(self, ip: str) -> None: ...

    async def play(self, request: StationRequest) -> Station | None:
        """None when a newer select overtook this one. The run loop starts stations one at a time,
        so it never sees that; a preset press on a slave can, which is why the return says so."""


class PrototypeMaster(ZonePort, Protocol):
    """What one whole prototype run needs: the run loop's three, and the lifecycle around them."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def dissolve(self) -> None: ...


class OpenPrototypeMaster(Protocol):
    """Build the master one run holds, from the option set that run was given."""

    def __call__(
        self,
        *,
        bind_ip: str,
        device_id: str,
        log: LogFn,
        encryption: Encryption,
        ignore_selects: bool,
    ) -> PrototypeMaster: ...


StationSource = Callable[[str, int], "Awaitable[StationRequest]"]
"""A station off a speaker's preset key, which is where the prototype gets something to play."""


# --- what a command runs ------------------------------------------------------------------------


RunService = Callable[["ServiceOptions"], "Awaitable[int]"]
"""Hold the house until stopped. The seam the service command's tests substitute."""

RunZone = Callable[["Options"], "Awaitable[int]"]
"""One whole run of the prototype. The seam the run-loop tests substitute."""


# --- the two bundles ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ZoneServicePorts:
    """Everything the service reaches the world through, in one argument.

    Every field required and none defaulted, on purpose: a port that defaulted to the real adapter
    would make ``application`` import it, and a port that defaulted to a no-op would let a test
    pass while the thing it meant to exercise was never called.
    """

    load_state: LoadState
    save_state: SaveState
    load_channels: LoadChannels
    save_channels: SaveChannels
    open_switch: OpenSwitch
    fetch_speakers: FetchSpeakers
    watch_speaker: WatchSpeaker
    open_zone_master: OpenZoneMaster
    read_volume: ReadVolume
    set_volume: SetVolume
    select_station: SelectStation
    ask_now_playing: AskNowPlaying
    read_preset: ReadPreset
    open_mpd: OpenMpdControl


@dataclass(frozen=True, slots=True, kw_only=True)
class PrototypePorts:
    """The two the prototype needs: something to hold, and something to play on it."""

    open_master: OpenPrototypeMaster
    station_source: StationSource
