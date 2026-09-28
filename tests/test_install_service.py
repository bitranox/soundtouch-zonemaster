"""What an install does to the machine that runs it, and the one thing it must never do.

`tools/install_service.py` is shipped to the service host and run there, which is why its decisions are a
record rather than a sequence of ssh commands: they can be read here, on a temporary directory,
with real files. The steps it plans are proven by the state they leave behind - a venv path that
exists, a house database whose switch row says off - and never by a recorded call alone.

The rule that earns most of this file: an install must never flip the switch. Somebody may have
turned the house on hours ago, and a deploy that quietly writes `off` over it stops the music
without anybody touching a speaker; one that writes `on` over an `off` starts a zone in a flat
where somebody deliberately stood it down. And a first start must find it OFF: the switch lives in
the database now, and the unit no longer names a switch file that could carry an `off` in.

The switch step runs the real `tools/service_venv.py` in a subprocess, with this suite's own
interpreter standing in for the service venv's python (it has the package installed, which is the
one thing that script needs). Only `uv` is faked.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest
from service_database import created_by_the_service

from soundtouch_zonemaster.composition import open_house_store

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from install_service import (
    COMMAND_TIMEOUT_S,
    INSTALL_TIMEOUT_S,
    CommandTimedOutError,
    Install,
    Ran,
    apply,
    main,
    plan,
    run_command,
    timeout_for,
)

_DATABASE_URL_ENV = "SOUNDTOUCH_ZONEMASTER___DATABASE__URL"
_DATABASE_PASSWORD_ENV = "SOUNDTOUCH_ZONEMASTER___DATABASE__PASSWORD"


def _install(tmp_path: Path, *, wheel_exists: bool = True) -> Install:
    wheel = tmp_path / "soundtouch_zonemaster-0.1.0-py3-none-any.whl"
    if wheel_exists:
        wheel.write_bytes(b"not really a wheel, and this install never opens it")
    return Install(prefix=tmp_path / "opt", state_dir=tmp_path / "state", wheel=wheel)


_SEEDED = {
    "ok": True,
    "command": "service_venv seed-switch",
    "data": {"database": "x", "switch": "off", "written": True, "configured": True},
}


class Recorder:
    """The process seam: it records instead of running, and can be told to fail.

    The seed's answer is a canned envelope here; the tests that care what the seed DOES use
    :class:`RealSeed`, which runs it.
    """

    def __init__(self, *, fails: str | None = None, seed_stdout: str = json.dumps(_SEEDED)) -> None:
        self.calls: list[list[str]] = []
        self.fails = fails
        self.seed_stdout = seed_stdout

    def __call__(self, argv: list[str]) -> Ran:
        self.calls.append(argv)
        if self.fails is not None and self.fails in " ".join(argv):
            return Ran(code=1, stdout="", stderr=f"pretend failure of {self.fails}")
        if "seed-switch" in argv:
            return Ran(code=0, stdout=self.seed_stdout, stderr="")
        return Ran(code=0, stdout="ok", stderr="")


class RealSeed(Recorder):
    """``uv`` is recorded; the seed is RUN, by this interpreter in place of the venv's python.

    Its environment is this process's, which the suite's conftest has already cut off from the
    machine's config layers, and it starts in ``cwd`` so no checkout ``.env`` is found above it.
    ``database.url`` and ``database.password`` are pinned in the ENV layer, which beats every file
    layer, so a developer's private defaults file cannot move the database these tests look at: an
    empty url means "no layer names one", and the installer's default applies.
    """

    def __init__(self, install: Install, *, cwd: Path, database_url: str = "") -> None:
        super().__init__()
        self.install = install
        self.cwd = cwd
        self.database_url = database_url

    def __call__(self, argv: list[str]) -> Ran:
        if argv[:1] != [str(self.install.python)]:
            return super().__call__(argv)
        self.calls.append(argv)
        env = {**os.environ, _DATABASE_URL_ENV: self.database_url, _DATABASE_PASSWORD_ENV: ""}
        finished = subprocess.run(  # noqa: S603 - argv list: this interpreter and the installer's own argv
            [sys.executable, *argv[1:]], capture_output=True, text=True, check=False, cwd=self.cwd, env=env
        )
        return Ran(code=finished.returncode, stdout=finished.stdout, stderr=finished.stderr)


def _switch_row(path: Path) -> str | None:
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute("SELECT word FROM switch WHERE id = 1").fetchone()
    return None if row is None else str(row[0])


def _set_switch(path: Path, *, on: bool) -> None:
    store = open_house_store(str(path), password=None, log=lambda _kind, _text: None)
    store.open(exclusive=False, create=False)
    try:
        store.set_switch(on=on)
    finally:
        store.close()


def test_a_fresh_machine_gets_every_piece(tmp_path: Path) -> None:
    steps = plan(_install(tmp_path))

    assert steps.create_venv
    assert steps.create_state_dir


def test_a_venv_that_is_already_there_is_kept(tmp_path: Path) -> None:
    """An update installs the new wheel into the venv that exists; it does not start over."""
    install = _install(tmp_path)
    (install.prefix / ".venv" / "bin").mkdir(parents=True)
    install.python.write_text("#!/bin/sh\n", encoding="utf-8")

    assert not plan(install).create_venv


def test_a_fresh_machine_starts_with_the_switch_off_in_the_database(tmp_path: Path) -> None:
    """A first start must not be able to take the house; turning it on stays a deliberate act."""
    install = _install(tmp_path)

    report = apply(install, plan(install), run=RealSeed(install, cwd=tmp_path))

    assert _switch_row(install.database) == "off"
    assert report.switch == "off"
    assert report.database == str(install.database)
    assert "switch" in report.changed
    assert not install.switch.exists(), "the switch file is gone: nothing would read it"


def test_a_switch_that_says_on_is_never_written_over(tmp_path: Path) -> None:
    """The failure this refuses: a deploy that stands the house down, or starts it up, silently."""
    install = _install(tmp_path)
    install.state_dir.mkdir()
    created_by_the_service(install.database)
    _set_switch(install.database, on=True)

    report = apply(install, plan(install), run=RealSeed(install, cwd=tmp_path))

    assert _switch_row(install.database) == "on", "the switch is the operator's, not the install's"
    assert report.switch == "on"
    assert "switch" not in report.changed


def test_the_configured_database_is_the_one_written(tmp_path: Path) -> None:
    """database.url in a config layer wins over the installer's default, as it does for the service."""
    install = _install(tmp_path)
    configured = tmp_path / "elsewhere" / "house.sqlite"
    configured.parent.mkdir()

    report = apply(install, plan(install), run=RealSeed(install, cwd=tmp_path, database_url=str(configured)))

    assert _switch_row(configured) == "off"
    assert report.database == str(configured)
    assert not install.database.exists(), "the default path is only for a machine that names no database"


