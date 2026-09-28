"""A deploy of the zone service, carried out against a fake systemd and a fake house.

`tools/deploy_service.py` runs on the service host and changes three things a person notices: the
unit, the venv, and the switch. Everything here drives its real ``deploy`` through its two seams -
the command runner (systemctl, uv) and the house database - with fakes that keep STATE rather than
a script of answers: the fake systemd is active until it is stopped, the fake zone lets its members
go only once the switch is off. A test is judged by the state the deploy leaves behind (unit
active, switch as it was, venv clean) and by the order the runner saw, never by a mock's call count
alone.

The pure decisions - which steps to take, when a unit counts as up, what makes a venv unclean - are
tested as the functions they are. The real `VenvHouse` is driven once against the real
`tools/service_venv.py` in a subprocess, which is what proves the two scripts agree on argv and on
the envelope.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from lib_cli_exit_tools import SigIntInterrupt
from service_database import created_by_the_service

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from deploy_service import (
    CONSOLE_SCRIPTS,
    POLL_S,
    BackupView,
    DeployInterruptedError,
    HangupError,
    HouseUnreadableError,
    HouseView,
    Situation,
    Step,
    StepFailedError,
    StepTimedOutError,
    Target,
    UnitMissingError,
    UnitNotActiveError,
    UvMissingError,
    VenvHouse,
    VenvNotCleanError,
    VenvUnreadableError,
    WheelMissingError,
    ZoneStillHeldError,
    deploy,
    hangups_raise,
    main,
    plan_steps,
    settle_window,
    timespan_s,
    unit_verdict,
    venv_findings,
)
from install_service import CommandTimedOutError, Install, Ran

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = Path(__file__).resolve().parents[1]
UNIT = "soundtouch-multiroom.service"
_SEEDED = json.dumps(
    {"ok": True, "command": "service_venv seed-switch", "data": {"database": "db", "switch": "on", "written": False}}
)


class FakeClock:
    """Time that passes only when the deploy sleeps, so a bounded wait is bounded in the test too."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class FakeSystemd:
    """The runner: a unit with a state, uv that answers, and the installer's commands recorded.

    ``start_states`` is what ``is-active`` reports after a start, one entry per poll, the last one
    repeated for ever - ``["activating", "active"]`` is a unit that takes one poll to come up, and
    ``["active", "active", "failed"]`` one that exits two polls after systemd called it started.
    ``restart_counts`` is what ``NRestarts`` reads, the same way: a unit that systemd restarts
    between two polls is ``active`` at both, and only the count shows it. ``restart_sec`` is the
    unit's ``RestartSec`` as ``systemctl show`` prints it.
    """

    def __init__(
        self,
        *,
        loaded: bool = True,
        active: bool = True,
        start_states: tuple[str, ...] = ("active",),
        uv: bool = True,
        on_stop: Callable[[], None] | None = None,
        on_install: Callable[[], None] | None = None,
        restart_counts: tuple[int, ...] = (0,),
        restart_sec: str = "10s",
        stop_fails: bool = False,
        stop_hangs: bool = False,
    ) -> None:
        self.loaded = loaded
        self.state = "active" if active else "inactive"
        self.start_states = list(start_states)
        self.restart_counts = list(restart_counts)
        self.restart_sec = restart_sec
        self.stop_fails = stop_fails
        self.stop_hangs = stop_hangs
        self.uv = uv
        self.on_stop = on_stop
        self.on_install = on_install
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> Ran:
        self.calls.append(argv)
        if argv[:2] == ["uv", "--version"]:
            if not self.uv:
                raise FileNotFoundError(2, "No such file or directory", "uv")
            return Ran(code=0, stdout="uv 0.11.0\n", stderr="")
        if argv[0] == "systemctl":
            return self._systemctl(argv[1:])
        if "seed-switch" in argv:
            return Ran(code=0, stdout=_SEEDED, stderr="")
        if argv[:3] == ["uv", "pip", "install"] and self.on_install is not None:
            self.on_install()
        return Ran(code=0, stdout="", stderr="")

    def _systemctl(self, args: list[str]) -> Ran:
        if args[:2] == ["show", "-p"]:
            return Ran(code=0, stdout=f"{self._property(args[2])}\n", stderr="")
        if args[0] == "is-active":
            shown = self._after_start() if self.state == "starting" else self.state
            return Ran(code=0 if shown == "active" else 3, stdout=f"{shown}\n", stderr="")
        if args[0] == "stop":
            if self.stop_hangs:
                message = "systemctl stop soundtouch-multiroom.service did not finish within 300 s and was killed"
                raise CommandTimedOutError(message)
            if self.stop_fails:
                return Ran(code=1, stdout="", stderr="Job for the unit failed; it may be half stopped.")
            if self.on_stop is not None:
                self.on_stop()
            self.state = "inactive"
        elif args[0] == "start":
            self.state = "starting"
        return Ran(code=0, stdout="", stderr="")

    def _after_start(self) -> str:
        return self.start_states.pop(0) if len(self.start_states) > 1 else self.start_states[0]

    def _property(self, name: str) -> str:
        if name == "LoadState":
            return "loaded" if self.loaded else "not-found"
        if name == "RestartUSec":
            return self.restart_sec
        if name == "NRestarts":
            counts = self.restart_counts
            return str(counts.pop(0) if len(counts) > 1 else counts[0])
        message = f"the fake systemd has no property {name}"
        raise AssertionError(message)

    def verbs(self) -> list[str]:
        """The state-changing commands in the order they ran: what a person would have seen happen."""
        seen: list[str] = []
        for argv in self.calls:
            if argv[0] == "systemctl" and argv[1] in ("stop", "start"):
                seen.append(argv[1])
            elif argv[:3] == ["uv", "pip", "install"]:
                seen.append("install")
        return seen


