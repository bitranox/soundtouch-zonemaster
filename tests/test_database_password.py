"""The database password: a setting in every configuration layer, and a secret everywhere it goes.

The value used throughout is a fake (``TOPSECRET``). Every layer the service reads is exercised
through the real ``main`` with a real argv, the run substituted through ``main``'s own parameter,
so what is asserted is the whole way from a file, a variable or a ``--set`` into the record.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from nothing_typed import NOTHING_TYPED

from soundtouch_zonemaster.adapters.cli.boundary import parse_service_options
from soundtouch_zonemaster.adapters.config.loader import ENV_PREFIX
from soundtouch_zonemaster.application.options import ServiceOptions
from soundtouch_zonemaster.application.outcome import OptionsError
from soundtouch_zonemaster.application.zone_service import ZoneService
from soundtouch_zonemaster.composition import build_production, open_house_store
from soundtouch_zonemaster.domain.secret import Secret
from soundtouch_zonemaster.entry import service_main as main

if TYPE_CHECKING:
    from pathlib import Path

    from soundtouch_zonemaster.application.ports import HouseStore, OpenHouseStore, RunService
    from soundtouch_zonemaster.domain.logfn import LogFn

FAKE = "TOPSECRET"
PASSWORD_ENV = f"{ENV_PREFIX}DATABASE__PASSWORD"


def _capture() -> tuple[list[ServiceOptions], RunService]:
    """A stand-in run that records what it was handed instead of holding a zone."""
    seen: list[ServiceOptions] = []

    async def run(options: ServiceOptions) -> int:
        seen.append(options)
        return 0

    return seen, run


def _user_config(root: Path, body: str) -> Path:
    path = root / "xdg" / "soundtouch-zonemaster" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _run_with(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *argv: str) -> ServiceOptions:
    """One service start with a SQLite database and an address given, the record it was handed.

    The database is CONFIGURED rather than typed: a configured password goes only with the
    configured database, so a typed one would take the password out of what these tests watch."""
    seen, run = _capture()
    monkeypatch.setenv(f"{ENV_PREFIX}DATABASE__URL", str(tmp_path / "zonemaster.sqlite"))
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "--bind-ip", "10.0.0.1", *argv])
    assert main(run_service=run) == 0
    return seen[0]


def test_no_password_anywhere_is_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert _run_with(monkeypatch, tmp_path).database_password is None


def test_the_password_from_a_config_file_reaches_the_record(
    monkeypatch: pytest.MonkeyPatch, isolated_config_layers: Path, tmp_path: Path
) -> None:
    _user_config(isolated_config_layers, f'[database]\npassword = "{FAKE}"\n')
    assert _run_with(monkeypatch, tmp_path).database_password == Secret(FAKE)


def test_the_password_from_the_environment_reaches_the_record(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(PASSWORD_ENV, FAKE)
    assert _run_with(monkeypatch, tmp_path).database_password == Secret(FAKE)


def test_the_password_from_a_dotenv_reaches_the_record(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The service keeps its dotenv layer: a ``.env`` beside where it is started is read."""
    home = tmp_path / "somewhere-with-a-dotenv"
    home.mkdir()
    (home / ".env").write_text(f"DATABASE__PASSWORD={FAKE}\n", encoding="utf-8")
    monkeypatch.chdir(home)
    assert _run_with(monkeypatch, tmp_path).database_password == Secret(FAKE)


def test_the_password_from_a_set_override_reaches_the_record_and_beats_a_file(
    monkeypatch: pytest.MonkeyPatch, isolated_config_layers: Path, tmp_path: Path
) -> None:
    _user_config(isolated_config_layers, '[database]\npassword = "from-the-file"\n')
    options = _run_with(monkeypatch, tmp_path, "--set", f"database.password={FAKE}")
    assert options.database_password == Secret(FAKE)


def test_the_environment_beats_a_file(
    monkeypatch: pytest.MonkeyPatch, isolated_config_layers: Path, tmp_path: Path
) -> None:
    _user_config(isolated_config_layers, '[database]\npassword = "from-the-file"\n')
    monkeypatch.setenv(PASSWORD_ENV, FAKE)
    assert _run_with(monkeypatch, tmp_path).database_password == Secret(FAKE)


