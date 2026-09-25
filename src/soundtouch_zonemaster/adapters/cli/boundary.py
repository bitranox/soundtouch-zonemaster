"""The service's edge: what argv and the six config layers said, validated into one record.

The record the rest of the program reads is a frozen dataclass with no framework in it
(``application/options.py``). Getting there from a command line and a stack of files is a parsing
job - a window written ``"0.6"`` in a file is 0.6, a console list arrives as JSON from an
environment variable - so the parsing happens HERE, in a pydantic model that keeps the archive's
fields, coercions and validators exactly.

Keeping the VALIDATORS, and not only the field types, is what makes the refusals identical rather
than similar. A validator that raises something other than a ``ValueError`` aborts a pydantic run
instead of being collected into it, so a bad device id beside a missing address refuses the device
id and never mentions the address. That is what the archive did, and the golden options corpus
records it case by case.

Two checks that look like invariants are here rather than on the record, because they are
questions about the MACHINE rather than about the option set: whether the state file's directory
exists, and - for the other program - whether an address is one this house never touches.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from ...__init__conf__ import service_command
from ...application.options import (
    DEFAULT_BASE_URL,
    MPD_HOST,
    MPD_PORT,
    MPD_REWIND_S,
    REGISTRY_POLL_S,
    ChannelPolicy,
    ServiceOptions,
    default_device_id,
)
from ...application.outcome import ExitCode, OptionsError, device_id_or_refuse, tcp_port_or_refuse
from ...domain.dialling import WINDOW_CEILING_S, WINDOW_DEFAULT_S, WINDOW_FLOOR_S
from ...domain.longpress import HOLD_THRESHOLD_CEILING_S, HOLD_THRESHOLD_DEFAULT_S, HOLD_THRESHOLD_FLOOR_S
from ...domain.membership import UNREACHABLE_TIMEOUT_S
from ...domain.switch import POLL_S
from ..config.loader import ENV_PREFIX
from ..config.settings_map import config_path_of, env_name_of, service_settings, unknown_settings
from ..logging.narration import log

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from lib_layered_config import Config

__all__ = [
    "ChannelPolicyInput",
    "ServiceOptionsInput",
    "configured_settings",
    "merge_service_settings",
    "parse_service_options",
]


class ChannelPolicyInput(BaseModel):
    """A channel policy as a config file delivers it, before it is the record's own.

    Its own model rather than a nested dict, so that a port written ``"x"`` is refused as
    ``channel_policy.port`` and not as the whole table.
    """

    model_config = ConfigDict(frozen=True)

    port: int = ChannelPolicy().port
    backoff_s: tuple[float, ...] = ChannelPolicy().backoff_s

    def record(self) -> ChannelPolicy:
        """The validated policy as the frozen record the program reads."""
        return ChannelPolicy(port=self.port, backoff_s=self.backoff_s)


class ServiceOptionsInput(BaseModel):
    """One service as argv and the config layers delivered it, before it is the record.

    The archive's ``ServiceOptions`` model, field for field and validator for validator. What moved
    is where it sits: the record it produces is a frozen dataclass now, and this is the boundary that
    produces it.
    """

    model_config = ConfigDict(frozen=True)

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
    """How long a key must stay down to be held rather than tapped."""
    registry_poll_s: float = REGISTRY_POLL_S
    switch_poll_s: float = POLL_S
    unreachable_timeout_s: float = UNREACHABLE_TIMEOUT_S
    channel_policy: ChannelPolicyInput = ChannelPolicyInput()
    """Where a speaker's notification channel is and how patiently a lost one is retried."""
    mpd_host: str = MPD_HOST
    """Where MPD answers, for the channels whose sound is a stored playlist it holds."""
    mpd_port: int = MPD_PORT
    mpd_rewind_s: float = MPD_REWIND_S
    """How far back an MPD channel starts when the house comes back to it."""

    @field_validator("device_id")
    @classmethod
    def _twelve_hex_digits(cls, value: str) -> str:
        return device_id_or_refuse(value)

    @field_validator("dial_window_s")
    @classmethod
    def _inside_the_measured_bounds(cls, value: float) -> float:
        """Below the floor a two-digit number starts splitting into two; above the ceiling the
        wait stops reading as a wait and starts reading as a fault."""
        if not WINDOW_FLOOR_S <= value <= WINDOW_CEILING_S:
            message = (
                f"refused: the dialling window must be between {WINDOW_FLOOR_S} and {WINDOW_CEILING_S} s, not {value}"
            )
            raise OptionsError(message, exit_code=ExitCode.REFUSED)
        return value

    @field_validator("mpd_port")
    @classmethod
    def _a_port_a_caller_can_dial(cls, value: int) -> int:
        """Zero means "any free port" to a listener and nothing at all to a caller."""
        return tcp_port_or_refuse(value, what="the mpd port")

    @field_validator("mpd_rewind_s")
    @classmethod
    def _an_overlap_rather_than_a_jump_forward(cls, value: float) -> float:
        """A negative overlap would start LATER than the house stopped, skipping what it did not
        hear, which is the one thing this setting must not be able to do. Zero is allowed and
        means no overlap at all, which is what music rather than speech wants."""
        if value < 0:
            message = f"refused: the mpd rewind must not be negative, not {value}"
            raise OptionsError(message, exit_code=ExitCode.REFUSED)
        return value

    @field_validator("hold_threshold_s")
    @classmethod
    def _a_hold_a_person_can_make(cls, value: float) -> float:
        """Below the floor an ordinary slow tap is read as a hold; above the ceiling a held key
        acts so late that the person has let go and pressed again."""
        if not HOLD_THRESHOLD_FLOOR_S <= value <= HOLD_THRESHOLD_CEILING_S:
            message = (
                f"refused: the hold threshold must be between {HOLD_THRESHOLD_FLOOR_S} and "
                f"{HOLD_THRESHOLD_CEILING_S} s, not {value}"
            )
            raise OptionsError(message, exit_code=ExitCode.REFUSED)
        return value

    @model_validator(mode="after")
    def _somewhere_for_every_file_it_names(self) -> ServiceOptionsInput:
        """Refused now rather than hours later, when the first speaker joins and nothing can save.

        All three paths, not just the state file, because each fails LATE and in its own way and
        none of them fails at startup on its own. A channel file in a directory that is not there
        reads exactly like a first run - the list is seeded in memory and the house plays - and
        the crash arrives on the first save. A switch file there cannot be turned off at all: the
        operator writes "off" into the path they meant while the service keeps reading the path
        they typed, where a missing file means on.

        The state file is checked first so that an argv with two bad directories refuses with the
        same message it always did.
        """
        for parent, what in (
            (self.state_file.parent, "write the state file in"),
            (self.channel_file.parent, "write the channel file in"),
            (self.switch_file.parent, "read the switch file from"),
        ):
            if not parent.is_dir():
                message = f"refused: {parent} is not a directory to {what}"
                raise OptionsError(message, exit_code=ExitCode.REFUSED)
        return self

    def record(self) -> ServiceOptions:
        """The validated settings as the frozen record the service runs on.

        Every check above has already passed, so the record's own ``__post_init__`` - which
        repeats the device id and the window for a caller that builds one directly - cannot
        refuse what reaches it here.
        """
        return ServiceOptions(
            bind_ip=self.bind_ip,
            device_id=self.device_id,
            switch_file=self.switch_file,
            state_file=self.state_file,
            channel_file=self.channel_file,
            registry_url=self.registry_url,
            consoles_allowed=self.consoles_allowed,
            dial_window_s=self.dial_window_s,
            hold_threshold_s=self.hold_threshold_s,
            registry_poll_s=self.registry_poll_s,
            switch_poll_s=self.switch_poll_s,
            unreachable_timeout_s=self.unreachable_timeout_s,
            channel_policy=self.channel_policy.record(),
            mpd_host=self.mpd_host,
            mpd_port=self.mpd_port,
            mpd_rewind_s=self.mpd_rewind_s,
        )