class FakeHouse:
    """The house database: a switch and a zone that lets its members go only while switched off.

    ``drain_after`` is how many reads the zone needs, after the switch goes off, to be empty -
    ``None`` is a zone that never empties.
    """

    def __init__(
        self,
        *,
        on: bool = True,
        members: tuple[str, ...] = ("AABBCC0000A1", "AABBCC0000A2"),
        drain_after: int | None = 1,
        exists: bool = True,
        distributions: tuple[str, ...] = ("pydantic", "soundtouch-zonemaster"),
        clock: FakeClock | None = None,
        interrupted_while_off: BaseException | None = None,
    ) -> None:
        self.on = on
        self.members = list(members)
        self.drain_after = drain_after
        self.exists = exists
        self.dists = list(distributions)
        self.reads_while_off = 0
        self.switched: list[bool] = []
        self.clock = clock
        self.interrupted_while_off = interrupted_while_off
        self.switched_at: list[tuple[bool, float]] = []
        self.backups: list[Path] = []

    def show(self) -> HouseView:
        if not self.on and self.interrupted_while_off is not None:
            raise self.interrupted_while_off
        if not self.on and self.drain_after is not None:
            self.reads_while_off += 1
            if self.reads_while_off > self.drain_after:
                self.members = []
        return HouseView(database="db", backend="sqlite", exists=self.exists, on=self.on, members=self.members)

    def set_switch(self, *, on: bool) -> None:
        self.on = on
        self.switched.append(on)
        if self.clock is not None:
            self.switched_at.append((on, self.clock.t))

    def backup(self, to: Path) -> BackupView:
        self.backups.append(to)
        return BackupView(database="db", backend="sqlite", backup=str(to / "copy.sqlite"), note="copied")

    def distributions(self) -> list[str]:
        return self.dists


def _target(tmp_path: Path, *, venv: bool = True, scripts: tuple[str, ...] = tuple(sorted(CONSOLE_SCRIPTS))) -> Target:
    wheel = tmp_path / "soundtouch_zonemaster-0.5.3-py3-none-any.whl"
    wheel.write_bytes(b"not really a wheel")
    install = Install(prefix=tmp_path / "opt", state_dir=tmp_path / "state", wheel=wheel)
    if venv:
        bin_dir = install.venv / "bin"
        bin_dir.mkdir(parents=True)
        for name in ("python", *scripts):
            (bin_dir / name).write_text("#!/bin/sh\n", encoding="utf-8")
    return Target(install=install, backup_dir=tmp_path / "backups", drain_timeout_s=5, start_timeout_s=5)


