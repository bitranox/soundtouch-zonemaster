"""What research/capture_zone.py builds and reads before it ever touches a speaker.

The driver is audible in the house, so it cannot be run against the boxes from a test. What can
be tested is everything that decides whether a run is correct: the option checks, the request
documents it builds, and the parsers it reads the answers with. Those are the parts where a typo
costs a whole three-minute experiment and shows up only as a speaker that does nothing.

The journal assertion pins the record format: a later reader greps events.jsonl for
``"kind": "http"``, so the enum has to reach the file as its bare value, never as its member name.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))
from _settings import CaptureSettings
from capture_zone import (
    EventKind,
    Journal,
    KeyName,
    KeyState,
    Options,
    SourceName,
    Step,
    ZoneMember,
    device_id_in,
    key_body,
    parse_options,
    preset_item_in,
    refused_speaker,
    run,
    source_of,
    ssh_argv,
    volume_in,
    zone_document,
)

NOW_PLAYING_STANDBY = '<nowPlaying deviceID="ABCDEF012345" source="STANDBY"></nowPlaying>'
NOW_PLAYING_RADIO = '<nowPlaying deviceID="ABCDEF012345" source="LOCAL_INTERNET_RADIO"></nowPlaying>'
PRESETS = (
    '<presets><preset id="1"><ContentItem source="A">one</ContentItem></preset>'
    '<preset id="2"><ContentItem source="B">two</ContentItem></preset></presets>'
)


@pytest.mark.os_agnostic
def test_options_reject_an_address_that_is_not_one() -> None:
    with pytest.raises(ValueError, match="--slave is not an IP address"):
        Options(master="192.168.0.35", slave="192.168.0.333", out=Path("/tmp/x"), volume=12, force=False)


@pytest.mark.os_agnostic
def test_options_reject_a_volume_off_the_speaker_scale() -> None:
    with pytest.raises(ValueError, match="--volume out of range"):
        Options(master="192.168.0.35", slave="192.168.0.33", out=Path("/tmp/x"), volume=250, force=False)


@pytest.mark.os_agnostic
def test_parse_options_builds_the_record_from_argv() -> None:
    options = parse_options(master="192.168.0.35", slave="192.168.0.33", out=Path("/tmp/zone"))
    assert options.speakers == ("192.168.0.35", "192.168.0.33")
    assert options.out == Path("/tmp/zone")
    assert options.volume == 12
    assert options.force is False


@pytest.mark.os_agnostic
def test_speakers_is_a_named_pair_that_still_iterates() -> None:
    """Both halves matter: the eighteen call sites in ``research/capture_zone.py`` iterate or test
    membership, and the two same-typed fields are only distinguishable by name, which is what the
    record adds. The five here are the only ones that read a field by name."""
    options = parse_options(master="192.168.0.35", slave="192.168.0.33", out=Path("/tmp/zone"))
    assert options.speakers.master == "192.168.0.35"
    assert options.speakers.slave == "192.168.0.33"
    assert list(options.speakers) == ["192.168.0.35", "192.168.0.33"]
    assert "192.168.0.33" in options.speakers


@pytest.mark.os_agnostic
def test_the_cli_is_wired_and_names_every_option() -> None:
    """--help is the one invocation of this tool that cannot wake the house.

    It still proves the command is constructible and that each option reached it, which is the
    part a refactor of the parser silently breaks.
    """
    from capture_zone import cli
    from click.testing import CliRunner

    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    for flag in ("--master", "--slave", "--out", "--volume", "--force", "--json", "--json-bare"):
        assert flag in result.output, flag


@pytest.mark.os_agnostic
def test_a_key_is_a_press_then_a_release_naming_the_key() -> None:
    """The speaker acts on the release, so the order of KeyState is part of the behaviour."""
    assert [key_body(KeyName.PRESET_1, state) for state in KeyState] == [
        '<key state="press" sender="Gabbo">PRESET_1</key>',
        '<key state="release" sender="Gabbo">PRESET_1</key>',
    ]


@pytest.mark.os_agnostic
def test_the_zone_document_names_every_member_by_ip_and_device_id() -> None:
    members = [ZoneMember("192.168.0.31", "AAAAAAAAAAAA"), ZoneMember("192.168.0.35", "BBBBBBBBBBBB")]
    assert zone_document("CCCCCCCCCCCC", members) == (
        '<zone master="CCCCCCCCCCCC">'
        '<member ipaddress="192.168.0.31">AAAAAAAAAAAA</member>'
        '<member ipaddress="192.168.0.35">BBBBBBBBBBBB</member>'
        "</zone>"
    )


@pytest.mark.os_agnostic
def test_an_empty_member_list_dissolves_the_zone() -> None:
    assert zone_document("CCCCCCCCCCCC", []) == '<zone master="CCCCCCCCCCCC"></zone>'


@pytest.mark.os_agnostic
def test_source_of_reads_the_attribute_and_reports_unknown_without_one() -> None:
    assert source_of(NOW_PLAYING_STANDBY) == SourceName.STANDBY
    assert source_of(NOW_PLAYING_RADIO) != SourceName.STANDBY
    assert source_of("<nowPlaying />") == "?"


@pytest.mark.os_agnostic
def test_the_parsers_read_what_a_speaker_answers() -> None:
    assert device_id_in('<info deviceID="AABBCCDDEEFF"><name>Room1</name></info>') == "AABBCCDDEEFF"
    assert volume_in("<volume><actualvolume>17</actualvolume></volume>") == 17
    assert preset_item_in(PRESETS, 2) == '<ContentItem source="B">two</ContentItem>'


@pytest.mark.os_agnostic
def test_a_missing_answer_is_refused_rather_than_guessed() -> None:
    assert volume_in("<volume />") == -1
    with pytest.raises(RuntimeError, match="no deviceID"):
        device_id_in("<info />")
    with pytest.raises(RuntimeError, match="preset 3 not found"):
        preset_item_in(PRESETS, 3)


@pytest.mark.os_agnostic
def test_the_journal_writes_the_enums_as_their_bare_values(tmp_path: Path) -> None:
    """A reader greps events.jsonl for the value, so no member name may reach the file."""
    path = tmp_path / "events.jsonl"
    j = Journal(path)
    j.add(EventKind.HTTP, "192.168.0.33", req="/volume", resp="<ok />")
    j.add(EventKind.STEP, "run", name=Step.SET_ZONE)
    first, second = (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    assert first["kind"] == "http"
    assert first["box"] == "192.168.0.33"
    assert isinstance(first["t"], float)
    assert second == {"t": second["t"], "kind": "step", "box": "run", "name": "1b-setZone"}


@pytest.mark.os_agnostic
def test_a_speaker_named_in_never_touch_is_refused_as_master_or_slave() -> None:
    options = parse_options(master="192.168.0.35", slave="192.168.0.30", out=Path("/tmp/zone"))
    assert refused_speaker(options.speakers, ("192.168.0.30",)) == "192.168.0.30"
    as_master = parse_options(master="192.168.0.30", slave="192.168.0.35", out=Path("/tmp/zone"))
    assert refused_speaker(as_master.speakers, ("192.168.0.30",)) == "192.168.0.30"
    assert refused_speaker(options.speakers, ()) is None
    assert refused_speaker(options.speakers, ("192.168.0.99",)) is None


@pytest.mark.os_agnostic
def test_the_run_refuses_before_it_sends_anything(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The refusal is the first thing run() decides: a speaker it names is never asked a question.

    Both addresses are TEST-NET-1 (RFC 5737), so a regression that reached the network would
    time out against nobody rather than wake a speaker.
    """
    options = parse_options(master="192.0.2.35", slave="192.0.2.30", out=tmp_path / "run")
    settings = CaptureSettings(never_touch=("192.0.2.30",), ssh_user="root")
    assert run(options, settings) == 1
    assert "192.0.2.30" in capsys.readouterr().err
    assert (tmp_path / "run" / "events.jsonl").read_text(encoding="utf-8") == ""


@pytest.mark.os_agnostic
def test_the_speaker_shell_logs_in_as_the_configured_user() -> None:
    argv = ssh_argv("admin", "192.168.0.33", "netstat -tunap")
    assert argv[0] == "ssh"
    assert argv[-2:] == ["admin@192.168.0.33", "netstat -tunap"]
    assert "BatchMode=yes" in argv
