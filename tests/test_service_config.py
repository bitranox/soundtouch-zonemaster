"""Where the service's settings come from once there are six layers under the command line.

Everything here runs the real ``main`` with a real argv and touches no speaker: the merge sits
ahead of the first socket, and the run is substituted through ``main``'s own parameter, so what is
asserted is the whole way from a file on disk into :class:`ServiceOptions`.

The first test is the one this feature is judged by. The house runs a systemd unit that names four
settings and no subcommand, and that unit is re-synced over the machine at every boot from a
different repository. If the argv in its ``ExecStart`` ever stopped meaning what it means, the
speakers would find out at 04:00 rather than here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from lib_layered_config import REDACTED_PLACEHOLDER

from soundtouch_zonemaster.adapters.config.loader import ENV_PREFIX, clear_config_cache, defaults_from
from soundtouch_zonemaster.adapters.files.house_store import SqlHouseStore
from soundtouch_zonemaster.application.errors import StoreError
from soundtouch_zonemaster.application.zone_service.service import ZoneService
from soundtouch_zonemaster.composition import build_production
from soundtouch_zonemaster.domain.preferences import PreferenceName, PreferenceSource
from soundtouch_zonemaster.entry import service_main as main

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path

    from soundtouch_zonemaster.application.options import ServiceOptions
    from soundtouch_zonemaster.application.ports import HouseStore, RunService
    from soundtouch_zonemaster.domain.logfn import LogFn
    from soundtouch_zonemaster.domain.preferences import PreferenceValue
    from soundtouch_zonemaster.domain.secret import Secret

DEPLOYED_ARGV = (
    "--bind-ip",
    "192.168.0.190",
    "--channel-file",
    "/var/lib/zonemaster/channels.json",
    "--switch-file",
    "/var/lib/zonemaster/zone.switch",
    "--state-file",
    "/var/lib/zonemaster/zone-state.json",
)
"""The options the deployed unit passes. The unit's ``ExecStart`` still names the three file paths,
which are now one-time import sources. The database it needs comes from the host layer,
``database.url`` in the deployment host's own configuration file, which is what a test using this
argv must add through ``_user_config`` or an equivalent isolated layer. Only the paths are rewritten
below, because ``/var/lib/zonemaster`` does not exist on a development host and each file's directory
is checked at startup."""


def _capture() -> tuple[list[ServiceOptions], RunService]:
    """A stand-in run that records what it was handed instead of holding a zone."""
    seen: list[ServiceOptions] = []

    async def run(options: ServiceOptions) -> int:
        seen.append(options)
        return 0

    return seen, run


def _user_config(root: Path, body: str) -> Path:
    """Write the user layer, which is the one a person edits and ``config-deploy`` writes."""
    path = root / "xdg" / "soundtouch-zonemaster" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _house(tmp_path: Path, *, bind_ip: str = "10.0.0.1", zone: str = "", extra: str = "") -> str:
    """A config body holding the settings that have no default, so a run can get past them.

    The sections are the scopes the shipped files use: ``zone`` is what the master is on the
    network and ``files`` is what this deployment owns. ``zone`` adds a key inside that first
    section and ``extra`` adds whole sections after both, because TOML forbids reopening a table
    and a key appended to the wrong one is silently somebody else's setting.
    """
    return (
        f'[zone]\nbind_ip = "{bind_ip}"\n{zone}'
        f'[database]\nurl = "{tmp_path}/zonemaster.sqlite"\n'
        f'[files]\nchannel_file = "{tmp_path}/ch.json"\n'
        f'switch_file = "{tmp_path}/sw"\nstate_file = "{tmp_path}/st.json"\n' + extra
    )


def test_the_deployed_units_argv_still_wins_over_a_config_file_that_disagrees(
    monkeypatch: pytest.MonkeyPatch, isolated_config_layers: Path, tmp_path: Path
) -> None:
    """The whole reason the options were kept: a machine can be configured by file WITHOUT the
    unit having to change on the same day. A file that contradicts the unit must lose."""
    _user_config(
        isolated_config_layers,
        f'[zone]\nbind_ip = "10.9.9.9"\n'
        f'[database]\nurl = "{tmp_path}/zonemaster.sqlite"\n'
        f'[files]\nchannel_file = "/tmp/other.json"\n'
        f"[dialling]\nwindow_s = 1.4\n",
    )
    argv = [part.replace("/var/lib/zonemaster", str(tmp_path)) for part in DEPLOYED_ARGV]
    seen, run = _capture()
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", *argv])

    assert main(run_service=run) == 0
    options = seen[0]
    assert options.bind_ip == "192.168.0.190", "the unit's address, not the file's"
    assert options.channel_file == tmp_path / "channels.json", "the unit's own value, not the file's"
    assert options.dial_window_s == 1.4, "and a setting the unit says nothing about still comes from the file"


def test_a_run_with_no_options_at_all_is_configured_entirely_by_file(
    monkeypatch: pytest.MonkeyPatch, isolated_config_layers: Path, tmp_path: Path
) -> None:
    """The other half of the same promise: once a file holds the two settings that have no
    default, the command line has nothing left to say."""
    _user_config(isolated_config_layers, _house(tmp_path, bind_ip="10.9.9.9", zone='device_id = "AABBCC001122"\n'))
    seen, run = _capture()
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service"])

    assert main(run_service=run) == 0
    options = seen[0]
    assert options.bind_ip == "10.9.9.9"
    assert options.state_file == tmp_path / "st.json"
    assert options.device_id == "AABBCC001122", "device_id sits in [zone] beside bind_ip, not in [files]"


def test_a_setting_missing_from_every_layer_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Click used to refuse this with "Missing option" and exit 2. It still exits 2, and it now
    names the other place the value could have been put, because the flag is no longer the only
    one."""
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service"])

    assert main() == 2
    message = capsys.readouterr().err
    for missing in ("bind_ip", "database"):
        assert missing in message
    assert "--bind-ip" in message
    assert "config-deploy" in message


def test_an_environment_variable_beats_a_file_and_loses_to_the_command_line(
    monkeypatch: pytest.MonkeyPatch, isolated_config_layers: Path, tmp_path: Path
) -> None:
    """The precedence is only worth having if the middle of it is real, so the same setting is
    given in three places at once and the answer has to be the top one."""
    _user_config(isolated_config_layers, _house(tmp_path, extra='[registry]\nurl = "http://file"\n'))
    monkeypatch.setenv(f"{ENV_PREFIX}ZONE__BIND_IP", "10.0.0.2")
    monkeypatch.setenv(f"{ENV_PREFIX}REGISTRY__URL", "http://env")
    seen, run = _capture()
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "--bind-ip", "10.0.0.3"])

    assert main(run_service=run) == 0
    assert seen[0].bind_ip == "10.0.0.3", "typed beats the environment"
    assert seen[0].registry_url == "http://env", "and the environment beats the file"


def test_a_set_override_reaches_the_service_and_still_loses_to_a_typed_option(
    monkeypatch: pytest.MonkeyPatch, isolated_config_layers: Path, tmp_path: Path
) -> None:
    _user_config(
        isolated_config_layers,
        f'[database]\nurl = "{tmp_path}/zonemaster.sqlite"\n'
        f'[files]\nchannel_file = "{tmp_path}/ch.json"\n'
        f'switch_file = "{tmp_path}/sw"\nstate_file = "{tmp_path}/st.json"\n',
    )
    seen, run = _capture()
    monkeypatch.setattr(
        "sys.argv",
        [
            "soundtouch-zonemaster-service",
            "--set",
            "zone.bind_ip=10.5.5.5",
            "--set",
            "dialling.window_s=1.6",
            "--dial-window-s",
            "1.2",
        ],
    )

    assert main(run_service=run) == 0
    assert seen[0].bind_ip == "10.5.5.5"
    assert seen[0].dial_window_s == 1.2


def test_a_repeatable_option_nobody_used_leaves_the_configured_list_alone(
    monkeypatch: pytest.MonkeyPatch, isolated_config_layers: Path, tmp_path: Path
) -> None:
    """A ``multiple=True`` option arrives as an empty tuple whether or not it was typed. Letting
    that count as a value would mean the command line could only ever ADD a console and never
    leave the configured ones standing."""
    _user_config(
        isolated_config_layers,
        _house(tmp_path, extra='[membership]\nconsoles_allowed = ["AABBCC000012"]\n'),
    )
    seen, run = _capture()
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service"])

    assert main(run_service=run) == 0
    assert seen[0].consoles_allowed == ("AABBCC000012",)


def test_a_lower_case_console_id_in_a_config_layer_no_longer_stops_the_start(
    monkeypatch: pytest.MonkeyPatch, isolated_config_layers: Path, tmp_path: Path
) -> None:
    """A deployed config spelling an id lower-case must not exit 1 at start: it is folded to
    upper case, the same value a speaker's own device id compares against."""
    _user_config(
        isolated_config_layers,
        _house(tmp_path, extra='[membership]\nconsoles_allowed = ["aabbcc000012"]\n'),
    )
    seen, run = _capture()
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service"])

    assert main(run_service=run) == 0
    assert seen[0].consoles_allowed == ("AABBCC000012",)