# --- the whole deploy -------------------------------------------------------------------------


def test_a_deploy_hands_the_house_back_as_it_found_it(tmp_path: Path) -> None:
    systemd, house = FakeSystemd(), FakeHouse(on=True)

    report = deploy(_target(tmp_path), run=systemd, house=house, clock=FakeClock())

    assert systemd.verbs() == ["stop", "install", "start"]
    assert house.switched == [False, True], "off for the stop, on again once the unit is active"
    assert house.on is True
    assert systemd.state != "inactive"
    assert report.done == report.plan
    assert report.plan == [
        Step.BACKUP,
        Step.SWITCH_OFF,
        Step.DRAIN,
        Step.STOP,
        Step.INSTALL,
        Step.CHECK_VENV,
        Step.START,
        Step.RESTORE_SWITCH,
    ]
    assert house.backups == [tmp_path / "backups"]


def test_the_unit_is_stopped_only_after_the_zone_has_let_every_box_go(tmp_path: Path) -> None:
    """A stop while boxes are still in the zone leaves them following a master that has gone."""
    house = FakeHouse(drain_after=3)
    stopped_with: list[list[str]] = []
    systemd = FakeSystemd(on_stop=lambda: stopped_with.append(list(house.members)))

    deploy(_target(tmp_path), run=systemd, house=house, clock=FakeClock())

    assert stopped_with == [[]]


def test_a_switch_that_was_off_stays_off_and_is_never_touched(tmp_path: Path) -> None:
    house = FakeHouse(on=False, members=())

    report = deploy(_target(tmp_path), run=FakeSystemd(), house=house, clock=FakeClock())

    assert house.switched == []
    assert house.on is False
    assert Step.RESTORE_SWITCH not in report.plan
    assert report.switch_before is False


def test_a_zone_that_does_not_empty_changes_nothing_and_answers_no(tmp_path: Path) -> None:
    """Exit 1: the house is in use. The switch goes back on and the unit is never stopped."""
    systemd, house, clock = FakeSystemd(), FakeHouse(drain_after=None), FakeClock()

    with pytest.raises(ZoneStillHeldError) as caught:
        deploy(_target(tmp_path), run=systemd, house=house, clock=clock)

    assert caught.value.exit_code == 1
    assert 5 <= clock.t <= 5 + POLL_S, "the wait is --drain-timeout (5 s here) and one poll, not patience"
    assert systemd.verbs() == [], "nothing stopped, nothing installed"
    assert house.on is True
    assert "AABBCC0000A1" in str(caught.value)


def test_a_stop_that_fails_leaves_the_house_off_and_installs_nothing(tmp_path: Path) -> None:
    """A failed stop can leave the unit half stopped, and a half-stopped service is not one to hand
    the house back to: the switch stays off, and nothing is installed over it."""
    systemd, house = FakeSystemd(stop_fails=True), FakeHouse()

    with pytest.raises(StepFailedError, match="stop") as caught:
        deploy(_target(tmp_path), run=systemd, house=house, clock=FakeClock())

    assert house.on is False
    assert house.switched == [False]
    assert "install" not in systemd.verbs()
    assert "done: backup, switch_off, drain" in str(caught.value)


def test_a_stop_that_hangs_is_refused_by_name_with_the_house_left_off(tmp_path: Path) -> None:
    systemd, house = FakeSystemd(stop_hangs=True), FakeHouse()

    with pytest.raises(StepTimedOutError, match="did not finish"):
        deploy(_target(tmp_path), run=systemd, house=house, clock=FakeClock())

    assert house.on is False
    assert "install" not in systemd.verbs()


