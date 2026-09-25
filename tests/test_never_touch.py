"""The one refusal that protects a real room, now that it is a setting rather than a constant.

Until this rebuild the address of the Room5 Lifestyle console was written into the source, so
the only way to protect a second box - or to lift the protection for a deliberate test against the
console itself - was to edit the program. It is ``[prototype] never_touch`` now, read through the
same six layers as everything else.

The wheel ships the list EMPTY, because which box must be left alone is a fact about one flat. The
house names its console in its own host layer, which is what the ``a_house_that_protects_its_console``
fixture writes. What has NOT changed is what a person in that house sees: the message is byte for
byte the sentence the constant produced, and so is the exit code. The first test below is the one
that says so, the second that the wheel alone protects nobody; the rest are the things a setting
can do that a constant could not.

Nothing here reaches a speaker. Every case is refused, or substituted through ``main``'s own
``run_zone`` parameter, before the first socket.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from soundtouch_zonemaster.adapters.config.loader import ENV_PREFIX
from soundtouch_zonemaster.entry import prototype_main as main

if TYPE_CHECKING:
    from pathlib import Path

    from soundtouch_zonemaster.application.options import Options

ROOM5 = "192.168.0.30"
ELSEWHERE = "192.168.0.99"

_REFUSAL = "refused: Room5 (192.168.0.30) is the Lifestyle console"


def _argv(*extra: str) -> list[str]:
    """An otherwise-valid prototype argv with ``extra`` appended."""
    return ["soundtouch-zonemaster", "--bind-ip", "192.168.0.190", "--preset-from", "192.168.0.21", *extra]


async def _must_not_run(_options: Options) -> int:
    """Stands in for the run, and fails loudly if a refusal let one start."""
    raise AssertionError("the prototype must not start when an address was refused")


@pytest.mark.usefixtures("a_house_that_protects_its_console")
def test_the_house_s_host_layer_refuses_the_console_in_the_words_the_constant_used(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The sentence and the code are the archive's, which is what makes this a move and not a change."""
    monkeypatch.setattr("sys.argv", _argv("--slave", ROOM5))

    assert main(run_zone=_must_not_run) == 1
    assert capsys.readouterr().err.strip() == _REFUSAL


def test_the_wheel_alone_protects_nobody(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no house layer the address reaches the run: the shipped default names no one's console.

    This is the other side of moving the entry into the house's own layer, and the reason that
    layer must be on the machine BEFORE a wheel with the empty default is installed there.
    """
    seen: list[Options] = []

    async def capture(options: Options) -> int:
        seen.append(options)
        return 0

    monkeypatch.setattr("sys.argv", _argv("--slave", ROOM5))

    assert main(run_zone=capture) == 0
    assert seen[0].slaves == (ROOM5,)


@pytest.mark.usefixtures("a_house_that_protects_its_console")
def test_the_refusal_reaches_a_machine_caller_as_an_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refusal is an ANSWER, so a caller reading JSON gets it in the same shape as a success."""
    monkeypatch.setattr("sys.argv", _argv("--slave", ROOM5, "--json-bare"))

    assert main(run_zone=_must_not_run) == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope == {
        "ok": False,
        "command": "soundtouch-zonemaster",
        "error": "OptionsError",
        "message": _REFUSAL,
    }


@pytest.mark.usefixtures("a_house_that_protects_its_console")
def test_an_empty_list_lifts_the_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of making it a setting: a deliberate run against the console is possible without
    editing the program. It is still audible in the house, which is why it takes saying so."""
    seen: list[Options] = []

    async def capture(options: Options) -> int:
        seen.append(options)
        return 0

    monkeypatch.setattr("sys.argv", _argv("--slave", ROOM5, "--set", "prototype.never_touch=[]"))

    assert main(run_zone=capture) == 0
    assert seen[0].slaves == (ROOM5,), "the address reached the run, which is what lifting it means"


@pytest.mark.usefixtures("a_house_that_protects_its_console")
def test_an_environment_override_replaces_the_list_rather_than_adding_to_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A layer REPLACES the value below it, which is the library's rule for a list and is the
    answer somebody wants: a house that protects a different box protects that one and not both."""
    replacement = [{"ip": ELSEWHERE, "name": "Room6", "why": "somebody is asleep"}]
    monkeypatch.setenv(f"{ENV_PREFIX}PROTOTYPE__NEVER_TOUCH", json.dumps(replacement))
    monkeypatch.setattr("sys.argv", _argv("--slave", ELSEWHERE))

    assert main(run_zone=_must_not_run) == 1
    assert capsys.readouterr().err.strip() == f"refused: Room6 ({ELSEWHERE}) is somebody is asleep"


@pytest.mark.usefixtures("a_house_that_protects_its_console")
def test_the_replaced_list_no_longer_protects_the_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the same rule, stated separately because it is the dangerous half: an
    override that names a different box stops protecting the one the house's own layer named."""
    seen: list[Options] = []

    async def capture(options: Options) -> int:
        seen.append(options)
        return 0

    replacement = [{"ip": ELSEWHERE, "name": "Room6", "why": "somebody is asleep"}]
    monkeypatch.setenv(f"{ENV_PREFIX}PROTOTYPE__NEVER_TOUCH", json.dumps(replacement))
    monkeypatch.setattr("sys.argv", _argv("--slave", ROOM5))

    assert main(run_zone=capture) == 0
    assert seen[0].slaves == (ROOM5,)


@pytest.mark.usefixtures("a_house_that_protects_its_console")
def test_the_late_joining_list_is_checked_too(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Guarding only ``--slave`` would leave the console reachable through ``--late-slave``."""
    monkeypatch.setattr("sys.argv", _argv("--slave", "192.168.0.21", "--late-slave", ROOM5))

    assert main(run_zone=_must_not_run) == 1
    assert capsys.readouterr().err.strip() == _REFUSAL


def test_a_never_touch_entry_that_is_not_one_stops_the_run_rather_than_reading_as_empty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path
) -> None:
    """A list that cannot be read must not be treated as no list at all.

    Everywhere else in this program a malformed setting is REPORTED and the house carries on,
    because a stray key must not stop the speakers working. This one is the exception and the
    reason is what it guards: proceeding as though nothing were protected is exactly the outcome
    the setting exists to prevent.
    """
    written = isolated_config_layers / "xdg" / "soundtouch-zonemaster" / "config.toml"
    written.parent.mkdir(parents=True)
    written.write_text('[prototype]\nnever_touch = "not a list"\n', encoding="utf-8")
    monkeypatch.setattr("sys.argv", _argv("--slave", ROOM5))

    assert main(run_zone=_must_not_run) == 2
    assert "never_touch" in capsys.readouterr().err