def test_a_lower_case_device_id_in_a_config_layer_is_accepted_and_used_upper_case(
    monkeypatch: pytest.MonkeyPatch, isolated_config_layers: Path, tmp_path: Path
) -> None:
    """``zone.device_id`` follows the same rule ``membership.consoles_allowed`` already does: a
    person or a deployed config may spell an id lower-case, and it is folded to upper case rather
    than refused - the shape the registry itself reports."""
    _user_config(isolated_config_layers, _house(tmp_path, zone='device_id = "aabbcc001122"\n'))
    seen, run = _capture()
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service"])

    assert main(run_service=run) == 0
    assert seen[0].device_id == "AABBCC001122"


def test_a_lower_case_device_id_option_is_accepted_and_used_upper_case(
    monkeypatch: pytest.MonkeyPatch, isolated_config_layers: Path, tmp_path: Path
) -> None:
    """The same rule from the command line: ``--device-id`` is the setting's other surface, and a
    person typing it lower-case must not be refused where a config file spelling it the same way
    is not."""
    _user_config(isolated_config_layers, _house(tmp_path))
    argv = [part.replace("/var/lib/zonemaster", str(tmp_path)) for part in DEPLOYED_ARGV]
    seen, run = _capture()
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", *argv, "--device-id", "aabbcc001122"])

    assert main(run_service=run) == 0
    assert seen[0].device_id == "AABBCC001122"


REQUIRED_SETTINGS = (
    pytest.param("bind_ip", "zone.bind_ip", id="bind_ip"),
    pytest.param("device_id", "zone.device_id", id="device_id"),
    pytest.param("database", "database.url", id="database"),
)
"""Every ``ServiceOptionsInput`` field with no pydantic default: the ones a higher layer's ``null``
must refuse the same way a value given nowhere is refused, rather than as a record field that is
not a string."""


@pytest.mark.parametrize(("field", "path"), REQUIRED_SETTINGS)
def test_a_required_setting_given_as_null_is_refused_the_same_as_given_nowhere(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    isolated_config_layers: Path,
    tmp_path: Path,
    *,
    field: str,
    path: str,
) -> None:
    """``database.url`` was the only required setting this held for; a higher layer's ``null`` over
    ``zone.bind_ip`` or ``zone.device_id`` used to reach pydantic's own words about the record
    field instead ("Input should be a valid string"). All three now refuse by the SETTING's name,
    in the one sentence a value given nowhere gets."""
    _user_config(isolated_config_layers, _house(tmp_path, zone='device_id = "AABBCC001122"\n'))
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "--set", f"{path}=null"])

    assert main() == 2
    message = capsys.readouterr().err
    assert f"no value anywhere for {field}" in message
    assert path in message
    assert "Input should be a valid string" not in message


def test_a_misspelled_setting_gets_a_line_rather_than_silence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    """The record ignores a key it does not know, which is right for a house - a stray key must
    not stop the speakers working. But a setting that does nothing AND says nothing is the kind
    of thing somebody debugs for an hour."""
    _user_config(isolated_config_layers, _house(tmp_path, extra="[dialling]\nwindow = 1.4\n"))
    seen, run = _capture()
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service"])

    assert main(run_service=run) == 0
    assert "window" in capsys.readouterr().out
    assert seen[0].dial_window_s == 0.8, "and the misspelling changed nothing"


def test_config_prints_an_envelope_naming_the_layer_every_value_came_from(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path
) -> None:
    written = _user_config(isolated_config_layers, '[zone]\nbind_ip = "10.0.0.1"\n')
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "--json-bare", "config"])

    assert main() == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert envelope["command"] == "soundtouch-zonemaster-service config"
    assert envelope["data"]["config"]["zone.bind_ip"] == "10.0.0.1"
    assert envelope["data"]["provenance"]["zone.bind_ip"]["path"] == str(written)
    provenance = envelope["data"]["provenance"]["dialling.window_s"]
    assert provenance["layer"] == "defaults"
    assert provenance["path"].endswith("defaultconfig.d/50-dialling.toml"), (
        "a value from a scope file names THAT file, not the header it loads after"
    )


def test_redact_masks_every_value_that_came_out_of_a_dotenv(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    """``--redact`` is what an operator reaches for before pasting this output anywhere.

    The library masks by NAME, so ``e2e_key`` was masked while ``e2e_host``, ``e2e_user`` and
    ``e2e_speakers`` - one house's own addresses - printed in full, each with the ``.env`` path
    beside it in the provenance. A name list only knows the names somebody thought of, which is the
    same shape of hole the public export's rules file carries, so the rule here keys on the ORIGIN
    instead: a ``.env`` is per-machine by definition and everything out of it is masked.

    The first run is the control. Without the flag the value has to be VISIBLE, or the assertion
    after it would pass just as well against a command that cannot print that key at all.
    """
    home = tmp_path / "somewhere-with-a-dotenv"
    home.mkdir()
    (home / ".env").write_text('e2e_host = "10.9.9.9"\ne2e_key = "not-a-real-key"\n', encoding="utf-8")
    monkeypatch.chdir(home)

    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "--json-bare", "config"])
    assert main() == 0
    plain = json.loads(capsys.readouterr().out)["data"]["config"]
    assert plain["e2e_host"] == "10.9.9.9", "the control: this run is the one meant to show the value"

    clear_config_cache()
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "--json-bare", "config", "--redact"])
    assert main() == 0
    masked = json.loads(capsys.readouterr().out)["data"]["config"]
    assert masked["e2e_host"] == REDACTED_PLACEHOLDER, "a .env value is masked whatever its key is called"
    assert masked["e2e_key"] == REDACTED_PLACEHOLDER, "and the library's own name-based masking still applies"


def test_redact_masks_every_value_that_came_out_of_a_private_override_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tracked_defaults_only: Path, tmp_path: Path
) -> None:
    """A private ``-rnhome`` override file is per-machine in exactly the way a ``.env`` is.

    It sits in the defaults directory, so its values report the DEFAULTS layer, and a rule keyed
    on the ``.env`` layer alone printed ``zone.bind_ip`` in full with the private file's path
    beside it. The rule keys on the file instead: anything a private override file set is masked.

    The first run is the control, as in the ``.env`` test above: without the flag the value has to
    be visible, or the masked assertion would pass against a command that cannot print it at all.
    """
    base = tmp_path / tracked_defaults_only.name
    shutil.copy2(tracked_defaults_only, base)
    shutil.copytree(tracked_defaults_only.with_suffix(".d"), base.with_suffix(".d"))
    (base.with_suffix(".d") / "91-zone-rnhome.toml").write_text('[zone]\nbind_ip = "10.9.9.9"\n', encoding="utf-8")

    with defaults_from(base):
        monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "--json-bare", "config"])
        assert main() == 0
        plain = json.loads(capsys.readouterr().out)["data"]["config"]
        assert plain["zone.bind_ip"] == "10.9.9.9", "the control: this run is the one meant to show the value"

        clear_config_cache()
        monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "--json-bare", "config", "--redact"])
        assert main() == 0
        masked = json.loads(capsys.readouterr().out)["data"]["config"]
    assert masked["zone.bind_ip"] == REDACTED_PLACEHOLDER, "a private override file's value is masked"
    assert masked["dialling.window_s"] != REDACTED_PLACEHOLDER, "a tracked public default stays readable"


