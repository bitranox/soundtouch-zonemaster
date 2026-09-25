"""What one running service is: every field it holds, and the file it writes them to.

The bottom of the chain. ``ZoneService`` was one class of sixty-one methods, and it is eight
classes now, each extending the one before it - so this is where the constructor lives, together
with the one helper it calls, because between them they create every field the seven above read.

The chain's rule is that a method may call only methods of its own class or of an earlier one, so
reading it from here upwards is reading it in dependency order: the state, the channel list, the
speakers, the volumes, the zone, the dialling, the keys, and the run that starts them.

The one thing here that is not a field is the state FILE, which is written from a single place on
purpose: membership changes and a dialled channel both land in it, and a writer per caller would
have lost every channel somebody dialled without the zone also changing.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import TYPE_CHECKING

from ...domain.calibration import Calibration, Gesture
from ...domain.channellist import ChannelList
from ...domain.dialling import Dialler
from ...domain.housevolume import HouseVolume
from ...domain.longpress import LongPresses
from ...domain.membership import Membership
from ...domain.presses import Presses
from ...domain.state import Place, ZoneState

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ...domain.events import SpeakerEvent
    from ...domain.logfn import LogFn
    from ...domain.speakers import Speaker
    from ..options import ServiceOptions
    from ..ports import MpdControlPort, ZoneMasterPort, ZoneServicePorts

__all__ = ["LastReported", "OwedEcho", "OwedEchoes", "ServiceState"]


class OwedEcho:
    """One command of ours, waiting for the single frame the box will answer it with.

    A volume write is owed one touch; a ``/select`` is owed one touch AND one selection, held in
    two books because the two frames arrive in either order (measured 2026-09-24).
    """

    __slots__ = ("until",)

    def __init__(self) -> None:
        self.until = math.inf
        """Epoch seconds, the clock ``received_at`` is on. Infinite while the command is in flight."""


class OwedEchoes:
    """Every command of ours a box has not answered yet, per kind of frame and per box.

    Two books, because a ``/select`` is owed a selection AND a touch and the two arrive in either
    order (measured 2026-09-24), while a volume write is owed the touch alone. Held as one field
    because every caller that owes one kind has to be read against the other.
    """

    __slots__ = ("_selections", "_touches")

    def __init__(self) -> None:
        self._touches: dict[str, list[OwedEcho]] = {}
        self._selections: dict[str, list[OwedEcho]] = {}

    def owe_touch(self, device_id: str) -> OwedEcho:
        """Owe this box's next ``userActivityUpdate`` to a command about to be sent."""
        return _owe(self._touches, device_id)

    def owe_selection(self, device_id: str) -> OwedEcho:
        """Owe this box's next ``nowSelectionUpdated`` to a ``/select`` about to be sent."""
        return _owe(self._selections, device_id)

    def claim_touch(self, device_id: str, *, at: float) -> bool:
        """Whether a touch this box reported at ``at`` is one a command of ours is owed."""
        return _claim_one(self._touches, device_id, at=at)

    def claim_selection(self, device_id: str, *, at: float) -> bool:
        """Whether a selection this box reported at ``at`` is one a ``/select`` of ours is owed."""
        return _claim_one(self._selections, device_id, at=at)


def _owe(book: dict[str, list[OwedEcho]], device_id: str) -> OwedEcho:
    owed = OwedEcho()
    book.setdefault(device_id, []).append(owed)
    return owed


def _claim_one(book: dict[str, list[OwedEcho]], device_id: str, *, at: float) -> bool:
    """Hand ONE owed echo over to a frame this box sent at ``at``, if any is still owed.

    One frame per command and no more, which is what keeps a person pressing in the middle of our
    own traffic from being swallowed with it. Entries whose window has passed are dropped here, so
    a command whose echo never came stops claiming anything once it expires.
    """
    owed = [echo for echo in book.get(device_id, []) if at <= echo.until]
    if not owed:
        book.pop(device_id, None)
        return False
    book[device_id] = owed[1:]
    return True