def test_an_empty_password_is_no_password() -> None:
    """Empty means the driver gets nothing, so libpq's own ~/.pgpass, PGPASSFILE and PGPASSWORD
    still apply; it is the same as not setting it at all."""
    options = parse_service_options(
        bind_ip="10.0.0.1", database="db.sqlite", configured={"database_password": ""}, **NOTHING_TYPED
    )
    assert options.database_password is None


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(12345, id="digits-read-as-a-number"),
        pytest.param(1.5, id="read-as-a-float"),
        pytest.param(True, id="read-as-a-boolean"),
        pytest.param(["a"], id="read-as-a-list"),
    ],
)
def test_a_password_that_arrived_as_something_other_than_text_is_refused_without_its_value(value: object) -> None:
    """The environment layer reads ``12345`` as a number and ``true`` as a boolean, so a password
    of digits would reach the driver changed (a leading zero lost) if it were turned back into
    text. It is refused instead, naming the setting and saying to quote it, and never the value."""
    with pytest.raises(OptionsError) as caught:
        parse_service_options(
            bind_ip="10.0.0.1",
            database=None,
            configured={"database": "db.sqlite", "database_password": value},
            **NOTHING_TYPED,
        )
    message = str(caught.value)
    assert "database.password" in message
    assert "12345" not in message
    assert "1.5" not in message


def test_the_record_prints_no_password(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(PASSWORD_ENV, FAKE)
    options = _run_with(monkeypatch, tmp_path)
    assert options.database_password is not None, "the control: the password did arrive"
    assert FAKE not in repr(options)


def test_the_service_hands_its_store_the_password(tmp_path: Path) -> None:
    """The service opens its store through the ``open_store`` port, and the password travels with
    the database setting through it. Constructing the service is enough: it builds the store at
    once and opens it only when run."""
    handed: list[Secret | None] = []

    def open_store(database: str, *, password: Secret | None, log: LogFn) -> HouseStore:
        handed.append(password)
        return open_house_store(str(tmp_path / "zonemaster.sqlite"), password=None, log=log)

    options = ServiceOptions(
        bind_ip="127.0.0.1",
        device_id="AABBCC0000A1",
        database="postgresql+psycopg://zonemaster@db.example/zonemaster",
        database_password=Secret(FAKE),
    )
    ports = replace(build_production().zone_ports, open_store=open_store)
    ZoneService(options, log=lambda _kind, _text: None, ports=ports)
    assert handed == [Secret(FAKE)]


@pytest.mark.parametrize("mode", [["--json"], ["--json-bare"], []], ids=["json", "json-bare", "human"])
def test_the_service_run_prints_no_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, mode: list[str]
) -> None:
    """The run's envelope and everything narrated on the way into it, in every output mode."""
    monkeypatch.setenv(PASSWORD_ENV, FAKE)
    options = _run_with(monkeypatch, tmp_path, *mode)
    captured = capsys.readouterr()
    assert options.database_password == Secret(FAKE), "the control: the password did arrive"
    assert FAKE not in captured.out
    assert FAKE not in captured.err


@pytest.mark.parametrize(
    ("spelling", "where"),
    [
        pytest.param("passwrod", "file", id="file-misspelled"),
        pytest.param("Password", "file", id="file-mixed-case"),
        pytest.param("passwrod", "env", id="env-misspelled"),
    ],
)
@pytest.mark.parametrize("mode", [["--json"], ["--json-bare"], []], ids=["json", "json-bare", "human"])
def test_a_misspelled_password_key_is_named_without_its_value(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    isolated_config_layers: Path,
    tmp_path: Path,
    *,
    spelling: str,
    where: str,
    mode: list[str],
) -> None:
    """A stray key in our own section gets a line; the line is about the KEY, whatever it holds."""
    if where == "file":
        _user_config(isolated_config_layers, f'[database]\n{spelling} = "{FAKE}"\n')
    else:
        monkeypatch.setenv(f"{ENV_PREFIX}DATABASE__{spelling.upper()}", FAKE)
    options = _run_with(monkeypatch, tmp_path, *mode)
    captured = capsys.readouterr()
    assert f"has no setting called {spelling!r}" in captured.out + captured.err, "the control: the key is named"
    assert options.database_password is None
    assert FAKE not in captured.out
    assert FAKE not in captured.err


DIGITS = "8675309"