def test_config_masks_a_database_url_password_without_redact(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A database URL's password is a secret regardless of which layer set it or whether
    ``--redact`` was asked for - unlike ``--redact``'s own masking, which only reaches a ``.env``
    or a private override file. Set through the ENVIRONMENT layer, which ``--redact`` does not
    touch at all, so a pass here that came from ``--redact`` catching it a different way is ruled
    out."""
    monkeypatch.setenv(f"{ENV_PREFIX}DATABASE__URL", "postgresql+psycopg://zm@db.example/zm?sslpassword=TOPSECRET")
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "--json-bare", "config", "--section", "database"])

    assert main() == 0
    envelope = json.loads(capsys.readouterr().out)
    value = envelope["data"]["config"]["database.url"]
    assert "TOPSECRET" not in value
    assert value == "postgresql+psycopg://***", "nothing after the scheme of a URL carrying a password"


def test_config_masks_a_database_url_password_with_redact_too(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other shape the finding measured: a userinfo password AND a ``?password=`` query value,
    with ``--redact`` also given. Both must still be gone - ``--redact``'s own by-origin masking
    does not cover the environment layer either, so this pins the URL rule holds even then."""
    monkeypatch.setenv(f"{ENV_PREFIX}DATABASE__URL", "postgresql+psycopg://zm:pw1@db.example/zm?password=s3cret")
    monkeypatch.setattr(
        "sys.argv",
        ["soundtouch-zonemaster-service", "--json-bare", "config", "--section", "database", "--redact"],
    )

    assert main() == 0
    captured = capsys.readouterr()
    assert "pw1" not in captured.out
    assert "pw1" not in captured.err
    assert "s3cret" not in captured.out
    assert "s3cret" not in captured.err


@pytest.mark.parametrize(
    ("url", "secrets"),
    [
        pytest.param(
            "postgresql+psycopg://zm@db.example/zm?a=1#&password=TOPSECRET", ("TOPSECRET",), id="hash-then-query-key"
        ),
        pytest.param("postgresql+psycopg://zm:TOPSECRET@[bad/zm", ("TOPSECRET",), id="unbalanced-bracket-host"),
        pytest.param(
            "postgresql+psycopg://zm@db.example:5432/zm?password=TOP@SECRET",
            ("TOP", "SECRET"),
            id="raw-at-in-query-value",
        ),
    ],
)
@pytest.mark.parametrize("mode", [["--json-bare"], []], ids=["json", "human"])
@pytest.mark.parametrize("redact", [["--redact"], []], ids=["redact", "no-redact"])
def test_config_never_prints_a_password_sqlalchemy_would_read(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    url: str,
    secrets: tuple[str, ...],
    mode: list[str],
    redact: list[str],
) -> None:
    """Shapes where a mask that cut the password out would have to agree with SQLAlchemy on where
    it ends, and SQLAlchemy reads a password out of each: a query key behind a ``#`` (SQLAlchemy's
    query runs to the end of the string), a host ``urlsplit`` cannot even split, and a raw ``@``
    inside a query value. Every output mode, with and without
    ``--redact``, through the environment layer, which ``--redact``'s own masking does not reach.
    The secrets are named by the row, not by any parser's reading of it."""
    monkeypatch.setenv(f"{ENV_PREFIX}DATABASE__URL", url)
    monkeypatch.setattr(
        "sys.argv", ["soundtouch-zonemaster-service", *mode, "config", "--section", "database", *redact]
    )

    assert main() == 0
    captured = capsys.readouterr()
    assert "database.url" in captured.out, "the control: the URL is in the output, masked"
    for secret in secrets:
        assert secret not in captured.out
        assert secret not in captured.err


def test_config_refuses_a_section_that_is_not_there_rather_than_printing_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Asking for a section is asking a question, and an empty listing would answer a different
    one - "it is empty" rather than "there is no such section"."""
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "--json-bare", "config", "--section", "nope"])

    assert main() == 2
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is False
    assert envelope["error"] == "ConfigInputError"


def test_a_section_this_program_owns_answers_empty_rather_than_refusing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path
) -> None:
    """``zone`` is one of the two sections whose every setting ships commented out, because each
    describes ONE machine - so it is also the section an operator asks about first.

    Until a value is written it holds nothing, and answering that with the message a typo gets
    sends somebody checking whether ``bind_ip`` arrived off to look for a misspelling, at the
    moment the true answer is that nothing in any layer set it. ``config.SECTIONS`` already knows
    the name, so the two cases are distinguishable and have to read differently.

    The first run is the control: without it the second would pass just as well against a command
    that cannot print this section at all.
    """
    written = _user_config(isolated_config_layers, '[zone]\nbind_ip = "10.0.0.1"\n')
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "--json-bare", "config", "--section", "zone"])
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["data"]["config"] == {"zone.bind_ip": "10.0.0.1"}

    written.unlink()
    clear_config_cache()
    assert main() == 0, "it ran and answered; 2 is for a command that could not run"
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert envelope["data"]["config"] == {}, "nothing set it, and that is the answer rather than a failure"


def test_the_other_deployment_scoped_section_answers_the_same_way(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path
) -> None:
    """``files`` ships commented out for the same reason ``zone`` does, and an operator reaches
    for it in the same breath. Named rather than parametrised because these are the only two."""
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "--json-bare", "config", "--section", "files"])

    assert main() == 0
    assert json.loads(capsys.readouterr().out)["data"]["config"] == {}


def test_an_empty_section_says_so_in_words_rather_than_printing_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path
) -> None:
    """Silence is what the refusal was written to avoid, and it stays avoided: a person reading
    the terminal is told the name is one this program reads and that nothing has set it."""
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "config", "--section", "zone"])

    assert main() == 0
    said = capsys.readouterr().out
    assert said.strip(), "an empty listing printed as nothing reads as a command that did nothing"
    assert "zone" in said


def test_a_setting_this_program_owns_answers_empty_by_its_full_name_too(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path
) -> None:
    """``--section`` takes a dotted key as well as a scope, and ``zone.bind_ip`` is as much a name
    this program owns as ``zone`` is. A key nobody set still is not a typo."""
    monkeypatch.setattr(
        "sys.argv", ["soundtouch-zonemaster-service", "--json-bare", "config", "--section", "zone.bind_ip"]
    )

    assert main() == 0
    assert json.loads(capsys.readouterr().out)["data"]["config"] == {}


def test_a_misspelling_inside_a_section_this_program_owns_is_still_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path
) -> None:
    """The control for the three above. Widening the answer to "empty" must not widen it to every
    name that merely starts with one we own, or the refusal stops catching the typo it exists for.
    """
    monkeypatch.setattr(
        "sys.argv", ["soundtouch-zonemaster-service", "--json-bare", "config", "--section", "zone.bnid_ip"]
    )

    assert main() == 2
    assert json.loads(capsys.readouterr().out)["error"] == "ConfigInputError"


def test_a_config_file_that_will_not_parse_stops_the_service_with_an_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path
) -> None:
    """A unit is not a person: a startup that cannot read its configuration has to say so in the
    shape the caller parses, and "could not run" is exit 2 rather than "the answer is no"."""
    _user_config(isolated_config_layers, "[service]\nbind_ip = \n")
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "--json-bare"])

    assert main() == 2
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is False
    assert envelope["error"] == "ConfigInputError"


def test_config_deploy_writes_the_file_once_and_then_says_it_did_not(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path
) -> None:
    """Running it twice on a configured machine must not overwrite a house's settings, and must
    say plainly that it changed nothing rather than reporting a write it did not do."""
    monkeypatch.setattr(
        "sys.argv", ["soundtouch-zonemaster-service", "--json-bare", "config-deploy", "--target", "user"]
    )
    assert main() == 0
    first = json.loads(capsys.readouterr().out)
    written = isolated_config_layers / "xdg" / "soundtouch-zonemaster" / "config.toml"

    scopes = isolated_config_layers / "xdg" / "soundtouch-zonemaster" / "config.d"

    assert first["ok"] is True
    assert written.exists()
    assert first["data"]["written"][0] == str(written), "the base file first"
    assert sorted(first["data"]["written"][1:]) == sorted(str(path) for path in scopes.iterdir()), (
        "and every scope file beside it, or a deploy writes eight files and reports one"
    )

    assert main() == 0
    assert json.loads(capsys.readouterr().out)["data"]["written"] == []


def test_a_deployed_file_is_a_file_the_service_then_reads(
    monkeypatch: pytest.MonkeyPatch, isolated_config_layers: Path, tmp_path: Path
) -> None:
    """The two commands are only worth having if they meet: what config-deploy writes has to be
    the thing a later run picks up, uncommented by hand exactly as the files tell an operator -
    and that includes the config.d directory, which is where every setting now lives."""
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "config-deploy", "--target", "user"])
    assert main() == 0

    scopes = isolated_config_layers / "xdg" / "soundtouch-zonemaster" / "config.d"
    for name, settings in (
        ("10-zone.toml", (("bind_ip", "10.0.0.1"),)),
        (
            "20-files.toml",
            (
                ("channel_file", f"{tmp_path}/ch.json"),
                ("switch_file", f"{tmp_path}/sw"),
                ("state_file", f"{tmp_path}/st.json"),
            ),
        ),
        ("25-database.toml", (("url", f"{tmp_path}/zonemaster.sqlite"),)),
    ):
        path = scopes / name
        body = path.read_text(encoding="utf-8")
        for key, value in settings:
            marker = next(line for line in body.splitlines() if line.startswith(f"# {key} = "))
            body = body.replace(marker, f'{key} = "{value}"')
        path.write_text(body, encoding="utf-8")

    seen, run = _capture()
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service"])
    assert main(run_service=run) == 0
    assert seen[0].bind_ip == "10.0.0.1"
    assert seen[0].state_file == tmp_path / "st.json"


def test_the_human_view_prints_every_value_with_the_file_it_came_from(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path
) -> None:
    """The mode a person actually runs. A dump that does not say where a value came from answers
    the easy half of the question and leaves the half that made this worth building."""
    written = _user_config(isolated_config_layers, '[zone]\nbind_ip = "10.0.0.1"\n')
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "config"])

    assert main() == 0
    lines = {line.split(" = ")[0]: line for line in capsys.readouterr().out.splitlines()}
    assert str(written) in lines["zone.bind_ip"], "a value from a file names that file"
    assert "defaults:" in lines["dialling.window_s"], "and one nobody set names the shipped defaults"
    assert "observer.backoff_s" in lines, "a list is one leaf, not a table to walk into"


def test_a_set_override_is_reported_as_coming_from_the_command_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path
) -> None:
    """The library keeps the ORIGINAL provenance through an override, so without the extra record
    this line would name the shipped defaults for a value the caller had just replaced."""
    _user_config(isolated_config_layers, "[dialling]\nwindow_s = 1.1\n")
    monkeypatch.setattr(
        "sys.argv",
        ["soundtouch-zonemaster-service", "--set", "dialling.window_s=1.9", "config", "--section", "dialling"],
    )

    assert main() == 0
    line = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("dialling.window_s"))
    assert "1.9" in line
    assert line.endswith("# --set"), line


def test_a_value_that_is_not_a_setting_at_all_is_refused_naming_what_is_wrong_with_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    """A missing setting and an unusable one are different news, and only the first has a list of
    places to put it. This is the second, and it has to say which key and why."""
    _user_config(isolated_config_layers, _house(tmp_path, extra='[dialling]\nwindow_s = "not a number"\n'))
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service"])

    assert main() == 2
    message = capsys.readouterr().err
    assert "dial_window_s" in message
    assert "config-deploy" not in message, "nothing is missing, so do not send the reader to deploy a file"


def test_config_deploy_says_in_words_that_it_wrote_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "config-deploy", "--target", "user"])
    assert main() == 0
    assert "wrote " in capsys.readouterr().out

    assert main() == 0
    second = capsys.readouterr().out
    assert "nothing written" in second
    assert "--force" in second, "and it says which flag would have changed that"


def test_config_deploy_reports_a_target_it_cannot_write_rather_than_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path
) -> None:
    """A deploy that cannot happen is still news a caller has to be able to parse."""
    blocked = isolated_config_layers / "xdg" / "soundtouch-zonemaster"
    blocked.parent.mkdir(parents=True, exist_ok=True)
    blocked.write_text("a file where the config directory should be", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv", ["soundtouch-zonemaster-service", "--json-bare", "config-deploy", "--target", "user"]
    )

    assert main() == 2
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is False
    assert envelope["command"] == "soundtouch-zonemaster-service config-deploy"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits this test sets, so nothing is denied")
def test_a_deploy_that_is_denied_says_which_target_needs_root(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path
) -> None:
    """The one failure worth its own branch: --target app writes under /etc, and "permission
    denied" on its own does not tell a reader that --target user would have worked.

    The machine-mode half is the same branch read by the caller the envelope exists for. ``error``
    is the exception CLASS a caller branches on, so wrapping the hint in a bare ``OSError`` would
    name a base class for a refusal whose real one says exactly what went wrong.
    """
    home = isolated_config_layers / "xdg"
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o500)
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "config-deploy", "--target", "user"])
    try:
        assert main() == 2
        assert "needs root" in capsys.readouterr().err

        monkeypatch.setattr(
            "sys.argv", ["soundtouch-zonemaster-service", "--json-bare", "config-deploy", "--target", "user"]
        )
        assert main() == 2
        envelope = json.loads(capsys.readouterr().out)
    finally:
        home.chmod(0o700)
    assert envelope["error"] == "PermissionError", "the class a caller branches on, not one of its bases"
    assert "needs root" in envelope["message"]


def test_the_mpd_settings_come_from_a_file_and_a_typed_option_still_wins(
    monkeypatch: pytest.MonkeyPatch, isolated_config_layers: Path, tmp_path: Path
) -> None:
    """M3a's two settings are a scope like every other one: a ``[mpd]`` table, and argv above it.

    They matter to a deployment that does not keep the music beside the service, which is the
    case the user's decision of 2026-09-20 made supportable: the files reach the machine three
    different ways and MPD may not be on this one at all.
    """
    _user_config(
        isolated_config_layers,
        _house(tmp_path, zone='device_id = "AABBCC001122"\n', extra='[mpd]\nhost = "10.0.0.9"\nport = 6601\n'),
    )
    seen, run = _capture()
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "--mpd-port", "6602"])

    assert main(run_service=run) == 0
    assert seen[0].mpd_host == "10.0.0.9", "the file answers where nothing was typed"
    assert seen[0].mpd_port == 6602, "and a typed option beats the file, as every other one does"


def test_an_mpd_port_no_caller_could_dial_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    """Zero is the value worth refusing: it means "any free port" to a listener and nothing at all
    to a caller, so it would be carried around as a setting that can never be dialled and reported
    as a daemon that is down. Refused at startup instead, naming which port it means - there is
    more than one in these options."""
    # The unit's argv names no database; the deployment host's layer file supplies one, so we add
    # it here through the isolated user layer.
    _user_config(isolated_config_layers, f'[database]\nurl = "{tmp_path}/zonemaster.sqlite"\n')
    argv = [part.replace("/var/lib/zonemaster", str(tmp_path)) for part in DEPLOYED_ARGV]
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", *argv, "--mpd-port", "0"])

    assert main() == 1, "it ran and the answer is no, which is not the same as could not run"
    message = capsys.readouterr().err
    assert "the mpd port" in message, message
    assert "65535" in message, message


def test_the_hold_threshold_comes_from_a_file_and_defaults_to_one_second(
    monkeypatch: pytest.MonkeyPatch, isolated_config_layers: Path, tmp_path: Path
) -> None:
    """Its own setting beside the window (user, 2026-09-24), read through the same layers."""
    _user_config(isolated_config_layers, _house(tmp_path))
    seen, run = _capture()
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service"])
    assert main(run_service=run) == 0
    assert seen[0].hold_threshold_s == 1.0, "the shipped default"

    _user_config(isolated_config_layers, _house(tmp_path, extra="[dialling]\nhold_threshold_s = 1.4\n"))
    clear_config_cache()
    assert main(run_service=run) == 0
    assert seen[1].hold_threshold_s == 1.4, "and a file changes it"


@pytest.mark.parametrize("threshold", ["0.9", "2.1"])
def test_a_hold_threshold_outside_its_bounds_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    isolated_config_layers: Path,
    tmp_path: Path,
    threshold: str,
) -> None:
    """Below 1.0 s a slow tap reads as a hold; above 2.0 s a hold acts after the person let go."""
    _user_config(isolated_config_layers, _house(tmp_path, extra=f"[dialling]\nhold_threshold_s = {threshold}\n"))
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service"])

    assert main() == 1, "a refusal, like the window's (ExitCode.REFUSED)"
    assert f"the hold threshold must be between 1.0 and 2.0 s, not {threshold}" in capsys.readouterr().err


def _password_from_the_environment(monkeypatch: pytest.MonkeyPatch, _root: Path) -> list[str]:
    monkeypatch.setenv(f"{ENV_PREFIX}DATABASE__PASSWORD", "TOPSECRET")
    return []


def _password_from_a_user_file(_monkeypatch: pytest.MonkeyPatch, root: Path) -> list[str]:
    _user_config(root, '[database]\npassword = "TOPSECRET"\n')
    return []


def _password_from_a_set_override(_monkeypatch: pytest.MonkeyPatch, _root: Path) -> list[str]:
    return ["--set", "database.password=TOPSECRET"]


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(_password_from_the_environment, id="env"),
        pytest.param(_password_from_a_user_file, id="file"),
        pytest.param(_password_from_a_set_override, id="set"),
    ],
)
@pytest.mark.parametrize("mode", [["--json-bare"], ["--json"], []], ids=["json-bare", "json", "human"])
@pytest.mark.parametrize("redact", [["--redact"], []], ids=["redact", "no-redact"])
def test_config_always_masks_the_database_password(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    isolated_config_layers: Path,
    *,
    source: Callable[[pytest.MonkeyPatch, Path], list[str]],
    mode: list[str],
    redact: list[str],
) -> None:
    """The password is a secret from EVERY layer, in every output mode, with or without
    ``--redact`` - which on its own reaches only a ``.env`` or a private file, and masks by key
    name only when asked. The key must still be listed: the control that the view read it."""
    overrides = source(monkeypatch, isolated_config_layers)
    monkeypatch.setattr(
        "sys.argv",
        ["soundtouch-zonemaster-service", *mode, *overrides, "config", "--section", "database", *redact],
    )

    assert main() == 0
    captured = capsys.readouterr()
    assert "database.password" in captured.out, "the control: the setting is listed"
    assert "TOPSECRET" not in captured.out
    assert "TOPSECRET" not in captured.err


def _stray_key_in_a_user_file(spelling: str) -> Callable[[pytest.MonkeyPatch, Path], tuple[str, list[str]]]:
    def write(_monkeypatch: pytest.MonkeyPatch, root: Path) -> tuple[str, list[str]]:
        _user_config(root, f'[database]\n{spelling} = "TOPSECRET"\n')
        return spelling, []

    return write


def _stray_key_in_the_environment(monkeypatch: pytest.MonkeyPatch, _root: Path) -> tuple[str, list[str]]:
    monkeypatch.setenv(f"{ENV_PREFIX}DATABASE__PASSWROD", "TOPSECRET")
    return "passwrod", []


def _password_as_a_table_in_the_environment(monkeypatch: pytest.MonkeyPatch, _root: Path) -> tuple[str, list[str]]:
    """The environment layer reads a value opening with ``{`` as a JSON object, so the password
    setting itself becomes a table whose leaves are keys the exact-key mask never names. What is
    listed is ``database.password.inner``, or ``database.password`` whole where ``--redact``'s own
    by-name mask got there first; the control reads the prefix both share."""
    monkeypatch.setenv(f"{ENV_PREFIX}DATABASE__PASSWORD", '{"inner": "TOPSECRET"}')
    return "password", []


URL_WITH_A_PASSWORD = "postgresql+psycopg://zm:TOPSECRET@db.example/zm"
"""A database URL carrying a password: masked whole when it is text, by the url's own rule."""


def _url_as_an_array_in_the_environment(monkeypatch: pytest.MonkeyPatch, _root: Path) -> tuple[str, list[str]]:
    """The environment layer reads a value opening with ``[`` as a JSON array, so the url setting
    arrives as a list: a leaf the url rule, which reads text, would pass through unread."""
    monkeypatch.setenv(f"{ENV_PREFIX}DATABASE__URL", f'["{URL_WITH_A_PASSWORD}"]')
    return "url", []


def _url_as_a_table_in_the_environment(monkeypatch: pytest.MonkeyPatch, _root: Path) -> tuple[str, list[str]]:
    """A value opening with ``{`` makes the url setting a table; its leaf is ``database.url.inner``."""
    monkeypatch.setenv(f"{ENV_PREFIX}DATABASE__URL", f'{{"inner": "{URL_WITH_A_PASSWORD}"}}')
    return "url", []


def _url_as_an_array_in_a_user_file(_monkeypatch: pytest.MonkeyPatch, root: Path) -> tuple[str, list[str]]:
    _user_config(root, f'[database]\nurl = ["{URL_WITH_A_PASSWORD}"]\n')
    return "url", []


def _url_as_an_array_in_a_set_override(_monkeypatch: pytest.MonkeyPatch, _root: Path) -> tuple[str, list[str]]:
    return "url", ["--set", f'database.url=["{URL_WITH_A_PASSWORD}"]']


def _url_as_null_in_the_environment(monkeypatch: pytest.MonkeyPatch, _root: Path) -> tuple[str, list[str]]:
    """The environment layer reads ``null`` (and ``none``, in any case) as no value at all."""
    monkeypatch.setenv(f"{ENV_PREFIX}DATABASE__URL", "null")
    return "url", []


def _url_as_null_in_a_set_override(_monkeypatch: pytest.MonkeyPatch, _root: Path) -> tuple[str, list[str]]:
    return "url", ["--set", "database.url=null"]


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(_stray_key_in_a_user_file("passwrod"), id="file-misspelled"),
        pytest.param(_stray_key_in_a_user_file("Password"), id="file-mixed-case"),
        pytest.param(_stray_key_in_the_environment, id="env-misspelled"),
        pytest.param(_password_as_a_table_in_the_environment, id="env-table"),
        pytest.param(_url_as_an_array_in_the_environment, id="env-url-array"),
        pytest.param(_url_as_a_table_in_the_environment, id="env-url-table"),
        pytest.param(_url_as_an_array_in_a_user_file, id="file-url-array"),
        pytest.param(_url_as_an_array_in_a_set_override, id="set-url-array"),
        pytest.param(_url_as_null_in_the_environment, id="env-url-null"),
        pytest.param(_url_as_null_in_a_set_override, id="set-url-null"),
    ],
)
@pytest.mark.parametrize("mode", [["--json-bare"], ["--json"], []], ids=["json-bare", "json", "human"])
@pytest.mark.parametrize("redact", [["--redact"], []], ids=["redact", "no-redact"])
def test_config_masks_every_database_key_but_the_url(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    isolated_config_layers: Path,
    *,
    source: Callable[[pytest.MonkeyPatch, Path], tuple[str, list[str]]],
    mode: list[str],
    redact: list[str],
) -> None:
    """A key under ``[database]`` that is not a setting is almost certainly a password somebody
    misspelled, so its value is masked like the password's own, in every output mode and with or
    without ``--redact``. The url keeps its own rule only while it is text: a url that arrived as
    a list or a table is not a URL, and is masked whole rather than printed as it came; one that
    arrived as no value is listed too, and shown as null (pinned below). The key itself is still
    listed: the control that the view read it."""
    key, overrides = source(monkeypatch, isolated_config_layers)
    monkeypatch.setattr(
        "sys.argv",
        ["soundtouch-zonemaster-service", *mode, *overrides, "config", "--section", "database", *redact],
    )

    assert main() == 0
    captured = capsys.readouterr()
    assert f"database.{key}" in captured.out, "the control: the key is listed"
    assert "TOPSECRET" not in captured.out
    assert "TOPSECRET" not in captured.err


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(_url_as_null_in_the_environment, id="env-url-null"),
        pytest.param(_url_as_null_in_a_set_override, id="set-url-null"),
    ],
)
@pytest.mark.parametrize("redact", [["--redact"], []], ids=["redact", "no-redact"])
@pytest.mark.parametrize("mode", [["--json-bare"], ["--json"]], ids=["json-bare", "json"])
def test_config_shows_a_database_url_that_arrived_as_no_value_as_null(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    isolated_config_layers: Path,
    *,
    source: Callable[[pytest.MonkeyPatch, Path], tuple[str, list[str]]],
    mode: list[str],
    redact: list[str],
) -> None:
    """No value carries no password, and showing it says what is wrong: the service refuses it as
    a database given nowhere. Every other url that is not text is masked whole."""
    _, overrides = source(monkeypatch, isolated_config_layers)
    monkeypatch.setattr(
        "sys.argv",
        ["soundtouch-zonemaster-service", *mode, *overrides, "config", "--section", "database", *redact],
    )

    assert main() == 0
    shown = json.loads(capsys.readouterr().out)["data"]["config"]
    assert "database.url" in shown, "the control: the setting is listed"
    assert shown["database.url"] is None


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(_url_as_null_in_the_environment, id="env-url-null"),
        pytest.param(_url_as_null_in_a_set_override, id="set-url-null"),
    ],
)
@pytest.mark.parametrize("redact", [["--redact"], []], ids=["redact", "no-redact"])
def test_config_shows_a_database_url_that_arrived_as_no_value_as_null_to_a_person(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    isolated_config_layers: Path,
    *,
    source: Callable[[pytest.MonkeyPatch, Path], tuple[str, list[str]]],
    redact: list[str],
) -> None:
    _, overrides = source(monkeypatch, isolated_config_layers)
    monkeypatch.setattr(
        "sys.argv", ["soundtouch-zonemaster-service", *overrides, "config", "--section", "database", *redact]
    )

    assert main() == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("database.url ")]
    assert len(lines) == 1, "the control: the setting is listed once"
    assert lines[0].startswith("database.url = null ")


