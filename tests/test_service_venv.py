"""What the installed service's own interpreter answers about the house database, driven for real.

`tools/service_venv.py` runs on the service host with the service venv's python, because only that
interpreter has the package the database is read through. Every test here drives its command line
in-process against a SQLite file in a temporary directory, through the same config layers the
service reads (isolated by the suite's conftest), and judges it by the database it leaves behind -
never by a recorded call.

The rule that earns most of this file is the installer's: a first start must find the switch OFF,
and nothing here may ever change a switch somebody already set. A database that exists without a
switch row reads as ON to the service, so that is a switch somebody set too.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from service_database import created_by_the_service

from soundtouch_zonemaster.composition import open_house_store
from soundtouch_zonemaster.domain.state import ZoneState

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from service_venv import main

if TYPE_CHECKING:
    from collections.abc import Sequence


def _quiet(_kind: str, _text: str) -> None:
    """The store's narration, which these tests judge by the database instead."""


def _drive(argv: Sequence[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any]]:
    """Run one verb with ``--json-bare`` and return its exit code and its envelope."""
    code = main(["--json-bare", *argv])
    printed = capsys.readouterr().out
    return code, json.loads(printed)


def _switch_row(path: Path) -> str | None:
    """The switch word as the file holds it, read with the stdlib so the reading is independent."""
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute("SELECT word FROM switch WHERE id = 1").fetchone()
    return None if row is None else str(row[0])


def _set_switch(path: Path, *, on: bool) -> None:
    store = open_house_store(str(path), password=None, log=_quiet)
    store.open(exclusive=False, create=False)
    try:
        store.set_switch(on=on)
    finally:
        store.close()


def _host_layer(root: Path, text: str) -> Path:
    """Write THIS machine's host layer, which is where the deployed host keeps its database setting."""
    written = root / "etc" / "soundtouch-zonemaster" / "hosts" / f"{socket.gethostname()}.toml"
    written.parent.mkdir(parents=True)
    written.write_text(text, encoding="utf-8")
    return written


@pytest.fixture(autouse=True)
def _no_checkout_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Start the dotenv walk from a directory with no ``.env`` above it, as the host does."""
    monkeypatch.chdir(tmp_path)


def test_a_fresh_machine_gets_a_database_whose_switch_says_off(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    default = tmp_path / "state" / "zonemaster.sqlite"
    default.parent.mkdir()

    code, document = _drive(["seed-switch", "--default", str(default)], capsys)

    assert code == 0
    assert document["ok"] is True
    assert document["data"]["created"] is True
    assert document["data"]["written"] is True
    assert document["data"]["switch"] == "off"
    assert _switch_row(default) == "off", "the first start must not be able to take the house"


def test_a_switch_that_says_on_is_left_on(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    database = created_by_the_service(tmp_path / "zonemaster.sqlite")
    _set_switch(database, on=True)

    code, document = _drive(["seed-switch", "--default", str(database)], capsys)

    assert code == 0
    assert document["data"]["written"] is False
    assert document["data"]["switch"] == "on"
    assert _switch_row(database) == "on", "the switch is the operator's, not the install's"


def test_a_database_that_was_never_switched_is_left_reading_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A database the service created without a switch row reads as ON, and the house has run so.

    Writing OFF there because "no row" looks like "fresh" would stand a running house down. Only a
    database this command creates is fresh.
    """
    database = created_by_the_service(tmp_path / "zonemaster.sqlite")

    code, document = _drive(["seed-switch", "--default", str(database)], capsys)

    assert code == 0
    assert document["data"]["created"] is False
    assert document["data"]["written"] is False
    assert document["data"]["switch"] == "unset"
    assert _switch_row(database) is None