class LastReported:
    """The conditions that are said when they CHANGE, each as the last pass or poll found it.

    Each one persists across many passes - a busy port, a number being dialled, a placeholder in
    the device list - and a line per pass was the same sentence several times a second.
    """

    __slots__ = ("ports_busy", "skipped_in_registry", "waiting_on_a_number")

    def __init__(self) -> None:
        self.ports_busy = False
        """Whether a listening port was held by something else. It also paces the next pass."""
        self.waiting_on_a_number: frozenset[str] = frozenset()
        """The boxes left out because a number was still open."""
        self.skipped_in_registry: frozenset[str] = frozenset()
        """The device-list entries the read had to skip."""


class ServiceState:
    """One service's whole state, and the one writer of the file a restart starts from."""

    def __init__(self, options: ServiceOptions, *, log: LogFn, ports: ZoneServicePorts) -> None:
        self.options = options
        self.log = log
        self.ports = ports
        """Everything outside this program: the two files, the speakers, the service next door.

        One record rather than one argument per port, and with no defaults, so that adding a
        port breaks every place that builds one - which is the composition root and the tests."""
        self.master: ZoneMasterPort | None = None
        """The zone, and only while the switch is on; ``None`` means we are standing down."""
        self.events: asyncio.Queue[SpeakerEvent] = asyncio.Queue()
        """The one stream: the observers put frames on it, the master's HTTP face puts keys."""
        self._positions: dict[str, Place] = {}
        """Where each MPD channel was left, by channel number, as the state file remembers it.

        Written when the house LEAVES a channel rather than while it plays one: MPD keeps no
        position per stored playlist, and asking it every second would be a file write per second
        for a number nothing reads until the next time somebody dials that channel."""
        self._mpd_channel: str | None = None
        """The channel number MPD is currently holding, or nothing while it holds none.

        It cannot be derived from what the ZONE is playing, because a dialled number sets that
        before anything is asked of MPD - so by the time the position of the channel being LEFT is
        wanted, the other answer already names the new one."""
        self._mpd: MpdControlPort | None = None
        """The control connection to MPD, opened on the first MPD channel and never before.

        A house with nothing but radio channels never opens one, which is what keeps MPD an
        optional part of the machine rather than something the service checks for at startup.
        It is not closed on a stand-down: the switch going off ends the ZONE, and MPD holding a
        socket open costs nothing, while reconnecting per channel change would cost a round trip
        in front of the one exchange whose ORDER the house can hear."""
        self.switch = ports.open_switch(options.switch_file, log=log, poll_s=options.switch_poll_s)
        self.policy = Membership(
            master_device_id=options.device_id,
            now=time.time,
            unreachable_timeout_s=options.unreachable_timeout_s,
            consoles_allowed=options.consoles_allowed,
        )
        self._speakers: dict[str, Speaker] = {}
        self._observers: dict[str, asyncio.Task[None]] = {}
        self._joined: dict[str, str] = {}
        """Device id to the address it was joined at: what the zone HOLDS, not what it should."""
        self._refused: dict[str, float] = {}
        self._owed = OwedEchoes()
        """Our own volume writes and ``/select`` calls whose answer has not come back yet. Each is
        answered by frames of exactly the shape a person makes, and those frames are not one."""
        self._out_of_multiroom: set[str] = set()
        """Boxes a person switched out of the zone by holding a thumb down on one of them.

        Mirrored into the state file, because nothing a person can see records the decision and a
        restart that took the box back in would undo it silently. It is taken off the answer in
        ``_who_belongs``, which is the one place membership is decided."""
        self._last_reported = LastReported()
        """What the last pass or poll found, so a line is written when it changes and not on the
        passes in between - a pass runs for every frame a box sends."""
        self._believed: tuple[str, ...] = ()
        self._channel: str | None = None
        self._on_air: str | None = None
        """The channel number the station now running was started from, or nothing before one is.

        A different question from :attr:`_channel`, which is the channel the house is MEANT to be
        on: that one is written the moment a number completes, including when the zone is empty and
        nothing is started. This one is written where a channel is actually put on the zone, and it
        is what answers "is this already playing".

        A URL cannot answer it. MPD has ONE ``httpd`` output, so every MPD channel names the same
        address and what tells two of them apart is the playlist - which means a comparison of URLs
        reads a move from one audiobook to another as the same channel pressed twice. Measured in
        the flat 2026-09-21: eleven dials, every MPD-to-MPD one refused as "already playing it",
        and the only way through was to dial a radio channel in between."""
        self._channels = ChannelList()
        """The house's channels. Empty until the file is read or the seeding has run."""
        self._switched_on: list[str] = []
        """Device ids in the order they were first seen out of standby.

        The first of them seeds the channel list when there is none (user, 2026-09-07). The house
        should not have to carry the name of a box in a unit file, and the box somebody switches on
        is the box they are standing at.
        """
        self._asked_for_presets: set[str] = set()
        """Boxes already asked for their presets, so none is asked twice.

        A box with nothing on its keys must not consume the chance - the next one switched on gets
        it - and it must not be asked again on every frame it sends either.
        """
        self._on = True
        self._no_volume_moved_yet()
        self._nothing_has_been_pressed_yet(options)
        self._lock = asyncio.Lock()
        self._seeding = asyncio.Lock()
        """Its own lock, because seeding deliberately runs OUTSIDE ``_lock`` and is still called
        from two independently scheduled tasks - the registry poll and the pass."""
        self._wanted = asyncio.Event()
        """Set by anything that changes the answer, cleared by the pass that acts on it."""
        self._starting: set[asyncio.Task[None]] = set()
        """Channel starts running on their own, so a gesture never waits behind one.

        A start waits up to ten seconds for somebody else's server, and ``ZoneMaster.play`` is
        built for several of them at once - the newest wins and the older ones give up. Held here
        only because asyncio keeps a weak reference to a task, so one nobody holds can be
        collected while it is still fetching.
        """

    def _no_volume_moved_yet(self) -> None:
        """Every volume this service takes away or moves, at its empty start.

        One group for the same reason as the presses below: they are one subject, and it is the
        whole of ``VolumeGuard`` - a box turned down while it is taken in and the fade that puts it
        back, and the house volume a person moves from one box that every other box follows.
        """
        self._fading: dict[str, asyncio.Task[None]] = {}
        """The put-it-back task per box, held so it is not garbage collected mid-fade, and so the
        pass can tell a fade in progress from a mute that was abandoned."""
        self._muted: dict[str, int] = {}
        """Boxes turned down to zero while they are taken in, and the volume each was on.

        Mirrored into the state file, because the one failure this feature can cause is a speaker
        left silently at zero, and that reads as broken hardware rather than as a service that
        stopped halfway."""
        self._house_volume = HouseVolume()
        """The level every box last reported, and whose tapped thumb has the volume keys now."""
        self._owed_volume: dict[str, int] = {}
        """House-volume steps a box missed while it was off, taken when it next joins.

        Mirrored into the state file: the house can be turned up hours before a box is switched on,
        and a restart in between must not forget Room1 is owed it."""
        self._house_steps: dict[str, int] = {}
        """Steps a box in the zone is to be moved by and has not been written yet, per device id.

        Steps rather than levels, so that a report of one of our own writes arriving between two
        steps cannot put a level from before the first step under the second."""
        self._house_writers: dict[str, asyncio.Task[None]] = {}
        """The one write task per box, so the steps of a held volume key queue up behind it rather
        than racing it - a held key reports about every 300 ms."""

    def _nothing_has_been_pressed_yet(self, options: ServiceOptions) -> None:
        """Everything that remembers what somebody is in the middle of doing, at its empty start.

        One group and one method because they are one subject: a digit buffer per speaker, which
        selections were a person rather than our own voice, how long each thumb has been held, and
        a calibration that may be running. They are created together because they MEAN the same
        thing together - at the start of a run nobody has pressed anything - and because a
        constructor that lists every field the service has in one run reads as a list rather than
        as the handful of things a service is.
        """
        self._dialler = Dialler(window_s=options.dial_window_s)
        self._presses = Presses()
        """Which selections a person made. A selection the master caused looks exactly like a
        press, so nothing here reaches the dialler until the box says a human touched it."""
        self._longpresses = LongPresses(threshold_s=options.hold_threshold_s)
        """Which keys are down, for every key rather than only the thumbs.

        One rule tells a tap from a hold for all of them. A key released inside the hold threshold
        is a tap; one still down when it passes is a hold, and the waiting loop acts on it then
        rather than when the finger comes up (user, 2026-09-20). The threshold is its own number,
        not the dialling window (user, 2026-09-24): the window measures the pause between two keys,
        this how long one is down, and a calibration measures both on the same presses."""
        self._calibration = Calibration()
        """One calibration at a time for the whole house: the numbers it writes are one setting each."""
        self._gesture = Gesture()
        self._calibrated_window_s: float | None = None
        """What a calibration measured, kept so the state file can be written from one place."""
        self._calibrated_hold_s: float | None = None
        """The hold threshold the same calibration measured, kept for the same reason."""
        self._dialled = asyncio.Event()
        """Set when a digit lands, so the dialling worker recomputes its deadline rather than
        sleeping through a number that is still being typed."""

    def _write_down(self, believed: frozenset[str]) -> None:
        """Record who the zone belongs to, so that a restart does not start from nothing.

        Sorted, because a set has no order and a file that reshuffles itself reads as one that
        changed. The switch is deliberately not part of this: it says whether we are holding the
        house, not who is in it, and a service that emptied the file on the way out would come
        back to an empty house.
        """
        ordered = tuple(sorted(believed))
        if ordered == self._believed:
            return
        self._believed = ordered
        self._save_the_state()
        self.log("state", f"the zone belongs to: {', '.join(ordered) or 'nobody'}")

    def _save_the_state(self) -> None:
        """Write down what a restart starts from. One writer, so the two halves cannot drift.

        Membership changes and a dialled channel both land here: writing only on a membership
        change would have lost every channel somebody dialled without the zone also changing.
        """
        self.ports.save_state(
            self.options.state_file,
            ZoneState(
                channel=self._channel,
                members=self._believed,
                muted=dict(self._muted),
                out_of_multiroom=tuple(sorted(self._out_of_multiroom)),
                positions=dict(self._positions),
                dial_window_s=self._calibrated_window_s,
                hold_threshold_s=self._calibrated_hold_s,
                owed_volume=dict(self._owed_volume),
            ),
        )

    def _names(self, device_ids: Sequence[str]) -> str:
        """The boxes named the way a person reads them, for one log line about several of them."""
        return ", ".join(self._name(device_id) for device_id in device_ids)

    def _name_of(self, address: str) -> str:
        """What to call a box the master knows only by address."""
        for device_id, joined_at in self._joined.items():
            if joined_at == address:
                return self._name(device_id)
        return address

    def _the_window_is_now(self, window_s: float) -> None:
        """Set the dialling window: how long the pause after a key may be before the digits are read."""
        self._dialler.window_s = window_s

    def _the_hold_is_now(self, hold_s: float) -> None:
        """Set the hold threshold: how long a key must stay down to be held rather than tapped."""
        self._longpresses.threshold_s = hold_s

    def _name(self, device_id: str) -> str:
        """What to call a box in the log; its id if the registry has never named it."""
        speaker = self._speakers.get(device_id)
        return speaker.name if speaker is not None else device_id
