"""The database password: a setting in every configuration layer, and a secret everywhere it goes.

The value used throughout is a fake (``TOPSECRET``). Every layer the service reads is exercised
through the real ``main`` with a real argv, the run substituted through ``main``'s own parameter,
so what is asserted is the whole way from a file, a variable or a ``--set`` into the record.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from nothing_typed import NOTHING_TYPED

from soundtouch_zonemaster.adapters.cli.boundary import parse_service_options
from soundtouch_zonemaster.adapters.config.loader import ENV_PREFIX
from soundtouch_zonemaster.application.outcome import OptionsError
from soundtouch_zonemaster.domain.secret import Secret
from soundtouch_zonemaster.entry import service_main as main

if TYPE_CHECKING:
    from pathlib import Path

    from soundtouch_zonemaster.application.options import ServiceOptions
    from soundtouch_zonemaster.application.ports import RunService

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
    """One service start with a SQLite database and an address given, the record it was handed."""
    seen, run = _capture()
    database = str(tmp_path / "zonemaster.sqlite")
    monkeypatch.setattr(
        "sys.argv", ["soundtouch-zonemaster-service", "--bind-ip", "10.0.0.1", "--database", database, *argv]
    )
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
            bind_ip="10.0.0.1", database="db.sqlite", configured={"database_password": value}, **NOTHING_TYPED
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
