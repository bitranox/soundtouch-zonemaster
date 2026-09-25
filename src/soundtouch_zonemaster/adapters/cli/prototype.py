"""Run a software zone master against one or more real SoundTouch speakers. AUDIBLE.

    uv run python -m soundtouch_zonemaster \
        --bind-ip 192.168.0.190 --slave 192.168.0.31 --preset-from 192.168.0.31 --preset 1 \
        --duration 60 [--switch-after 30 --preset2 2] [--encryption none|obfuscated] \
        [--late-slave 192.168.0.34 --join-after 30 --join-mode schedule|restart]

Prototype: it proves (or refutes) that a speaker accepts a non-Bose master and unobfuscated data,
and (late join) that a speaker joining a running stream lands in sync. ``--join-mode schedule`` is
the S6 rule (the joiner starts at the byte the zone reaches 3 s later); ``restart`` is the control
arm, the stream restarted for everyone at the join, with its one-second gap.
Exit 0 finished, 1 refused or nothing to play to, 2 error. Ctrl-C dissolves the zone first.

**One refusal here is not about the option set at all**, and that is why it is at this edge rather
than on the record: an address this house never touches is a fact about the flat, read from
``[prototype] never_touch`` in the configuration. It was a constant in the source until this
rebuild, and the wheel now ships it EMPTY: which box must be left alone is known only to the house
it stands in, so a house names it in a layer of its own (service-host uses its host layer).
``--profile`` and ``--set`` are the whole of what the prototype gained with it, and they are the
only difference between this command's help and the archive's.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import lib_cli_exit_tools
import rich_click as click
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from ...__init__conf__ import shell_command
from ...application.options import Options, default_device_id
from ...application.outcome import ExitCode, OptionsError, device_id_or_refuse
from ...domain.enums import Encryption, JoinMode
from ...domain.logfn import ERROR_KIND
from ...domain.speakers import ProtectedSpeaker, first_protected
from ..config.errors import ConfigInputError
from ..config.settings_map import prototype_settings
from ..logging.narration import LogRouting, log
from .context import Shared, config_for
from .envelope import Envelope, OutputMode, report_crash, report_failure, write_envelope
from .typed_click import current_context, option

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from lib_layered_config import Config

    from ...application.ports import RunZone

__all__ = [
    "OptionsInput",
    "PrototypeSettings",
    "RunReport",
    "cli",
    "never_touch_of",
    "parse_options",
    "zone_options",
]


class PrototypeSettings(BaseModel):
    """What a config file says to the prototype, which is one thing: what never to touch.

    The default is the shipped ``80-prototype.toml`` value, and ``tests/test_config.py`` fails if
    the two ever disagree - the same one-source-of-truth check every service setting gets. It is
    empty: a wheel cannot know which box in somebody's flat must not be woken, so the house that
    has one names it in its own layer.
    """

    model_config = ConfigDict(frozen=True)

    never_touch: tuple[ProtectedSpeaker, ...] = ()


def never_touch_of(config: Config) -> tuple[ProtectedSpeaker, ...]:
    """The addresses this house never touches, as the configuration layers delivered them.

    A malformed entry is a :class:`ConfigInputError` like every other way a configuration can be
    wrong, so the command catches one type and is complete. It is NOT allowed to fall through as a
    pydantic error: this list is the thing standing between a typo and a speaker somebody is
    listening to, and a run that could not read it must not proceed as though the list were empty.
    """
    try:
        return PrototypeSettings.model_validate(prototype_settings(config)).never_touch
    except ValidationError as exc:
        message = f"refused: [prototype] never_touch: {exc}"
        raise ConfigInputError(message) from exc


class OptionsInput(BaseModel):
    """One run of the zone master as argv delivered it, before it is the record.

    The archive's ``Options`` model, field for field, minus two things that moved: the wire
    lookup for the encryption is the SoundTouch adapter's (``adapters/soundtouch/wire.py``), and
    the console refusal is now a configured list checked in :func:`parse_options`.
    """

    model_config = ConfigDict(frozen=True)

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

    @field_validator("device_id")
    @classmethod
    def _twelve_hex_digits(cls, value: str) -> str:
        return device_id_or_refuse(value)

    def record(self) -> Options:
        """The validated option set as the frozen record a run is handed."""
        return Options(
            bind_ip=self.bind_ip,
            device_id=self.device_id,
            slaves=self.slaves,
            preset_from=self.preset_from,
            preset=self.preset,
            preset2=self.preset2,
            switch_after=self.switch_after,
            duration=self.duration,
            encryption=self.encryption,
            late_slaves=self.late_slaves,
            join_after=self.join_after,
            join_mode=self.join_mode,
            ignore_selects=self.ignore_selects,
        )


def zone_options(func: Callable[..., Any]) -> Callable[..., Any]:
    """Everything the operator can say, as click options.

    The ``choices`` come from the enums, so they cannot drift apart from what the program accepts.
    A decorator rather than a parser object because the command lives in ``__main__`` while the
    validated record lives here, and this is what keeps both halves in one place.
    """
    for decorate in (
        option(
            "--ignore-selects",
            is_flag=True,
            help="accept a slave's preset press and do nothing: a measurement run, not a feature",
        ),
        option("--json-bare", "as_json_bare", is_flag=True, help="as --json, on one line for jq"),
        option("--json", "as_json", is_flag=True, help="log to stderr and print a JSON envelope on stdout"),
        option(
            "--set",
            "set_overrides",
            multiple=True,
            metavar="SECTION.KEY=VALUE",
            help="override one configuration value; repeatable, still below a command-line option",
        ),
        option("--profile", default=None, help="read the profile/<name>/ config tree instead of the base one"),
        option(
            "--join-mode",
            type=click.Choice([m.value for m in JoinMode]),
            default=JoinMode.SCHEDULE.value,
            show_default=True,
            help="schedule places the joiner on the running stream; restart restarts it for everyone",
        ),
        option("--join-after", type=float, default=30.0, show_default=True, help="seconds before a --late-slave joins"),
        option(
            "--late-slave",
            "late_slaves",
            multiple=True,
            help="slave that joins after --join-after seconds (repeatable)",
        ),
        option(
            "--encryption",
            type=click.Choice([e.value for e in Encryption]),
            default=Encryption.NONE.value,
            show_default=True,
            help="how the data channel is obfuscated; the speakers accept none",
        ),
        option(
            "--duration", type=float, default=60.0, show_default=True, help="seconds to keep playing before stopping"
        ),
        option(
            "--switch-after",
            type=float,
            default=0.0,
            show_default=True,
            help="seconds after start to switch to --preset2",
        ),
        option("--preset2", type=int, default=2, show_default=True, help="preset to switch to at --switch-after"),
        option("--preset", type=int, default=1, show_default=True, help="preset number to start on"),
        option("--preset-from", required=True, help="speaker whose presets provide the station"),
        option("--slave", "slaves", multiple=True, required=True, help="slave speaker IP (repeatable)"),
        option("--device-id", default=default_device_id, help="twelve hex digits; a MAC, as a speaker has"),
        option("--bind-ip", required=True, help="address the master serves on; the speakers must reach it"),
    ):
        func = decorate(func)
    return func


def parse_options(  # noqa: PLR0913 - one keyword per Options field; collapsing them is the untyped dict this module exists to avoid
    *,
    bind_ip: str,
    device_id: str,
    slaves: Sequence[str],
    preset_from: str,
    preset: int,
    preset2: int,
    switch_after: float,
    duration: float,
    encryption: str,
    late_slaves: Sequence[str],
    join_after: float,
    join_mode: str,
    ignore_selects: bool,
    never_touch: Sequence[ProtectedSpeaker],
) -> Options:
    """Validate what the CLI collected. Raises :class:`OptionsError` on a refusal.

    This is the single place the program turns loose CLI values into a checked record; pydantic
    coerces and validates from here, so every consumer of the result is fully typed.

    ``never_touch`` has no default on purpose. It was a constant in the archive, and a default
    here would let a caller forget the one refusal that protects a room somebody is in.
    """
    options = OptionsInput.model_validate(
        {
            "bind_ip": bind_ip,
            "device_id": device_id,
            "slaves": tuple(slaves),
            "preset_from": preset_from,
            "preset": preset,
            "preset2": preset2,
            "switch_after": switch_after,
            "duration": duration,
            "encryption": encryption,
            "late_slaves": tuple(late_slaves),
            "join_after": join_after,
            "join_mode": join_mode,
            "ignore_selects": ignore_selects,
        }
    ).record()
    protected = first_protected((*options.slaves, *options.late_slaves), never_touch)
    if protected is not None:
        message = f"refused: {protected.name} ({protected.ip}) is {protected.why}"
        raise OptionsError(message, exit_code=ExitCode.REFUSED)
    return options


class RunReport(BaseModel):
    """What one run of the master did, for a caller that reads JSON rather than the log."""

    bind_ip: str
    device_id: str
    slaves: list[str]
    late_slaves: list[str]
    station_preset: int
    duration_s: float
    join_mode: str
    exit_code: int


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@zone_options
def cli(  # noqa: PLR0913 - a click callback's signature IS the option list; shortening it means an untyped dict
    *,
    bind_ip: str,
    device_id: str,
    slaves: tuple[str, ...],
    preset_from: str,
    preset: int,
    preset2: int,
    switch_after: float,
    duration: float,
    encryption: str,
    late_slaves: tuple[str, ...],
    join_after: float,
    join_mode: str,
    ignore_selects: bool,
    profile: str | None,
    set_overrides: tuple[str, ...],
    as_json: bool,
    as_json_bare: bool,
) -> None:
    """Hold a software zone master against one or more real SoundTouch speakers. AUDIBLE."""
    mode = OutputMode.of(as_json=as_json, as_json_bare=as_json_bare)
    LogRouting.to_stderr = mode.machine
    ctx = current_context()
    # Read before anything replaces it: ``ctx.obj`` is how main.run hands the callback the run it
    # is supposed to perform, which is the seam the run-loop tests substitute.
    run_zone = cast("RunZone", ctx.obj)
    try:
        shared = Shared(mode=mode, profile=profile, overrides=set_overrides)
        options = parse_options(
            bind_ip=bind_ip,
            device_id=device_id,
            slaves=slaves,
            preset_from=preset_from,
            preset=preset,
            preset2=preset2,
            switch_after=switch_after,
            duration=duration,
            encryption=encryption,
            late_slaves=late_slaves,
            join_after=join_after,
            join_mode=join_mode,
            ignore_selects=ignore_selects,
            never_touch=never_touch_of(config_for(shared).config),
        )
    except OptionsError as exc:
        report_failure(exc, command=shell_command, mode=mode)
        ctx.exit(exc.exit_code)
    except ConfigInputError as exc:
        report_failure(exc, command=shell_command, mode=mode)
        ctx.exit(ExitCode.ERROR)
    try:
        rc = asyncio.run(run_zone(options))
    except (KeyboardInterrupt, lib_cli_exit_tools.CliSignalError):
        # A signal is how this is ENDED, not a failure. lib_cli_exit_tools installs the handlers
        # that raise here, and its exception is a RuntimeError, so it would otherwise fall into
        # the generic branch below and report "could not run" for a shutdown that ran correctly -
        # measured on the real unit 2026-09-07, where the zone dissolved and systemd still marked
        # the service failed and sent the OnFailure mail.
        rc = ExitCode.OK
    except Exception as exc:  # noqa: BLE001 - CLI edge
        log(ERROR_KIND, f"{type(exc).__name__}: {exc}")
        report_crash(exc, command=shell_command, mode=mode)
        ctx.exit(ExitCode.ERROR)
    if mode.machine:
        report = RunReport(
            bind_ip=options.bind_ip,
            device_id=options.device_id,
            slaves=list(options.slaves),
            late_slaves=list(options.late_slaves),
            station_preset=options.preset,
            duration_s=options.duration,
            join_mode=options.join_mode.value,
            exit_code=rc,
        )
        write_envelope(Envelope[RunReport](ok=rc == ExitCode.OK, command=shell_command, data=report), mode=mode)
    ctx.exit(rc)