def test_the_configured_database_is_the_one_written(
    tmp_path: Path, isolated_config_layers: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The host layer's database.url wins over the installer's default path, as it does for the service."""
    configured = tmp_path / "elsewhere" / "house.sqlite"
    configured.parent.mkdir()
    _host_layer(isolated_config_layers, f'[database]\nurl = "{configured}"\n')
    default = tmp_path / "state" / "zonemaster.sqlite"
    default.parent.mkdir()

    code, document = _drive(["seed-switch", "--default", str(default)], capsys)

    assert code == 0
    assert document["data"]["database"] == str(configured)
    assert _switch_row(configured) == "off"
    assert not default.exists(), "the default is only for a machine that configures no database"


@pytest.mark.parametrize("word", ["on", "off"])
def test_an_old_switch_file_carries_its_word_into_a_new_database(
    tmp_path: Path, word: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """An upgrade from before the database: the operator's word is in zone.switch, not in a row.

    Seeding OFF regardless would override an ``on`` in that file, because the service's one-time
    import skips a switch the database already holds; and a unit that no longer names the file
    would import nothing at all. So the file's word is what goes in.
    """
    default = tmp_path / "zonemaster.sqlite"
    legacy = tmp_path / "zone.switch"
    legacy.write_text(f"{word}\n", encoding="utf-8")

    code, document = _drive(["seed-switch", "--default", str(default), "--legacy-switch-file", str(legacy)], capsys)

    assert code == 0
    assert document["data"]["switch"] == word
    assert _switch_row(default) == word


def test_show_reports_the_switch_and_the_members(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    database = created_by_the_service(tmp_path / "zonemaster.sqlite")
    store = open_house_store(str(database), password=None, log=_quiet)
    store.open(exclusive=False, create=False)
    try:
        store.set_switch(on=False)
        store.save_state(ZoneState(members=("AABBCC0000A1", "AABBCC0000A2")))
    finally:
        store.close()

    code, document = _drive(["show", "--default", str(database)], capsys)

    assert code == 0
    assert document["data"] == {
        "database": str(database),
        "backend": "sqlite",
        "exists": True,
        "on": False,
        "members": ["AABBCC0000A1", "AABBCC0000A2"],
    }


def test_show_answers_a_database_that_is_not_there_without_creating_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "zonemaster.sqlite"

    code, document = _drive(["show", "--default", str(missing)], capsys)

    assert code == 0
    assert document["data"]["exists"] is False
    assert document["data"]["members"] == []
    assert not missing.exists()


def test_set_switch_changes_the_row(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    database = created_by_the_service(tmp_path / "zonemaster.sqlite")

    code, document = _drive(["set-switch", "off", "--default", str(database)], capsys)

    assert code == 0
    assert document["data"]["on"] is False
    assert _switch_row(database) == "off"


def test_set_switch_refuses_a_database_that_is_not_there(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Only the service and the installer create the database; a switch verb answering a typo with a
    new, empty one is the failure the store verbs already refuse."""
    missing = tmp_path / "zonemaster.sqlite"

    code, document = _drive(["set-switch", "off", "--default", str(missing)], capsys)

    assert code == 2
    assert document["ok"] is False
    assert document["error"] == "StoreMissingError"
    assert not missing.exists()


def test_a_backup_is_a_consistent_copy_taken_through_the_backup_api(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The copy is opened as a house database and holds what the original held."""
    database = created_by_the_service(tmp_path / "zonemaster.sqlite")
    _set_switch(database, on=False)
    backups = tmp_path / "backups"

    code, document = _drive(["backup", "--to", str(backups), "--default", str(database)], capsys)

    assert code == 0
    copy = Path(document["data"]["backup"])
    assert copy.parent == backups
    assert _switch_row(copy) == "off"
    with closing(sqlite3.connect(copy)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_a_postgresql_database_is_not_copied_but_named_for_a_pg_dump(
    tmp_path: Path, isolated_config_layers: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A server database is the operator's to dump; this never connects to one to back it up."""
    _host_layer(isolated_config_layers, '[database]\nurl = "postgresql+psycopg://zm@db.invalid/zonemaster"\n')

    code, document = _drive(["backup", "--to", str(tmp_path / "backups"), "--default", "unused"], capsys)

    assert code == 0
    assert document["data"]["backup"] is None
    assert "pg_dump" in document["data"]["note"]
    assert not (tmp_path / "backups").exists()


def test_a_backup_of_a_database_that_is_not_there_says_so(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, document = _drive(
        ["backup", "--to", str(tmp_path / "backups"), "--default", str(tmp_path / "none.sqlite")], capsys
    )

    assert code == 0
    assert document["data"]["backup"] is None
    assert not (tmp_path / "none.sqlite").exists()


def test_distributions_names_this_program(capsys: pytest.CaptureFixture[str]) -> None:
    code, document = _drive(["distributions"], capsys)

    assert code == 0
    assert "soundtouch-zonemaster" in document["data"]["names"]


def test_a_database_url_carrying_a_password_is_refused_without_repeating_it(
    tmp_path: Path, isolated_config_layers: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _host_layer(isolated_config_layers, '[database]\nurl = "postgresql+psycopg://zm:hunter2@db.invalid/zonemaster"\n')

    code, document = _drive(["seed-switch", "--default", str(tmp_path / "x.sqlite")], capsys)

    assert code == 2
    assert document["ok"] is False
    assert "hunter2" not in json.dumps(document)
