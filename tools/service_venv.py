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
``set-switch on|off``
    Set it, in a database that already holds a house schema.
``backup --to DIR``
    A consistent copy through SQLite's backup API, which is safe while the service writes (a
    plain copy of a WAL database can miss what is still in the ``-wal`` file). A PostgreSQL
    database is never copied here; the report says to take a ``pg_dump``.
``distributions``
    Every distribution installed in this interpreter, which is how a deploy proves the venv holds
    exactly one of this program's.

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
from typing import TYPE_CHECKING, override

import rich_click as click
from _click import argument, current_context, option, run_cli
from pydantic import BaseModel
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import SQLAlchemyError

from soundtouch_zonemaster.adapters.cli.context import Shared, named_database
from soundtouch_zonemaster.adapters.cli.envelope import OutputMode
from soundtouch_zonemaster.adapters.config.errors import ConfigInputError
from soundtouch_zonemaster.adapters.files.house_db import HouseDatabase, database_url, reason_for
from soundtouch_zonemaster.adapters.files.house_state import read_state
from soundtouch_zonemaster.adapters.files.house_switch import read_switch, write_switch
from soundtouch_zonemaster.adapters.files.switch_file import Switch
from soundtouch_zonemaster.application.errors import StoreBusyError, StoreError, StoreMissingError
from soundtouch_zonemaster.application.outcome import OptionsError
from soundtouch_zonemaster.domain.database_url import masked
from soundtouch_zonemaster.domain.state import ZoneState

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence

    from sqlalchemy.engine import Connection

    from soundtouch_zonemaster.domain.secret import Secret

COMMAND = "service_venv"

EXIT_OK, EXIT_BUSY, EXIT_ERROR = 0, 1, 2

_POSTGRES_PROBE_TIMEOUT_S = 5
"""How long the freshness probe waits for a PostgreSQL server, the store's own connect bound."""

_REFUSALS = (ConfigInputError, OptionsError, StoreError, OSError, sqlite3.Error)
"""Everything a verb here can meet that must end in the envelope rather than a traceback."""


def _narrate(kind: str, text: str) -> None:
    """Narration goes to stderr, so stdout carries the envelope and nothing else."""
    sys.stderr.write(f"{kind}: {text}\n")


def configured_database(*, default: Path) -> tuple[str, Secret | None]:
    """The database the service would open, else ``default``; with the password the layers give.

    ``named_database`` is the store verbs' own reading of the layers, so this and the service
    cannot come to disagree about which database the configuration names.
    """
    shared = Shared(mode=OutputMode(machine=True, indent=None), profile=None, overrides=())
    choice = named_database(shared, narrate=_narrate)
    if choice is None:
        return str(default), None
    return choice.setting, choice.password


class SeedReport(BaseModel):
    """What ``seed-switch`` found and did."""

    database: str
    created: bool
    """Whether this run found no house database there and made one."""
    switch: str
    """``on``, ``off``, or ``unset`` (no row: the service reads that as on)."""
    written: bool


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


class BackupReport(BaseModel):
    database: str
    backend: str
    backup: str | None
    note: str


class DistributionsReport(BaseModel):
    names: list[str]


@contextmanager
def _read_only(setting: str, password: Secret | None) -> Generator[Connection | None]:
    """A plain connection that creates nothing and migrates nothing; ``None`` for a missing SQLite file.

    Not the store: opening the store brings any database it reaches up to head, which on
    PostgreSQL - where no file tells an empty database from a missing one - creates the schema.
    A question about the house must leave the house as it found it, so it is asked here. A SQLite
    file that is not there is answered without connecting, because connecting would create it.
    ``HouseDatabase`` is built only for its reading of the setting (and its refusals); it never
    opens. Anything the database library raises leaves as the one ``StoreError``.
    """
    house = HouseDatabase(setting, password=password)
    connect_args: dict[str, object] = {}
    if house.url.get_backend_name() == "sqlite":
        if not Path(str(house.url.database)).exists():
            yield None
            return
    else:
        connect_args["connect_timeout"] = _POSTGRES_PROBE_TIMEOUT_S
        if password is not None:
            connect_args["password"] = password.reveal()
    engine = create_engine(house.url, connect_args=connect_args)
    try:
        with engine.connect() as connection:
            yield connection
    except SQLAlchemyError as exc:
        message = f"{house.where}: could not be read ({reason_for(exc)})"
        raise StoreError(message) from exc
    finally:
        engine.dispose()


def _holds_a_house_schema(connection: Connection) -> bool:
    """Alembic's version table, which the first open of any version of the service writes."""
    return inspect(connection).has_table("alembic_version")


def house_schema_exists(setting: str, password: Secret | None) -> bool:
    """Whether the database already holds a house schema. Creates nothing.

    Asked BEFORE the store opens it, because opening is what creates the file and brings the
    schema to head, after which a new database and an old one look the same.
    """
    with _read_only(setting, password) as connection:
        return connection is not None and _holds_a_house_schema(connection)