def test_an_interrupt_during_the_drain_says_where_it_left_the_house(tmp_path: Path) -> None:
    """Ctrl-C (or a dropped session) mid-deploy used to end in a traceback and an empty stdout.

    The person who pressed it then had to work out for themselves that the switch was off and the
    unit still running. The refusal says so, and names the command that turns the house back on.
    """
    systemd, house = FakeSystemd(), FakeHouse(interrupted_while_off=SigIntInterrupt())

    with pytest.raises(DeployInterruptedError) as caught:
        deploy(_target(tmp_path), run=systemd, house=house, clock=FakeClock())

    message = str(caught.value)
    assert "SIGINT during drain" in message
    assert "the switch is OFF" in message
    assert "the unit was not stopped" in message
    assert "switch on" in message
    assert systemd.verbs() == []
    assert caught.value.exit_code == 2


def test_an_interrupt_during_the_install_is_not_reported_as_a_failed_install(tmp_path: Path) -> None:
    """The library's signal exceptions are RuntimeErrors, which the install step turns into a failure."""

    def hang_up() -> None:
        raise HangupError

    systemd, house = FakeSystemd(on_install=hang_up), FakeHouse()

    with pytest.raises(DeployInterruptedError, match="SIGHUP during install") as caught:
        deploy(_target(tmp_path), run=systemd, house=house, clock=FakeClock())

    assert "the unit is stopped" in str(caught.value)
    assert "the switch is OFF" in str(caught.value)


def test_a_hangup_raises_instead_of_ending_the_process_where_it_stands() -> None:
    """A dropped ``pct exec`` session sends SIGHUP, whose default ends the process mid-step."""
    before = signal.getsignal(signal.SIGHUP)

    with hangups_raise(), pytest.raises(HangupError):
        os.kill(os.getpid(), signal.SIGHUP)
        time.sleep(1)

    assert signal.getsignal(signal.SIGHUP) == before


def test_activating_is_not_ready(tmp_path: Path) -> None:
    """The switch goes back on only once systemd says active, not when the start command returns."""
    systemd = FakeSystemd(start_states=("activating", "activating", "active"))
    house = FakeHouse()

    report = deploy(_target(tmp_path), run=systemd, house=house, clock=FakeClock())

    polls = [argv for argv in systemd.calls if argv[:2] == ["systemctl", "is-active"]]
    assert len(polls) >= 1 + 3, "one read before, then until active"
    assert report.unit_state == "active"
    assert house.on is True


def test_a_unit_that_fails_to_start_leaves_the_house_off(tmp_path: Path) -> None:
    """After the stop nothing installed is proven, so the house is not handed back to it."""
    systemd, house = FakeSystemd(start_states=("activating", "failed")), FakeHouse()

    with pytest.raises(UnitNotActiveError, match="failed") as caught:
        deploy(_target(tmp_path), run=systemd, house=house, clock=FakeClock())

    assert house.on is False
    assert house.switched == [False]
    assert "install" in str(caught.value), "the refusal says how far the deploy got"


def test_a_unit_that_exits_after_it_became_active_is_not_deployed(tmp_path: Path) -> None:
    """``active`` proves only that the program was exec'd (the unit is ``Type=exec``).

    A service that refuses its configuration exits a moment later, and with ``Restart=on-failure``
    systemd starts it again and again. Handing the house back to that is handing it to nothing, so
    the unit has to stay active for a while first.
    """
    systemd, house = FakeSystemd(start_states=("activating", "active", "active", "failed")), FakeHouse()

    with pytest.raises(UnitNotActiveError, match="failed"):
        deploy(_target(tmp_path), run=systemd, house=house, clock=FakeClock())

    assert house.on is False, "the switch is not handed back to a unit that did not stay up"
    assert house.switched == [False]


def test_a_restart_between_two_polls_is_caught_by_the_restart_count(tmp_path: Path) -> None:
    """With a short RestartSec a crashing unit can read ``active`` at every poll; NRestarts cannot hide it."""
    systemd = FakeSystemd(start_states=("active",), restart_counts=(0, 0, 0, 1), restart_sec="100ms")
    house = FakeHouse()

    with pytest.raises(UnitNotActiveError, match="restart"):
        deploy(_target(tmp_path), run=systemd, house=house, clock=FakeClock())

    assert house.on is False


