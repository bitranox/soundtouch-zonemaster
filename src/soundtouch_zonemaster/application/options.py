"""Every validated option set the two programs run on, and the constants that default them.

One record per program - :class:`Options` for the prototype, :class:`ServiceOptions` for the
service - built once at the boundary and handed down. The rest of the program is given the record
and never a loose mapping of CLI values, whose entries are ``Any``: a body that reads them is
unchecked however strict the type checker is set.

**The invariants that are pure live here; the ones that need the world do not.** A device id that
is not twelve hex digits is not a device id, and a dialling window outside the measured bounds is
not a window, so both are refused in ``__post_init__`` with the message and exit code they always
had. Whether the state file's directory EXISTS, and whether a named address is a speaker this
house never touches, are questions about the machine rather than about the option set; they are
refused at the CLI boundary, with the same message and the same code.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..domain.dialling import WINDOW_CEILING_S, WINDOW_DEFAULT_S, WINDOW_FLOOR_S
from ..domain.longpress import HOLD_THRESHOLD_CEILING_S, HOLD_THRESHOLD_DEFAULT_S, HOLD_THRESHOLD_FLOOR_S
from ..domain.membership import UNREACHABLE_TIMEOUT_S
from ..domain.switch import POLL_S
from .outcome import ExitCode, OptionsError, device_id_or_refuse, tcp_port_or_refuse

if TYPE_CHECKING:
    from pathlib import Path

    from ..domain.enums import Encryption, JoinMode

__all__ = [
    "DEFAULT_BASE_URL",
    "MPD_HOST",
    "MPD_PORT",
    "MPD_REWIND_S",
    "RECONNECT_BACKOFF_S",
    "REGISTRY_POLL_S",
    "WS_PORT",
    "ChannelPolicy",
    "Options",
    "ServiceOptions",
    "default_device_id",
]


DEFAULT_BASE_URL = "http://127.0.0.1:8000"
"""Where the replacement service answers inside the container the zone service runs in.

A loopback address on purpose: both live in the same container, so this read crosses no network
and cannot be reached from the LAN.
"""

WS_PORT = 8080
"""Where a speaker's notification channel listens. Overridable per observer so a test can bind an
ephemeral port; nothing in the program passes anything but the default."""

RECONNECT_BACKOFF_S = (1.0, 2.0, 5.0, 10.0)
"""How long to wait before trying again, by attempt, holding at the last value.

A speaker being away for minutes is normal, so this climbs to something that does not hammer a
box that is simply out of range, and never gives up.
"""

MPD_HOST = "127.0.0.1"
"""Where the Music Player Daemon answers its control protocol.

The loopback, like the registry above and for the same reason: MPD runs beside this service, and
the sound it serves reaches the speakers as an ordinary HTTP stream rather than through here. It
is a setting all the same, because which machine holds the house's music is a deployment question
and the three ways of arranging that - a bind mount, a network mount, a copy - are the user's
decision of 2026-09-20.
"""

MPD_PORT = 6600

MPD_REWIND_S = 20.0
"""How far back a channel starts when the house comes back to it, in seconds.

The overlap an audiobook wants (user, 2026-09-20): somebody returning hears their way back in
rather than landing mid-sentence. Twenty seconds because that is about a sentence and a half of
speech, and being a few seconds generous costs a listener nothing while being short costs them the
thread. An offset shorter than this starts that same file again rather than stepping into the one
before it, which is a different recording."""
"""MPD's own default control port, and what a house that has not changed it answers on."""

REGISTRY_POLL_S = 30.0
"""How often the device list is read again.

Slow, because it is how the service learns of a box that has appeared or moved, and nothing that
has to happen quickly waits for it: what a speaker is doing arrives on its own channel in
milliseconds.
"""


def default_device_id() -> str:
    """This host's MAC as a speaker-shaped device id."""
    return f"{uuid.getnode():012X}"


@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelPolicy:
    """Where a speaker's channel is, and how patiently a lost one is retried.

    The two travel together because neither is ever set in the program: a real box answers on
    :data:`WS_PORT` and the backoff above is the one the flat needs. They exist so a test can bind
    an ephemeral port and not wait out a real retry, and keeping them in one record says that in
    the signature rather than in a comment.
    """

    port: int = WS_PORT
    backoff_s: tuple[float, ...] = RECONNECT_BACKOFF_S