def test_a_machine_that_names_no_database_is_warned_that_the_service_will_not_open_this_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default path is the installer's own, not the service's: the service names no default.

    It refuses to start until a layer names ``database.url``, and if that ends up naming another
    database, the service creates that one with no switch row - which it reads as ON. A quiet
    seed of the default would read as the job done, so it is said, in the report and on stderr.
    """
    install = _install(tmp_path)

    report = apply(install, plan(install), run=RealSeed(install, cwd=tmp_path))

    assert len(report.warnings) == 1
    assert "database.url" in report.warnings[0]
    assert str(install.database) in report.warnings[0]
    assert report.warnings[0] in capsys.readouterr().err


def test_a_configured_database_draws_no_warning(tmp_path: Path) -> None:
    install = _install(tmp_path)
    configured = tmp_path / "elsewhere" / "house.sqlite"
    configured.parent.mkdir()

    report = apply(install, plan(install), run=RealSeed(install, cwd=tmp_path, database_url=str(configured)))

    assert report.warnings == []


def test_an_old_switch_file_that_says_on_is_what_a_new_database_starts_with(tmp_path: Path) -> None:
    """An upgrade from before the database: writing OFF would override the operator's last word."""
    install = _install(tmp_path)
    install.state_dir.mkdir()
    install.switch.write_text("on\n", encoding="utf-8")

    report = apply(install, plan(install), run=RealSeed(install, cwd=tmp_path))

    assert _switch_row(install.database) == "on"
    assert report.switch == "on"


def test_the_switch_is_seeded_by_the_new_venv_after_the_wheel_is_in(tmp_path: Path) -> None:
    """Only the venv's own python has the package the database is read through, so it comes last."""
    install = _install(tmp_path)
    recorder = Recorder()

    apply(install, plan(install), run=recorder)

    assert recorder.calls[-1][0] == str(install.python)
    assert recorder.calls[-1][2:4] == ["--json-bare", "seed-switch"]
    assert recorder.calls[-2][1:3] == ["pip", "install"]


def test_the_wheel_is_installed_into_this_prefix_s_own_interpreter(tmp_path: Path) -> None:
    """The one thing a wrong argument here would do quietly: install into the SYSTEM python."""
    install = _install(tmp_path)
    recorder = Recorder()

    apply(install, plan(install), run=recorder)

    venv_call = [argv for argv in recorder.calls if argv[1:2] == ["venv"]]
    install_call = [argv for argv in recorder.calls if argv[1:3] == ["pip", "install"]]
    assert venv_call == [["uv", "venv", str(install.prefix / ".venv")]]
    assert install_call, "the wheel has to be installed"
    assert "--python" in install_call[0], "never the interpreter uv happens to find"
    assert install_call[0][install_call[0].index("--python") + 1] == str(install.python)
    assert str(install.wheel) == install_call[0][-1]


def test_a_wheel_that_is_not_there_is_refused_before_anything_is_touched(tmp_path: Path) -> None:
    install = _install(tmp_path, wheel_exists=False)
    recorder = Recorder()

    with pytest.raises(FileNotFoundError):
        apply(install, plan(install), run=recorder)

    assert recorder.calls == [], "nothing may run before the input is known to exist"
    assert not install.state_dir.exists()