@pytest.mark.parametrize(
    ("environment", "argv", "arrived_as"),
    [
        pytest.param(DIGITS, [], "int", id="env-digits-read-as-a-number"),
        pytest.param(f'["{DIGITS}"]', [], "list", id="env-json-array-read-as-a-list"),
        pytest.param(None, ["--set", f'database.password=["{DIGITS}"]'], "list", id="set-json-array-read-as-a-list"),
    ],
)
def test_a_refused_password_type_is_reported_without_its_value(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    *,
    environment: str | None,
    argv: list[str],
    arrived_as: str,
) -> None:
    """The environment layer reads digits as a number and a value opening with ``[`` as a JSON
    array, and ``--set`` reads JSON too (measured through the real loader). The refusal the service
    prints names the setting and the type it arrived as, and neither stream carries the value."""
    if environment is not None:
        monkeypatch.setenv(PASSWORD_ENV, environment)
    monkeypatch.setenv(f"{ENV_PREFIX}DATABASE__URL", str(tmp_path / "z.sqlite"))
    seen, run = _capture()
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "--json", *argv, "--bind-ip", "10.0.0.1"])
    assert main(run_service=run) == 1
    captured = capsys.readouterr()
    assert seen == [], "a refused start runs nothing"
    assert f"database.password arrived as {arrived_as}" in captured.out
    assert DIGITS not in captured.out
    assert DIGITS not in captured.err


NO_VALUE_SPELLINGS = ["null", "NULL", "Null", "none", "None", "NONE"]
"""Every spelling the environment layer turns into no value at all: it lowercases the text and
compares it with ``null`` and ``none`` (measured on lib_layered_config's env adapter). A ``.env``
reads every one of them as text, and ``--set`` reads only the JSON ``null`` that way."""


def _refused_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str
) -> dict[str, object]:
    """One service start expected to be refused before anything runs; its envelope."""
    seen, run = _capture()
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "--json", *argv])
    assert main(run_service=run) == 2
    assert seen == [], "a refused start runs nothing"
    return json.loads(capsys.readouterr().out)


def _refuses_a_password_that_arrived_as_no_value(envelope: dict[str, object], spelling: str) -> None:
    message = str(envelope["message"])
    assert envelope["ok"] is False
    assert "database.password" in message
    assert "no value" in message
    assert spelling not in message, "the value is not echoed"


@pytest.mark.parametrize("spelling", NO_VALUE_SPELLINGS)
def test_a_password_the_environment_read_as_no_value_refuses_the_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, spelling: str
) -> None:
    """``PASSWORD=none`` must not quietly mean "no password": somebody wrote a value, and the
    service would otherwise run as if they had written nothing."""
    monkeypatch.setenv(PASSWORD_ENV, spelling)
    envelope = _refused_start(monkeypatch, capsys, "--bind-ip", "10.0.0.1", "--database", str(tmp_path / "z.sqlite"))
    _refuses_a_password_that_arrived_as_no_value(envelope, spelling)


def test_a_password_set_to_json_null_refuses_the_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    envelope = _refused_start(
        monkeypatch,
        capsys,
        "--set",
        "database.password=null",
        "--bind-ip",
        "10.0.0.1",
        "--database",
        str(tmp_path / "z.sqlite"),
    )
    _refuses_a_password_that_arrived_as_no_value(envelope, "null")


@pytest.mark.parametrize("verb", [["switch"], ["channels", "export", "--output", "{out}"]], ids=["switch", "export"])
@pytest.mark.parametrize("spelling", ["null", "None"])
def test_a_password_read_as_no_value_refuses_the_store_verbs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    *,
    verb: list[str],
    spelling: str,
) -> None:
    """The store verbs read the password through the same boundary as the run, so they refuse it
    the same way, and open nothing."""
    database = tmp_path / "z.sqlite"
    monkeypatch.setenv(f"{ENV_PREFIX}DATABASE__URL", str(database))
    monkeypatch.setenv(PASSWORD_ENV, spelling)
    argv = [part.format(out=tmp_path / "out.json") for part in verb]
    envelope = _refused_start(monkeypatch, capsys, *argv)
    _refuses_a_password_that_arrived_as_no_value(envelope, spelling)
    assert not database.exists(), "nothing was opened"


