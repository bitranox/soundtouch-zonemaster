"""The prefs verbs, from argv to the house database and back."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

import pytest

from soundtouch_zonemaster.adapters.files.house_store import SqlHouseStore
from soundtouch_zonemaster.entry import service_main as main

if TYPE_CHECKING:
    from pathlib import Path

    from soundtouch_zonemaster.application.options import ServiceOptions


async def _never(_options: ServiceOptions) -> int:
    raise AssertionError("a prefs verb must not start the service")


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", *argv])
    return main(run_service=_never)


def _envelope(capsys: pytest.CaptureFixture[str]) -> Any:
    return json.loads(capsys.readouterr().out)


def _stored(database: str) -> list[tuple[str, str]]:
    store = SqlHouseStore(database, log=lambda _k, _t: None)
    store.open(exclusive=False)
    try:
        return [(row.name, row.text) for row in store.load_preferences()]
    finally:
        store.close()


def test_prefs_lists_all_five_from_the_configuration_on_a_new_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    assert _run(monkeypatch, "--json", "--database", str(tmp_path / "db.sqlite"), "prefs") == 0
    data = _envelope(capsys)["data"]
    assert isinstance(data, dict)
    preferences = cast("list[dict[str, Any]]", data["preferences"])
    assert [(p["name"], p["source"]) for p in preferences] == [
        ("dialling.window_s", "configuration"),
        ("dialling.hold_threshold_s", "configuration"),
        ("mpd.rewind_s", "configuration"),
        ("volume.fade_s", "configuration"),
        ("membership.consoles_allowed", "configuration"),
    ]


def test_set_then_unset_report_before_and_after(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    database = str(tmp_path / "db.sqlite")
    assert _run(monkeypatch, "--json", "--database", database, "prefs", "set", "volume.fade_s", "1.5") == 0
    change = _envelope(capsys)["data"]
    assert isinstance(change, dict)
    assert (change["before"]["source"], change["after"]["value"], change["after"]["source"]) == (
        "configuration",
        1.5,
        "cli",
    )
    assert _run(monkeypatch, "--json", "--database", database, "prefs", "unset", "volume.fade_s") == 0
    change = _envelope(capsys)["data"]
    assert isinstance(change, dict)
    assert (change["before"]["value"], change["after"]["source"], change["after"]["value"]) == (
        1.5,
        "configuration",
        0.8,
    )


def test_a_list_is_written_as_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    database = str(tmp_path / "db.sqlite")
    argv = ("--json", "--database", database, "prefs", "set", "membership.consoles_allowed", '["AABBCC000012"]')
    assert _run(monkeypatch, *argv) == 0
    change = _envelope(capsys)["data"]
    assert isinstance(change, dict)
    assert change["after"]["value"] == ["AABBCC000012"]
    assert _stored(database) == [("membership.consoles_allowed", '["AABBCC000012"]')]


@pytest.mark.parametrize(
    ("argv", "rc", "message"),
    [
        (("set", "dialling.window_s", "9"), 1, "refused: the dialling window must be between 0.5 and 2.0 s, not 9.0"),
        (("set", "zone.bind_ip", '"1.2.3.4"'), 2, "refused: zone.bind_ip is not a preference; set it in a config file"),
        (("set", "dialling.speed", "1"), 2, "refused: no preference called 'dialling.speed'"),
        (("set", "dialling.window_s", "fast"), 2, "refused: 'fast' is not JSON"),
        (("unset", "dialling.speed"), 2, "refused: no preference called 'dialling.speed'"),
    ],
)
def test_a_refused_change_writes_nothing_and_says_why(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    isolated_config_layers: Path,
    tmp_path: Path,
    *,
    argv: tuple[str, ...],
    rc: int,
    message: str,
) -> None:
    database = str(tmp_path / "db.sqlite")
    assert _run(monkeypatch, "--json", "--database", database, "prefs", *argv) == rc
    envelope = _envelope(capsys)
    assert envelope["ok"] is False
    assert str(envelope["message"]).startswith(message)
    assert not (tmp_path / "db.sqlite").exists() or _stored(database) == []


def test_prefs_set_works_while_the_service_holds_the_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    database = str(tmp_path / "db.sqlite")
    service = SqlHouseStore(database, log=lambda _k, _t: None)
    service.open(exclusive=True)
    try:
        assert _run(monkeypatch, "--json", "--database", database, "prefs", "set", "mpd.rewind_s", "5") == 0
        assert [row.text for row in service.load_preferences()] == ["5.0"]
    finally:
        service.close()


def test_the_human_listing_names_each_source(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    database = str(tmp_path / "db.sqlite")
    assert _run(monkeypatch, "--database", database, "prefs", "set", "dialling.window_s", "0.7") == 0
    capsys.readouterr()
    assert _run(monkeypatch, "--database", database, "prefs") == 0
    out = capsys.readouterr().out
    assert "dialling.window_s = 0.7    # database: cli, " in out
    assert "volume.fade_s = 0.8    # configuration" in out
