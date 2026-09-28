#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["rich-click>=1.9.9", "lib_cli_exit_tools>=2.3.4", "pydantic>=2.13.5"]
# ///
"""Deploy a new wheel of the zone service on THIS machine, with the house left as it was found.

Every deploy used to be written again by hand as a row of shell steps, and each copy was a fresh
chance to stop the unit before the zone had let its speakers go, or to start it back up with the
switch still off. This is those steps, once, tested: shipped to the machine that runs the service
with ``install_service.py``, ``service_venv.py`` and ``_click.py`` beside it, and run there -
``uv run deploy_service.py --wheel <wheel> --json``.

The steps, in the order that matters:

1. **Preconditions**: the wheel is there, ``uv`` answers, and systemd knows the unit. Each refuses
   by name before anything is touched.
2. **Backup** of the house database: SQLite through its backup API into ``--backup-dir`` (a plain
   copy of a WAL database can miss what is still in the ``-wal`` file); PostgreSQL gets a note to
   take a ``pg_dump``, because a server database is not this tool's to copy.
3. **Switch off**, and wait - bounded - until the zone holds no members: the service stands the
   house down by itself, box by box, and a stop before that finishes leaves speakers in a zone
   whose master has gone. If the zone does not empty in time nothing else happens: the switch is
   put back, the unit keeps running, and the answer is exit 1.
4. **Stop** the unit.
5. **Install**, which is ``install_service.py``'s own ``apply`` - not a second copy of it.
6. **Check the venv** holds exactly one of this program's distributions and none of the console
   scripts an earlier name left behind: a new wheel never removes the old distribution, and a unit
   naming a superseded script starts cleanly on the OLD code while the deploy reports success.
7. **Start** the unit and wait - bounded - until systemd says ``active``. ``activating`` is not
   ready, and ``failed`` ends the wait at once. Then it has to STAY active: the unit is
   ``Type=exec``, so ``active`` proves only that the program was started, and a service that
   refuses its configuration exits a second later and is restarted by ``Restart=on-failure``
   again and again. It must stay active, with systemd's restart count unchanged, for twice the
   unit's ``RestartSec`` (at least ``--settle`` seconds) before the house is handed back to it.
8. **Restore the switch** to what it was before step 3.

A failure after the stop leaves the unit stopped and the switch OFF, and says which steps were
done: a half-deployed service is not one to hand a house back to. ``--dry-run`` reads the machine,
prints the plan, and changes nothing.

Exit codes are the house's: 0 deployed (or the plan printed), 1 refused because the zone did not
empty in time and nothing was changed, 2 it could not run or did not finish. The envelope is always
printed on stdout; progress goes to stderr.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import rich_click as click
from _click import current_context, option, run_cli
from install_service import HELPER, PREFIX, STATE_DIR, Install, Ran, Runner, apply, plan, run_command
from install_service import Report as InstallReport
from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

COMMAND = "deploy_service"

UNIT = "soundtouch-multiroom.service"
"""The unit on the house's machine. Kept under its old name on purpose: renaming it is a migration."""

BACKUP_DIR = Path("/var/backups/zonemaster")
"""Where the database copies go: beside the other backups of the machine, not in the state dir."""

DRAIN_TIMEOUT_S = 90.0
"""How long the zone gets to let every box go. The unit's own stop allows 60 s for the same
dissolve, each box with an 8 s HTTP timeout; this is that plus the service noticing the switch."""

START_TIMEOUT_S = 60.0
POLL_S = 1.0

SETTLE_MIN_S = 15.0
"""The least time a started unit must stay active. The window is twice the unit's ``RestartSec``
when that is longer: a crash inside the window shows as a state other than ``active`` or as a
restart counted, and a restart delay shorter than the window cannot hide one between two polls."""

PROGRAM = "soundtouch-zonemaster"
"""The distribution this deploys. Any other installed distribution of ours is a superseded one."""

CONSOLE_SCRIPTS = frozenset({"soundtouch-zonemaster", "soundtouch-zonemaster-service"})
"""The console scripts the program installs; ``tests/test_deploy_service.py`` holds these to
``[project.scripts]``. A script in the venv that looks like ours but is not one of these was left
by an earlier name of the program."""