def test_config_still_shows_a_database_url_without_a_password_as_typed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path
) -> None:
    """Masking the rest of ``[database]`` leaves ``url`` to its own rule: shown as typed when it
    carries no password, beside a stray key that is masked."""
    url = "postgresql+psycopg://zm@db.example/zm"
    _user_config(isolated_config_layers, f'[database]\nurl = "{url}"\npasswrod = "TOPSECRET"\n')
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "--json", "config", "--section", "database"])

    assert main() == 0
    shown = json.loads(capsys.readouterr().out)["data"]["config"]
    assert shown["database.url"] == url
    assert shown["database.passwrod"] == REDACTED_PLACEHOLDER


def test_the_fade_in_is_a_setting_with_its_default(
    monkeypatch: pytest.MonkeyPatch, isolated_config_layers: Path, tmp_path: Path
) -> None:
    _user_config(isolated_config_layers, _house(tmp_path))
    seen, run = _capture()
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service"])
    assert main(run_service=run) == 0
    assert seen[0].fade_s == 0.8
    assert seen[0].preferences.fade_s == 0.8


def test_a_fade_in_outside_its_bounds_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    _user_config(isolated_config_layers, _house(tmp_path, extra="[volume]\nfade_s = 6.0\n"))
    seen, run = _capture()
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "--json-bare"])
    assert main(run_service=run) == 1
    assert seen == []
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["message"] == "refused: the fade-in must be between 0.0 and 5.0 s, not 6.0"


