"""What an install does to the machine that runs it, and the one thing it must never do.

`tools/install_service.py` is shipped to the service host and run there, which is why its decisions are a
record rather than a sequence of ssh commands: they can be read here, on a temporary directory,
with real files. The steps it plans are proven by the state they leave behind - a venv path that
exists, a switch file whose CONTENT is off - and never by a recorded call.

The rule that earns most of this file: an install must never flip the switch. Somebody may have
turned the house on hours ago, and a deploy that quietly writes `off` over it stops the music
without anybody touching a speaker; one that writes `on` over an `off` starts a zone in a flat
where somebody deliberately stood it down.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from install_service import Install, apply, main, plan


def _install(tmp_path: Path, *, wheel_exists: bool = True) -> Install:
    wheel = tmp_path / "soundtouch_zonemaster-0.1.0-py3-none-any.whl"
    if wheel_exists:
        wheel.write_bytes(b"not really a wheel, and this install never opens it")
    return Install(prefix=tmp_path / "opt", state_dir=tmp_path / "state", wheel=wheel)


class Recorder:
    """The process seam: it records instead of running, and can be told to fail."""

    def __init__(self, *, fails: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fails = fails

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        if self.fails is not None and self.fails in " ".join(argv):
            return 1, f"pretend failure of {self.fails}"
        return 0, "ok"


def test_a_fresh_machine_gets_every_piece(tmp_path: Path) -> None:
    steps = plan(_install(tmp_path))

    assert steps.create_venv
    assert steps.create_state_dir
    assert steps.create_switch


def test_a_venv_that_is_already_there_is_kept(tmp_path: Path) -> None:
    """An update installs the new wheel into the venv that exists; it does not start over."""
    install = _install(tmp_path)
    (install.prefix / ".venv" / "bin").mkdir(parents=True)
    install.python.write_text("#!/bin/sh\n", encoding="utf-8")

    assert not plan(install).create_venv


def test_a_switch_that_says_on_is_never_written_over(tmp_path: Path) -> None:
    """The failure this refuses: a deploy that stands the house down, or starts it up, silently."""
    install = _install(tmp_path)
    install.state_dir.mkdir(parents=True)
    install.switch.write_text("on\n", encoding="utf-8")

    steps = plan(install)
    assert not steps.create_switch

    report = apply(install, steps, run=Recorder())
    assert install.switch.read_text(encoding="utf-8") == "on\n", "the switch is the operator's, not the install's"
    assert "switch" not in report.changed


def test_the_switch_a_fresh_install_writes_says_off(tmp_path: Path) -> None:
    """A first start must not be able to take the house; turning it on stays a deliberate act."""
    install = _install(tmp_path)

    report = apply(install, plan(install), run=Recorder())

    assert install.switch.read_text(encoding="utf-8").strip() == "off"
    assert install.state_dir.is_dir()
    assert "switch" in report.changed


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


def test_a_failing_install_says_which_command_failed(tmp_path: Path) -> None:
    install = _install(tmp_path)

    with pytest.raises(RuntimeError, match="pip install"):
        apply(install, plan(install), run=Recorder(fails="pip install"))


def test_the_report_is_json_a_caller_can_read(tmp_path: Path) -> None:
    install = _install(tmp_path)

    report = apply(install, plan(install), run=Recorder())
    document = json.loads(report.model_dump_json())

    assert document["prefix"] == str(install.prefix)
    assert document["switch_file"] == str(install.switch)
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command driven into a RUNTIME failure, which is the only way to check its envelope.

    It used to catch ``FileNotFoundError`` and ``RuntimeError`` alone, and everything the file
    steps raise is an ``OSError`` of another kind - so a state directory that could not be written
    left lib_cli_exit_tools to end the process on a raw traceback with an empty stdout, while the
    wheel was already installed.
    """
    install = _install(tmp_path)
    install.state_dir.write_text("a file where the directory should be", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "install_service",
            "--json",
            "--wheel",
            str(install.wheel),
            "--prefix",
            str(install.prefix),
            "--state-dir",
            str(install.state_dir),
        ],
    )

    code = main()

    printed = capsys.readouterr()
    document = json.loads(printed.out)
    assert code == 2
    assert document["ok"] is False
    assert document["command"] == "install_service"
    assert document["error"] == "FileExistsError", "the class a caller branches on, not a traceback"
    assert str(install.state_dir) in document["message"]
    assert not install.venv.exists(), "and it refused before it had changed anything"