def test_the_unit_stays_active_for_longer_than_its_restart_delay_before_the_house_is_handed_back(
    tmp_path: Path,
) -> None:
    """A crash inside the window is seen only if the window outlasts one RestartSec; twice it is."""
    clock = FakeClock()
    house = FakeHouse(clock=clock)

    deploy(_target(tmp_path), run=FakeSystemd(restart_sec="30s"), house=house, clock=clock)

    restored_at = [at for on, at in house.switched_at if on]
    assert house.on is True
    assert restored_at, "the switch was handed back"
    assert restored_at[0] >= 2 * 30, "not before the unit outlived two restart delays"


def test_a_unit_that_stays_activating_is_refused_when_the_wait_runs_out(tmp_path: Path) -> None:
    clock = FakeClock()

    with pytest.raises(UnitNotActiveError, match="activating"):
        deploy(_target(tmp_path), run=FakeSystemd(start_states=("activating",)), house=FakeHouse(), clock=clock)

    assert 5 <= clock.t < 60, "bounded by --start-timeout, not by patience"


def test_a_superseded_console_script_stops_the_deploy_before_the_house_is_touched(tmp_path: Path) -> None:
    """A unit naming the old script would start cleanly on the OLD code while the deploy said success.

    A new wheel never removes what an earlier name left, so a leftover that is there before the
    install is still there after it. Finding it only then cost a stopped unit and a house left
    off; finding it first costs nothing.
    """
    systemd, house = FakeSystemd(), FakeHouse()
    target = _target(tmp_path, scripts=(*sorted(CONSOLE_SCRIPTS), "zonemaster-service"))

    with pytest.raises(VenvNotCleanError, match="zonemaster-service"):
        deploy(target, run=systemd, house=house, clock=FakeClock())

    assert systemd.verbs() == []
    assert house.switched == []
    assert house.on is True


def test_two_distributions_of_this_program_stop_the_deploy_before_the_house_is_touched(tmp_path: Path) -> None:
    systemd, house = FakeSystemd(), FakeHouse(distributions=("soundtouch-multiroom", "soundtouch-zonemaster"))

    with pytest.raises(VenvNotCleanError, match="soundtouch-multiroom"):
        deploy(_target(tmp_path), run=systemd, house=house, clock=FakeClock())

    assert systemd.verbs() == []
    assert house.switched == []


def test_a_leftover_is_named_by_a_dry_run_too(tmp_path: Path) -> None:
    target = _target(tmp_path, scripts=(*sorted(CONSOLE_SCRIPTS), "zonemaster-service"))

    with pytest.raises(VenvNotCleanError, match="zonemaster-service"):
        deploy(target, run=FakeSystemd(), house=FakeHouse(), clock=FakeClock(), dry_run=True)


def test_a_venv_the_install_leaves_without_the_service_script_is_never_started(tmp_path: Path) -> None:
    """What only the install can show - a script it did not put there - is still checked after it."""
    systemd, house = FakeSystemd(), FakeHouse()

    with pytest.raises(VenvNotCleanError, match="missing: soundtouch-zonemaster-service"):
        deploy(_target(tmp_path, scripts=("soundtouch-zonemaster",)), run=systemd, house=house, clock=FakeClock())

    assert systemd.verbs() == ["stop", "install"]
    assert house.on is False


def test_a_venv_whose_scripts_cannot_be_listed_is_refused_by_name(tmp_path: Path) -> None:
    """A PermissionError from listing the venv is a refusal in the envelope, not a traceback."""
    target = _target(tmp_path)
    bin_dir = target.install.venv / "bin"
    bin_dir.chmod(0o300)
    try:
        with pytest.raises(VenvUnreadableError, match="could not be listed"):
            deploy(target, run=FakeSystemd(), house=FakeHouse(), clock=FakeClock())
    finally:
        bin_dir.chmod(0o755)


def test_a_dry_run_reads_and_plans_but_changes_nothing(tmp_path: Path) -> None:
    systemd, house = FakeSystemd(), FakeHouse()

    report = deploy(_target(tmp_path), run=systemd, house=house, clock=FakeClock(), dry_run=True)

    assert report.dry_run is True
    assert report.plan[0] == Step.BACKUP
    assert report.done == []
    assert systemd.verbs() == []
    assert house.switched == []
    assert house.backups == []