def seed_word(*, schema_existed: bool, legacy_switch: bool | None) -> bool | None:
    """The switch a new database starts with, or ``None`` for leave it alone. Pure.

    Only a database that had no house schema before this run is seeded; an old switch file's word
    beats the OFF default, because it is what the operator last said.
    """
    if schema_existed:
        return None
    return False if legacy_switch is None else legacy_switch


class _CreatedWithItsSwitch(HouseDatabase):
    """A house database whose FIRST write transaction also carries the switch a seed owes it.

    On a database this run creates, that first transaction is the one ``open()`` brings the schema
    to head in, so the schema and the switch are committed together or not at all. Two
    transactions left a window: a seed cut off between them - a Ctrl-C, a dropped ``pct exec``
    session, a full disk - left a schema with no switch row, the next run took that for a database
    somebody had been using and left it alone, and the first start read it as ON and took the
    house. An interrupted run now leaves no schema, so the next one still sees a new database.

    When the schema was already there (another process created it between the freshness probe and
    ``open()``), the first write transaction is the seed's own, and the owed switch is written
    there only if that transaction did not write one itself.
    """

    def __init__(self, setting: str, *, password: Secret | None, owed: bool | None) -> None:
        super().__init__(setting, password=password)
        self._owed = owed
        self.written = False

    @property
    def owes_a_switch(self) -> bool:
        return self._owed is not None

    @override
    @contextmanager
    def writing(self) -> Generator[Connection]:
        with super().writing() as connection:
            yield connection
            if self._owed is not None:
                if read_switch(connection) is None:
                    write_switch(connection, on=self._owed)
                    self.written = True
                self._owed = None


def seed_switch(*, default: Path, legacy_switch_file: Path | None) -> SeedReport:
    setting, password = configured_database(default=default)
    existed = house_schema_exists(setting, password)
    legacy = None
    if legacy_switch_file is not None and legacy_switch_file.exists():
        legacy = Switch(legacy_switch_file, log=_narrate).is_on()
    word = seed_word(schema_existed=existed, legacy_switch=legacy)
    house = _CreatedWithItsSwitch(setting, password=password, owed=word)
    house.open(exclusive=False, create=True)
    try:
        if house.owes_a_switch:
            # open() found the schema already at head and wrote nothing, so no transaction has
            # carried the switch yet: an empty one of the seed's own does, as it ends.
            with house.writing():
                pass
        with house.reading() as connection:
            held = read_switch(connection)
    except SQLAlchemyError as exc:
        message = f"{house.where}: {reason_for(exc)}"
        raise StoreError(message) from exc
    finally:
        house.close()
    shown = "unset" if held is None else ("on" if held else "off")
    return SeedReport(database=house.where, created=not existed, switch=shown, written=house.written)


def show(*, default: Path) -> ShowReport:
    """The switch and the members, read without the store: nothing is created, migrated or assumed.

    Two things the store does are right for the service and wrong here. Opening it migrates, so a
    ``--dry-run`` would create the schema of a database that had none. And its switch read answers
    ON when the read fails, so that a lost database cannot silently stop the house; but a deploy
    takes this answer as the switch it hands back at the end, and a failed read that said ON
    turned a house somebody had switched off back on. So a read that fails is refused here.
    """
    setting, password = configured_database(default=default)
    backend = database_url(setting).get_backend_name()
    where = masked(setting)
    with _read_only(setting, password) as connection:
        if connection is None or not _holds_a_house_schema(connection):
            return ShowReport(database=where, backend=backend, exists=False, on=True, members=[])
        held = read_switch(connection)
        state = read_state(connection) or ZoneState()
    on = True if held is None else held
    return ShowReport(database=where, backend=backend, exists=True, on=on, members=list(state.members))


def set_switch(*, default: Path, on: bool) -> SwitchReport:
    """Set it and read it back in one transaction, in a database that already holds a house schema.

    A database without one is refused rather than opened, because opening would create it: only
    the service and the installer do that.
    """
    setting, password = configured_database(default=default)
    if not house_schema_exists(setting, password):
        message = f"{masked(setting)}: holds no house database (only the service and the installer create one)"
        raise StoreMissingError(message)
    house = HouseDatabase(setting, password=password)
    house.open(exclusive=False, create=False)
    try:
        with house.writing() as connection:
            changed = write_switch(connection, on=on)
            held = read_switch(connection)
    except SQLAlchemyError as exc:
        message = f"{house.where}: {reason_for(exc)}"
        raise StoreError(message) from exc
    finally:
        house.close()
    return SwitchReport(database=house.where, on=True if held is None else held, changed=changed)


def backup(*, default: Path, to: Path) -> BackupReport:
    """Copy a SQLite house database with the backup API, or say what to do for PostgreSQL."""
    setting, _password = configured_database(default=default)
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
@option("--default", "default", required=True, help=_DEFAULT_HELP)
def cli_set_switch(*, word: str, default: str) -> None:
    """Set the switch in a database that exists."""
    _answer("set-switch", lambda: set_switch(default=Path(default), on=word == "on"))


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
