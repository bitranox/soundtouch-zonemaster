"""``prefs``: the house preferences a person may change while the service runs (``domain/preferences.py``).

It opens the database WITHOUT the writer lock, like ``switch``: a preference is changed on a running
service, which reads the table again within a switch poll. A value is parsed as JSON - the rule
``--set`` and the environment layer already follow - so a list can be typed from a shell, and it is
checked by the same rule a config file is, BEFORE anything is written. A refused change writes
nothing and opens nothing.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, NoReturn

import rich_click as click
from pydantic import BaseModel

from ....__init__conf__ import service_command
from ....application.errors import StoreError
from ....application.outcome import ExitCode, OptionsError
from ....domain.preferences import (
    PreferenceName,
    PreferenceNotJsonError,
    PreferenceRefusedError,
    PreferenceRow,
    PreferenceSource,
    checked,
    decoded,
    plain_value,
    quoted,
    resolved,
    shown,
    value_of,
)
from ...config.errors import ConfigInputError
from ...config.settings_map import CONFIGURABLE_NAMES
from .. import safe_console
from ..boundary import configured_settings, layered_preferences
from ..context import config_for, shared_of
from ..envelope import Envelope, report_failure, write_envelope
from ..typed_click import argument
from .store_cmd import open_store_or_exit

if TYPE_CHECKING:
    from ....domain.preferences import HousePreferences
    from ..context import Shared

__all__ = ["IgnoredRow", "PrefChange", "PreferenceView", "PrefsReport", "cli_prefs"]

CONFIGURATION = "configuration"
"""The source a preference has when no stored row decides it."""

TAKEN_IN = "a running service reads it within about a second"
"""When a change reaches a running service: its preference watch reads the table every switch poll."""

CONSOLE_TAKEN_IN = (
    "a running service reads it within about a second; a console no longer allowed is let go at the next pass, "
    "one newly allowed is watched from the next registry read (registry.poll_s) and taken in when it next wakes"
)
"""The console list is read as fast as the rest, but a console first allowed is not watched until the registry
is read again, and one already awake is not taken in until it wakes again - so the note says so."""


class PreferenceView(BaseModel):
    """One preference as ``prefs`` reports it: its value, and who is deciding it right now."""

    name: str
    value: Any
    source: str
    """``configuration``, or the stored row's source (calibration, cli, app)."""
    changed_at: str | None


class IgnoredRow(BaseModel):
    """A stored row nobody could use, and why - never dropped silently."""

    name: str
    text: str
    why: str


class PrefsReport(BaseModel):
    """Every preference, as ``prefs`` (no verb) reports them."""

    database: str
    preferences: list[PreferenceView]
    ignored: list[IgnoredRow]


class PrefChange(BaseModel):
    """What ``set`` or ``unset`` did: the value before and after, and the database it touched."""

    database: str
    name: str
    before: PreferenceView
    after: PreferenceView
    note: str


def _view(name: PreferenceName, layered: HousePreferences, rows: tuple[PreferenceRow, ...]) -> PreferenceView:
    """One preference as it stands: the stored row when a usable one decides it, else the layers."""
    resolution = resolved(layered, rows)
    row = resolution.set_by.get(name)
    return PreferenceView(
        name=str(name),
        value=plain_value(value_of(resolution.preferences, name)),
        source=CONFIGURATION if row is None else row.source,
        changed_at=None if row is None else row.changed_at,
    )


def _refuse(ctx: click.Context, shared: Shared, *, command: str, message: str, code: ExitCode) -> NoReturn:
    """Report a refusal and end the invocation. Typed ``NoReturn`` so a caller needs no dead
    ``raise`` or ``return`` after it for pyright to see every path covered - the same idiom
    ``click.Context.exit`` itself uses, one level up."""
    report_failure(OptionsError(message, exit_code=code), command=command, mode=shared.mode)
    ctx.exit(code)


def _name_or_refuse(ctx: click.Context, shared: Shared, *, command: str, name: str) -> PreferenceName:
    try:
        return PreferenceName(name)
    except ValueError:
        if name in CONFIGURABLE_NAMES:
            _refuse(
                ctx,
                shared,
                command=command,
                message=f"refused: {name} is not a preference; set it in a config file",
                code=ExitCode.ERROR,
            )
        known = ", ".join(PreferenceName)
        _refuse(
            ctx,
            shared,
            command=command,
            message=f"refused: no preference called {quoted(name)}; the preferences are: {known}",
            code=ExitCode.ERROR,
        )


def _layered_or_exit(ctx: click.Context, shared: Shared, *, command: str) -> HousePreferences:
    try:
        return layered_preferences(configured_settings(config_for(shared).config))
    except OptionsError as exc:
        report_failure(exc, command=command, mode=shared.mode)
        ctx.exit(exc.exit_code)
    except ConfigInputError as exc:
        report_failure(exc, command=command, mode=shared.mode)
        ctx.exit(ExitCode.ERROR)