def test_a_first_install_has_no_house_to_read_and_nothing_to_restore(tmp_path: Path) -> None:
    """No venv yet: the installer creates the database with the switch off, and it stays off."""
    systemd, house = FakeSystemd(active=False), FakeHouse()

    report = deploy(_target(tmp_path, venv=False), run=systemd, house=house, clock=FakeClock(), dry_run=True)

    assert report.plan == [Step.INSTALL, Step.CHECK_VENV, Step.START]
    assert report.switch_before is None


# --- preconditions ----------------------------------------------------------------------------


def test_a_missing_wheel_is_refused_before_anything_runs(tmp_path: Path) -> None:
    target = _target(tmp_path)
    target.install.wheel.unlink()
    systemd = FakeSystemd()

    with pytest.raises(WheelMissingError):
        deploy(target, run=systemd, house=FakeHouse(), clock=FakeClock())

    assert systemd.calls == []


def test_uv_that_is_not_on_the_path_is_named(tmp_path: Path) -> None:
    with pytest.raises(UvMissingError, match="PATH"):
        deploy(_target(tmp_path), run=FakeSystemd(uv=False), house=FakeHouse(), clock=FakeClock())


def test_a_unit_systemd_does_not_know_is_named(tmp_path: Path) -> None:
    systemd = FakeSystemd(loaded=False)

    with pytest.raises(UnitMissingError, match="not-found"):
        deploy(_target(tmp_path), run=systemd, house=FakeHouse(), clock=FakeClock())

    assert systemd.verbs() == []


# --- the pure decisions -----------------------------------------------------------------------


def _view(*, on: bool, exists: bool = True) -> HouseView:
    return HouseView(database="db", backend="sqlite", exists=exists, on=on, members=[])


@pytest.mark.parametrize(
    ("situation", "expected"),
    [
        (Situation(house=None, unit_active=False), ["install", "check_venv", "start"]),
        (
            Situation(house=_view(on=True), unit_active=True),
            ["backup", "switch_off", "drain", "stop", "install", "check_venv", "start", "restore_switch"],
        ),
        (
            Situation(house=_view(on=False), unit_active=True),
            ["backup", "drain", "stop", "install", "check_venv", "start"],
        ),
        (
            Situation(house=_view(on=True), unit_active=False),
            ["backup", "switch_off", "install", "check_venv", "start", "restore_switch"],
        ),
        (Situation(house=_view(on=True, exists=False), unit_active=False), ["install", "check_venv", "start"]),
    ],
)
def test_the_plan_follows_from_what_was_read(situation: Situation, expected: list[str]) -> None:
    assert [str(step) for step in plan_steps(situation)] == expected


@pytest.mark.parametrize(
    ("state", "verdict"),
    [("active", True), ("failed", False), ("activating", None), ("inactive", None), ("deactivating", None)],
)
def test_only_active_is_ready_and_only_failed_ends_the_wait(state: str, verdict: bool | None) -> None:
    assert unit_verdict(state) is verdict


@pytest.mark.parametrize(
    ("printed", "seconds"),
    [
        ("10s", 10.0),
        ("100ms", 0.1),
        ("1min 30s", 90.0),
        ("2h", 7200.0),
        ("500us", 0.0005),
        ("0", 0.0),
        ("infinity", None),
        ("", None),
        ("10 parsecs", None),
    ],
)
def test_a_restart_delay_is_read_as_systemctl_prints_it(printed: str, seconds: float | None) -> None:
    read = timespan_s(printed)
    if seconds is None:
        assert read is None
    else:
        assert read == pytest.approx(seconds)


@pytest.mark.parametrize(("restart_s", "window"), [(None, 15.0), (0.1, 15.0), (10.0, 20.0), (30.0, 60.0)])
def test_the_settle_window_outlasts_two_restart_delays_and_never_falls_below_its_floor(
    restart_s: float | None, window: float
) -> None:
    assert settle_window(restart_s) == window


def test_a_clean_venv_has_no_findings() -> None:
    assert venv_findings(distributions=["pydantic", "soundtouch-zonemaster"], scripts=[*CONSOLE_SCRIPTS, "uv"]) == []