_OURS = ("zonemaster", "multiroom")
"""What every name this program has ever had contains: soundtouch-multiroom, bose-zonemaster,
zonemaster-service, soundtouch-zonemaster."""

EXIT_OK, EXIT_REFUSED, EXIT_ERROR = 0, 1, 2


class Step(StrEnum):
    BACKUP = "backup"
    SWITCH_OFF = "switch_off"
    DRAIN = "drain"
    STOP = "stop"
    INSTALL = "install"
    CHECK_VENV = "check_venv"
    START = "start"
    RESTORE_SWITCH = "restore_switch"


class DeployRefusedError(Exception):
    """A named refusal. The class name is what the envelope's ``error`` carries."""

    exit_code = EXIT_ERROR


class WheelMissingError(DeployRefusedError):
    """The wheel named on the command line is not there."""


class UvMissingError(DeployRefusedError):
    """``uv`` does not answer, so the installer could not run."""


class UnitMissingError(DeployRefusedError):
    """systemd does not know the unit, so there is nothing to stop or start."""


class HouseUnreadableError(DeployRefusedError):
    """``service_venv.py`` could not read or change the house database."""


class ZoneStillHeldError(DeployRefusedError):
    """The zone did not let its boxes go in time. Nothing was changed, so the answer is no (exit 1)."""

    exit_code = EXIT_REFUSED


class StepFailedError(DeployRefusedError):
    """A command the deploy ran (a stop, a start, the install) failed."""


class VenvNotCleanError(DeployRefusedError):
    """The venv holds a superseded distribution or console script after the install."""


class UnitNotActiveError(DeployRefusedError):
    """The unit did not reach ``active`` in time, or failed on the way."""


# ---------------------------------------------------------------------------------------------
# What was read, and the pure decisions made from it
# ---------------------------------------------------------------------------------------------


class HouseView(BaseModel):
    """What ``service_venv.py show`` says about the house database."""

    database: str
    backend: str
    exists: bool
    on: bool
    members: list[str]


class BackupView(BaseModel):
    database: str
    backend: str
    backup: str | None
    note: str


@dataclass(frozen=True)
class Situation:
    """The machine as read before anything is done. ``house`` is ``None`` before a first install."""

    house: HouseView | None
    unit_active: bool


def plan_steps(situation: Situation) -> list[Step]:
    """The steps this deploy takes, decided from what was read. Pure.

    A switch that is already off is neither turned off nor turned back on. The zone is waited on
    only while the unit runs: a stopped service lets nobody go, and a zone it left behind is not
    one this can empty by waiting.
    """
    house = situation.house
    exists = house is not None and house.exists
    was_on = house is not None and house.exists and house.on
    steps: list[Step] = []
    if exists:
        steps.append(Step.BACKUP)
    if was_on:
        steps.append(Step.SWITCH_OFF)
    if exists and situation.unit_active:
        steps.append(Step.DRAIN)
    if situation.unit_active:
        steps.append(Step.STOP)
    steps += [Step.INSTALL, Step.CHECK_VENV, Step.START]
    if was_on:
        steps.append(Step.RESTORE_SWITCH)
    return steps


def unit_verdict(state: str) -> bool | None:
    """``True`` ready, ``False`` failed, ``None`` keep waiting. Pure.

    Only ``active`` is ready: ``activating`` is systemd still running the start, and a unit with
    ``Restart=`` passes through ``inactive`` between two attempts. ``failed`` will not recover
    inside a wait, so the wait ends there.
    """
    if state == "active":
        return True
    if state == "failed":
        return False
    return None


_SPAN_UNITS = {
    "us": 1e-6,
    "usec": 1e-6,
    "ms": 1e-3,
    "msec": 1e-3,
    "s": 1.0,
    "sec": 1.0,
    "m": 60.0,
    "min": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "d": 86400.0,
}
_SPAN_PART = re.compile(r"(\d+(?:\.\d+)?)([a-z]*)")


