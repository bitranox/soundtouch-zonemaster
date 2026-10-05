"""Every validated option set the two programs run on, and the constants that default them.

One record per program - :class:`Options` for the prototype, :class:`ServiceOptions` for the
service - built once at the boundary and handed down. The rest of the program is given the record
and never a loose mapping of CLI values, whose entries are ``Any``: a body that reads them is
unchecked however strict the type checker is set.

**The invariants that are pure live here; the ones that need the world do not.** A device id that
is not twelve hex digits is not a device id, and a dialling window outside the measured bounds is
not a window, so both are refused in ``__post_init__`` with the message and exit code they always
had. Whether the database's directory EXISTS, and whether a named address is a speaker this
house never touches, are questions about the machine rather than about the option set; they are
refused at the CLI boundary, with the same message and the same code.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..domain.dialling import WINDOW_DEFAULT_S
from ..domain.longpress import HOLD_THRESHOLD_DEFAULT_S
from ..domain.membership import UNREACHABLE_TIMEOUT_S
from ..domain.preferences import FADE_DEFAULT_S, HousePreferences, PreferenceName
from ..domain.switch import POLL_S
from .outcome import device_id_or_refuse, preference_or_refuse, tcp_port_or_refuse

if TYPE_CHECKING:
    from ..domain.enums import Encryption, JoinMode
    from ..domain.secret import Secret

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


def _hold(record: object, name: str, value: object) -> None:
    """Put the normalised form of a field back on a frozen record, from its own ``__post_init__``.

    ``object.__setattr__`` is the documented way a frozen dataclass sets a field after its
    ``__init__``; it is wrapped so each use says which field is being normalised, not how.
    """
    object.__setattr__(record, name, value)


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
    registry_url: str
    """``[registry] url``: the service whose BMX registry completes a relative Orion preset."""

    def __post_init__(self) -> None:
        """Refuse a device id that is not one, and hold the one that is in upper case.

        Folded HERE rather than only at the CLI boundary, because a record is also built by
        callers that are not the CLI, and a speaker's report of whose stream it plays is compared
        with this id exactly: kept lower-case, it would name the master as somebody else.

        The console refusal that used to sit beside this is NOT gone: it became a
        configured list of addresses this house never touches, checked at the CLI boundary
        where the configuration is read, with the same message and the same exit code.
        """
        _hold(self, "device_id", device_id_or_refuse(self.device_id))


@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelsExport:
    """A channel list export, both faces of the same parse.

    One read of the store produces both fields, so ``count`` can never disagree with ``text``: a
    caller that read the count from a SECOND store read could see a different list than the one it
    just wrote, if a writer changed the store between the two.
    """

    text: str
    count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceOptions:
    """One validated service: where it binds, what it plays, and the database it owns."""

    bind_ip: str
    device_id: str
    database: str
    """The house database: a URL, or a plain path meaning a SQLite file (``adapters/files/house_db.py``)."""
    database_password: Secret | None = None
    """The PostgreSQL password, handed to the driver as a connect argument and never put in the URL.

    ``None`` passes nothing, so libpq's own ``~/.pgpass``, ``PGPASSFILE`` and ``PGPASSWORD`` still
    apply. A SQLite database has no password, and the store refuses one given for it."""
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
    fade_s: float = FADE_DEFAULT_S
    """How long a joining box's volume climbs from zero back to its own level (``domain/preferences.py``).

    A preference: a value set in the house database overrides this one while it is set."""

    def __post_init__(self) -> None:
        """Refuse a device id that is not one, then every preference outside its bounds.

        In that order, because that is the order the refusals came in when they were field
        validators and a caller giving both a bad id and a bad value saw the id refused. The
        MPD port is checked between the window and the hold threshold, for that reason: it
        arrived with M3a, and putting it anywhere else would change which refusal an option
        set with two faults reports. The rewind, the fade-in and the consoles arrived later
        still, so they are checked after the ones above them. The five preference bounds
        themselves live in one place, ``domain/preferences.py``, so a value can never be legal
        from one source and refused from the other.

        The device id and the consoles are HELD as the checks return them, in upper case, for the
        reason :class:`Options` gives: a record built outside the CLI must compare equal to what
        the registry and the speakers report.

        Whether the state file's directory exists is NOT checked here: it is a question
        about the machine rather than about the option set, and it is refused at the CLI
        boundary with the same message and the same exit code.
        """
        _hold(self, "device_id", device_id_or_refuse(self.device_id))
        preference_or_refuse(PreferenceName.WINDOW, self.dial_window_s)
        tcp_port_or_refuse(self.mpd_port, what="the mpd port")
        preference_or_refuse(PreferenceName.HOLD, self.hold_threshold_s)
        preference_or_refuse(PreferenceName.REWIND, self.mpd_rewind_s)
        preference_or_refuse(PreferenceName.FADE, self.fade_s)
        _hold(self, "consoles_allowed", preference_or_refuse(PreferenceName.CONSOLES, self.consoles_allowed))

    @property
    def preferences(self) -> HousePreferences:
        """The five preferences as the options give them: what the house database's rows are laid over."""
        return HousePreferences(
            window_s=self.dial_window_s,
            hold_threshold_s=self.hold_threshold_s,
            rewind_s=self.mpd_rewind_s,
            fade_s=self.fade_s,
            consoles_allowed=self.consoles_allowed,
        )
