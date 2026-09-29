"""The questions about the house only the installed service's own interpreter can answer.

``install_service.py`` and ``deploy_service.py`` run under ``uv run`` with three dependencies of
their own, and the package is not among them: on a fresh machine it is not installed yet, and
installing it is what they do. But whether the switch is set, which members the zone holds, and
where the configured database even IS are answers that belong to the package - its config layers,
its store, its schema. So those two ask this script, run with the SERVICE VENV's python
(``/opt/zonemaster/.venv/bin/python service_venv.py ...``), and read its JSON envelope. It is
shipped beside them and needs ``_click.py`` beside it too.

Which database: the one the service would open - ``database.url`` through the same six config
layers, read by the same function the service's own store verbs use - and only when no layer names
one, the installer's default path (``--default``). There is no ``--database`` option on purpose: a
typed path is how a store verb once answered a typo with a new, empty database.

The verbs:

``seed-switch``
    Make sure a first start finds the switch OFF, and change no switch anybody set. Only a database
    this run CREATES is seeded: a database that exists without a switch row reads as ON to the
    service, and it has been running that way, so writing OFF there would stand a house down that
    somebody is using. An old ``zone.switch`` beside a new database is the operator's word from
    before the database, and that word goes in instead of OFF.
``show``
    The switch and the members the zone holds, read without the store: a database with no house
    schema is reported as not there and left without one (``--dry-run`` changes nothing), and a
    switch that cannot be read is refused rather than read as ON.
``set-switch on|off [--if-changed-at STAMP]``
    Set it, in a database that already holds a house schema, and answer with the ``changed_at``
    the row holds afterwards. With ``--if-changed-at`` it is set only while the row still holds
    exactly that stamp, in one statement, so the check and the write cannot be separated by
    another writer: that is how a deploy puts back the switch it turned off without overriding a
    ``switch off`` somebody ran in the minutes between (``written`` says which happened).
``backup --to DIR``
    A consistent copy through SQLite's backup API, which is safe while the service writes (a
    plain copy of a WAL database can miss what is still in the ``-wal`` file). A PostgreSQL
    database is never copied here; the report says to take a ``pg_dump``.
``distributions``
    Every distribution installed in this interpreter, which is how a deploy proves the venv holds
    exactly one of this program's.

Which package answers: the one installed in that venv, whichever version it is. A deploy asks
``show``, ``backup``, ``set-switch`` and ``distributions`` BEFORE it installs the new wheel, so
those run against the package it is about to replace - 0.5.2 on the house's machine as this is
written - and only ``seed-switch`` (the installer's, after the wheel is in) and the checks after
the install meet the new one. So everything imported at the top here, and every verb but
``seed-switch``, uses only what 0.5.2 already has; the two things the house database gained since
(``HouseDatabase.probe`` and ``schema_exists``) are asked for by name and stood in for when the
installed package predates them (``probe_of``, ``holds_a_house_schema``).

Exit codes are the house's: 0 done, 1 refused because the database is held by another writer,
2 it could not run.
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import rich_click as click
from _click import argument, current_context, option, run_cli
from pydantic import BaseModel
from sqlalchemy import inspect, select, update
from sqlalchemy.exc import SQLAlchemyError

from soundtouch_zonemaster.adapters.cli.context import Shared, named_database
from soundtouch_zonemaster.adapters.cli.envelope import OutputMode
from soundtouch_zonemaster.adapters.config.errors import ConfigInputError
from soundtouch_zonemaster.adapters.files import house_db
from soundtouch_zonemaster.adapters.files.house_db import HouseDatabase, database_url, reason_for
from soundtouch_zonemaster.adapters.files.house_schema import SWITCH
from soundtouch_zonemaster.adapters.files.house_state import read_state
from soundtouch_zonemaster.adapters.files.house_switch import ON, read_switch, write_switch
from soundtouch_zonemaster.adapters.files.switch_file import Switch
from soundtouch_zonemaster.application.errors import StoreBusyError, StoreError, StoreMissingError
from soundtouch_zonemaster.application.outcome import OptionsError
from soundtouch_zonemaster.domain.database_url import masked
from soundtouch_zonemaster.domain.state import ZoneState
from soundtouch_zonemaster.domain.switch import OFF

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence
    from contextlib import AbstractContextManager

    from sqlalchemy.engine import Connection, Engine

    from soundtouch_zonemaster.domain.secret import Secret

COMMAND = "service_venv"

EXIT_OK, EXIT_BUSY, EXIT_ERROR = 0, 1, 2

_REFUSALS = (ConfigInputError, OptionsError, StoreError, OSError, sqlite3.Error)
"""Everything a verb here can meet that must end in the envelope rather than a traceback."""


def _narrate(kind: str, text: str) -> None:
    """Narration goes to stderr, so stdout carries the envelope and nothing else."""
    sys.stderr.write(f"{kind}: {text}\n")


class Chosen(NamedTuple):
    """The database a verb works on, and whether a config layer named it or the default stood in."""

    setting: str
    password: Secret | None
    configured: bool


def configured_database(*, default: Path) -> Chosen:
    """The database the service would open, else ``default``; with the password the layers give.

    ``named_database`` is the store verbs' own reading of the layers, so this and the service
    cannot come to disagree about which database the configuration names.
    """
    shared = Shared(mode=OutputMode(machine=True, indent=None), profile=None, overrides=())
    choice = named_database(shared, narrate=_narrate)
    if choice is None:
        return Chosen(str(default), None, configured=False)
    return Chosen(choice.setting, choice.password, configured=True)


class SeedReport(BaseModel):
    """What ``seed-switch`` found and did."""

    database: str
    created: bool
    """Whether this run found no house database there and made one."""
    switch: str
    """``on``, ``off``, or ``unset`` (no row: the service reads that as on)."""
    written: bool
    configured: bool
    """Whether a config layer named the database. When none did, the default stood in, and the
    service - which has no default of its own - will not open it until ``database.url`` names it."""


class ShowReport(BaseModel):
    database: str
    backend: str
    exists: bool
    on: bool
    members: list[str]


class SwitchReport(BaseModel):
    database: str
    on: bool
    changed: bool
    written: bool
    """Whether this call wrote the row. Always, without ``--if-changed-at``; with it, only when the
    row still held that stamp."""
    changed_at: str | None
    """The row's stamp after the call, ``None`` when there is no row: what a later
    ``--if-changed-at`` names to mean "only if nobody has set it since"."""


class BackupReport(BaseModel):
    database: str
    backend: str
    backup: str | None
    note: str


class DistributionsReport(BaseModel):
    names: list[str]


def probe_of(house: HouseDatabase) -> AbstractContextManager[Connection | None]:
    """The house database's own read that migrates nothing and creates no file.

    :meth:`HouseDatabase.probe` when the installed package has it. A package from before it
    (0.5.2 and older, which is what a deploy reads the house through before it installs) gets
    :func:`_probe_before_it_existed`, the same read built from that package's own engine.
    """
    if hasattr(house, "probe"):
        return house.probe()
    return _probe_before_it_existed(house)


@contextmanager
def _probe_before_it_existed(house: HouseDatabase) -> Generator[Connection | None]:
    """What ``HouseDatabase.probe`` does, for a package that does not have it yet.

    Its engine comes from that package's own ``_build_engine``, the one ``open`` connects
    through, rather than from connect arguments repeated here: a copy is what drifts, and this
    only ever runs against a package already released, whose private method can no longer
    change under it. A package without even that is refused, never guessed at. A SQLite file that
    is not there is answered without connecting, because connecting would create it; anything the
    database library raises leaves as the one ``StoreError``, so a failed read is never an answer.
    """
    if house.url.get_backend_name() == "sqlite" and not Path(str(house.url.database)).exists():
        yield None
        return
    build: Callable[[], Engine] | None = getattr(house, "_build_engine", None)
    if build is None:
        message = f"{house.where}: the installed package has no way to read its database without migrating it"
        raise StoreError(message)
    try:
        engine = build()
    except (SQLAlchemyError, ImportError) as exc:
        message = f"{house.where}: could not be read ({reason_for(exc)})"
        raise StoreError(message) from exc
    try:
        with engine.connect() as connection:
            yield connection
    except SQLAlchemyError as exc:
        message = f"{house.where}: could not be read ({reason_for(exc)})"
        raise StoreError(message) from exc
    finally:
        engine.dispose()


def holds_a_house_schema(connection: Connection) -> bool:
    """Whether the house schema is there: the installed package's ``schema_exists``, or its rule.

    A package from before ``schema_exists`` gets the rule it states - Alembic's version table,
    which every version of the service writes in the transaction that creates its schema.
    """
    own: Callable[[Connection], bool] | None = getattr(house_db, "schema_exists", None)
    if own is not None:
        return own(connection)
    return inspect(connection).has_table("alembic_version")


def house_schema_exists(setting: str, password: Secret | None) -> bool:
    """Whether the database already holds a house schema. Creates nothing, migrates nothing.

    Asked BEFORE the store opens it, because opening is what creates the file and brings the
    schema to head, after which a new database and an old one look the same. Goes through
    :func:`probe_of`, the house's own non-migrating read, rather than a connection built here: a
    house database is asked about itself only one way.
    """
    house = HouseDatabase(setting, password=password)
    with probe_of(house) as connection:
        return connection is not None and holds_a_house_schema(connection)


def seed_word(*, schema_existed: bool, legacy_switch: bool | None) -> bool | None:
    """The switch a new database starts with, or ``None`` for leave it alone. Pure.

    Only a database that had no house schema before this run is seeded; an old switch file's word
    beats the OFF default, because it is what the operator last said.
    """
    if schema_existed:
        return None
    return False if legacy_switch is None else legacy_switch


def seed_switch(*, default: Path, legacy_switch_file: Path | None) -> SeedReport:
    """Make sure a first start finds the switch off, and change no switch anybody set.

    ``house.open(create=True, seed=...)`` carries the switch in the SAME transaction that creates
    the schema, on a database this run finds truly new, so an interrupt anywhere in between leaves
    no schema behind rather than a schema with no switch row - the next run still sees a database
    that was never created and seeds it again. The one case that hook cannot reach is a schema
    another process created between the freshness probe above and this call's own ``open()``: no
    migration then runs here, so its transaction never carries anything, and the switch is written
    in one of this call's own - but only if that other process left none, because a switch already
    there is the operator's, not a race to paper over.
    """
    setting, password, configured = configured_database(default=default)
    existed = house_schema_exists(setting, password)
    legacy = None
    if legacy_switch_file is not None and legacy_switch_file.exists():
        legacy = Switch(legacy_switch_file, log=_narrate).is_on()
    word = seed_word(schema_existed=existed, legacy_switch=legacy)
    written = False
    seed: Callable[[Connection], None] | None = None
    if word is not None:
        owed = word

        def _seed(connection: Connection) -> None:
            nonlocal written
            write_switch(connection, on=owed)
            written = True

        seed = _seed

    house = HouseDatabase(setting, password=password)
    try:
        house.open(exclusive=False, create=True, seed=seed)
        if word is not None and not written:
            with house.writing() as connection:
                if read_switch(connection) is None:
                    write_switch(connection, on=word)
                    written = True
        with house.reading() as connection:
            held = read_switch(connection)
    except SQLAlchemyError as exc:
        message = f"{house.where}: {reason_for(exc)}"
        raise StoreError(message) from exc
    finally:
        house.close()
    shown = "unset" if held is None else ("on" if held else "off")
    return SeedReport(database=house.where, created=not existed, switch=shown, written=written, configured=configured)


def show(*, default: Path) -> ShowReport:
    """The switch and the members, read without the store: nothing is created, migrated or assumed.

    Two things the store does are right for the service and wrong here. Opening it migrates, so a
    ``--dry-run`` would create the schema of a database that had none. And its switch read answers
    ON when the read fails, so that a lost database cannot silently stop the house; but a deploy
    takes this answer as the switch it hands back at the end, and a failed read that said ON
    turned a house somebody had switched off back on. So a read that fails is refused here.
    """
    setting, password, _configured = configured_database(default=default)
    backend = database_url(setting).get_backend_name()
    where = masked(setting)
    house = HouseDatabase(setting, password=password)
    with probe_of(house) as connection:
        if connection is None or not holds_a_house_schema(connection):
            return ShowReport(database=where, backend=backend, exists=False, on=True, members=[])
        held = read_switch(connection)
        state = read_state(connection) or ZoneState()
    on = True if held is None else held
    return ShowReport(database=where, backend=backend, exists=True, on=on, members=list(state.members))


def switch_stamp(connection: Connection) -> str | None:
    """When the switch row was last written, as the row records it; ``None`` when there is no row."""
    stamp = connection.scalar(select(SWITCH.c.changed_at).where(SWITCH.c.id == 1))
    return None if stamp is None else str(stamp)


def write_switch_if_unchanged(connection: Connection, *, on: bool, changed_at: str) -> bool:
    """Set the switch only while its row still holds ``changed_at``; whether it did. The caller holds the transaction.

    ONE conditional UPDATE rather than a read and then a write: under READ COMMITTED a ``switch
    off`` could land between the two, and the write would then undo it - which is exactly what
    this exists to prevent. PostgreSQL re-checks the condition against a row another transaction
    changed while this one waited for it, and SQLite has one writer at a time, so on both a row
    somebody set since is left alone. Built from the installed package's own table and words,
    which a deploy's recovery meets as the OLD package: all three are in every release that has a
    house database.
    """
    stamp = datetime.now(UTC).isoformat()
    result = connection.execute(
        update(SWITCH)
        .where(SWITCH.c.id == 1, SWITCH.c.changed_at == changed_at)
        .values(word=ON if on else OFF, changed_at=stamp)
    )
    return result.rowcount == 1


def set_switch(*, default: Path, on: bool, if_changed_at: str | None = None) -> SwitchReport:
    """Set it and read it back in one transaction, in a database that already holds a house schema.

    A database without one is refused rather than opened, because opening would create it: only
    the service and the installer do that. With ``if_changed_at`` it is set only while the row
    still holds that stamp (:func:`write_switch_if_unchanged`), and the report says whether it was.
    """
    setting, password, _configured = configured_database(default=default)
    if not house_schema_exists(setting, password):
        message = f"{masked(setting)}: holds no house database (only the service and the installer create one)"
        raise StoreMissingError(message)
    house = HouseDatabase(setting, password=password)
    house.open(exclusive=False, create=False)
    try:
        with house.writing() as connection:
            before = read_switch(connection)
            if if_changed_at is None:
                written = True
                write_switch(connection, on=on)
            else:
                written = write_switch_if_unchanged(connection, on=on, changed_at=if_changed_at)
            held = read_switch(connection)
            stamp = switch_stamp(connection)
    except SQLAlchemyError as exc:
        message = f"{house.where}: {reason_for(exc)}"
        raise StoreError(message) from exc
    finally:
        house.close()
    was_on = True if before is None else before
    now_on = True if held is None else held
    return SwitchReport(database=house.where, on=now_on, changed=was_on != now_on, written=written, changed_at=stamp)


def backup(*, default: Path, to: Path) -> BackupReport:
    """Copy a SQLite house database with the backup API, or say what to do for PostgreSQL."""
    setting, _password, _configured = configured_database(default=default)
    url = database_url(setting)
    where = masked(setting)
    backend = url.get_backend_name()
    if backend != "sqlite":
        note = f"{where}: a server database is not copied here; take a pg_dump of it before the deploy"
        return BackupReport(database=where, backend=backend, backup=None, note=note)
    source = Path(str(url.database))
    if not source.exists():
        return BackupReport(database=where, backend=backend, backup=None, note=f"{where}: not there, nothing to copy")
    to.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = to / f"{source.stem}-{stamp}{source.suffix}"
    if target.exists():
        message = f"{target}: already exists; a backup never writes over another"
        raise FileExistsError(message)
    with closing(sqlite3.connect(source)) as original, closing(sqlite3.connect(target)) as copy:
        original.backup(copy)
    return BackupReport(database=where, backend=backend, backup=str(target), note="copied with the SQLite backup API")


def distributions() -> DistributionsReport:
    """One name per installed distribution, duplicates kept: two copies of one is what a deploy looks for."""
    names = sorted(str(dist.metadata["Name"]) for dist in metadata.distributions())
    return DistributionsReport(names=names)


Report = SeedReport | ShowReport | SwitchReport | BackupReport | DistributionsReport


class Envelope(BaseModel):
    ok: bool = True
    command: str
    data: Report


class ErrorEnvelope(BaseModel):
    ok: bool = False
    command: str
    error: str
    message: str


def _indent() -> int | None:
    return None if bool(current_context().find_root().params.get("as_json_bare")) else 2


def _answer(verb: str, produce: Callable[[], Report]) -> None:
    """Print the envelope for what ``produce`` returned, or the refusal for what it raised."""
    ctx = current_context()
    command = f"{COMMAND} {verb}"
    indent = _indent()
    try:
        report = produce()
    except _REFUSALS as exc:
        sys.stderr.write(f"{exc}\n")
        refusal = ErrorEnvelope(command=command, error=type(exc).__name__, message=str(exc))
        sys.stdout.write(refusal.model_dump_json(indent=indent) + "\n")
        ctx.exit(EXIT_BUSY if isinstance(exc, StoreBusyError) else EXIT_ERROR)
    sys.stdout.write(Envelope(command=command, data=report).model_dump_json(indent=indent) + "\n")
    ctx.exit(EXIT_OK)


_DEFAULT_HELP = "the database when no config layer names one (the installer's own path)"


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@option("--json-bare", "as_json_bare", is_flag=True, help="as --json, on one line for jq")
@option("--json", "as_json", is_flag=True, help="the envelope indented (it is always printed)")
def cli(*, as_json: bool, as_json_bare: bool) -> None:
    """Ask the installed service's own interpreter about the house database. Always prints JSON."""
    del as_json, as_json_bare  # read by _indent from the root context