def timespan_s(text: str) -> float | None:
    """A systemd time span as ``systemctl show`` prints it (``10s``, ``100ms``, ``1min 30s``), in seconds. Pure.

    ``None`` for anything else - ``infinity``, an empty value, a unit this does not know - so the
    caller falls back to its own bound rather than to a number made up from half a reading.
    """
    parts = text.split()
    if not parts:
        return None
    total = 0.0
    for part in parts:
        match = _SPAN_PART.fullmatch(part)
        if match is None or (match.group(2) or "s") not in _SPAN_UNITS:
            return None
        total += float(match.group(1)) * _SPAN_UNITS[match.group(2) or "s"]
    return total


def settle_window(restart_s: float | None, *, least_s: float = SETTLE_MIN_S) -> float:
    """How long a started unit must stay active: twice its restart delay, and never less than ``least_s``. Pure."""
    return least_s if restart_s is None else max(least_s, 2 * restart_s)


def _ours(name: str) -> bool:
    lowered = name.lower()
    return any(part in lowered for part in _OURS)


def venv_findings(*, distributions: Sequence[str], scripts: Sequence[str]) -> list[str]:
    """What is wrong with the venv after the install, as sentences; empty when it is clean. Pure."""
    findings: list[str] = []
    ours = sorted(name for name in distributions if _ours(name))
    if ours != [PROGRAM]:
        findings.append(
            f"the venv holds {ours or 'none'} of this program's distributions; exactly [{PROGRAM!r}] belongs"
        )
    stale = sorted(name for name in scripts if _ours(name) and name not in CONSOLE_SCRIPTS)
    if stale:
        findings.append(f"superseded console scripts are still in the venv: {', '.join(stale)}")
    missing = sorted(CONSOLE_SCRIPTS - set(scripts))
    if missing:
        findings.append(f"console scripts the unit may name are missing: {', '.join(missing)}")
    return findings


# ---------------------------------------------------------------------------------------------
# The seams: the house database, and the clock. Commands go through install_service's Runner.
# ---------------------------------------------------------------------------------------------


class House(Protocol):
    """The house database, as the installed service's own interpreter reads it."""

    def show(self) -> HouseView: ...

    def set_switch(self, *, on: bool) -> None: ...

    def backup(self, to: Path) -> BackupView: ...

    def distributions(self) -> list[str]: ...


