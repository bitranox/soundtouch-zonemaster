"""The store verbs of the service CLI, from argv to the database and back."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from service_database import created_by_the_service

from soundtouch_zonemaster.adapters.config.loader import ENV_PREFIX
from soundtouch_zonemaster.adapters.files import house_store
from soundtouch_zonemaster.adapters.files.atomicfile import TEMP_SUFFIX
from soundtouch_zonemaster.adapters.files.channel_file import save_channels
from soundtouch_zonemaster.adapters.files.house_store import SqlHouseStore
from soundtouch_zonemaster.domain.channellist import Channel, ChannelList
from soundtouch_zonemaster.domain.database_url import masked
from soundtouch_zonemaster.domain.enums import ChannelKind
from soundtouch_zonemaster.entry import service_main as main

if TYPE_CHECKING:
    from conftest import PostgresLogin
    from sqlalchemy.engine import Connection

    from soundtouch_zonemaster.application.options import ServiceOptions

LIST = ChannelList(channels=(Channel(number="1", name="One", kind=ChannelKind.RADIO, url="http://radio.example/1"),))


async def _never(_options: ServiceOptions) -> int:
    raise AssertionError("a store verb must not start the service")


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", *argv])
    return main(run_service=_never)


def _envelope(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def test_the_switch_reads_on_in_a_database_nobody_has_switched(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    database = str(created_by_the_service(tmp_path / "db.sqlite"))
    rc = _run(monkeypatch, "--json", "--database", database, "switch")
    assert rc == 0
    assert _envelope(capsys)["data"] == {"database": database, "on": True, "changed": False}


def test_switching_off_is_read_back_and_reports_the_change(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    database = str(created_by_the_service(tmp_path / "db.sqlite"))
    assert _run(monkeypatch, "--json", "--database", database, "switch", "off") == 0
    assert _envelope(capsys)["data"] == {"database": database, "on": False, "changed": True}
    assert _run(monkeypatch, "--json", "--database", database, "switch") == 0
    assert _envelope(capsys)["data"] == {"database": database, "on": False, "changed": False}


def test_the_switch_works_while_the_service_holds_the_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    service = SqlHouseStore(str(tmp_path / "db.sqlite"), log=lambda _k, _t: None)
    service.open(exclusive=True)
    try:
        assert _run(monkeypatch, "--json", "--database", str(tmp_path / "db.sqlite"), "switch", "off") == 0
        assert service.is_on() is False
    finally:
        service.close()


def test_an_import_is_refused_while_the_service_runs_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    source = tmp_path / "edited.json"
    save_channels(source, LIST)
    service = SqlHouseStore(str(tmp_path / "db.sqlite"), log=lambda _k, _t: None)
    service.open(exclusive=True)
    try:
        rc = _run(monkeypatch, "--json", "--database", str(tmp_path / "db.sqlite"), "channels", "import", str(source))
        envelope = _envelope(capsys)
        assert service.load_channels() == ChannelList()
    finally:
        service.close()
    assert rc == 1
    assert envelope["ok"] is False
    assert envelope["error"] == "StoreBusyError"


def test_export_then_import_round_trips_the_list(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    database = created_by_the_service(tmp_path / "db.sqlite")
    source = tmp_path / "edited.json"
    save_channels(source, LIST)
    assert _run(monkeypatch, "--json", "--database", str(database), "channels", "import", str(source)) == 0
    assert _envelope(capsys)["data"] == {"database": str(database), "channels": 1, "path": str(source)}
    exported = tmp_path / "out.json"
    assert (
        _run(monkeypatch, "--json", "--database", str(database), "channels", "export", "--output", str(exported)) == 0
    )
    assert exported.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")


def test_channels_export_reads_the_store_only_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The count in the envelope must come from the SAME read as the exported text: a second read
    could see a different list if a writer changed the store in between (OPEN-WORK rank 202)."""
    database = created_by_the_service(tmp_path / "db.sqlite")
    source = tmp_path / "edited.json"
    save_channels(source, LIST)
    assert _run(monkeypatch, "--json", "--database", str(database), "channels", "import", str(source)) == 0
    capsys.readouterr()
    calls = 0
    real_read_channels = house_store.read_channels

    def _counting(connection: Connection, *, where: str) -> ChannelList:
        nonlocal calls
        calls += 1
        return real_read_channels(connection, where=where)

    monkeypatch.setattr(house_store, "read_channels", _counting)
    exported = tmp_path / "out.json"
    rc = _run(monkeypatch, "--json", "--database", str(database), "channels", "export", "--output", str(exported))
    assert rc == 0
    assert calls == 1
    assert _envelope(capsys)["data"] == {"database": str(database), "channels": 1, "path": str(exported)}