@cli.command("seed-switch")
@option("--legacy-switch-file", default=None, help="the old zone.switch, whose word a new database takes")
@option("--default", "default", required=True, help=_DEFAULT_HELP)
def cli_seed(*, default: str, legacy_switch_file: str | None) -> None:
    """Make sure a first start finds the switch off. Never changes a switch somebody set."""
    legacy = None if legacy_switch_file is None else Path(legacy_switch_file)
    _answer("seed-switch", lambda: seed_switch(default=Path(default), legacy_switch_file=legacy))


@cli.command("show")
@option("--default", "default", required=True, help=_DEFAULT_HELP)
def cli_show(*, default: str) -> None:
    """The switch and the zone's members, from the configured database."""
    _answer("show", lambda: show(default=Path(default)))


@cli.command("set-switch")
@argument("word", type=click.Choice(["on", "off"]))
@option(
    "--if-changed-at",
    "if_changed_at",
    default=None,
    help="set it only while the switch row still holds this changed_at (what an earlier set-switch answered)",
)
@option("--default", "default", required=True, help=_DEFAULT_HELP)
def cli_set_switch(*, word: str, default: str, if_changed_at: str | None) -> None:
    """Set the switch in a database that exists."""
    _answer("set-switch", lambda: set_switch(default=Path(default), on=word == "on", if_changed_at=if_changed_at))


@cli.command("backup")
@option("--to", "to", required=True, help="the directory the copy goes into")
@option("--default", "default", required=True, help=_DEFAULT_HELP)
def cli_backup(*, to: str, default: str) -> None:
    """A consistent copy of a SQLite house database; a pg_dump note for PostgreSQL."""
    _answer("backup", lambda: backup(default=Path(default), to=Path(to)))


@cli.command("distributions")
def cli_distributions() -> None:
    """Every distribution installed in this interpreter."""
    _answer("distributions", distributions)


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(cli, argv=argv, prog_name=COMMAND)


if __name__ == "__main__":
    sys.exit(main())