def test_a_helper_that_was_not_shipped_is_refused_before_anything_is_touched(tmp_path: Path) -> None:
    """Without it the switch cannot be seeded, and finding that out after the install is too late."""
    install = _install(tmp_path)
    recorder = Recorder()

    with pytest.raises(FileNotFoundError, match="helper"):
        apply(install, plan(install), run=recorder, helper=tmp_path / "service_venv.py")

    assert recorder.calls == []
    assert not install.state_dir.exists()


def test_a_failing_install_says_which_command_failed(tmp_path: Path) -> None:
    install = _install(tmp_path)

    with pytest.raises(RuntimeError, match="pip install"):
        apply(install, plan(install), run=Recorder(fails="pip install"))


def test_a_seed_that_refuses_is_named_not_reported_as_an_install(tmp_path: Path) -> None:
    install = _install(tmp_path)
    refusal = {"ok": False, "command": "service_venv seed-switch", "error": "StoreError", "message": "db: locked"}

    with pytest.raises(RuntimeError, match="StoreError: db: locked"):
        apply(install, plan(install), run=Recorder(seed_stdout=json.dumps(refusal)))


def test_a_seed_that_prints_no_envelope_is_named(tmp_path: Path) -> None:
    """A venv too old to run the helper answers a traceback, and that must not read as success."""
    install = _install(tmp_path)

    with pytest.raises(RuntimeError, match="no envelope"):
        apply(install, plan(install), run=Recorder(seed_stdout="Traceback (most recent call last):"))


def test_the_report_is_json_a_caller_can_read(tmp_path: Path) -> None:
    install = _install(tmp_path)

    report = apply(install, plan(install), run=Recorder())
    document = json.loads(report.model_dump_json())

    assert document["prefix"] == str(install.prefix)
    assert document["switch"] == "off"
    assert sorted(document["changed"]) == ["state_dir", "switch", "venv", "wheel"]


def test_a_state_directory_that_cannot_be_made_stops_before_anything_is_installed(tmp_path: Path) -> None:
    """Half an install is the outcome this order exists to make impossible.

    The wheel and its whole dependency tree used to go in first, and only then was the state
    directory made; a path that cannot be a directory - a typo naming an existing FILE, a
    read-only parent - failed after the machine had already been changed, with no report of what
    had been done. So the cheap, reversible pieces are proved first and the install follows them.
    """
    install = _install(tmp_path)
    install.state_dir.write_text("a file where the directory should be", encoding="utf-8")
    recorder = Recorder()

    with pytest.raises(OSError):
        apply(install, plan(install), run=recorder)

    assert recorder.calls == [], "nothing may be installed before the machine is known to take it"
    assert not install.venv.exists()


def test_a_deploy_that_cannot_write_prints_a_refusal_a_caller_can_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command driven into a RUNTIME failure, which is the only way to check its envelope.

    It used to catch ``FileNotFoundError`` and ``RuntimeError`` alone, and everything the file
    steps raise is an ``OSError`` of another kind - so a state directory that could not be written
    left lib_cli_exit_tools to end the process on a raw traceback with an empty stdout, while the
    wheel was already installed.
    """
    install = _install(tmp_path)
    install.state_dir.write_text("a file where the directory should be", encoding="utf-8")

    code = main(
        [
            "--json",
            "--wheel",
            str(install.wheel),
            "--prefix",
            str(install.prefix),
            "--state-dir",
            str(install.state_dir),
        ]
    )

    printed = capsys.readouterr()
    document = json.loads(printed.out)
    assert code == 2
    assert document["ok"] is False
    assert document["command"] == "install_service"
    assert document["error"] == "FileExistsError", "the class a caller branches on, not a traceback"
    assert str(install.state_dir) in document["message"]
    assert not install.venv.exists(), "and it refused before it had changed anything"


def test_a_command_that_does_not_finish_is_killed_and_named() -> None:
    """A deploy stuck on one command cannot be told from a hung one, so every command has a bound."""
    started = time.monotonic()

    with pytest.raises(CommandTimedOutError, match=r"did not finish within 0\.5 s"):
        run_command([sys.executable, "-c", "import time; time.sleep(5)"], timeout_s=0.5)

    assert time.monotonic() - started < 4, "refused at the bound, not when the command chose to end"


@pytest.mark.parametrize(
    ("argv", "bound"),
    [
        (["uv", "pip", "install", "--python", "p", "w.whl"], INSTALL_TIMEOUT_S),
        (["uv", "venv", "/opt/zonemaster/.venv"], INSTALL_TIMEOUT_S),
        (["uv", "--version"], COMMAND_TIMEOUT_S),
        (["systemctl", "stop", "soundtouch-multiroom.service"], COMMAND_TIMEOUT_S),
    ],
)
def test_only_the_install_itself_gets_the_long_bound(argv: list[str], bound: float) -> None:
    assert timeout_for(argv) == bound
