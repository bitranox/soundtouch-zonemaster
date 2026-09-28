"""``config``: the merged configuration, and which layer and file each value came from.

For the house preferences it also names the live source: a value set in the house database
(``prefs``) is shown over the file value it replaces, which stays listed beneath it.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any

import rich_click as click
from lib_layered_config import REDACTED_PLACEHOLDER, Layer
from pydantic import BaseModel

from ....__init__conf__ import service_command
from ....application.errors import StoreError, StoreMissingError
from ....application.outcome import ExitCode, OptionsError
from ....domain.database_url import masked as masked_database_url
from ...config.display import (
    flatten,
    line_beneath,
    mask_database_preferences,
    mask_database_settings,
    mask_values_from_layer,
    mask_values_from_private_files,
    overlay_preferences,
    shows_a_preference,
    where,
)
from ...config.errors import ConfigInputError
from ...config.settings_map import CONFIGURABLE_NAMES
from ...logging.narration import log_on_stderr
from ..context import config_for, named_database, shared_of, store_opener_of
from ..envelope import Envelope, report_failure, write_envelope
from ..typed_click import option

if TYPE_CHECKING:
    from ....application.ports import HouseStore
    from ....domain.logfn import LogFn
    from ....domain.preferences import PreferenceRow
    from ..context import Shared

__all__ = ["ConfigReport", "cli_config"]


class ConfigReport(BaseModel):
    """The merged configuration and, per dotted key, the layer and file that produced it."""

    profile: str | None
    config: dict[str, Any]
    provenance: dict[str, Any]
    database_note: str | None = None
    """What went wrong with a house database that is named: it does not exist, could not be read,
    or was read but not closed. ``None`` when there is nothing to say, including when none is named.
    With ``--redact`` it names neither the database nor an error that would quote it."""


_SET_BY_HAND: dict[str, Any] = {"layer": "--set", "path": None}
"""The origin of a value a ``--set`` wrote, since the layers below no longer decide it."""


def _say_nothing(_kind: str, _text: str) -> None:
    """The narrator ``config`` hands the database choice.

    Choosing the database reads the layers the way the store verbs do, and that read narrates a
    stray key and a configured password left out - on STDOUT in the human mode, which here is the
    view itself. ``config`` shows every key already, so a line about one is only noise in it. The
    store gets :func:`~...logging.narration.log_on_stderr` instead: what it says about its own
    work is news, and belongs where it is heard without landing in the view.
    """


@click.command("config", context_settings={"help_option_names": ["-h", "--help"]})
@option("--section", "only", default=None, help="show one section instead of all of them")
@option("--redact", is_flag=True, help="mask secret-looking keys AND everything from a .env or a private -rnhome file")
@click.pass_context
def cli_config(ctx: click.Context, *, only: str | None, redact: bool) -> None:
    """Show the merged configuration, and which layer and file each value came from.

    This is the view of the FILES, with one exception, and it is not quite the view the service
    runs on: an option typed on the command line beats every layer shown here, and the service's
    own ``--json`` envelope is what reports the values it actually used. The exception is a house
    preference set in the house database (``prefs set``), which beats the files, the environment
    and the command line alike: it is shown as the value, with the file value it replaces beneath
    it. The database is read only when a preference is in the view, and never created; one that
    cannot be read costs this view one line, never its answer.

    Reading this from inside a checkout also shows the checkout's private ``-rnhome`` override
    files and any gitignored ``.env`` (walking up from the working directory for one is the
    library's default). ``--redact`` masks everything from either, and everything the house
    database holds, and is what to reach for before pasting the output anywhere.
    """
    shared = shared_of(ctx)
    try:
        merged = config_for(shared)
        values = flatten(merged.config.as_dict(redact=redact), prefix=only, known_names=CONFIGURABLE_NAMES)
    except ConfigInputError as exc:
        report_failure(exc, command=f"{service_command} config", mode=shared.mode)
        ctx.exit(ExitCode.ERROR)
    provenance: dict[str, Any] = {
        key: _SET_BY_HAND if key in merged.overridden else merged.config.origin(key) for key, _ in values
    }
    # Unconditional and ahead of --redact: the database password, and a password inside a database
    # URL, can come from any layer, not only from a .env or a private file, so both are masked in
    # every output mode this command has.
    values = mask_database_settings(values, mask=REDACTED_PLACEHOLDER)
    database_note: str | None = None
    if shows_a_preference(values):
        rows, database_note = _house_rows(ctx, shared, redact=redact)
        values, provenance = overlay_preferences(values, provenance, rows)
    if redact:
        # The library masks by key NAME, which leaves a harmless-looking one like `e2e_host` in
        # clear. A `.env` holds what is true of ONE machine, so with --redact none of it is shown.
        values = mask_values_from_layer(values, provenance, layer=Layer.DOTENV.value, mask=REDACTED_PLACEHOLDER)
        # A private override file is per-machine in the same way, but it reports the DEFAULTS layer.
        values = mask_values_from_private_files(values, provenance, mask=REDACTED_PLACEHOLDER)
        # And so is the house database: what a row decides, and what it overrides.
        values, provenance = mask_database_preferences(values, provenance, mask=REDACTED_PLACEHOLDER)
    if shared.mode.machine:
        report = ConfigReport(
            profile=shared.profile,
            config=dict(values),
            provenance={key: origin for key, origin in provenance.items() if origin is not None},
            database_note=database_note,
        )
        write_envelope(
            Envelope[ConfigReport](ok=True, command=f"{service_command} config", data=report), mode=shared.mode
        )
        return
    if only is not None and not values:
        # Printing nothing here would be the silence the refusal was written to avoid, and this is
        # the case an operator meets most: the scopes that describe one machine are empty until
        # somebody configures that machine.
        sys.stdout.write(f"{only}: nothing in any layer sets it. It is a name this program reads.\n")
        return
    for key, value in values:
        sys.stdout.write(f"{key} = {json.dumps(value)}    # {where(provenance.get(key))}\n")
        beneath = line_beneath(key, provenance.get(key))
        if beneath is not None:
            sys.stdout.write(f"{beneath}\n")
    if database_note is not None:
        sys.stdout.write(f"# {database_note}\n")


def _redacting_log(*, where: str, redact: bool) -> LogFn:
    """The store's own narration, on stderr, with the database location masked too under --redact.

    Nothing on this path narrates today - ``load_preferences`` and ``close`` say nothing of their
    own - but a future line (a migration brought up to date, a future read) must not defeat
    --redact by naming the very database it was asked to hide (OPEN-WORK rank 218)."""
    if not redact:
        return log_on_stderr

    def _log(kind: str, text: str) -> None:
        log_on_stderr(kind, text.replace(where, REDACTED_PLACEHOLDER))

    return _log


def _house_rows(ctx: click.Context, shared: Shared, *, redact: bool) -> tuple[tuple[PreferenceRow, ...], str | None]:
    """The stored preference rows, or none and the one sentence that says why.

    A database is looked for only when one is named - typed, or a non-empty ``database.url`` in
    some layer (:func:`~..context.named_database` decides it, once) - so a view with none named
    prints exactly what it always printed. Opened the way ``prefs`` opens it, without the writer
    lock so this works beside a running service, but never created, and closed at once. A database
    that cannot be read costs this view one line, never its answer.
    """
    try:
        choice = named_database(shared, narrate=_say_nothing)
    except (ConfigInputError, OptionsError) as exc:
        return (), _not_read(exc, redact=redact)
    if choice is None:
        return (), None
    try:
        store_log = _redacting_log(where=masked_database_url(choice.setting), redact=redact)
        store = store_opener_of(ctx)(choice.setting, password=choice.password, log=store_log)
        store.open(exclusive=False, create=False)
    except StoreMissingError:
        where = "" if redact else f" {masked_database_url(choice.setting)}"
        return (), f"no preference is stored: the house database{where} does not exist"
    except StoreError as exc:
        return (), _not_read(exc, redact=redact)
    return _read_and_closed(store, redact=redact)


def _read_and_closed(store: HouseStore, *, redact: bool) -> tuple[tuple[PreferenceRow, ...], str | None]:
    """The rows, and the store closed. A close that fails after a good read keeps the rows: what was
    read is the answer, and the failure is the one line (a lock release, a descriptor that would
    not close - ``close`` lets an ``OSError`` through as well as a ``StoreError``)."""
    try:
        rows, note = store.load_preferences(), None
    except StoreError as exc:
        rows, note = (), _not_read(exc, redact=redact)
    try:
        store.close()
    except (StoreError, OSError) as exc:
        if note is None:
            note = f"the house database was read but not closed ({_reason(exc, redact=redact)})"
    return rows, note


def _not_read(exc: Exception, *, redact: bool) -> str:
    """The sentence for a database that is named but could not be read."""
    return f"the house database was not read ({_reason(exc, redact=redact)}); a preference set there is not shown"


def _reason(exc: Exception, *, redact: bool) -> str:
    """The error in words, or with ``--redact`` its kind alone.

    Every store error opens with the database it is about, and a driver's own words (PostgreSQL's
    above all) name the host again; the listing masks a ``database.url`` from a private file or a
    ``.env``, so this line must not print it back. The kind of failure is kept, which is what says
    whether to look at the file, the server or the setting."""
    return f"{type(exc).__name__}, the rest hidden by --redact" if redact else str(exc)
