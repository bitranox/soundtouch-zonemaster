#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["rich-click>=1.9.9", "lib_cli_exit_tools>=2.3.4", "pydantic>=2.13.5"]
# ///
"""Put the zone service on the machine that runs it: a venv, a state directory, a switch.

This is SHIPPED to the machine that runs the service and run there, rather than driven from an
admin machine as a row of ssh commands: the quoting layers between an admin machine and a container
are where a deploy quietly does something else, and a program on the host can be read, re-run and
tested. It needs ``_click.py`` and ``service_venv.py`` beside it, because the package is not
installed on that machine - installing it is what this does. The header above is what makes that
work: ``uv run install_service.py`` resolves the three dependencies on the target, which needs uv
and a route to an index. Check both before relying on it.

The switch lives in the house database, and only the package can read that database - its config
layers say WHICH one, its store says what is in it. So once the wheel is in, this asks
``service_venv.py seed-switch``, run by the new venv's own python, to make sure a first start finds
the switch OFF. That creates the database when the configured one (or, when no layer names one,
``<state-dir>/zonemaster.sqlite``) is not there yet, which is the one exception to "only the service
creates the house database": that rule keeps a TYPED path from answering a typo with a new, empty
database, and this takes no typed path; and it runs as the same user the unit runs the service as,
so the files it creates are ones the service can open.

Usage, on the target:

    uv run install_service.py --wheel /opt/zonemaster/wheels/soundtouch_zonemaster-0.1.0-....whl

Exit 0 it is installed, 2 it could not run; there is no "nothing to do" answer, because a deploy
always reinstalls the wheel. It is idempotent, and there is one thing it will not do: it never
changes a switch somebody set. Somebody may have turned the house on hours ago, and a deploy that
silently writes `off` over that stops the music with nobody having touched a speaker. A database
that exists without a switch row reads as ON, so that counts as set too: only a database this run
creates is seeded, with OFF, or with the word of an old ``zone.switch`` beside it.

The systemd unit that runs what this installs belongs to the machine's own configuration, not to
this repository: systemd configuration is a property of the machine, the program a property of
this one.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import rich_click as click
from _click import current_context, option, run_cli
from pydantic import BaseModel, ValidationError

COMMAND = "install_service"

PREFIX = Path("/opt/zonemaster")
"""Where the service's own venv goes. Under /opt because it is software this house installed
rather than software the distribution ships, and not under /tmp, which a reboot empties."""

STATE_DIR = Path("/var/lib/zonemaster")
"""The house database, unless a config layer names another. The unit declares it as its
StateDirectory, so systemd creates and owns it; this creates it too, so a first install can put
the database there before any start."""

HELPER = Path(__file__).with_name("service_venv.py")
"""The script the new venv's python runs to reach the house database; shipped beside this one."""

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
        """The switch file from before the database: never written, only read for its word."""
        return self.state_dir / "zone.switch"

    @property
    def database(self) -> Path:
        """The database when no config layer names one; ``service_venv.py`` decides which applies."""
        return self.state_dir / "zonemaster.sqlite"

    def seed_argv(self, helper: Path = HELPER) -> list[str]:
        """The seed, run by THIS venv's python: the only interpreter that has the package."""
        return [
            str(self.python),
            str(helper),
            "--json-bare",
            "seed-switch",
            "--default",
            str(self.database),
            "--legacy-switch-file",
            str(self.switch),
        ]


@dataclass(frozen=True)
class Steps:
    """What this machine still needs. The wheel is always installed and the switch always seeded
    (the seed decides for itself, from the database); the rest is what is missing."""

    create_venv: bool
    create_state_dir: bool


@dataclass(frozen=True)
class Ran:
    """What one command did. The two streams stay apart: the seed's stdout is a JSON envelope."""

    code: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return (self.stdout + self.stderr).strip()


Runner = Callable[[list[str]], Ran]
"""The process seam: every command this runs goes through one of these."""


class Report(BaseModel):
    """What an install did, for the person or the script that ran it."""

    prefix: str
    state_dir: str
    database: str
    """The house database the switch was read in, as the seed named it (never with a password)."""
    switch: str
    """``on``, ``off``, or ``unset`` (no row, which the service reads as on)."""
    wheel: str
    changed: list[str]