def test_channels_export_writes_atomically(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Written through :func:`atomicfile.write_atomic`, so no partial file and no leftover temp file."""
    database = created_by_the_service(tmp_path / "db.sqlite")
    source = tmp_path / "edited.json"
    save_channels(source, LIST)
    assert _run(monkeypatch, "--json", "--database", str(database), "channels", "import", str(source)) == 0
    exported = tmp_path / "out.json"
    assert (
        _run(monkeypatch, "--json", "--database", str(database), "channels", "export", "--output", str(exported)) == 0
    )
    assert exported.exists()
    assert not exported.with_name(exported.name + TEMP_SUFFIX).exists()


def test_an_unusable_import_is_could_not_run_and_names_the_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    source = tmp_path / "broken.json"
    source.write_text('{"channels": [{"number": "x"}]}', encoding="utf-8")
    database = created_by_the_service(tmp_path / "db.sqlite")
    rc = _run(monkeypatch, "--json", "--database", str(database), "channels", "import", str(source))
    envelope = _envelope(capsys)
    assert rc == 2
    assert envelope["error"] == "StoreError"
    assert "broken.json" in str(envelope["message"])


def test_an_export_to_a_missing_directory_is_could_not_run_and_names_the_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    output = tmp_path / "nonexistent-dir" / "out.json"
    database = created_by_the_service(tmp_path / "db.sqlite")
    rc = _run(monkeypatch, "--json", "--database", str(database), "channels", "export", "--output", str(output))
    envelope = _envelope(capsys)
    assert rc == 2
    assert envelope["error"] == "FileNotFoundError"
    assert str(output) in str(envelope["message"])


def test_the_switchs_human_output_names_the_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A mistyped ``--database`` must be visible in the human line, not only in the JSON envelope."""
    database = str(created_by_the_service(tmp_path / "db.sqlite"))
    assert _run(monkeypatch, "--database", database, "switch") == 0
    assert database in capsys.readouterr().out


def test_channels_exports_human_output_names_the_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The FINAL line - the answer, as opposed to the narration ``load_channels`` logs on its own
    way to answering it - must itself name the database, so a mistyped ``--database`` is visible
    even with narration silenced (``--quiet``, a systemd unit's own log level, and so on)."""
    database = created_by_the_service(tmp_path / "db.sqlite")
    source = tmp_path / "edited.json"
    save_channels(source, LIST)
    assert _run(monkeypatch, "--json", "--database", str(database), "channels", "import", str(source)) == 0
    capsys.readouterr()  # discard the import's own (JSON) output, which also names the database
    exported = tmp_path / "out.json"
    assert _run(monkeypatch, "--database", str(database), "channels", "export", "--output", str(exported)) == 0
    last_line = capsys.readouterr().out.splitlines()[-1]
    assert str(database) in last_line
    assert str(exported) in last_line


def _url_setting_and_its_where(tmp_path: Path) -> tuple[str, str]:
    """A database URL and the name every report must give it: the domain's mask of the setting.

    A setting the store opens carries no password (one that does is refused before either report
    is built), and the mask shows such a setting exactly as typed. So the envelope and the human
    line can only name the setting itself; a URL rather than a plain path keeps the URL branch of
    the display rule the one exercised."""
    setting = f"sqlite:///{created_by_the_service(tmp_path / 'db.sqlite')}"
    where = masked(setting)
    assert where == setting, "a setting the store opens is shown as typed"
    return setting, where


def test_the_switchs_envelope_names_the_same_database_the_human_line_would(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The envelope carries a ``database`` field equal to the store's ``where`` for the setting
    given, which is the domain's mask of that setting."""
    setting, where = _url_setting_and_its_where(tmp_path)
    assert _run(monkeypatch, "--json", "--database", setting, "switch") == 0
    assert _envelope(capsys)["data"] == {"database": where, "on": True, "changed": False}


def test_channels_exports_envelope_names_the_same_database_the_human_line_would(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    setting, where = _url_setting_and_its_where(tmp_path)
    source = tmp_path / "edited.json"
    save_channels(source, LIST)
    assert _run(monkeypatch, "--json", "--database", setting, "channels", "import", str(source)) == 0
    capsys.readouterr()
    exported = tmp_path / "out.json"
    assert _run(monkeypatch, "--json", "--database", setting, "channels", "export", "--output", str(exported)) == 0
    assert _envelope(capsys)["data"] == {"database": where, "channels": 1, "path": str(exported)}


def test_channels_imports_envelope_names_the_same_database_as_the_switchs_would(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """``channels import`` keeps its own human line unchanged (no database named in it), but its
    envelope carries the same masked ``where`` as every other verb's does."""
    setting, where = _url_setting_and_its_where(tmp_path)
    source = tmp_path / "edited.json"
    save_channels(source, LIST)
    assert _run(monkeypatch, "--json", "--database", setting, "channels", "import", str(source)) == 0
    assert _envelope(capsys)["data"] == {"database": where, "channels": 1, "path": str(source)}


def test_no_database_anywhere_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    rc = _run(monkeypatch, "--json", "switch")
    assert rc == 2
    assert "database" in str(_envelope(capsys)["message"])


@pytest.mark.parametrize(
    "verb",
    [
        pytest.param(("switch",), id="switch"),
        pytest.param(("switch", "off"), id="switch-off"),
        pytest.param(("channels", "export", "--output", "{out}"), id="channels-export"),
        pytest.param(("channels", "import", "{source}"), id="channels-import"),
        pytest.param(("prefs",), id="prefs"),
        pytest.param(("prefs", "set", "volume.fade_s", "1.5"), id="prefs-set"),
        pytest.param(("prefs", "unset", "volume.fade_s"), id="prefs-unset"),
    ],
)
def test_a_store_verb_never_creates_the_database_it_is_pointed_at(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    isolated_config_layers: Path,
    tmp_path: Path,
    *,
    verb: tuple[str, ...],
) -> None:
    """Only the service creates the house database. A mistyped ``--database`` used to get a new,
    empty database and ``switch off (changed)`` while the service went on reading the real one - and
    run as root before the first start, it left root-owned files the service could not open."""
    missing = tmp_path / "typo.sqlite"
    source = tmp_path / "edited.json"
    save_channels(source, LIST)
    argv = [part.format(out=tmp_path / "out.json", source=source) for part in verb]
    rc = _run(monkeypatch, "--json", "--database", str(missing), *argv)
    envelope = _envelope(capsys)
    assert not missing.exists()
    assert rc == 2
    assert envelope["error"] == "StoreMissingError"
    assert str(missing) in str(envelope["message"])
    assert "start the service" in str(envelope["message"])


@pytest.mark.parametrize(
    "verb",
    [
        pytest.param(("switch",), id="switch"),
        pytest.param(("channels", "export", "--output", "{out}"), id="channels-export"),
        pytest.param(("channels", "import", "{source}"), id="channels-import"),
    ],
)
@pytest.mark.parametrize("mode", [("--json",), ("--json-bare",), ()], ids=["json", "json-bare", "human"])
def test_every_store_verb_takes_the_password_setting_and_never_shows_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    *,
    verb: tuple[str, ...],
    mode: tuple[str, ...],
) -> None:
    """The verbs open the store without the service's option record, so they read the password
    from the configuration layers themselves. A SQLite database has none, and the store refuses
    one given for it by name - which is how a test without a PostgreSQL server can see that the
    verb handed it over at all. Neither the refusal nor anything else printed may carry it.

    The database is configured beside the password rather than typed: a configured password goes
    only with the configured database, and a typed SQLite file would be opened without it."""
    source = tmp_path / "edited.json"
    save_channels(source, LIST)
    monkeypatch.setenv(f"{ENV_PREFIX}DATABASE__URL", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv(f"{ENV_PREFIX}DATABASE__PASSWORD", "TOPSECRET")
    argv = [part.format(out=tmp_path / "out.json", source=source) for part in verb]

    rc = _run(monkeypatch, *mode, *argv)

    captured = capsys.readouterr()
    assert rc == 2
    assert "database.password" in captured.out + captured.err, "refused for the password, by name"
    assert "TOPSECRET" not in captured.out
    assert "TOPSECRET" not in captured.err


def test_a_config_file_that_will_not_parse_is_a_refusal_for_a_store_verb(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    """The verbs read the configuration for the password even when ``--database`` is typed, so a
    broken file must be the same named refusal it is everywhere else, not a traceback."""
    broken = isolated_config_layers / "xdg" / "soundtouch-zonemaster" / "config.toml"
    broken.parent.mkdir(parents=True)
    broken.write_text("[database\n", encoding="utf-8")
    rc = _run(monkeypatch, "--json", "--database", str(tmp_path / "db.sqlite"), "switch")
    envelope = _envelope(capsys)
    assert rc == 2
    assert envelope["ok"] is False
    assert envelope["error"] == "ConfigInputError"


def _installed_service_command() -> str:
    """The console script the deployed unit runs, from the environment this suite runs in."""
    found = shutil.which("soundtouch-zonemaster-service", path=str(Path(sys.executable).parent))
    assert found is not None, "the console script is installed beside this interpreter"
    return found


def _run_installed(argv: list[str], *, url: str, password: str | None, cwd: Path) -> subprocess.CompletedProcess[str]:
    """The installed command as a child with the password ONLY in the setting's own variable.

    ``PGPASSWORD`` is removed and ``PGPASSFILE`` points at an empty file, so libpq has no password
    of its own to fall back on; the child starts in a directory with no ``.env`` above it; and the
    parent's own ``SOUNDTOUCH_ZONEMASTER___*`` variables are dropped, so the two below are all it is
    told. The value travels in the child's environment mapping and nowhere else.
    """
    env = {key: value for key, value in os.environ.items() if key != "PGPASSWORD" and not key.startswith(ENV_PREFIX)}
    env["PGPASSFILE"] = "/dev/null"
    env[f"{ENV_PREFIX}DATABASE__URL"] = url
    if password is not None:
        env[f"{ENV_PREFIX}DATABASE__PASSWORD"] = password
    return subprocess.run(  # noqa: S603 - the project's own console script, no shell
        [_installed_service_command(), *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=cwd,
        check=False,
        timeout=60,
    )


def _shows(done: subprocess.CompletedProcess[str], secret: str) -> bool:
    """Whether a child printed ``secret`` anywhere. A bool, so a failing assert prints no value."""
    return secret in done.stdout or secret in done.stderr


def test_the_installed_command_opens_postgresql_with_the_password_setting_alone(
    postgres_login: PostgresLogin, tmp_path: Path
) -> None:
    """The production path end to end: the installed command, the environment layer, the store,
    the driver's connect argument, a real PostgreSQL login."""
    password = postgres_login.password.reveal()
    switch = _run_installed(["--json-bare", "switch"], url=postgres_login.url, password=password, cwd=tmp_path)
    assert switch.returncode == 0, f"exit {switch.returncode}"
    assert not _shows(switch, password), "the password appears in the command's output"
    envelope = json.loads(switch.stdout)
    assert envelope["ok"] is True
    assert envelope["data"] == {"database": masked(postgres_login.url), "on": True, "changed": False}

    exported = tmp_path / "channels.json"
    export = _run_installed(
        ["--json-bare", "channels", "export", "--output", str(exported)],
        url=postgres_login.url,
        password=password,
        cwd=tmp_path,
    )
    assert export.returncode == 0, f"exit {export.returncode}"
    assert not _shows(export, password), "the password appears in the command's output"
    assert exported.is_file()


def test_the_installed_command_without_the_password_setting_cannot_log_in_to_postgresql(
    postgres_login: PostgresLogin, tmp_path: Path
) -> None:
    """The control for the test above: with libpq's own sources gone and no setting, no login -
    so the one above logged in through the setting and through nothing else."""
    done = _run_installed(["--json-bare", "switch"], url=postgres_login.url, password=None, cwd=tmp_path)
    assert done.returncode == 2, f"exit {done.returncode}"
    envelope = json.loads(done.stdout)
    assert envelope["ok"] is False
    assert envelope["error"] == "StoreError"
    assert "password" in envelope["message"], "refused for the login, not for anything else"


def test_a_wrong_password_on_postgresql_is_refused_and_named_nowhere(
    postgres_login: PostgresLogin, tmp_path: Path
) -> None:
    wrong = "WRONG-TOPSECRET"
    done = _run_installed(["--json-bare", "switch"], url=postgres_login.url, password=wrong, cwd=tmp_path)
    assert done.returncode == 2, f"exit {done.returncode}"
    envelope = json.loads(done.stdout)
    assert envelope["ok"] is False
    assert envelope["error"] == "StoreError"
    assert "password" in envelope["message"], "refused for the login, not for anything else"
    assert not _shows(done, wrong), "the wrong password appears in the refusal"
    assert not _shows(done, postgres_login.password.reveal()), "the real password appears in the refusal"