def merge_service_settings(*, configured: Mapping[str, Any], given: Mapping[str, Any]) -> dict[str, Any]:
    """The configuration layers first, then whatever was actually typed on top.

    A CLI value counts as typed when it is neither ``None`` nor an empty tuple: a repeatable option
    nobody used arrives as ``()``, and letting that overwrite a configured list would mean the
    command line could only ever add consoles and never leave the configured ones alone.
    """
    merged: dict[str, Any] = dict(configured)
    merged.update({key: value for key, value in given.items() if value is not None and value != ()})
    merged.setdefault("device_id", default_device_id())
    return merged


def parse_service_options(  # noqa: PLR0913 - one keyword per field; collapsing them is the untyped dict this avoids
    *,
    bind_ip: str | None,
    device_id: str | None,
    channel_file: str | None,
    switch_file: str | None,
    state_file: str | None,
    registry_url: str | None,
    allow_console: Sequence[str],
    unreachable_timeout_s: float | None,
    dial_window_s: float | None,
    mpd_host: str | None,
    mpd_port: int | None,
    mpd_rewind_s: float | None,
    configured: Mapping[str, Any],
) -> ServiceOptions:
    """Validate what the CLI and the config files between them said.

    Raises :class:`OptionsError` on a refusal, including the one refusal click used to make
    itself: a setting that has no default and was given nowhere.
    """
    given = {
        "bind_ip": bind_ip,
        "device_id": device_id,
        "channel_file": channel_file,
        "switch_file": switch_file,
        "state_file": state_file,
        "consoles_allowed": tuple(allow_console),
        "registry_url": registry_url,
        "unreachable_timeout_s": unreachable_timeout_s,
        "dial_window_s": dial_window_s,
        "mpd_host": mpd_host,
        "mpd_port": mpd_port,
        "mpd_rewind_s": mpd_rewind_s,
    }
    merged = merge_service_settings(configured=configured, given=given)
    try:
        return ServiceOptionsInput.model_validate(merged).record()
    except ValidationError as exc:
        raise _refusal(exc) from exc


