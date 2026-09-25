"""The command line's own behaviour: what it refuses before it ever touches a speaker.

Everything here runs the real ``main`` with a real argv and no network: the checks under test all
sit ahead of the first socket. The Room5 refusal is the one that matters most - that address
is a Lifestyle console whose input flips when it is sent a POWER, so a run that reaches it is
audible in the house and hard to undo.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from soundtouch_zonemaster.__init__conf__ import shell_command, version
from soundtouch_zonemaster.adapters.logging.narration import log
from soundtouch_zonemaster.application.options import default_device_id
from soundtouch_zonemaster.entry import prototype_main as main

if TYPE_CHECKING:
    from soundtouch_zonemaster.application.options import Options

ROOM5 = "192.168.0.30"
BASE_ARGV = [
    "soundtouch-zonemaster",
    "--bind-ip",
    "192.168.0.190",
    "--preset-from",
    "192.168.0.21",
]


def _argv(*extra: str) -> list[str]:
    """Build an otherwise-valid argv with ``extra`` appended."""
    return [*BASE_ARGV, *extra]


def test_the_default_device_id_is_twelve_hex_digits() -> None:
    assert re.fullmatch(r"[0-9A-F]{12}", default_device_id())


def test_version_answers_with_no_other_option_and_starts_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--version`` is eager: it must print and exit before any required option is missed."""
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster", "--version"])
    assert main() == 0
    assert capsys.readouterr().out == f"{shell_command} {version}\n"


def test_a_log_line_carries_its_kind_and_text(capsys: pytest.CaptureFixture[str]) -> None:
    log("master", "listening")
    out = capsys.readouterr().out
    assert "master" in out
    assert "listening" in out


def test_a_device_id_that_is_not_twelve_hex_digits_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", _argv("--slave", "192.168.0.21", "--device-id", "nothex"))
    assert main() == 2
    assert "12 hex digits" in capsys.readouterr().err


@pytest.mark.usefixtures("a_house_that_protects_its_console")
def test_room5_is_refused_as_a_slave(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("sys.argv", _argv("--slave", ROOM5))
    assert main() == 1
    assert "Room5" in capsys.readouterr().err


@pytest.mark.usefixtures("a_house_that_protects_its_console")
def test_room5_is_refused_as_a_late_slave(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The late-joining list is checked too; only checking --slave would leave the console reachable."""
    monkeypatch.setattr("sys.argv", _argv("--slave", "192.168.0.21", "--late-slave", ROOM5))
    assert main() == 1
    assert "Room5" in capsys.readouterr().err


def test_ignore_selects_defaults_off_and_can_be_turned_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read where the run reads it, so the whole way from argv into Options is what is asserted.

    ``run_zone`` is a parameter of ``main``, so the substitution here is an argument rather than a
    patched global, and no socket is opened. A test of ``parse_options`` alone would have left the
    click option and the callback's signature - the two halves that actually drift - uncovered.
    """
    seen: list[Options] = []

    async def capture(options: Options) -> int:
        seen.append(options)
        return 0

    monkeypatch.setattr("sys.argv", _argv("--slave", "192.168.0.21"))
    assert main(run_zone=capture) == 0
    monkeypatch.setattr("sys.argv", _argv("--slave", "192.168.0.21", "--ignore-selects"))
    assert main(run_zone=capture) == 0
    assert [options.ignore_selects for options in seen] == [False, True]


def test_a_ctrl_c_ends_a_run_cleanly_rather_than_as_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-C is how every measurement run in the flat was ended, and it exited 2 the whole time.

    The signal handler lib_cli_exit_tools installs raises SigIntInterrupt, a RuntimeError, so the
    `except KeyboardInterrupt` below it never fired and the generic handler reported "could not
    run" for a run that had just dissolved its zone correctly.
    """
    import lib_cli_exit_tools

    async def interrupted(_options: Options) -> int:
        raise lib_cli_exit_tools.SigIntInterrupt("Aborted (SIGINT).")

    monkeypatch.setattr("sys.argv", _argv("--slave", "192.168.0.21"))

    assert main(run_zone=interrupted) == 0
