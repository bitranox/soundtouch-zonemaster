"""The two ways in: ``python -m`` and the two console scripts, and what they are wired to.

This package publishes TWO commands, which is the whole reason this file is not the template's.
``python -m soundtouch_zonemaster`` is the PROTOTYPE - the measurement run - because the service is
started by a systemd unit that names a script, and a module entry point for it would be a second
way to start the same unit.

Nothing here reaches a speaker: every case is ``--help`` or a usage error, both of which the CLI
answers before a run is ever built.
"""

from __future__ import annotations

import runpy
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from soundtouch_zonemaster import __init__conf__, entry

if TYPE_CHECKING:
    import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _scripts() -> dict[str, str]:
    """What ``pyproject.toml`` publishes as console scripts."""
    data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["scripts"]


def test_the_module_entry_point_is_the_prototype_and_answers_for_help(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["soundtouch-zonemaster", "--help"], raising=False)

    exit_raised = None
    try:
        runpy.run_module("soundtouch_zonemaster.__main__", run_name="__main__")
    except SystemExit as exc:
        exit_raised = exc

    captured = capsys.readouterr()
    assert exit_raised is not None, "__main__ ends the process rather than returning"
    assert exit_raised.code == 0
    assert "Usage:" in captured.out
    assert __init__conf__.shell_command in captured.out


def test_the_module_entry_point_refuses_an_incomplete_argv_rather_than_starting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bare invocation is a click USAGE error: prose on stderr, exit 2, no envelope.

    That is the one gap in this repository's "every CLI answers in JSON" rule, and it is recorded
    rather than hidden. What matters more here is that the run never starts: the addresses the
    prototype needs have no defaults, so a typo cannot make it play to something.
    """
    monkeypatch.setattr(sys, "argv", ["soundtouch-zonemaster"], raising=False)

    exit_raised = None
    try:
        runpy.run_module("soundtouch_zonemaster.__main__", run_name="__main__")
    except SystemExit as exc:
        exit_raised = exc

    captured = capsys.readouterr()
    assert exit_raised is not None
    assert exit_raised.code == 2
    assert captured.out == "", "machine mode was not asked for, so stdout stays empty"
    assert "--bind-ip" in captured.err


def test_the_prototype_console_script_answers_for_help(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["soundtouch-zonemaster", "--help"])

    assert entry.prototype_main() == 0
    assert "Usage:" in capsys.readouterr().out


def test_the_service_console_script_answers_for_help(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["soundtouch-zonemaster-service", "--help"])

    assert entry.service_main() == 0
    captured = capsys.readouterr().out
    assert "Usage:" in captured
    assert "config-deploy" in captured, "the group's two subcommands are part of its help"


def test_exactly_two_console_scripts_are_published_and_both_are_the_entry_module() -> None:
    """The template's ``rename.sh`` writes a snake-case alias beside each script and this project
    has none, so two is the whole list. Both go through ``entry``, which is the only module that
    names a command and the world it runs against."""
    assert _scripts() == {
        "soundtouch-zonemaster": "soundtouch_zonemaster.entry:prototype_main",
        "soundtouch-zonemaster-service": "soundtouch_zonemaster.entry:service_main",
    }


def test_the_published_names_are_the_ones_the_program_reports_itself_as() -> None:
    """A script named one thing and reporting another sends a reader looking for a command that
    does not exist - and the envelope's ``command`` is what an LLM driving this reads."""
    assert set(_scripts()) == {__init__conf__.shell_command, __init__conf__.service_command}


def test_the_module_entry_point_works_as_a_subprocess() -> None:
    """The path an end user takes, rather than the in-process one the tests above take."""
    completed = subprocess.run(
        [sys.executable, "-m", "soundtouch_zonemaster", "--help"],
        capture_output=True,
        timeout=60,
        check=False,
        cwd=PROJECT_ROOT,
        encoding="utf-8",
        errors="replace",
    )

    assert completed.returncode == 0, completed.stderr
    assert "Usage:" in completed.stdout
