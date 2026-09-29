"""``tools/service_venv.py`` answered by the package a deploy is about to REPLACE, not this one.

A deploy asks the helper about the house before it installs the new wheel, so the helper runs with
the venv's python and the package ALREADY installed there: 0.5.2 on the house's machine. The helper
ships with the new version, though, and once imported something 0.5.2 does not have; every one of
those reads then died with an ImportError and no envelope, and ``deploy_service.py --dry-run``
refused the house as unreadable every time.

So each test here runs the helper as a subprocess with the source of tag ``v0.5.2`` first on
``PYTHONPATH``, against a SQLite house database that same old package created, and judges it by its
envelope and by the file it leaves. The source is taken with ``git archive`` rather than a worktree:
nothing is registered in the checkout's ``.git``, so a killed run leaves nothing behind there. A
clone without the tag (a shallow CI checkout fetches none) skips, naming the tag.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import text
from switch_race import a_person_writing, wait_until_a_writer_waits

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from deploy_service import HouseView, VenvHouse
from install_service import Install, Ran

if TYPE_CHECKING:
    from collections.abc import Sequence

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tools" / "service_venv.py"
OLDEST_DEPLOYED = "v0.5.2"

_MAKE_A_HOUSE = """
import sys
from soundtouch_zonemaster.adapters.files.house_db import HouseDatabase
from soundtouch_zonemaster.adapters.files.house_state import write_state
from soundtouch_zonemaster.adapters.files.house_switch import write_switch
from soundtouch_zonemaster.domain.state import ZoneState

path, word, *members = sys.argv[1:]
house = HouseDatabase(path)
house.open(exclusive=True)
try:
    with house.writing() as connection:
        if word != "unset":
            write_switch(connection, on=word == "on")
        if members:
            write_state(connection, ZoneState(members=tuple(members)))
finally:
    house.close()
"""
"""A house database exactly as the old service leaves it: created, migrated and written by 0.5.2 itself."""

_WHAT_WAS_IMPORTED = """
import json
import soundtouch_zonemaster
from soundtouch_zonemaster.adapters.files import house_db
print(json.dumps({
    "file": soundtouch_zonemaster.__file__,
    "probe": hasattr(house_db.HouseDatabase, "probe"),
    "schema_exists": hasattr(house_db, "schema_exists"),
}))
"""


def _git() -> str:
    found = shutil.which("git")
    if found is None:
        pytest.skip("git is not on PATH, so the old package cannot be taken from the history")
    return found


@pytest.fixture(scope="module")
def old_package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The ``src`` directory of the oldest deployed release, for ``PYTHONPATH``."""
    git = _git()
    tag = f"{OLDEST_DEPLOYED}^{{commit}}"
    known = subprocess.run(  # noqa: S603 - argv list: git, and a tag named in this file
        [git, "-C", str(ROOT), "rev-parse", "--verify", "--quiet", tag], capture_output=True, check=False
    )
    if known.returncode != 0:
        pytest.skip(f"tag {OLDEST_DEPLOYED} is not in this clone (a shallow checkout fetches no tags)")
    target = tmp_path_factory.mktemp(OLDEST_DEPLOYED)
    archive = target / "src.tar"
    with archive.open("wb") as out:
        subprocess.run(  # noqa: S603 - argv list: git, and a tag named in this file
            [git, "-C", str(ROOT), "archive", OLDEST_DEPLOYED, "src/soundtouch_zonemaster"], stdout=out, check=True
        )
    with tarfile.open(archive) as extracted:
        extracted.extractall(target, filter="data")
    return target / "src"


def _env(old_package: Path) -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(old_package)}


def _run(argv: Sequence[str], *, old_package: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argv list: this interpreter, and arguments built in this file
        [sys.executable, *argv], capture_output=True, text=True, check=False, cwd=cwd, env=_env(old_package)
    )


def _helper(old_package: Path, cwd: Path, *verb: str) -> tuple[int, dict[str, Any]]:
    """Run one verb of the helper under the old package; its exit code and its envelope."""
    ran = _run([str(HELPER), "--json-bare", *verb], old_package=old_package, cwd=cwd)
    assert ran.stdout, f"no envelope at all (exit {ran.returncode}): {ran.stderr}"
    return ran.returncode, json.loads(ran.stdout)


def _old_house(old_package: Path, path: Path, *, word: str, members: Sequence[str] = ()) -> Path:
    ran = _run(["-c", _MAKE_A_HOUSE, str(path), word, *members], old_package=old_package, cwd=path.parent)
    assert ran.returncode == 0, ran.stderr
    return path


def _switch_row(path: Path) -> str | None:
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute("SELECT word FROM switch WHERE id = 1").fetchone()
    return None if row is None else str(row[0])


