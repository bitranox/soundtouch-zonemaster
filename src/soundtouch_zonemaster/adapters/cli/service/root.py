"""The service's group: argv over six config layers, one validated record, and the house held.

    uv run soundtouch-zonemaster-service --bind-ip 192.168.0.190 \
        --channel-file /var/lib/zonemaster/channels.json \
        --switch-file /var/lib/zonemaster/zone.switch \
        --state-file /var/lib/zonemaster/zone-state.json

Every setting may also live in a configuration file, and a value typed on the command line still
wins over all of them, so the line above keeps meaning exactly what it meant before there were any
files. The same run with nothing in argv works too, once ``config-deploy`` has written the five
settings that have no default:

    soundtouch-zonemaster-service config-deploy --target user     # write ~/.config/soundtouch-zonemaster/
    soundtouch-zonemaster-service config                          # what is merged, and from which file
    soundtouch-zonemaster-service                                 # hold the zone

Exit 0 it ran and finished, 1 it ran and the answer is no, 2 it could not run. **Stop it with
SIGINT, never SIGTERM**: the dissolve lives in a ``finally`` and the default handling of a TERM
does not run it, so a plain ``systemctl stop`` would leave real speakers in a zone whose master
has gone.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import lib_cli_exit_tools
import rich_click as click
from pydantic import BaseModel

from ....__init__conf__ import service_command, version
from ....application.outcome import ExitCode, OptionsError
from ....domain.logfn import ERROR_KIND
from ...config.errors import ConfigInputError
from ...logging.narration import LogRouting, log
from .. import safe_console
from ..boundary import configured_settings, parse_service_options
from ..context import Shared, config_for
from ..envelope import Envelope, OutputMode, report_crash, report_failure, write_envelope
from ..typed_click import option
from .config_cmd import cli_config
from .deploy_cmd import cli_config_deploy

if TYPE_CHECKING:
    from collections.abc import Callable

    from ....application.options import ServiceOptions
    from ....application.ports import RunService

__all__ = ["ServiceReport", "VersionReport", "cli", "service_options"]


class ServiceReport(BaseModel):
    """What this service was told to do, for a caller that reads JSON rather than the log."""

    bind_ip: str
    device_id: str
    channel_file: str
    registry_url: str
    switch_file: str
    state_file: str
    exit_code: int


class VersionReport(BaseModel):
    """Which release answered: the quick check of what a deploy actually landed."""

    version: str


def service_options(func: Callable[..., Any]) -> Callable[..., Any]:
    """Everything the operator can say to the service, as click options.

    Nothing is ``required`` any more, and every default is ``None`` rather than a value. That is
    what makes the precedence honest: ``None`` means "not typed", so the configuration layers get
    to answer, and anything typed overrides all of them. A setting that is missing everywhere is
    refused by name when the record is built, which is the same exit code click used to give.
    """
    for decorate in (
        option("--version", "show_version", is_flag=True, help="print the version and exit"),
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
            "--unreachable-timeout-s",
            type=float,
            default=None,
            help="seconds a speaker may say nothing before it stops counting as a member",
        ),
        option(
            "--allow-console",
            "allow_console",
            multiple=True,
            help="device id of a Lifestyle console that may join anyway (repeatable)",
        ),
        option("--registry-url", default=None, help="where the replacement service answers on the loopback"),
        option(
            "--mpd-rewind-s", default=None, type=float, help="how far back a channel starts when the house comes back"
        ),
        option("--mpd-port", default=None, type=int, help="the control port MPD answers on"),
        option("--mpd-host", default=None, help="where MPD answers, for channels whose sound it holds"),
        option("--state-file", default=None, help="what the zone is remembered in across a restart"),
        option("--switch-file", default=None, help="the file that says off; anything else means on"),
        option("--dial-window-s", default=None, type=float, help="how long digits are collected into one number"),
        option("--channel-file", default=None, help="the house channel list; seeded here on a first start"),
        option("--device-id", default=None, help="twelve hex digits; a MAC, as a speaker has"),
        option("--bind-ip", default=None, help="address the master serves on; the speakers must reach it"),
    ):
        func = decorate(func)
    return func


@click.group(invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@service_options
@click.pass_context
def cli(  # noqa: PLR0913 - a click callback's signature IS the option list; shortening it means an untyped dict
    ctx: click.Context,
    *,
    bind_ip: str | None,
    device_id: str | None,
    channel_file: str | None,
    switch_file: str | None,
    state_file: str | None,
    registry_url: str | None,
    allow_console: tuple[str, ...],
    unreachable_timeout_s: float | None,
    dial_window_s: float | None,
    mpd_host: str | None,
    mpd_port: int | None,
    mpd_rewind_s: float | None,
    profile: str | None,
    set_overrides: tuple[str, ...],
    as_json: bool,
    as_json_bare: bool,
    show_version: bool,
) -> None:
    """Hold the zone for the house: take in the speakers that belong, and leave the rest alone."""
    shared = Shared(
        mode=OutputMode.of(as_json=as_json, as_json_bare=as_json_bare), profile=profile, overrides=set_overrides
    )
    LogRouting.to_stderr = shared.mode.machine
    # Read before it is replaced: main.run hands the callback the run to perform as ``ctx.obj``,
    # and the group then puts what the subcommands need in the same slot.
    run_service = cast("RunService", ctx.obj)
    ctx.obj = shared
    if show_version:
        # A plain flag handled here rather than click's eager version option: that one runs
        # before --json is parsed, and could then only ever print prose.
        _report_version(mode=shared.mode)
        ctx.exit(ExitCode.OK)
    if ctx.invoked_subcommand is not None:
        return
    try:
        options = parse_service_options(
            bind_ip=bind_ip,
            device_id=device_id,
            channel_file=channel_file,
            switch_file=switch_file,
            state_file=state_file,
            registry_url=registry_url,
            allow_console=allow_console,
            unreachable_timeout_s=unreachable_timeout_s,
            dial_window_s=dial_window_s,
            mpd_host=mpd_host,
            mpd_port=mpd_port,
            mpd_rewind_s=mpd_rewind_s,
            configured=configured_settings(config_for(shared).config),
        )
    except OptionsError as exc:
        report_failure(exc, command=service_command, mode=shared.mode)
        ctx.exit(exc.exit_code)
    except ConfigInputError as exc:
        report_failure(exc, command=service_command, mode=shared.mode)
        ctx.exit(ExitCode.ERROR)
    try:
        rc = asyncio.run(run_service(options))
    except (KeyboardInterrupt, lib_cli_exit_tools.CliSignalError):
        # A signal is how this is ENDED, not a failure. lib_cli_exit_tools installs the handlers
        # that raise here, and its exception is a RuntimeError, so it would otherwise fall into
        # the generic branch below and report "could not run" for a shutdown that ran correctly -
        # measured on the real unit 2026-09-07, where the zone dissolved and systemd still marked
        # the service failed and sent the OnFailure mail.
        rc = ExitCode.OK
    except Exception as exc:  # noqa: BLE001 - CLI edge
        log(ERROR_KIND, f"{type(exc).__name__}: {exc}")
        report_crash(exc, command=service_command, mode=shared.mode)
        ctx.exit(ExitCode.ERROR)
    if shared.mode.machine:
        _report(options, rc, mode=shared.mode)
    ctx.exit(rc)


def _report_version(*, mode: OutputMode) -> None:
    """The version, as an envelope in machine mode and as one line otherwise."""
    if mode.machine:
        write_envelope(
            Envelope[VersionReport](ok=True, command=service_command, data=VersionReport(version=version)), mode=mode
        )
    else:
        safe_console.echo(f"{service_command} {version}")


def _report(options: ServiceOptions, rc: int, *, mode: OutputMode) -> None:
    """The envelope, on stdout, where nothing else has been written in this mode."""
    report = ServiceReport(
        bind_ip=options.bind_ip,
        device_id=options.device_id,
        channel_file=str(options.channel_file),
        registry_url=options.registry_url,
        switch_file=str(options.switch_file),
        state_file=str(options.state_file),
        exit_code=rc,
    )
    write_envelope(Envelope[ServiceReport](ok=rc == ExitCode.OK, command=service_command, data=report), mode=mode)


cli.add_command(cli_config)
cli.add_command(cli_config_deploy)