def plan(install: Install) -> Steps:
    """Read the machine and say what is missing. Touches nothing."""
    return Steps(
        create_venv=not install.python.exists(),
        create_state_dir=not install.state_dir.is_dir(),
    )


def run_command(argv: list[str]) -> Ran:
    """The real runner: a subprocess, its streams captured apart, never raising for a non-zero exit."""
    finished = subprocess.run(argv, capture_output=True, text=True, check=False, encoding="utf-8", errors="replace")
    return Ran(code=finished.returncode, stdout=finished.stdout, stderr=finished.stderr)


def apply(install: Install, steps: Steps, *, run: Runner = run_command, helper: Path = HELPER) -> Report:
    """Make the machine match, and say what changed.

    ``run`` is the process seam, so a test proves the arguments and the files without a uv on the
    machine it runs on.

    **The order is the design.** Everything that can refuse cheaply is done before anything is
    installed: the wheel and the helper have to exist, and the state directory has to be a
    directory this can write in - each reversible, and each a thing a typed path gets wrong. The
    wheel and its dependency tree go in after them, so a bad ``--state-dir`` cannot leave the
    machine installed, unconfigured, and with no report saying which half had happened. The switch
    comes last because it cannot come earlier: it is read through the package the wheel installs.
    A seed that fails leaves the program installed and the service NOT started - this never starts
    it - and says so by name.
    """
    for needed, what in ((install.wheel, "wheel"), (helper, "helper script (ship it beside this one)")):
        if not needed.is_file():
            message = f"{needed}: no such {what}"
            raise FileNotFoundError(message)
    changed: list[str] = []
    if steps.create_state_dir:
        install.state_dir.mkdir(parents=True, exist_ok=True)
        changed.append("state_dir")
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
    seeded = seed(install, run=run, helper=helper)
    if seeded.written:
        changed.append("switch")
    return Report(
        prefix=str(install.prefix),
        state_dir=str(install.state_dir),
        database=seeded.database,
        switch=seeded.switch,
        wheel=str(install.wheel),
        changed=changed,
    )


class Seeded(BaseModel):
    """What ``service_venv.py seed-switch`` answered: the fields of its report this reads."""

    database: str
    switch: str
    written: bool


class SeedEnvelope(BaseModel):
    """The seed's envelope, either shape: ``data`` on success, ``error`` and ``message`` on a refusal."""

    ok: bool
    data: Seeded | None = None
    error: str | None = None
    message: str | None = None


def seed(install: Install, *, run: Runner = run_command, helper: Path = HELPER) -> Seeded:
    """Ask the new venv to seed the switch, and read its envelope. Raises naming what went wrong."""
    ran = run(install.seed_argv(helper))
    try:
        envelope = SeedEnvelope.model_validate_json(ran.stdout)
    except ValidationError:
        message = f"the switch seed answered no envelope ({ran.code}): {ran.output}"
        raise RuntimeError(message) from None
    if ran.code != 0 or not envelope.ok or envelope.data is None:
        message = f"the switch seed refused ({ran.code}): {envelope.error}: {envelope.message}"
        raise RuntimeError(message)
    return envelope.data


def _must(run: Runner, argv: list[str]) -> None:
    """Run one command, or raise naming it. A failed step must not be reported as an install."""
    ran = run(argv)
    if ran.code != 0:
        message = f"{' '.join(argv[:3])} failed ({ran.code}): {ran.output}"
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
@option("--state-dir", default=str(STATE_DIR), show_default=True, help="where the house database lives by default")
@option("--prefix", default=str(PREFIX), show_default=True, help="where the service's venv goes")
@option("--wheel", required=True, help="the wheel built from this repository")
def cli(*, wheel: str, prefix: str, state_dir: str, as_json: bool, as_json_bare: bool) -> None:
    """Install the zone service on THIS machine. Idempotent, and it never changes a switch somebody set."""
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


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(cli, argv=argv, prog_name=COMMAND)


if __name__ == "__main__":
    sys.exit(main())
