#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["rich-click>=1.9.9", "lib_cli_exit_tools>=2.3.4", "pydantic>=2.13.5"]
# ///
"""Put the zone service on the machine that runs it: a venv, a state directory, a switch.

This is SHIPPED to the machine that runs the service and run there, rather than driven from an
admin machine as a row of ssh commands: the quoting layers between an admin machine and a container
are where a deploy quietly does something else, and a program on the host can be read, re-run and
tested. It needs ``_click.py`` beside it, the same two files ``research/capture_zone.py`` needs,
because the package is not installed on that machine - installing it is what this does. The header
above is what makes that work: ``uv run install_service.py`` resolves the three dependencies on the
target, which needs uv and a route to an index. Check both before relying on it.

Usage, on the target:

    uv run install_service.py --wheel /opt/zonemaster/wheels/soundtouch_zonemaster-0.1.0-....whl

Exit 0 it is installed, 2 it could not run; there is no "nothing to do" answer, because a deploy
always reinstalls the wheel. It is idempotent, and there is one
thing it will not do: it never writes over a switch file that already exists. Somebody may have
turned the house on hours ago, and a deploy that silently writes `off` over that stops the music
with nobody having touched a speaker.

The systemd unit that runs what this installs belongs to the machine's own configuration, not to
this repository: systemd configuration is a property of the machine, the program a property of
this one.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import rich_click as click
from _click import current_context, option, run_cli
from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Callable

COMMAND = "install_service"

PREFIX = Path("/opt/zonemaster")
"""Where the service's own venv goes. Under /opt because it is software this house installed
rather than software the distribution ships, and not under /tmp, which a reboot empties."""

STATE_DIR = Path("/var/lib/zonemaster")
"""The switch and the state file. The unit declares it as its StateDirectory, so systemd creates
and owns it; this creates it too, so a first install can put the switch there before any start."""

OFF = "off\n"
"""What a fresh install writes into the switch: the service starts, watches, and takes nothing."""

EXIT_OK, EXIT_ERROR = 0, 2
"""There is no "nothing to do" here, so the house's middle code is one this command cannot return.

The wheel is reinstalled on every run - that is what a deploy IS, and ``--reinstall-package`` is
there because a rebuilt wheel keeps its version - so a run that gets as far as reporting has always
changed something: a second, completely idempotent install reports ``changed: ["wheel"]`` and
exit 0.
"""


@dataclass(frozen=True)
class Install:
    """Where this install puts things on the machine it is running on."""

    prefix: Path
    state_dir: Path
    wheel: Path

    @property
    def venv(self) -> Path:
        return self.prefix / ".venv"

    @property
    def python(self) -> Path:
        """The interpreter every install goes into. Named, because uv's default is another one."""
        return self.venv / "bin" / "python"

    @property
    def switch(self) -> Path:
        return self.state_dir / "zone.switch"


@dataclass(frozen=True)
class Steps:
    """What this machine still needs. The wheel is always installed; the rest is what is missing."""

    create_venv: bool
    create_state_dir: bool
    create_switch: bool


class Report(BaseModel):
    """What an install did, for the person or the script that ran it."""

    prefix: str
    state_dir: str
    switch_file: str
    wheel: str
    changed: list[str]


def plan(install: Install) -> Steps:
    """Read the machine and say what is missing. Touches nothing."""
    return Steps(
        create_venv=not install.python.exists(),
        create_state_dir=not install.state_dir.is_dir(),
        create_switch=not install.switch.exists(),
    )


def _run(argv: list[str]) -> tuple[int, str]:
    finished = subprocess.run(argv, capture_output=True, text=True, check=False, encoding="utf-8", errors="replace")
    return finished.returncode, (finished.stdout + finished.stderr).strip()


def apply(install: Install, steps: Steps, *, run: Callable[[list[str]], tuple[int, str]] = _run) -> Report:
    """Make the machine match, and say what changed.

    ``run`` is the process seam, so a test proves the arguments and the files without a uv on the
    machine it runs on.

    **The order is the design.** Everything that can refuse is done before anything is installed:
    the wheel has to exist, the state directory has to be a directory this can write in, and the
    switch has to be writable there - each cheap, each reversible, and each a thing a typed path
    gets wrong. The wheel and its dependency tree go in last, so a deploy that cannot finish has
    not started. The other way round, a bad ``--state-dir`` would leave the machine installed,
    unconfigured, and with no report saying which half had happened.
    """
    if not install.wheel.is_file():
        message = f"{install.wheel}: no such wheel"
        raise FileNotFoundError(message)
    changed: list[str] = []
    if steps.create_state_dir:
        install.state_dir.mkdir(parents=True, exist_ok=True)
        changed.append("state_dir")
    if steps.create_switch:
        install.switch.write_text(OFF, encoding="utf-8")
        changed.append("switch")
    if steps.create_venv:
        _must(run, ["uv", "venv", str(install.venv)])
        changed.append("venv")
    _must(
        run,
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(install.python),
            "--reinstall-package",
            "soundtouch-zonemaster",
            str(install.wheel),
        ],
    )
    changed.append("wheel")
    return Report(
        prefix=str(install.prefix),
        state_dir=str(install.state_dir),
        switch_file=str(install.switch),
        wheel=str(install.wheel),
        changed=changed,
    )


def _must(run: Callable[[list[str]], tuple[int, str]], argv: list[str]) -> None:
    """Run one command, or raise naming it. A failed step must not be reported as an install."""
    code, output = run(argv)
    if code != 0:
        message = f"{' '.join(argv[:3])} failed ({code}): {output}"
        raise RuntimeError(message)


class Envelope(BaseModel):
    """The machine-readable result."""

    ok: bool
    command: str
    data: Report
    skipped: list[str] = []


class ErrorEnvelope(BaseModel):
    ok: bool = False
    command: str
    error: str
    message: str


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@option("--json-bare", "as_json_bare", is_flag=True, help="as --json, on one line for jq")
@option("--json", "as_json", is_flag=True, help="print the JSON envelope on stdout")
@option("--state-dir", default=str(STATE_DIR), show_default=True, help="where the switch and the state file live")
@option("--prefix", default=str(PREFIX), show_default=True, help="where the service's venv goes")
@option("--wheel", required=True, help="the wheel built from this repository")
def cli(*, wheel: str, prefix: str, state_dir: str, as_json: bool, as_json_bare: bool) -> None:
    """Install the zone service on THIS machine. Idempotent, and it never flips the switch."""
    indent: int | None = None if as_json_bare else 2
    ctx = current_context()
    install = Install(prefix=Path(prefix), state_dir=Path(state_dir), wheel=Path(wheel))
    try:
        report = apply(install, plan(install))
    except (OSError, RuntimeError) as exc:
        # OSError and not FileNotFoundError: every file step here raises a different one of its
        # subclasses - a path that is a file where a directory belongs, a read-only parent, a full
        # disk - and each must end in the envelope, not in a traceback with an empty stdout.
        document = ErrorEnvelope(command=COMMAND, error=type(exc).__name__, message=str(exc))
        sys.stderr.write(f"{exc}\n")
        if as_json or as_json_bare:
            sys.stdout.write(document.model_dump_json(indent=indent) + "\n")
        ctx.exit(EXIT_ERROR)
    envelope = Envelope(ok=True, command=COMMAND, data=report)
    sys.stdout.write(envelope.model_dump_json(indent=indent) + "\n")
    ctx.exit(EXIT_OK)


def main() -> int:
    return run_cli(cli, argv=None, prog_name=COMMAND)


if __name__ == "__main__":
    sys.exit(main())