def test_one_distribution_installed_twice_is_a_finding() -> None:
    findings = venv_findings(
        distributions=["soundtouch-zonemaster", "soundtouch-zonemaster"], scripts=sorted(CONSOLE_SCRIPTS)
    )

    assert len(findings) == 1
    assert "soundtouch-zonemaster', 'soundtouch-zonemaster'" in findings[0]


def test_a_venv_without_the_service_script_is_a_finding() -> None:
    findings = venv_findings(distributions=["soundtouch-zonemaster"], scripts=["soundtouch-zonemaster"])

    assert findings == ["console scripts the unit may name are missing: soundtouch-zonemaster-service"]


def test_the_console_scripts_are_the_ones_the_project_declares() -> None:
    """The deploy's list is a copy (it runs where the package's metadata is not at hand); pin it."""
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]

    assert set(declared) == CONSOLE_SCRIPTS


# --- the command line and the real helper -----------------------------------------------------


def test_the_command_answers_a_refusal_in_its_envelope(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Driven into a runtime refusal, which is the only way to see the refusal envelope."""
    code = main(["--json", "--wheel", str(tmp_path / "missing.whl"), "--prefix", str(tmp_path / "opt")])

    document = json.loads(capsys.readouterr().out)
    assert code == 2
    assert document == {
        "ok": False,
        "command": "deploy_service",
        "error": "WheelMissingError",
        "message": f"{tmp_path / 'missing.whl'}: no such wheel",
    }


class HelperRunner:
    """Runs ``service_venv.py`` for real, with this interpreter standing in for the venv's python."""

    def __init__(self, install: Install, *, cwd: Path) -> None:
        self.install = install
        self.cwd = cwd

    def __call__(self, argv: list[str]) -> Ran:
        assert argv[0] == str(self.install.python), "the house is only ever asked through the venv's python"
        env = {
            **os.environ,
            "SOUNDTOUCH_ZONEMASTER___DATABASE__URL": "",
            "SOUNDTOUCH_ZONEMASTER___DATABASE__PASSWORD": "",
        }
        finished = subprocess.run(  # noqa: S603 - argv list: this interpreter and the deploy's own argv
            [sys.executable, *argv[1:]], capture_output=True, text=True, check=False, cwd=self.cwd, env=env
        )
        return Ran(code=finished.returncode, stdout=finished.stdout, stderr=finished.stderr)


def test_the_real_helper_answers_what_the_deploy_reads(tmp_path: Path) -> None:
    install = Install(prefix=tmp_path / "opt", state_dir=tmp_path / "state", wheel=tmp_path / "w.whl")
    install.state_dir.mkdir()
    created_by_the_service(install.database)
    house = VenvHouse(install, run=HelperRunner(install, cwd=tmp_path))

    house.set_switch(on=False)
    view = house.show()
    copy = house.backup(tmp_path / "backups")

    assert view == HouseView(database=str(install.database), backend="sqlite", exists=True, on=False, members=[])
    assert copy.backup is not None
    assert Path(copy.backup).is_file()
    assert "soundtouch-zonemaster" in house.distributions()


@pytest.mark.parametrize(
    ("stdout", "code", "named"),
    [
        ("Traceback (most recent call last):", 1, "no envelope"),
        (
            '{"ok": false, "command": "c", "error": "StoreMissingError", "message": "gone"}',
            2,
            "StoreMissingError: gone",
        ),
        ('{"ok": true, "command": "c", "data": {"database": "db"}}', 0, "cannot read"),
    ],
)
def test_a_helper_answer_the_deploy_cannot_use_is_named(tmp_path: Path, stdout: str, code: int, named: str) -> None:
    """A venv too old for the helper answers a traceback; that must refuse, never read as a house."""
    install = Install(prefix=tmp_path / "opt", state_dir=tmp_path / "state", wheel=tmp_path / "w.whl")
    house = VenvHouse(install, run=lambda _argv: Ran(code=code, stdout=stdout, stderr=""))

    with pytest.raises(HouseUnreadableError, match=named):
        house.show()