def _has_a_house_schema(path: Path) -> bool:
    with closing(sqlite3.connect(path)) as connection:
        found = connection.execute("SELECT name FROM sqlite_master WHERE name = 'alembic_version'").fetchone()
    return found is not None


@pytest.fixture(autouse=True)
def _no_checkout_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Start the dotenv walk from a directory with no ``.env`` above it, as the host does."""
    monkeypatch.chdir(tmp_path)


def test_the_package_under_test_is_the_old_one_and_predates_the_probe(old_package: Path, tmp_path: Path) -> None:
    """The control every other test here leans on: the fallback is only exercised if this holds."""
    ran = _run(["-c", _WHAT_WAS_IMPORTED], old_package=old_package, cwd=tmp_path)
    assert ran.returncode == 0, ran.stderr
    imported = json.loads(ran.stdout)
    assert Path(imported["file"]).is_relative_to(old_package)
    assert imported["probe"] is False
    assert imported["schema_exists"] is False


def test_show_reads_a_house_the_old_service_switched_off(old_package: Path, tmp_path: Path) -> None:
    database = _old_house(old_package, tmp_path / "zonemaster.sqlite", word="off", members=["AABBCC0000A1"])

    code, document = _helper(old_package, tmp_path, "show", "--default", str(database))

    assert code == 0
    assert document["ok"] is True
    assert document["data"] == {
        "database": str(database),
        "backend": "sqlite",
        "exists": True,
        "on": False,
        "members": ["AABBCC0000A1"],
    }


def test_show_reads_a_house_with_no_switch_row_as_on_under_the_old_package(old_package: Path, tmp_path: Path) -> None:
    """No row is what the service reads as ON, so a deploy must hand that house back ON."""
    database = _old_house(old_package, tmp_path / "zonemaster.sqlite", word="unset")

    code, document = _helper(old_package, tmp_path, "show", "--default", str(database))

    assert code == 0
    assert document["data"]["exists"] is True
    assert document["data"]["on"] is True


def test_show_under_the_old_package_creates_and_migrates_nothing(old_package: Path, tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite"
    schema_less = tmp_path / "schema-less.sqlite"
    with closing(sqlite3.connect(schema_less)) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER)")
        connection.commit()

    missing_code, missing_answer = _helper(old_package, tmp_path, "show", "--default", str(missing))
    bare_code, bare_answer = _helper(old_package, tmp_path, "show", "--default", str(schema_less))

    assert (missing_code, missing_answer["data"]["exists"]) == (0, False)
    assert not missing.exists(), "a read must not create the file"
    assert (bare_code, bare_answer["data"]["exists"]) == (0, False)
    assert not _has_a_house_schema(schema_less), "a read must not migrate"


def test_a_switch_the_old_package_cannot_read_is_refused_not_reported_as_on(old_package: Path, tmp_path: Path) -> None:
    database = _old_house(old_package, tmp_path / "zonemaster.sqlite", word="off")
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("DROP TABLE switch")
        connection.commit()

    code, document = _helper(old_package, tmp_path, "show", "--default", str(database))

    assert code == 2
    assert document["ok"] is False
    assert document["error"] == "StoreError"


def test_distributions_answers_under_the_old_package(old_package: Path, tmp_path: Path) -> None:
    code, document = _helper(old_package, tmp_path, "distributions")

    assert code == 0
    assert document["ok"] is True
    assert "soundtouch-zonemaster" in document["data"]["names"]


def test_the_deploy_s_own_reads_and_writes_work_through_the_old_package(old_package: Path, tmp_path: Path) -> None:
    """Everything ``deploy_service.py`` asks before it installs, through its own ``VenvHouse``."""
    install = Install(prefix=tmp_path / "opt", state_dir=tmp_path / "state", wheel=tmp_path / "w.whl")
    install.state_dir.mkdir()
    _old_house(old_package, install.database, word="on", members=["AABBCC0000A2"])

    def runner(argv: list[str]) -> Ran:
        assert argv[0] == str(install.python), "the house is only ever asked through the venv's python"
        ran = _run(argv[1:], old_package=old_package, cwd=tmp_path)
        return Ran(code=ran.returncode, stdout=ran.stdout, stderr=ran.stderr)

    house = VenvHouse(install, run=runner)

    before = house.show()
    copy = house.backup(tmp_path / "backups")
    house.set_switch(on=False)
    after = house.show()

    assert before == HouseView(
        database=str(install.database), backend="sqlite", exists=True, on=True, members=["AABBCC0000A2"]
    )
    assert copy.backup is not None
    assert Path(copy.backup).is_file()
    assert after.on is False
    assert _switch_row(install.database) == "off"
    assert "soundtouch-zonemaster" in house.distributions()


def test_the_deploy_puts_back_only_its_own_switch_off_through_the_old_package(
    old_package: Path, tmp_path: Path
) -> None:
    """The recovery path's put-back runs BEFORE the install, so against the package being replaced.

    The conditional write is built from that package's own switch table and words, and this proves
    they are enough: a ``switch off`` the OLD service's store wrote after the deploy's is left alone,
    and the deploy's own is put back.
    """
    install = Install(prefix=tmp_path / "opt", state_dir=tmp_path / "state", wheel=tmp_path / "w.whl")
    install.state_dir.mkdir()
    _old_house(old_package, install.database, word="on")

    def runner(argv: list[str]) -> Ran:
        ran = _run(argv[1:], old_package=old_package, cwd=tmp_path)
        return Ran(code=ran.returncode, stdout=ran.stdout, stderr=ran.stderr)

    house = VenvHouse(install, run=runner)
    own = house.set_switch(on=False).changed_at
    assert own is not None
    _old_house(old_package, install.database, word="off")  # a person's `switch off`, by the old store

    left = house.set_switch(on=True, if_changed_at=own)

    assert left.written is False
    assert left.on is False
    assert _switch_row(install.database) == "off"

    assert left.changed_at is not None
    put_back = house.set_switch(on=True, if_changed_at=left.changed_at)
    assert put_back.written is True, "the control: over the stamp the row holds, it writes"
    assert _switch_row(install.database) == "on"


def test_a_switch_off_over_one_already_off_says_it_changed_nothing_through_the_old_package(
    old_package: Path, tmp_path: Path
) -> None:
    """The deploy keeps its switch-off's stamp only when that write turned the house off.

    A person's ``switch off`` can land during the backup, before the deploy's own switch-off, and
    the switch-off runs against the package being replaced. ``changed`` is the whole signal, so it
    must be false through that package's store too - otherwise the deploy would record the fresh
    stamp as its own and turn the house back on over the person's word.
    """
    install = Install(prefix=tmp_path / "opt", state_dir=tmp_path / "state", wheel=tmp_path / "w.whl")
    install.state_dir.mkdir()
    _old_house(old_package, install.database, word="on")

    def runner(argv: list[str]) -> Ran:
        ran = _run(argv[1:], old_package=old_package, cwd=tmp_path)
        return Ran(code=ran.returncode, stdout=ran.stdout, stderr=ran.stderr)

    house = VenvHouse(install, run=runner)
    assert house.set_switch(on=False).changed is True, "the control: over a switch that was on, it changed it"
    house.set_switch(on=True)
    _old_house(old_package, install.database, word="off")  # a person's `switch off`, by the old store

    switched = house.set_switch(on=False)

    assert switched.written is True
    assert switched.changed is False
    assert _switch_row(install.database) == "off"


def test_a_switch_off_a_person_commits_while_the_deploy_waits_is_no_change_through_the_old_package(
    old_package: Path, tmp_path: Path, house_database: str, isolated_config_layers: Path
) -> None:
    """The deploy's switch-off runs against the package being replaced, so that is where it must lock.

    0.5.2's ``write_switch`` takes no lock and reads nothing the helper uses; the helper's own read
    is what ``changed`` comes from. The person's ``switch off`` is written the way the 0.5.2 service
    writes it, the row deleted and another inserted, and held uncommitted while the helper's
    ``set-switch off`` starts behind it on a house the old package created. Once the helper waits
    the person commits, and the helper must answer that its switch-off changed nothing.
    """
    if not house_database.startswith("postgresql"):
        pytest.skip("SQLite's writer takes BEGIN IMMEDIATE before it reads, so no other write can come between")
    made = _run(["-c", _MAKE_A_HOUSE, house_database, "on"], old_package=old_package, cwd=tmp_path)
    assert made.returncode == 0, made.stderr
    host = isolated_config_layers / "etc" / "soundtouch-zonemaster" / "hosts" / f"{socket.gethostname()}.toml"
    host.parent.mkdir(parents=True)
    host.write_text(f'[database]\nurl = "{house_database}"\n', encoding="utf-8")
    argv = [sys.executable, str(HELPER), "--json-bare", "set-switch", "off", "--default", str(tmp_path / "unused")]

    with a_person_writing(house_database) as their:
        their.execute(text("DELETE FROM switch"))
        their.execute(text("INSERT INTO switch (id, word, changed_at) VALUES (1, 'off', '2026-09-29T12:00:00+00:00')"))
        helper = subprocess.Popen(  # noqa: S603 - argv list: this interpreter, and arguments built in this file
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=tmp_path, env=_env(old_package)
        )
        try:
            wait_until_a_writer_waits(house_database)
            their.commit()
            out, err = helper.communicate(timeout=30)
        finally:
            if helper.poll() is None:
                helper.kill()
                helper.communicate()

    assert helper.returncode == 0, err
    switched = json.loads(out)["data"]
    assert switched["changed"] is False, "the person turned the house off, not the deploy"
    assert switched["on"] is False