def test_a_non_finite_rewind_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    """NaN passes rewind's bare ``< 0`` check, so it must be caught earlier, at the same layered
    boundary that already refuses an out-of-bounds fade-in or hold threshold."""
    _user_config(isolated_config_layers, _house(tmp_path, extra="[mpd]\nrewind_s = nan\n"))
    seen, run = _capture()
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "--json-bare"])
    assert main(run_service=run) == 1
    assert seen == []
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["message"] == "refused: mpd.rewind_s must be finite, not nan"


def test_a_console_that_is_not_a_device_id_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    _user_config(isolated_config_layers, _house(tmp_path, extra='[membership]\nconsoles_allowed = ["notadeviceid"]\n'))
    seen, run = _capture()
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "--json-bare"])
    assert main(run_service=run) == 1
    assert seen == []
    assert "which is not a device id" in json.loads(capsys.readouterr().out)["message"]


@dataclass(frozen=True)
class _Given:
    """One preference given in every place it can be given, each with a different value."""

    name: PreferenceName
    field: str
    """The ``ServiceOptions`` field the layers fill, which is the control that a layer arrived."""
    section: str
    key: str
    file: str
    env: str
    command_line: str
    """The three layered values as TOML / JSON text; the value each is expected to arrive as follows."""
    arrives_as: dict[str, object]
    row: PreferenceValue
    said: str
    """What the service says at start once the row decides - the value only the row holds."""


