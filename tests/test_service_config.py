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

import json
import os
import shutil
from typing import TYPE_CHECKING

import pytest
from lib_layered_config import REDACTED_PLACEHOLDER

from soundtouch_zonemaster.adapters.config.loader import ENV_PREFIX, clear_config_cache, defaults_from
from soundtouch_zonemaster.entry import service_main as main

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from soundtouch_zonemaster.application.options import ServiceOptions

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
"""The options the deployed unit passes, read off ``systemctl cat soundtouch-multiroom.service``
on 2026-09-11. Only the paths are rewritten below, because ``/var/lib/zonemaster`` does not exist
on a development host and the state file's directory is checked at startup."""


def _capture() -> tuple[list[ServiceOptions], Callable[[ServiceOptions], Awaitable[int]]]:
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
        '[zone]\nbind_ip = "10.9.9.9"\n[files]\nchannel_file = "/tmp/other.json"\n[dialling]\nwindow_s = 1.4\n',
    )
    argv = [part.replace("/var/lib/zonemaster", str(tmp_path)) for part in DEPLOYED_ARGV]
    seen, run = _capture()
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", *argv])

    assert main(run_service=run) == 0
    options = seen[0]
    assert options.bind_ip == "192.168.0.190", "the unit's address, not the file's"
    assert options.channel_file == tmp_path / "channels.json"
    assert options.dial_window_s == 1.4, "and a setting the unit says nothing about still comes from the file"


def test_a_run_with_no_options_at_all_is_configured_entirely_by_file(
    monkeypatch: pytest.MonkeyPatch, isolated_config_layers: Path, tmp_path: Path
) -> None:
    """The other half of the same promise: once a file holds the five settings that have no
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
    for missing in ("bind_ip", "channel_file", "switch_file", "state_file"):
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
        f'[files]\nchannel_file = "{tmp_path}/ch.json"\nswitch_file = "{tmp_path}/sw"\n'
        f'state_file = "{tmp_path}/st.json"\n',
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
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Zero is the value worth refusing: it means "any free port" to a listener and nothing at all
    to a caller, so it would be carried around as a setting that can never be dialled and reported
    as a daemon that is down. Refused at startup instead, naming which port it means - there is
    more than one in these options."""
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