def _refusal(exc: ValidationError) -> OptionsError:
    """A pydantic complaint turned into the sentence an operator can act on.

    A missing setting is named with the two places it can be put, because the whole point of the
    configuration layers is that the command line is no longer the only one and an error that
    only mentions the flag would send a reader back to the unit file.
    """
    missing = sorted({str(error["loc"][0]) for error in exc.errors() if error["type"] == "missing" and error["loc"]})
    if missing:
        names = ", ".join(missing)
        flags = " ".join(f"--{name.replace('_', '-')}" for name in missing)
        where = ", ".join(sorted(config_path_of(name) for name in missing))
        message = (
            f"refused: no value anywhere for {names}. "
            f"Give it on the command line ({flags}), or in a config file as {where} - "
            f"run `{service_command} config-deploy --target user` to write one - "
            f"or set {ENV_PREFIX}{env_name_of(missing[0])}."
        )
        return OptionsError(message, exit_code=ExitCode.ERROR)
    complaints = "; ".join(f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors())
    return OptionsError(f"refused: {complaints}", exit_code=ExitCode.ERROR)


def configured_settings(config: Config) -> dict[str, Any]:
    """What the config files said, with a line for anything in our sections that is not a setting.

    The record ignores an unknown key either way, which is the right behaviour for a house - a
    stray key must not stop the speakers working - but a misspelled setting that does nothing and
    says nothing is the kind of thing somebody debugs for an hour.
    """
    for stray in unknown_settings(config):
        section, _, key = stray.partition(".")
        log("config", f"ignored: [{section}] has no setting called {key!r}")
    return service_settings(config)