_GIVEN = (
    _Given(
        name=PreferenceName.WINDOW,
        field="dial_window_s",
        section="dialling",
        key="window_s",
        file="0.6",
        env="0.7",
        command_line="0.8",
        arrives_as={"file": 0.6, "env": 0.7, "command line": 0.8},
        row=1.9,
        said="the dialling window is 1.9 s, set by cli at ",
    ),
    _Given(
        name=PreferenceName.HOLD,
        field="hold_threshold_s",
        section="dialling",
        key="hold_threshold_s",
        file="1.1",
        env="1.2",
        command_line="1.3",
        arrives_as={"file": 1.1, "env": 1.2, "command line": 1.3},
        row=1.9,
        said="a key is held after 1.9 s, set by cli at ",
    ),
    _Given(
        name=PreferenceName.REWIND,
        field="mpd_rewind_s",
        section="mpd",
        key="rewind_s",
        file="5.0",
        env="6.0",
        command_line="7.0",
        arrives_as={"file": 5.0, "env": 6.0, "command line": 7.0},
        row=42.0,
        said="an MPD channel starts 42 s back, set by cli at ",
    ),
    _Given(
        name=PreferenceName.FADE,
        field="fade_s",
        section="volume",
        key="fade_s",
        file="1.1",
        env="1.2",
        command_line="1.3",
        arrives_as={"file": 1.1, "env": 1.2, "command line": 1.3},
        row=4.5,
        said="a joining box fades in over 4.5 s, set by cli at ",
    ),
    _Given(
        name=PreferenceName.CONSOLES,
        field="consoles_allowed",
        section="membership",
        key="consoles_allowed",
        file='["AABBCC000001"]',
        env='["AABBCC000002"]',
        command_line='["AABBCC000003"]',
        arrives_as={"file": ("AABBCC000001",), "env": ("AABBCC000002",), "command line": ("AABBCC000003",)},
        row=("AABBCC000004",),
        said="consoles allowed into the zone: AABBCC000004, set by cli at ",
    ),
)


def _given_in(layer: str, given: _Given, monkeypatch: pytest.MonkeyPatch) -> tuple[str, list[str]]:
    """Put the preference in ONE layer: the user file's extra sections, and the argv, for that layer."""
    if layer == "env":
        monkeypatch.setenv(f"{ENV_PREFIX}{given.section.upper()}__{given.key.upper()}", given.env)
    if layer == "file":
        return f"[{given.section}]\n{given.key} = {given.file}\n", []
    if layer == "command line":
        return "", ["--set", f"{given.section}.{given.key}={given.command_line}"]
    return "", []


@contextlib.contextmanager
def _a_registry_that_never_answers() -> Generator[str]:
    """A registry address that takes the connection and never answers.

    The start-up says its preferences BEFORE it reads the registry, and waits on that read, so the
    service is held there while a test listens - and never goes on to bind the zone's ports, which
    the loopback suite owns, or to talk to whatever holds the default registry port on this machine.
    """
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"


def _start_and_listen(until: str, heard: list[str], seen: list[ServiceOptions]) -> RunService:
    """A run that starts the REAL service on the options it is handed, and stops it once it said ``until``.

    What the service says at start is written from the values it then runs on, so the line is the
    service's own account of which value won. Cancelled the way the unit's SIGINT cancels it, and
    waited for with ``asyncio.wait``, which never re-raises: a service that stopped on its own is
    written into ``heard`` instead, so a failing case names what the service did rather than an
    exit code.
    """

    async def run(options: ServiceOptions) -> int:
        seen.append(options)
        service = ZoneService(
            options, log=lambda kind, text: heard.append(f"{kind}: {text}"), ports=build_production().zone_ports
        )
        task = asyncio.create_task(service.run())
        deadline = asyncio.get_running_loop().time() + 5.0
        while not any(until in line for line in heard) and not task.done():
            if asyncio.get_running_loop().time() > deadline:
                heard.append("(the test stopped waiting)")
                break
            await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.wait({task})
        if not task.cancelled() and task.exception() is not None:
            heard.append(f"(the service stopped on its own: {task.exception()!r})")
        return 0

    return run


@pytest.mark.parametrize("layer", ["file", "env", "command line"])
@pytest.mark.parametrize("given", _GIVEN, ids=lambda given: given.name.value)
def test_a_stored_preference_beats_every_layer_it_could_have_come_from(
    given: _Given, layer: str, monkeypatch: pytest.MonkeyPatch, isolated_config_layers: Path, tmp_path: Path
) -> None:
    """The top of the precedence (user, 2026-09-27): a row in the house database wins over the
    config file, the environment and the command line alike, for each of the five preferences.

    Each layer is proved to have ARRIVED first - the option holds that layer's value - so the row
    is shown beating a value that was really there, not a default nobody set."""
    store = SqlHouseStore(str(tmp_path / "zonemaster.sqlite"), log=lambda _kind, _text: None)
    store.open(exclusive=False)
    try:
        store.set_preference(given.name, given.row, source=PreferenceSource.CLI)
    finally:
        store.close()
    sections, argv = _given_in(layer, given, monkeypatch)
    heard: list[str] = []
    seen: list[ServiceOptions] = []
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", *argv])

    with _a_registry_that_never_answers() as registry:
        extra = f'[registry]\nurl = "{registry}"\n{sections}'
        _user_config(isolated_config_layers, _house(tmp_path, zone='device_id = "AABBCC001122"\n', extra=extra))
        assert main(run_service=_start_and_listen(given.said, heard, seen)) == 0

    assert getattr(seen[0], given.field) == given.arrives_as[layer], f"the control: the {layer} value arrived"
    assert any(given.said in line for line in heard), heard


# --- `config` names the live source: a preference stored in the house database ----------------


def _stored(tmp_path: Path, name: PreferenceName, value: PreferenceValue) -> str:
    """A house database holding one stored preference, as ``prefs set`` would leave it.

    Written through the store rather than ``prefs set``, which checks the value first: a value out
    of bounds is how a test gets the row a person edited by hand, and the store takes it as given.
    """
    database = str(tmp_path / "db.sqlite")
    store = SqlHouseStore(database, log=lambda _kind, _text: None)
    store.open(exclusive=False)
    try:
        store.set_preference(name, value, source=PreferenceSource.CLI)
    finally:
        store.close()
    return database