@dataclass(frozen=True, slots=True, kw_only=True)
class Options:
    """One validated run of the zone master."""

    bind_ip: str
    device_id: str
    slaves: tuple[str, ...]
    preset_from: str
    preset: int
    preset2: int
    switch_after: float
    duration: float
    encryption: Encryption
    late_slaves: tuple[str, ...]
    join_after: float
    join_mode: JoinMode
    ignore_selects: bool

    def __post_init__(self) -> None:
        """Refuse a device id that is not one.

        The console refusal that used to sit beside this is NOT gone: it became a
        configured list of addresses this house never touches, checked at the CLI boundary
        where the configuration is read, with the same message and the same exit code.
        """
        device_id_or_refuse(self.device_id)


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceOptions:
    """One validated service: where it binds, what it plays, and the two files it owns."""

    bind_ip: str
    device_id: str
    switch_file: Path
    """Off means we stand down. Missing, empty or unreadable means on (``domain/switch.py``)."""
    state_file: Path
    """What a restart starts from: the channel and the membership."""
    channel_file: Path
    """The house's channel list, in a file a person can read and repair.

    Missing means "not seeded yet" and is normal on a first start; unusable is REFUSED rather than
    started empty, because this is the only copy of something a person built (``domain/channellist.py``).
    """
    registry_url: str = DEFAULT_BASE_URL
    consoles_allowed: tuple[str, ...] = ()
    """Device ids of consoles that may be taken into the zone anyway.

    A Lifestyle console is otherwise left out and not even watched: an API POWER flips its input,
    so a service that reached it would change what somebody is doing in the room it stands in.
    """
    dial_window_s: float = WINDOW_DEFAULT_S
    """How long a speaker's digits are collected into one number before it is read.

    ONE setting for the house, not one per speaker: the design considered per-speaker and declined
    it. The bounds are measured (``domain/dialling.py``). A calibration at a speaker measures it on
    the person who presses, and what it measured outranks this value (``domain/calibration.py``).
    """
    hold_threshold_s: float = HOLD_THRESHOLD_DEFAULT_S
    """How long a key must stay down to be HELD rather than tapped (``domain/longpress.py``).

    Its own number rather than the dialling window (user, 2026-09-24): the window is the pause
    between two keys, this is how long one key is down. A calibration measures both on the same
    presses, and a measured threshold outranks this value the way a measured window does.
    """
    registry_poll_s: float = REGISTRY_POLL_S
    switch_poll_s: float = POLL_S
    unreachable_timeout_s: float = UNREACHABLE_TIMEOUT_S
    channel_policy: ChannelPolicy = field(default_factory=ChannelPolicy)
    """Where a speaker's notification channel is and how patiently a lost one is retried."""
    mpd_host: str = MPD_HOST
    """Where MPD answers, for the channels whose sound is a stored playlist it holds.

    A house with nothing but radio channels never opens the connection, so a wrong value here
    costs nothing until somebody dials an MPD channel - which is also why it is not checked at
    startup: a service that refused to hold the zone because a daemon it may never need is absent
    would take the radio down with it."""
    mpd_port: int = MPD_PORT
    mpd_rewind_s: float = MPD_REWIND_S
    """How far back an MPD channel starts when the house comes back to it.

    Read when a place is resumed and never when one is written, so what is recorded stays where
    the house actually stopped and this can be changed at any time."""

    def __post_init__(self) -> None:
        """Refuse a device id that is not one, then a window outside the measured bounds.

        In that order, because that is the order the two refusals came in when they were
        field validators and a caller giving both a bad id and a bad window saw the id
        refused. Below the floor a two-digit number starts splitting into two; above the
        ceiling the wait stops reading as a wait and starts reading as a fault. The MPD port
        is checked after both, for that reason: it arrived with M3a, and putting it anywhere
        else would change which refusal an option set with two faults reports. The hold
        threshold arrived later still, so it is checked after the port.

        Whether the state file's directory exists is NOT checked here: it is a question
        about the machine rather than about the option set, and it is refused at the CLI
        boundary with the same message and the same exit code.
        """
        device_id_or_refuse(self.device_id)
        if not WINDOW_FLOOR_S <= self.dial_window_s <= WINDOW_CEILING_S:
            message = (
                f"refused: the dialling window must be between {WINDOW_FLOOR_S} and {WINDOW_CEILING_S} s, "
                f"not {self.dial_window_s}"
            )
            raise OptionsError(message, exit_code=ExitCode.REFUSED)
        tcp_port_or_refuse(self.mpd_port, what="the mpd port")
        if not HOLD_THRESHOLD_FLOOR_S <= self.hold_threshold_s <= HOLD_THRESHOLD_CEILING_S:
            message = (
                f"refused: the hold threshold must be between {HOLD_THRESHOLD_FLOOR_S} and "
                f"{HOLD_THRESHOLD_CEILING_S} s, not {self.hold_threshold_s}"
            )
            raise OptionsError(message, exit_code=ExitCode.REFUSED)