def test_an_empty_password_in_the_environment_is_no_password(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Empty text is not no value: it reaches the boundary as ``""`` and means "no password", so
    libpq's own ``~/.pgpass``, ``PGPASSFILE`` and ``PGPASSWORD`` still apply."""
    monkeypatch.setenv(PASSWORD_ENV, "")
    assert _run_with(monkeypatch, tmp_path).database_password is None


CONFIGURED_URL = "postgresql+psycopg://zonemaster@db.example/zonemaster"
"""The database the configuration names. Never opened: every test here either types another
database or hands the opener's result to a SQLite file of its own."""

ANOTHER_SERVER = "postgresql+psycopg://zonemaster@elsewhere.example/zonemaster"

NOT_USED = "the configured database.password was not used"
"""The one line saying a typed database went without the configured password."""


def _configured_house(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PostgreSQL host's configuration: the URL and its password, from the environment."""
    monkeypatch.setenv(f"{ENV_PREFIX}DATABASE__URL", CONFIGURED_URL)
    monkeypatch.setenv(PASSWORD_ENV, FAKE)


def _recording_opener(tmp_path: Path) -> tuple[list[tuple[str, Secret | None]], OpenHouseStore]:
    """An opener that records what it was asked to open and opens a SQLite file instead."""
    handed: list[tuple[str, Secret | None]] = []

    def open_store(database: str, *, password: Secret | None, log: LogFn) -> HouseStore:
        handed.append((database, password))
        return open_house_store(str(tmp_path / "stand-in.sqlite"), password=None, log=log)

    return handed, open_store


async def _never(_options: ServiceOptions) -> int:
    raise AssertionError("a store verb must not start the service")


def _switch(monkeypatch: pytest.MonkeyPatch, *typed: str, open_store: OpenHouseStore | None = None) -> int:
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "--json", *typed, "switch"])
    return main(run_service=_never, open_store=open_store)


def test_a_typed_sqlite_database_opens_on_a_host_with_a_configured_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The configured password belongs to the configured PostgreSQL database; a SQLite file typed
    for one command is opened without it rather than refused for having been handed one."""
    _configured_house(monkeypatch)
    database = tmp_path / "copy.sqlite"
    assert _switch(monkeypatch, "--database", str(database)) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["ok"] is True
    assert database.exists(), "the typed database was opened"
    assert NOT_USED in captured.err
    assert FAKE not in captured.out + captured.err


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        pytest.param([], (CONFIGURED_URL, Secret(FAKE)), id="nothing-typed"),
        pytest.param(["--database", CONFIGURED_URL], (CONFIGURED_URL, Secret(FAKE)), id="typed-the-configured-url"),
        pytest.param(["--database", ANOTHER_SERVER], (ANOTHER_SERVER, None), id="typed-another-server"),
    ],
)
def test_a_store_verb_hands_the_configured_password_only_to_the_configured_database(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    *,
    typed: list[str],
    expected: tuple[str, Secret | None],
) -> None:
    _configured_house(monkeypatch)
    handed, open_store = _recording_opener(tmp_path)
    assert _switch(monkeypatch, *typed, open_store=open_store) == 0
    captured = capsys.readouterr()
    assert handed == [expected]
    assert (NOT_USED in captured.err) is (expected[1] is None), "the line appears exactly when it was not used"
    assert FAKE not in captured.out + captured.err


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        pytest.param(None, Secret(FAKE), id="nothing-typed"),
        pytest.param(CONFIGURED_URL, Secret(FAKE), id="typed-the-configured-url"),
        pytest.param(ANOTHER_SERVER, None, id="typed-another-server"),
        pytest.param("{sqlite}", None, id="typed-a-sqlite-file"),
    ],
)
def test_the_service_run_takes_the_configured_password_only_for_the_configured_database(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    *,
    typed: str | None,
    expected: Secret | None,
) -> None:
    """The run reaches the store through its record, so the record is what is asserted; that the
    record's password is what the service hands its opener is pinned above."""
    _configured_house(monkeypatch)
    seen, run = _capture()
    database = [] if typed is None else ["--database", typed.format(sqlite=tmp_path / "copy.sqlite")]
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "--json", "--bind-ip", "10.0.0.1", *database])
    assert main(run_service=run) == 0
    captured = capsys.readouterr()
    assert seen[0].database_password == expected
    assert (NOT_USED in captured.err) is (expected is None), "the line appears exactly when it was not used"
    assert FAKE not in captured.out + captured.err