def _config_output(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    """What the service wrote to STDOUT for ``argv``, requiring the exit code a working view has."""
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", *argv])
    assert main() == 0
    return capsys.readouterr().out


def _line_and_the_one_beneath(out: str, key: str) -> tuple[str, str]:
    """The line naming ``key`` and the line printed after it."""
    lines = [*out.splitlines(), ""]
    at = next(index for index, line in enumerate(lines) if line.startswith(f"{key} = "))
    return lines[at], lines[at + 1]


def _shown_value(line: str) -> str:
    """The value a ``<key> = <value>    # <where>`` line shows, without the note on where it came from.

    The note carries the time a preference was stored, and a microsecond count holds every short
    decimal sooner or later: ``30.790103`` contains ``0.7``. A test that looks for a value in the
    whole line fails about once in a hundred runs on the clock rather than on the code.
    """
    return line.split(" = ", 1)[1].split("    # ", 1)[0]


_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T[\d:.]+(?:[+-]\d{2}:\d{2}|Z)?")


def _without_timestamps(text: str) -> str:
    """``text`` with every ISO timestamp replaced, so a search for a value cannot match the clock."""
    return _TIMESTAMP.sub("<time>", text)


def test_config_shows_a_stored_preference_over_the_file_value_it_replaces(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    database = _stored(tmp_path, PreferenceName.WINDOW, 0.7)
    out = _config_output(monkeypatch, capsys, "--json-bare", "--database", database, "config", "--section", "dialling")

    data = json.loads(out)["data"]
    assert data["config"]["dialling.window_s"] == 0.7
    origin = data["provenance"]["dialling.window_s"]
    assert (origin["layer"], origin["source"], origin["overrides"]["value"]) == ("database", "cli", 0.8)
    assert origin["changed_at"], "the time the row was set is part of who decided it"
    assert origin["overrides"]["origin"]["path"].endswith("defaultconfig.d/50-dialling.toml")
    assert data["provenance"]["dialling.hold_threshold_s"]["layer"] == "defaults", "a key no row holds is left alone"
    assert data["database_note"] is None


def test_a_stored_console_list_is_shown_as_the_list_it_was_typed_as(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    """The record holds the list as a tuple; the view prints what ``prefs`` prints for it."""
    database = _stored(tmp_path, PreferenceName.CONSOLES, ("AABBCC0000A5",))
    out = _config_output(monkeypatch, capsys, "--database", database, "config", "--section", "membership")

    line, _ = _line_and_the_one_beneath(out, "membership.consoles_allowed")
    assert line.startswith('membership.consoles_allowed = ["AABBCC0000A5"]    # database (cli, '), line


def test_the_human_view_says_the_database_decides_and_names_the_file_value_beneath_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    """The design's shape: the row as the value, and the file value it overrides on its own line
    beneath it, so somebody who edited the file and saw nothing change is told why in one view."""
    database = _stored(tmp_path, PreferenceName.WINDOW, 0.7)
    out = _config_output(monkeypatch, capsys, "--database", database, "config", "--section", "dialling")

    line, beneath = _line_and_the_one_beneath(out, "dialling.window_s")
    assert line.startswith("dialling.window_s = 0.7    # database (cli, "), line
    assert beneath.startswith("#   overridden: dialling.window_s = 0.8    # defaults: "), beneath
    assert beneath.endswith("defaultconfig.d/50-dialling.toml"), beneath


def test_a_stored_row_the_rule_refuses_is_shown_ignored_with_its_raw_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    """A row out of bounds (edited by hand, or written by a newer version) does not decide the
    value - the file does - and it is shown, not dropped, so ``prefs unset`` can be reached for."""
    database = _stored(tmp_path, PreferenceName.WINDOW, 9.0)
    argv = ("--database", database, "config", "--section", "dialling")
    data = json.loads(_config_output(monkeypatch, capsys, "--json-bare", *argv))["data"]
    human = _config_output(monkeypatch, capsys, *argv)

    assert data["config"]["dialling.window_s"] == 0.8, "the file value stays the value in force"
    origin = data["provenance"]["dialling.window_s"]
    assert origin["layer"] == "defaults"
    ignored = origin["ignored_database_row"]
    assert (ignored["text"], ignored["source"]) == ("9.0", "cli")
    assert ignored["why"] == "refused: the dialling window must be between 0.5 and 2.0 s, not 9.0"
    line, beneath = _line_and_the_one_beneath(human, "dialling.window_s")
    assert line.startswith("dialling.window_s = 0.8    # defaults: "), line
    assert beneath == (
        "#   ignored in the house database: dialling.window_s = 9.0 "
        "(refused: the dialling window must be between 0.5 and 2.0 s, not 9.0)"
    )


def test_a_stored_number_no_float_can_hold_is_shown_ignored_and_cut_short(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    """A row holding a 401-digit integer once raised OverflowError out of ``config`` with no envelope.
    It is a row the rule refuses like any other: the file value stays, the JSON keeps the raw text
    whole, and the human line quotes it cut short rather than printing every digit."""
    huge = 10**400
    database = _stored(tmp_path, PreferenceName.REWIND, huge)
    argv = ("--database", database, "config", "--section", "mpd")
    data = json.loads(_config_output(monkeypatch, capsys, "--json-bare", *argv))["data"]
    human = _config_output(monkeypatch, capsys, *argv)

    assert data["provenance"]["mpd.rewind_s"]["layer"] == "defaults"
    ignored = data["provenance"]["mpd.rewind_s"]["ignored_database_row"]
    assert ignored["text"] == str(huge)
    assert ignored["why"] == "refused: mpd.rewind_s is too large to be a number of seconds"
    _, beneath = _line_and_the_one_beneath(human, "mpd.rewind_s")
    assert beneath == (
        f"#   ignored in the house database: mpd.rewind_s = {str(huge)[:80]}... "
        "(refused: mpd.rewind_s is too large to be a number of seconds)"
    )


def test_a_database_that_does_not_exist_holds_no_preference_and_is_not_created(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    """``config`` is a view: a mistyped ``--database`` must not leave a new, empty database
    behind (OPEN-WORK rank 201 is that defect in ``switch``)."""
    missing = tmp_path / "db.sqlite"
    argv = ("--database", str(missing), "config", "--section", "dialling")
    human = _config_output(monkeypatch, capsys, *argv)
    data = json.loads(_config_output(monkeypatch, capsys, "--json-bare", *argv))["data"]

    said = f"no preference is stored: the house database {missing} does not exist"
    assert f"# {said}" in human.splitlines(), human
    assert "dialling.window_s = 0.8    # defaults: " in human
    assert data["database_note"] == said
    assert [path.name for path in tmp_path.iterdir() if path.name.startswith("db.sqlite")] == []


def test_a_database_that_cannot_be_read_costs_config_one_line_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    """Everything else is printed exactly as without the database, and the exit code is the same.

    The whole view is compared, so the run is moved away from the checkout: its ``.env`` is a layer
    ``config`` would print, and a failing comparison would print it again."""
    monkeypatch.chdir(tmp_path)
    unreadable = tmp_path / "db.sqlite"
    unreadable.write_bytes(b"this is not a database, it is a sentence " * 100)
    without = _config_output(monkeypatch, capsys, "config").splitlines()
    named = _config_output(monkeypatch, capsys, "--database", str(unreadable), "config").splitlines()
    data = json.loads(_config_output(monkeypatch, capsys, "--json-bare", "--database", str(unreadable), "config"))

    notes = [line for line in named if line.startswith("# the house database was not read (")]
    assert len(notes) == 1, named
    assert notes[0].endswith("); a preference set there is not shown"), notes[0]
    assert [line for line in named if line not in notes] == without
    assert data["ok"] is True
    assert data["data"]["database_note"].startswith("the house database was not read (")


def test_config_of_a_section_holding_no_preference_never_opens_the_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    """The store is opened only for a view that shows a preference, so ``config --section database``
    stays a read of the files - and never dials a PostgreSQL server somebody configured.

    The database here cannot be read, so opening it would say so: the control shows it does."""
    unreadable = tmp_path / "db.sqlite"
    unreadable.write_bytes(b"this is not a database, it is a sentence " * 100)
    argv = ("--json-bare", "--database", str(unreadable), "config", "--section")
    control = json.loads(_config_output(monkeypatch, capsys, *argv, "dialling"))["data"]
    assert control["database_note"] is not None, "the control: a view with a preference in it opens the database"

    for section in ("zone", "database", "switch"):
        data = json.loads(_config_output(monkeypatch, capsys, *argv, section))["data"]
        assert data["database_note"] is None, section
        human = _config_output(monkeypatch, capsys, *argv[1:], section)
        assert "house database" not in human, section


def test_naming_a_database_adds_nothing_but_the_preference_lines_to_what_config_prints(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    """Choosing the database reads the config layers the way the store verbs do, and that read
    narrates - a misspelled setting, a configured password not sent to a typed database - on
    STDOUT in the human mode. ``config`` already shows every key, so none of it belongs here.

    The control is ``switch``, the store verb that makes the same choice and says both lines."""
    empty = str(tmp_path / "db.sqlite")
    store = SqlHouseStore(empty, log=lambda _kind, _text: None)
    store.open(exclusive=False)
    store.close()
    body = f'[database]\nurl = "{tmp_path}/other.sqlite"\npassword = "example-password"\n[dialling]\nwindwo_s = 1.0\n'
    _user_config(isolated_config_layers, body)
    control = _config_output(monkeypatch, capsys, "--database", empty, "switch")
    assert "has no setting called 'windwo_s'" in control, control
    assert "was not used" in control, control

    out = _config_output(monkeypatch, capsys, "--database", empty, "config", "--section", "dialling")
    assert all(line.startswith("dialling.") for line in out.splitlines()), out
    assert "dialling.windwo_s = 1.0" in out, "the stray key is still shown, as a key"


def test_config_with_no_database_named_anywhere_says_nothing_about_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path
) -> None:
    out = _config_output(monkeypatch, capsys, "config", "--section", "dialling")
    assert "house database" not in out
    assert "overridden" not in out


def test_redact_masks_a_value_the_database_holds_and_the_value_it_overrides(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    """What the house database holds is per-machine, like a ``.env``: ``--redact`` shows none of it."""
    database = _stored(tmp_path, PreferenceName.WINDOW, 0.7)
    argv = ("--database", database, "config", "--redact", "--section", "dialling")
    data = json.loads(_config_output(monkeypatch, capsys, "--json-bare", *argv))["data"]
    human = _config_output(monkeypatch, capsys, *argv)

    assert data["config"]["dialling.window_s"] == REDACTED_PLACEHOLDER
    assert data["provenance"]["dialling.window_s"]["overrides"]["value"] == REDACTED_PLACEHOLDER
    line, beneath = _line_and_the_one_beneath(human, "dialling.window_s")
    assert _shown_value(line) == json.dumps(REDACTED_PLACEHOLDER), line
    assert beneath.startswith("#   overridden: "), beneath
    assert _shown_value(beneath) == json.dumps(REDACTED_PLACEHOLDER), beneath


def test_redact_masks_the_raw_text_of_a_row_the_rule_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    """The reason quotes the value too (``not -7.5``), so it is masked with the text it explains."""
    database = _stored(tmp_path, PreferenceName.REWIND, -7.5)
    argv = ("--database", database, "config", "--redact", "--section", "mpd")
    data = json.loads(_config_output(monkeypatch, capsys, "--json-bare", *argv))["data"]
    human = _config_output(monkeypatch, capsys, *argv)

    ignored = data["provenance"]["mpd.rewind_s"]["ignored_database_row"]
    assert ignored["text"] == REDACTED_PLACEHOLDER
    assert "7.5" not in _without_timestamps(json.dumps(data))
    _, beneath = _line_and_the_one_beneath(human, "mpd.rewind_s")
    assert beneath.startswith("#   ignored in the house database: mpd.rewind_s = "), beneath
    assert "7.5" not in _without_timestamps(human)


def test_a_database_url_from_a_config_layer_is_read_without_a_typed_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    """How a house runs: the database is named once, in its configuration, and nothing is typed."""
    database = _stored(tmp_path, PreferenceName.WINDOW, 0.7)
    _user_config(isolated_config_layers, f'[database]\nurl = "{database}"\n')
    data = json.loads(_config_output(monkeypatch, capsys, "--json-bare", "config", "--section", "dialling"))["data"]

    assert data["config"]["dialling.window_s"] == 0.7
    assert data["provenance"]["dialling.window_s"]["layer"] == "database"


def test_an_empty_database_url_names_no_database_and_says_nothing_about_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path
) -> None:
    """An empty ``database.url`` is a setting nobody filled in, not a database that failed to open."""
    without = _config_output(monkeypatch, capsys, "config", "--section", "dialling")
    _user_config(isolated_config_layers, '[database]\nurl = ""\n')
    clear_config_cache()
    human = _config_output(monkeypatch, capsys, "config", "--section", "dialling")
    data = json.loads(_config_output(monkeypatch, capsys, "--json-bare", "config", "--section", "dialling"))["data"]

    assert human == without
    assert data["database_note"] is None


@contextlib.contextmanager
def _a_private_database_file(tracked: Path, tmp_path: Path, url: str) -> Generator[None]:
    """The house's database named where a deployment keeps it: a private ``-rnhome`` file beside the
    shipped defaults, which ``--redact`` exists to hide."""
    base = tmp_path / "defaults" / tracked.name
    base.parent.mkdir()
    shutil.copy2(tracked, base)
    shutil.copytree(tracked.with_suffix(".d"), base.with_suffix(".d"))
    private = base.with_suffix(".d") / "93-database-rnhome.toml"
    private.write_text(f'[database]\nurl = "{url}"\n', encoding="utf-8")
    with defaults_from(base):
        clear_config_cache()
        yield
    clear_config_cache()


@pytest.mark.parametrize("state", ["missing", "unreadable"])
def test_redact_keeps_the_database_location_out_of_the_note_too(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    isolated_config_layers: Path,
    tracked_defaults_only: Path,
    tmp_path: Path,
    state: str,
) -> None:
    """``--redact`` masks a private file's ``database.url`` in the listing, so the one line about the
    database must not print it either: neither the path nor an error that quotes it.

    The run without ``--redact`` is the control: there the location has to be visible."""
    monkeypatch.chdir(tmp_path)
    location = tmp_path / "private-house-location" / "db.sqlite"
    if state == "unreadable":
        location.parent.mkdir()
        location.write_bytes(b"this is not a database, it is a sentence " * 100)
    with _a_private_database_file(tracked_defaults_only, tmp_path, str(location)):
        control = _config_output(monkeypatch, capsys, "config")
        human = _config_output(monkeypatch, capsys, "config", "--redact")
        data = json.loads(_config_output(monkeypatch, capsys, "--json-bare", "config", "--redact"))["data"]

    assert "private-house-location" in control, "the control: without --redact the note names it"
    assert "private-house-location" not in human, human
    assert "private-house-location" not in json.dumps(data)
    expected = {
        "missing": "no preference is stored: the house database does not exist",
        "unreadable": "the house database was not read (StoreError, the rest hidden by --redact); "
        "a preference set there is not shown",
    }[state]
    assert data["database_note"] == expected
    assert f"# {expected}" in human.splitlines()


class _StoreThatFailsToClose(SqlHouseStore):
    """The real store, whose close raises once it has closed - the way a lock release can fail."""

    def close(self) -> None:
        super().close()
        message = f"{self.where}: the writer lock could not be released"
        raise StoreError(message)


class _StoreThatNarrates(SqlHouseStore):
    """The real store, saying one line while it opens - the way a store narrates its own work."""

    def open(self, *, exclusive: bool, create: bool = True) -> None:
        self.log("store", "a line the store says while it opens")
        super().open(exclusive=exclusive, create=create)


class _StoreThatNarratesItsLocation(SqlHouseStore):
    """The real store, saying a line naming ITS OWN LOCATION while it opens - the way a future
    narration line (a migration, an import) could."""

    def open(self, *, exclusive: bool, create: bool = True) -> None:
        self.log("store", f"{self.where}: a line the store says while it opens")
        super().open(exclusive=exclusive, create=create)


def test_a_database_that_fails_to_close_costs_config_one_line_and_not_the_view(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    database = _stored(tmp_path, PreferenceName.WINDOW, 0.7)

    def opener(database: str, *, password: Secret | None, log: LogFn) -> HouseStore:
        return _StoreThatFailsToClose(database, password=password, log=log)

    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "--database", database, "config"])
    assert main(open_store=opener) == 0
    out = capsys.readouterr().out

    line, _ = _line_and_the_one_beneath(out, "dialling.window_s")
    assert line.startswith("dialling.window_s = 0.7    # database (cli, "), "what was read is still shown"
    notes = [line for line in out.splitlines() if line.startswith("# the house database was read but not closed (")]
    assert len(notes) == 1
    assert "the writer lock could not be released" in notes[0]


def test_what_the_store_says_goes_to_stderr_and_leaves_the_view_alone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    """``config``'s STDOUT is the view, but a store that narrates its own work (a migration, one
    day) must still be heard: on STDERR. The control is the same view from the plain store."""
    database = _stored(tmp_path, PreferenceName.WINDOW, 0.7)
    plain = _config_output(monkeypatch, capsys, "--database", database, "config", "--section", "dialling")

    def opener(database: str, *, password: Secret | None, log: LogFn) -> HouseStore:
        return _StoreThatNarrates(database, password=password, log=log)

    monkeypatch.setattr(
        "sys.argv", ["soundtouch-zonemaster-service", "--database", database, "config", "--section", "dialling"]
    )
    assert main(open_store=opener) == 0
    captured = capsys.readouterr()

    assert captured.out == plain
    assert "a line the store says while it opens" in captured.err


def test_the_stores_own_narration_is_masked_too_under_redact(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    """A future narration line naming the database (a migration, an import) must not defeat
    ``--redact`` by printing the very location it was asked to hide (OPEN-WORK rank 218). The
    control is the same run without ``--redact``, which still names it."""
    database = _stored(tmp_path, PreferenceName.WINDOW, 0.7)

    def opener(database: str, *, password: Secret | None, log: LogFn) -> HouseStore:
        return _StoreThatNarratesItsLocation(database, password=password, log=log)

    argv = ["soundtouch-zonemaster-service", "--database", database, "config", "--section", "dialling"]
    monkeypatch.setattr("sys.argv", argv)
    assert main(open_store=opener) == 0
    plain_err = capsys.readouterr().err
    assert database in plain_err, "the control: the plain run does name the database"

    monkeypatch.setattr("sys.argv", [*argv, "--redact"])
    assert main(open_store=opener) == 0
    redacted_err = capsys.readouterr().err

    assert database not in redacted_err
    assert "a line the store says while it opens" in redacted_err
