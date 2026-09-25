"""The orchestration of a run: joining, holding the zone, switching station, the late join.

These four helpers are what a run IS, and until now nothing reached them - the two tests that
touched `run` replaced it wholesale, so its body never executed under test. They take their master
through the `ZonePort` seam, so a stand-in here is a real substitution at a real boundary rather
than a patched module global.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from soundtouch_zonemaster.adapters.cli.prototype import OptionsInput
from soundtouch_zonemaster.application.outcome import ExitCode
from soundtouch_zonemaster.application.prototype import join_slaves, late_join, run_until_done
from soundtouch_zonemaster.domain.enums import Encryption, JoinMode
from soundtouch_zonemaster.domain.station import Station, StationRequest
from soundtouch_zonemaster.entry import prototype_main as main

if TYPE_CHECKING:
    import pytest

    from soundtouch_zonemaster.application.options import Options


@dataclass
class FakeZone:
    """A master that records what the run loop asked of it, and can refuse a named speaker."""

    unreachable: frozenset[str] = frozenset()
    station: Station | None = None
    joined: list[str] = field(default_factory=list[str])
    played: list[str] = field(default_factory=list[str])

    async def add_slave(self, ip: str) -> None:
        if ip in self.unreachable:
            msg = f"no route to {ip}"
            raise ConnectionError(msg)
        self.joined.append(ip)

    async def play(self, request: StationRequest) -> Station:
        self.played.append(request.name)
        self.station = Station(url_id=1, playback_url=request.playback_url, name=request.name, content_item_xml="")
        return self.station


def _options(**overrides: object) -> Options:
    base: dict[str, object] = {
        "bind_ip": "10.0.0.1",
        "device_id": "AABBCC000001",
        "slaves": ["10.0.0.2"],
        "preset_from": "10.0.0.2",
        "preset": 1,
        "preset2": 2,
        "switch_after": 0.0,
        "duration": 0.0,
        "encryption": Encryption.NONE.value,
        "late_slaves": [],
        "join_after": 30.0,
        "join_mode": JoinMode.SCHEDULE.value,
        "ignore_selects": False,
    }
    return OptionsInput.model_validate(base | overrides).record()


def _quiet(_kind: str, _text: str) -> None:
    """A narrator that says nothing. None of the nine calls below reads a line."""


def _station(name: str) -> StationRequest:
    return StationRequest(playback_url=f"http://example.invalid/{name}", name=name, content_item_xml="")


async def test_every_reachable_slave_is_counted() -> None:
    zone = FakeZone()
    assert await join_slaves(zone, ("10.0.0.2", "10.0.0.3"), log=_quiet) == 2
    assert zone.joined == ["10.0.0.2", "10.0.0.3"]


async def test_one_unreachable_speaker_does_not_end_the_run() -> None:
    """The documented rule: a speaker that will not answer is not fatal to the others."""
    zone = FakeZone(unreachable=frozenset({"10.0.0.3"}))
    assert await join_slaves(zone, ("10.0.0.2", "10.0.0.3", "10.0.0.4"), log=_quiet) == 2
    assert zone.joined == ["10.0.0.2", "10.0.0.4"], "the reachable ones still joined"


async def test_no_reachable_speaker_counts_zero() -> None:
    zone = FakeZone(unreachable=frozenset({"10.0.0.2"}))
    assert await join_slaves(zone, ("10.0.0.2",), log=_quiet) == 0, "run() turns this into ExitCode.REFUSED"


async def test_the_zone_is_held_for_the_duration_and_nothing_fires_by_default() -> None:
    zone = FakeZone()
    await run_until_done(zone, _options(duration=0.05), log=_quiet, station_source=_never_called, poll_s=0.01)
    assert zone.played == [], "no --switch-after and no --late-slave means nothing happens"


async def _never_called(_ip: str, _number: int) -> StationRequest:
    msg = "the station source must not be consulted when --switch-after is unset"
    raise AssertionError(msg)


async def test_the_station_switch_fires_once_after_switch_after() -> None:
    zone = FakeZone()
    calls: list[int] = []

    async def source(_ip: str, number: int) -> StationRequest:
        calls.append(number)
        return _station("second")

    await run_until_done(
        zone, _options(duration=0.2, switch_after=0.01), log=_quiet, station_source=source, poll_s=0.01
    )
    assert calls == [2], "preset2, exactly once, however many times the loop goes round"
    assert zone.played == ["second"]


async def test_the_late_slave_joins_once_after_join_after() -> None:
    zone = FakeZone()
    opts = _options(duration=0.2, join_after=0.01, late_slaves=["10.0.0.9"])
    await run_until_done(zone, opts, log=_quiet, station_source=_never_called, poll_s=0.01)
    assert zone.joined == ["10.0.0.9"], "joined once, not once per poll"


async def test_the_restart_arm_replays_the_station_for_everyone() -> None:
    """The control arm: the stream restarts for the whole zone, gap and all."""
    zone = FakeZone()
    await zone.play(_station("first"))
    await late_join(zone, _options(late_slaves=["10.0.0.9"], join_mode=JoinMode.RESTART.value), log=_quiet)
    assert zone.joined == ["10.0.0.9"]
    assert zone.played == ["first", "first"], "played again, which is what makes it the control arm"


async def test_the_schedule_arm_does_not_replay() -> None:
    zone = FakeZone()
    await zone.play(_station("first"))
    await late_join(zone, _options(late_slaves=["10.0.0.9"], join_mode=JoinMode.SCHEDULE.value), log=_quiet)
    assert zone.played == ["first"], "the joiner is placed on the running stream instead"


async def test_a_late_slave_that_will_not_answer_still_leaves_the_zone_playing() -> None:
    zone = FakeZone(unreachable=frozenset({"10.0.0.9"}))
    await zone.play(_station("first"))
    await late_join(zone, _options(late_slaves=["10.0.0.9"], join_mode=JoinMode.SCHEDULE.value), log=_quiet)
    assert zone.joined == []
    assert zone.played == ["first"], "the run carries on for the speakers already in"


def test_main_returns_what_the_run_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    """`run_zone` is a parameter, so the substitution is an argument, not a patched global."""
    monkeypatch.setattr(
        "sys.argv",
        ["soundtouch-zonemaster", "--bind-ip", "10.0.0.1", "--slave", "10.0.0.2", "--preset-from", "10.0.0.2"],
    )

    async def fake_run(_options: Options) -> int:
        return ExitCode.OK

    assert main(run_zone=fake_run) == ExitCode.OK


def test_main_reports_two_when_the_run_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["soundtouch-zonemaster", "--bind-ip", "10.0.0.1", "--slave", "10.0.0.2", "--preset-from", "10.0.0.2"],
    )

    async def boom(_options: Options) -> int:
        msg = "no route to host"
        raise OSError(msg)

    assert main(run_zone=boom) == ExitCode.ERROR
    captured = capsys.readouterr()
    assert "no route to host" in captured.err, "the fatal line belongs on stderr, beside OptionsError"
    assert "no route to host" not in captured.out, "a caller redirecting stdout must not collect it"


def test_main_exits_zero_on_a_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl-C is how a run is ended on purpose; run() dissolves the zone in its finally."""
    monkeypatch.setattr(
        "sys.argv",
        ["soundtouch-zonemaster", "--bind-ip", "10.0.0.1", "--slave", "10.0.0.2", "--preset-from", "10.0.0.2"],
    )

    async def interrupted(_options: Options) -> int:
        raise KeyboardInterrupt

    assert main(run_zone=interrupted) == ExitCode.OK