@click.group("prefs", invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.pass_context
def cli_prefs(ctx: click.Context) -> None:
    """The house preferences: every one, its value, and who set it. `set` and `unset` change one.

    "configuration" is the config layers plus any `--set`. It cannot see the flags a running service
    was started with (`--allow-console`, `--dial-window-s`, `--mpd-rewind-s`), which beat the
    layers for that run; a stored row beats them all.
    """
    if ctx.invoked_subcommand is not None:
        return
    shared = shared_of(ctx)
    command = f"{service_command} prefs"
    layered = _layered_or_exit(ctx, shared, command=command)
    store = open_store_or_exit(ctx, shared, exclusive=False, command=command)
    try:
        rows = store.load_preferences()
        where = store.where
    except StoreError as exc:
        report_failure(exc, command=command, mode=shared.mode)
        ctx.exit(ExitCode.ERROR)
        return
    finally:
        store.close()
    report = PrefsReport(
        database=where,
        preferences=[_view(name, layered, rows) for name in PreferenceName],
        ignored=[IgnoredRow(name=row.name, text=row.text, why=why) for row, why in resolved(layered, rows).rejected],
    )
    if shared.mode.machine:
        write_envelope(Envelope[PrefsReport](ok=True, command=command, data=report), mode=shared.mode)
        return
    for view in report.preferences:
        origin = (
            CONFIGURATION
            if view.changed_at is None
            else f"database: {view.source}, {view.changed_at or 'time not recorded'}"
        )
        safe_console.echo(f"{view.name} = {json.dumps(view.value)}    # {origin}")
    for row in report.ignored:
        # Cut and escaped for the terminal only: the JSON above keeps the raw text whole.
        safe_console.echo(f"# ignored: {shown(row.name)} = {shown(row.text)} ({row.why})")


@cli_prefs.command("set", context_settings={"help_option_names": ["-h", "--help"]})
@argument("name")
@argument("value")
@click.pass_context
def cli_prefs_set(ctx: click.Context, *, name: str, value: str) -> None:
    """Set one preference in the house database; it beats every config layer until it is unset."""
    shared = shared_of(ctx)
    command = f"{service_command} prefs set"
    preference = _name_or_refuse(ctx, shared, command=command, name=name)
    try:
        typed = decoded(value)
    except PreferenceNotJsonError:
        _refuse(
            ctx,
            shared,
            command=command,
            message=(
                f"refused: {quoted(value)} is not JSON; a number is written as it is, a list as '[\"AABBCC000012\"]'"
            ),
            code=ExitCode.ERROR,
        )
    try:
        checked_value = checked(preference, typed)
    except PreferenceRefusedError as exc:
        _refuse(ctx, shared, command=command, message=str(exc), code=ExitCode.REFUSED)
    layered = _layered_or_exit(ctx, shared, command=command)
    store = open_store_or_exit(ctx, shared, exclusive=False, command=command)
    try:
        before = _view(preference, layered, store.load_preferences())
        store.set_preference(preference, checked_value, source=PreferenceSource.CLI)
        after = _view(preference, layered, store.load_preferences())
        where = store.where
    except StoreError as exc:
        report_failure(exc, command=command, mode=shared.mode)
        ctx.exit(ExitCode.ERROR)
        return
    finally:
        store.close()
    _report_change(
        shared,
        command=command,
        change=PrefChange(
            database=where,
            name=str(preference),
            before=before,
            after=after,
            note=_when_taken_in(preference),
        ),
    )


@cli_prefs.command("unset", context_settings={"help_option_names": ["-h", "--help"]})
@argument("name")
@click.pass_context
def cli_prefs_unset(ctx: click.Context, *, name: str) -> None:
    """Remove one preference from the house database, so the config layers decide it again."""
    shared = shared_of(ctx)
    command = f"{service_command} prefs unset"
    preference = _name_or_refuse(ctx, shared, command=command, name=name)
    layered = _layered_or_exit(ctx, shared, command=command)
    store = open_store_or_exit(ctx, shared, exclusive=False, command=command)
    try:
        before = _view(preference, layered, store.load_preferences())
        removed = store.unset_preference(preference)
        after = _view(preference, layered, store.load_preferences())
        where = store.where
    except StoreError as exc:
        report_failure(exc, command=command, mode=shared.mode)
        ctx.exit(ExitCode.ERROR)
        return
    finally:
        store.close()
    note = "nothing was set" if removed is None else _when_taken_in(preference)
    _report_change(
        shared,
        command=command,
        change=PrefChange(database=where, name=str(preference), before=before, after=after, note=note),
    )


def _when_taken_in(preference: PreferenceName) -> str:
    return CONSOLE_TAKEN_IN if preference is PreferenceName.CONSOLES else TAKEN_IN


def _report_change(shared: Shared, *, command: str, change: PrefChange) -> None:
    if shared.mode.machine:
        write_envelope(Envelope[PrefChange](ok=True, command=command, data=change), mode=shared.mode)
        return
    safe_console.echo(
        f"{change.database}: {change.name} = {json.dumps(change.after.value)} ({change.after.source}), "
        f"was {json.dumps(change.before.value)} ({change.before.source}); {change.note}"
    )
