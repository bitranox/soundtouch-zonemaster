"""``switch`` and ``channels``: the house database, read and changed from the command line.

``switch`` and ``channels export`` open the store WITHOUT the writer lock, so they work while the
service runs. That is the point of the switch: turning the house off is done to a running
service. ``channels import`` takes the lock and is refused while the service holds it (exit 1),
because the service keeps the list in memory and would write its own copy over the import on its
next change.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import rich_click as click
from pydantic import BaseModel

from ....__init__conf__ import service_command
from ....application.errors import StoreBusyError, StoreError
from ....application.outcome import ExitCode, OptionsError
from ...logging.narration import log
from .. import safe_console
from ..context import database_for, shared_of, store_opener_of
from ..envelope import Envelope, report_failure, write_envelope
from ..typed_click import argument, option

if TYPE_CHECKING:
    from ....application.ports import HouseStore
    from ..context import Shared

__all__ = ["ChannelsReport", "SwitchReport", "cli_channels", "cli_switch"]


class SwitchReport(BaseModel):
    """The switch, read back after any change this invocation made."""

    database: str
    on: bool
    changed: bool


class ChannelsReport(BaseModel):
    """What an export or an import did: how many channels, and the file involved."""

    database: str
    channels: int
    path: str


def _open(ctx: click.Context, shared: Shared, *, exclusive: bool, command: str) -> HouseStore:
    """The store for this invocation, opened, or the refusal reported and the context exited."""
    try:
        database = database_for(shared)
        store = store_opener_of(ctx)(database, log=log)
        store.open(exclusive=exclusive)
    except OptionsError as exc:
        report_failure(exc, command=command, mode=shared.mode)
        ctx.exit(exc.exit_code)
    except StoreBusyError as exc:
        report_failure(exc, command=command, mode=shared.mode)
        ctx.exit(ExitCode.REFUSED)
    except StoreError as exc:
        report_failure(exc, command=command, mode=shared.mode)
        ctx.exit(ExitCode.ERROR)
    return store


@click.command("switch", context_settings={"help_option_names": ["-h", "--help"]})
@argument("word", required=False, type=click.Choice(["on", "off"]))
@click.pass_context
def cli_switch(ctx: click.Context, *, word: str | None) -> None:
    """Show the switch, or set it: off stands the house down, on lets the service take it in again."""
    shared = shared_of(ctx)
    command = f"{service_command} switch"
    store = _open(ctx, shared, exclusive=False, command=command)
    try:
        changed = False if word is None else store.set_switch(on=word == "on")
        where = store.where
        report = SwitchReport(database=where, on=store.is_on(), changed=changed)
    finally:
        store.close()
    if shared.mode.machine:
        write_envelope(Envelope[SwitchReport](ok=True, command=command, data=report), mode=shared.mode)
        return
    safe_console.echo(f"{where}: switch {'on' if report.on else 'off'}{' (changed)' if report.changed else ''}")


@click.group("channels", context_settings={"help_option_names": ["-h", "--help"]})
def cli_channels() -> None:
    """The house's channel list: export it to a file a person can edit, and import it back."""


@cli_channels.command("export", context_settings={"help_option_names": ["-h", "--help"]})
@option("--output", "output", required=True, help="the file to write the channel list to")
@click.pass_context
def cli_channels_export(ctx: click.Context, *, output: str) -> None:
    """Write the channel list as the JSON document a person reads and repairs."""
    shared = shared_of(ctx)
    command = f"{service_command} channels export"
    store = _open(ctx, shared, exclusive=False, command=command)
    try:
        text = store.export_channels()
        count = len(store.load_channels().channels)
        where = store.where
    except StoreError as exc:
        report_failure(exc, command=command, mode=shared.mode)
        ctx.exit(ExitCode.ERROR)
    finally:
        store.close()
    try:
        Path(output).write_text(text, encoding="utf-8")
    except OSError as exc:
        report_failure(exc, command=command, mode=shared.mode)
        ctx.exit(ExitCode.ERROR)
    _report_channels(shared, command=command, report=ChannelsReport(database=where, channels=count, path=output))


@cli_channels.command("import", context_settings={"help_option_names": ["-h", "--help"]})
@argument("path")
@click.pass_context
def cli_channels_import(ctx: click.Context, *, path: str) -> None:
    """Replace the channel list with a file's. Refused while the service runs; stop it first."""
    shared = shared_of(ctx)
    command = f"{service_command} channels import"
    store = _open(ctx, shared, exclusive=True, command=command)
    try:
        count = len(store.import_channels(Path(path)).channels)
        where = store.where
    except StoreError as exc:
        report_failure(exc, command=command, mode=shared.mode)
        ctx.exit(ExitCode.ERROR)
    finally:
        store.close()
    _report_channels(
        shared, command=command, report=ChannelsReport(database=where, channels=count, path=path), announce=False
    )


def _report_channels(shared: Shared, *, command: str, report: ChannelsReport, announce: bool = True) -> None:
    """Report an export or an import. ``report.database`` is already the redacted display name, so
    the envelope's own field agrees with the human line by construction; ``announce`` also puts it
    ahead of the human line - ``channels import`` passes ``False``, so its own human output is
    unchanged."""
    if shared.mode.machine:
        write_envelope(Envelope[ChannelsReport](ok=True, command=command, data=report), mode=shared.mode)
        return
    named = f"{report.database}: " if announce else ""
    safe_console.echo(f"{named}{report.channels} channel(s): {report.path}")
