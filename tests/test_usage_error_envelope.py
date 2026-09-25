"""A click usage error, answered in the shape the caller asked for.

Every command here promises that ``--json`` and ``--json-bare`` put an envelope on stdout whatever
happens, and for years the one exception was a USAGE error - a missing option, an unknown one, a
bad value - because click raises it while parsing, before any command body has read the flag. So
an LLM driving these got prose on stderr and an empty stdout for exactly the mistake it is most
likely to make. The facades' ``run_cli`` reads the flags off argv and answers it with the same
refusal envelope as every other failure, exit code still click's 2.

One command per runnable area, driven through its real argv, because the three facades are three
files and a fix that reached only one of them would read as done.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from soundtouch_zonemaster.entry import prototype_main, service_main

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = Path(__file__).resolve().parents[1]


def _prototype(argv: list[str]) -> int:
    sys.argv = ["soundtouch-zonemaster", *argv]
    return prototype_main()


def _service(argv: list[str]) -> int:
    sys.argv = ["soundtouch-zonemaster-service", *argv]
    return service_main()


def _script(path: Path) -> Callable[[list[str]], int]:
    """Run a research script or a tool as it is really run: its own interpreter, its own directory
    first on the path. In-process, the two areas share the module name ``_click`` and whichever is
    imported first answers for both, so the other copy's ``run_cli`` would never be exercised."""

    def run(argv: list[str]) -> int:
        done = subprocess.run(  # noqa: S603 - the test's own interpreter on a file of this repo
            [sys.executable, str(path), *argv], capture_output=True, text=True, encoding="utf-8", check=False
        )
        sys.stdout.write(done.stdout)
        sys.stderr.write(done.stderr)
        return done.returncode

    return run


CASES: dict[str, tuple[Callable[[list[str]], int], list[str], str]] = {
    # area: (entry, argv with --json in it, the command name the envelope carries)
    "prototype (src)": (_prototype, ["--json", "--no-such-option"], "soundtouch-zonemaster"),
    "service group (src)": (_service, ["--json", "config", "--no-such-option"], "soundtouch-zonemaster-service"),
    "research": (_script(ROOT / "research" / "sync_report.py"), ["--json"], "sync_report"),
    "tools": (_script(ROOT / "tools" / "export_public.py"), ["--json"], "export_public"),
}


@pytest.mark.parametrize("area", sorted(CASES))
def test_a_usage_error_under_json_is_an_envelope_on_stdout(
    area: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    entry, argv, command = CASES[area]

    assert entry(argv) == 2
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is False
    assert envelope["command"] == command
    assert envelope["error"] in {"NoSuchOption", "MissingParameter", "UsageError", "BadParameter"}
    assert envelope["message"], "the message is click's own sentence, not an empty string"


def test_json_bare_is_one_line(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    assert _prototype(["--json-bare", "--no-such-option"]) == 2
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert json.loads(out)["error"] == "NoSuchOption"


def test_without_a_json_flag_a_usage_error_stays_prose_on_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    assert _prototype(["--no-such-option"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--no-such-option" in captured.err


def _direct_run_cli_calls(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run_cli"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "lib_cli_exit_tools"
    )


def test_nothing_calls_the_library_s_run_cli_past_its_facade() -> None:
    """The fix lives in the facades, so a command that called the library directly would lose it
    silently - still a working CLI, with prose again on a usage error. This is what makes the next
    command added here unable to forget."""
    facades = {
        ROOT / "src" / "soundtouch_zonemaster" / "adapters" / "cli" / "typed_click.py",
        ROOT / "research" / "_click.py",
        ROOT / "tools" / "_click.py",
    }
    scanned = [p for area in ("src", "research", "tools") for p in (ROOT / area).rglob("*.py") if "/pb/" not in str(p)]
    assert len(scanned) > 40, "the scan must actually reach the tree"
    offenders = [str(p.relative_to(ROOT)) for p in scanned if p not in facades and _direct_run_cli_calls(p)]
    assert offenders == []
    assert all(_direct_run_cli_calls(p) == 1 for p in facades), "each facade is the one place that calls it"