class Clock(Protocol):
    def now(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class RealClock:
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class _Envelope(BaseModel):
    ok: bool
    data: dict[str, object] | None = None
    error: str | None = None
    message: str | None = None


class _Names(BaseModel):
    names: list[str]


class _Switched(BaseModel):
    on: bool


class VenvHouse:
    """The real :class:`House`: ``service_venv.py`` run by the service venv's python."""

    def __init__(self, install: Install, *, run: Runner, helper: Path = HELPER) -> None:
        self._install = install
        self._run = run
        self._helper = helper

    def show(self) -> HouseView:
        return self._ask(HouseView, "show", "--default", str(self._install.database))

    def set_switch(self, *, on: bool) -> None:
        word = "on" if on else "off"
        switched = self._ask(_Switched, "set-switch", word, "--default", str(self._install.database))
        if switched.on is not on:
            message = f"service_venv set-switch {word} left the switch {'on' if switched.on else 'off'}"
            raise HouseUnreadableError(message)

    def backup(self, to: Path) -> BackupView:
        return self._ask(BackupView, "backup", "--to", str(to), "--default", str(self._install.database))

    def distributions(self) -> list[str]:
        return self._ask(_Names, "distributions").names

    def _ask[M: BaseModel](self, model: type[M], *verb: str) -> M:
        """Run one verb and read its envelope as ``model``, or refuse naming what came back instead."""
        argv = [str(self._install.python), str(self._helper), "--json-bare", *verb]
        ran = _ran(self._run, argv)
        try:
            envelope = _Envelope.model_validate_json(ran.stdout)
        except ValidationError:
            message = f"service_venv {verb[0]} answered no envelope ({ran.code}): {ran.output}"
            raise HouseUnreadableError(message) from None
        if ran.code != 0 or not envelope.ok or envelope.data is None:
            message = f"service_venv {verb[0]} refused ({ran.code}): {envelope.error}: {envelope.message}"
            raise HouseUnreadableError(message)
        try:
            return model.model_validate(envelope.data)
        except ValidationError as exc:
            message = (
                f"service_venv {verb[0]} answered an envelope this deploy cannot read: {exc.error_count()} error(s)"
            )
            raise HouseUnreadableError(message) from None


def _ran(run: Runner, argv: list[str]) -> Ran:
    """Run one command; a program that is not there at all is an answer (127), not a traceback."""
    try:
        return run(argv)
    except OSError as exc:
        return Ran(code=127, stdout="", stderr=f"{argv[0]}: {exc}")


def poll_until[T](
    read: Callable[[], T], settled: Callable[[T], bool | None], *, timeout_s: float, clock: Clock
) -> tuple[bool | None, T]:
    """Read until ``settled`` says True or False, or the time is up (None). Returns the last reading.

    The bound is the point: a deploy that waits for ever on a zone or a unit is a deploy nobody
    can tell from a hung one.
    """
    deadline = clock.now() + timeout_s
    while True:
        reading = read()
        verdict = settled(reading)
        if verdict is not None or clock.now() >= deadline:
            return verdict, reading
        clock.sleep(POLL_S)


# ---------------------------------------------------------------------------------------------
# The deploy
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """What to deploy and where, as the command line gave it."""

    install: Install
    unit: str = UNIT
    backup_dir: Path = BACKUP_DIR
    drain_timeout_s: float = DRAIN_TIMEOUT_S
    start_timeout_s: float = START_TIMEOUT_S
    settle_min_s: float = SETTLE_MIN_S


class DeployReport(BaseModel):
    dry_run: bool
    unit: str
    wheel: str
    plan: list[Step]
    done: list[Step]
    database: str | None
    switch_before: bool | None
    """``None`` when there was no database to read it in (a first install)."""
    backup: BackupView | None = None
    install: InstallReport | None = None
    unit_state: str | None = None
    settled_s: float | None = None
    """How long the started unit was watched staying active before the house was handed back."""


def _say(text: str) -> None:
    sys.stderr.write(f"deploy: {text}\n")


def check_preconditions(target: Target, *, run: Runner) -> None:
    """Refuse by name before anything is touched."""
    if not target.install.wheel.is_file():
        message = f"{target.install.wheel}: no such wheel"
        raise WheelMissingError(message)
    uv = _ran(run, ["uv", "--version"])
    if uv.code != 0:
        message = f"uv does not answer ({uv.code}): {uv.output}; put it on the PATH (it lives in /usr/local/bin here)"
        raise UvMissingError(message)
    loaded = _ran(run, ["systemctl", "show", "-p", "LoadState", "--value", target.unit])
    if loaded.code != 0 or loaded.stdout.strip() != "loaded":
        message = f"{target.unit}: systemd does not know it ({loaded.stdout.strip() or loaded.output})"
        raise UnitMissingError(message)


def unit_state(unit: str, *, run: Runner) -> str:
    """What ``systemctl is-active`` prints. Its exit code is non-zero for every state but active."""
    return _ran(run, ["systemctl", "is-active", unit]).stdout.strip() or "unknown"


def unit_property(unit: str, name: str, *, run: Runner) -> str:
    """One property as ``systemctl show -p NAME --value`` prints it; empty when it cannot be read."""
    return _ran(run, ["systemctl", "show", "-p", name, "--value", unit]).stdout.strip()


def read_situation(target: Target, *, run: Runner, house: House) -> Situation:
    view = house.show() if target.install.python.exists() else None
    return Situation(house=view, unit_active=unit_state(target.unit, run=run) == "active")


@dataclass
class _Run:
    """One deploy in progress: what it acts through, and what it has done so far."""

    target: Target
    run: Runner
    house: House
    clock: Clock
    report: DeployReport

    def do(self, step: Step) -> None:
        _say(f"{step}")
        actions: dict[Step, Callable[[], None]] = {
            Step.BACKUP: self._backup,
            Step.SWITCH_OFF: self._switch_off,
            Step.DRAIN: self._drain,
            Step.STOP: self._stop,
            Step.INSTALL: self._install,
            Step.CHECK_VENV: self._check_venv,
            Step.START: self._start,
            Step.RESTORE_SWITCH: self._restore_switch,
        }
        actions[step]()
        self.report.done.append(step)

    def _backup(self) -> None:
        self.report.backup = self.house.backup(self.target.backup_dir)
        _say(self.report.backup.note)

    def _switch_off(self) -> None:
        self.house.set_switch(on=False)

    def _drain(self) -> None:
        verdict, view = poll_until(
            self.house.show,
            lambda v: True if not v.members else None,
            timeout_s=self.target.drain_timeout_s,
            clock=self.clock,
        )
        if verdict is not True:
            message = f"the zone still holds {', '.join(view.members)} after {self.target.drain_timeout_s:g} s"
            raise ZoneStillHeldError(message)

    def _stop(self) -> None:
        self._must(["systemctl", "stop", self.target.unit])

    def _install(self) -> None:
        install = self.target.install
        try:
            self.report.install = apply(install, plan(install), run=self.run)
        except (OSError, RuntimeError) as exc:
            raise StepFailedError(f"install: {exc}") from exc

    def _check_venv(self) -> None:
        bin_dir = self.target.install.venv / "bin"
        scripts = sorted(path.name for path in bin_dir.iterdir()) if bin_dir.is_dir() else []
        findings = venv_findings(distributions=self.house.distributions(), scripts=scripts)
        if findings:
            raise VenvNotCleanError("; ".join(findings))

    def _start(self) -> None:
        unit = self.target.unit
        self._must(["systemctl", "start", unit])
        # Read once the start job has finished: an explicit start is where systemd resets the
        # count, so a restart counted after this is one of THIS start's.
        restarts = self._restarts()
        verdict, state = poll_until(
            lambda: unit_state(unit, run=self.run),
            unit_verdict,
            timeout_s=self.target.start_timeout_s,
            clock=self.clock,
        )
        self.report.unit_state = state
        if verdict is not True:
            message = f"{unit} is {state}, not active (waited up to {self.target.start_timeout_s:g} s)"
            raise UnitNotActiveError(message)
        self._settle(restarts)

    def _settle(self, restarts: str) -> None:
        """Watch the started unit stay active, with no restart counted, for the whole window."""
        unit = self.target.unit
        restart_sec = unit_property(unit, "RestartUSec", run=self.run)
        window = settle_window(timespan_s(restart_sec), least_s=self.target.settle_min_s)
        deadline = self.clock.now() + window
        while self.clock.now() < deadline:
            self.clock.sleep(POLL_S)
            state = unit_state(unit, run=self.run)
            now_restarts = self._restarts()
            self.report.unit_state = state
            if state != "active":
                message = f"{unit} was active and then {state} within {window:g} s (RestartSec {restart_sec or '?'})"
                raise UnitNotActiveError(message)
            if now_restarts != restarts:
                message = (
                    f"{unit} was restarted by systemd within {window:g} s of starting "
                    f"(NRestarts {restarts} -> {now_restarts}): it exits after it starts"
                )
                raise UnitNotActiveError(message)
        self.report.settled_s = window

    def _restarts(self) -> str:
        return unit_property(self.target.unit, "NRestarts", run=self.run)

    def _restore_switch(self) -> None:
        self.house.set_switch(on=True)

    def _must(self, argv: list[str]) -> None:
        ran = _ran(self.run, argv)
        if ran.code != 0:
            message = f"{' '.join(argv)} failed ({ran.code}): {ran.output}"
            raise StepFailedError(message)


def deploy(target: Target, *, run: Runner, house: House, clock: Clock, dry_run: bool = False) -> DeployReport:
    """Read the machine, plan, and - unless ``dry_run`` - carry the plan out. Raises a DeployRefusedError."""
    check_preconditions(target, run=run)
    situation = read_situation(target, run=run, house=house)
    view = situation.house
    report = DeployReport(
        dry_run=dry_run,
        unit=target.unit,
        wheel=str(target.install.wheel),
        plan=plan_steps(situation),
        done=[],
        database=None if view is None else view.database,
        switch_before=view.on if view is not None and view.exists else None,
    )
    if dry_run:
        return report
    progress = _Run(target=target, run=run, house=house, clock=clock, report=report)
    for step in report.plan:
        try:
            progress.do(step)
        except DeployRefusedError as exc:
            _recover(progress, failed=step)
            done = ", ".join(report.done) or "nothing"
            raise type(exc)(f"{exc} (done: {done})") from exc
    return report


def _recover(progress: _Run, *, failed: Step) -> None:
    """Put the switch back when the deploy fails BEFORE the stop; after it, leave the house off.

    Before the stop the service is still the one that was running, so the house can be handed back
    to it exactly as it was. After the stop it cannot: whatever is installed has not been proved.
    """
    report = progress.report
    turned_off = Step.SWITCH_OFF in report.done
    if not turned_off or Step.STOP in report.done or failed == Step.STOP:
        return
    try:
        progress.house.set_switch(on=True)
    except DeployRefusedError as exc:
        _say(f"the switch could not be put back on: {exc}")
        return
    _say("the switch is back on; the unit was not stopped")


# ---------------------------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------------------------


class Envelope(BaseModel):
    ok: bool = True
    command: str
    data: DeployReport


class ErrorEnvelope(BaseModel):
    ok: bool = False
    command: str
    error: str
    message: str


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@option("--json-bare", "as_json_bare", is_flag=True, help="the envelope on one line for jq")
@option("--json", "as_json", is_flag=True, help="the envelope indented (it is always printed)")
@option("--dry-run", "dry_run", is_flag=True, help="read the machine and print the plan; change nothing")
@option(
    "--settle",
    default=SETTLE_MIN_S,
    show_default=True,
    type=float,
    help="least seconds the unit must stay active (twice its RestartSec when longer)",
)
@option("--start-timeout", default=START_TIMEOUT_S, show_default=True, type=float, help="seconds to wait for active")
@option("--drain-timeout", default=DRAIN_TIMEOUT_S, show_default=True, type=float, help="seconds for the zone to empty")
@option("--backup-dir", default=str(BACKUP_DIR), show_default=True, help="where the database copy goes")
@option("--unit", default=UNIT, show_default=True, help="the systemd unit that runs the service")
@option("--state-dir", default=str(STATE_DIR), show_default=True, help="where the house database lives by default")
@option("--prefix", default=str(PREFIX), show_default=True, help="where the service's venv is")
@option("--wheel", required=True, help="the wheel built from this repository")
def cli(  # noqa: PLR0913 - a click callback's signature IS the option list
    *,
    wheel: str,
    prefix: str,
    state_dir: str,
    unit: str,
    backup_dir: str,
    drain_timeout: float,
    start_timeout: float,
    settle: float,
    dry_run: bool,
    as_json: bool,
    as_json_bare: bool,
) -> None:
    """Deploy a wheel of the zone service on THIS machine and hand the house back as it was."""
    del as_json
    indent: int | None = None if as_json_bare else 2
    ctx = current_context()
    install = Install(prefix=Path(prefix), state_dir=Path(state_dir), wheel=Path(wheel))
    target = Target(
        install=install,
        unit=unit,
        backup_dir=Path(backup_dir),
        drain_timeout_s=drain_timeout,
        start_timeout_s=start_timeout,
        settle_min_s=settle,
    )
    house = VenvHouse(install, run=run_command)
    try:
        report = deploy(target, run=run_command, house=house, clock=RealClock(), dry_run=dry_run)
    except DeployRefusedError as exc:
        sys.stderr.write(f"{exc}\n")
        refusal = ErrorEnvelope(command=COMMAND, error=type(exc).__name__, message=str(exc))
        sys.stdout.write(refusal.model_dump_json(indent=indent) + "\n")
        ctx.exit(exc.exit_code)
    sys.stdout.write(Envelope(command=COMMAND, data=report).model_dump_json(indent=indent) + "\n")
    ctx.exit(EXIT_OK)


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(cli, argv=argv, prog_name=COMMAND)


if __name__ == "__main__":
    sys.exit(main())
