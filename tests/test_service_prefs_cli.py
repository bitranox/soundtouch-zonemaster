"""The prefs verbs, from argv to the house database and back."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from typing import TYPE_CHECKING, Any, cast

import pytest
from service_database import created_by_the_service

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


HUGE_INTEGER = "1" + "0" * 400
"""JSON for an integer no float can hold: ``float()`` raises OverflowError on it."""

TOO_MANY_DIGITS = "1" * 5000
"""An integer literal past Python's 4300-digit conversion limit: ``json.loads`` raises ValueError."""


def _stored_by_hand(database: str, name: str, text: str) -> None:
    """A row written straight into the table, as somebody editing the database by hand leaves it.

    The store is opened first so the table exists at the current schema; the row then bypasses
    every check, which ``prefs set`` and the store's own writes would apply.
    """
    store = SqlHouseStore(database, log=lambda _k, _t: None)
    store.open(exclusive=False)
    store.close()
    with contextlib.closing(sqlite3.connect(database)) as raw, raw:
        raw.execute("INSERT INTO preference VALUES (?, ?, 'cli', '')", (name, text))


def test_prefs_lists_all_five_from_the_configuration_on_a_new_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    assert _run(monkeypatch, "--json", "--database", str(created_by_the_service(tmp_path / "db.sqlite")), "prefs") == 0
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
    database = str(created_by_the_service(tmp_path / "db.sqlite"))
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
    database = str(created_by_the_service(tmp_path / "db.sqlite"))
    argv = ("--json", "--database", database, "prefs", "set", "membership.consoles_allowed", '["AABBCC000012"]')
    assert _run(monkeypatch, *argv) == 0
    change = _envelope(capsys)["data"]
    assert isinstance(change, dict)
    assert change["after"]["value"] == ["AABBCC000012"]
    assert _stored(database) == [("membership.consoles_allowed", '["AABBCC000012"]')]


def test_a_lower_case_console_id_is_stored_and_shown_upper_case(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    database = str(created_by_the_service(tmp_path / "db.sqlite"))
    argv = ("--json", "--database", database, "prefs", "set", "membership.consoles_allowed", '["a1b2c3d4e5f6"]')
    assert _run(monkeypatch, *argv) == 0
    change = _envelope(capsys)["data"]
    assert isinstance(change, dict)
    assert change["after"]["value"] == ["A1B2C3D4E5F6"]
    assert _stored(database) == [("membership.consoles_allowed", '["A1B2C3D4E5F6"]')]


@pytest.mark.parametrize(
    ("argv", "rc", "message"),
    [
        (("set", "dialling.window_s", "9"), 1, "refused: the dialling window must be between 0.5 and 2.0 s, not 9.0"),
        (("set", "zone.bind_ip", '"1.2.3.4"'), 2, "refused: zone.bind_ip is not a preference; set it in a config file"),
        (("set", "dialling.speed", "1"), 2, "refused: no preference called 'dialling.speed'"),
        (("set", "dialling.window_s", "fast"), 2, "refused: 'fast' is not JSON"),
        (
            ("set", "dialling.window_s", HUGE_INTEGER),
            1,
            "refused: dialling.window_s is too large to be a number of seconds",
        ),
        (("set", "dialling.window_s", TOO_MANY_DIGITS), 2, f"refused: '{'1' * 80}'... is not JSON"),
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
    assert len(str(envelope["message"])) < 300, "a refusal quotes a typed value cut short, never whole"
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
    database = str(created_by_the_service(tmp_path / "db.sqlite"))
    assert _run(monkeypatch, "--database", database, "prefs", "set", "dialling.window_s", "0.7") == 0
    capsys.readouterr()
    assert _run(monkeypatch, "--database", database, "prefs") == 0
    out = capsys.readouterr().out
    assert "dialling.window_s = 0.7    # database: cli, " in out
    assert "volume.fade_s = 0.8    # configuration" in out


@pytest.mark.parametrize(
    ("name", "text", "why"),
    [
        ("dialling.window_s", "9.0", "refused: the dialling window must be between 0.5 and 2.0 s, not 9.0"),
        ("mpd.rewind_s", HUGE_INTEGER, "refused: mpd.rewind_s is too large to be a number of seconds"),
        ("volume.fade_s", TOO_MANY_DIGITS, "not JSON"),
    ],
    ids=["out-of-bounds", "overflows-a-float", "past-the-digit-limit"],
)
def test_a_row_nobody_can_use_is_listed_ignored_with_its_raw_text_and_unset_clears_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    isolated_config_layers: Path,
    tmp_path: Path,
    *,
    name: str,
    text: str,
    why: str,
) -> None:
    """A hand-edited row does not decide its preference, is named with what it holds, and the way
    out - ``prefs unset`` - works on it. The two numbers no float or decoder can take once raised
    out of all three commands, leaving the row stuck in the database with no verb to reach it."""
    database = str(tmp_path / "db.sqlite")
    _stored_by_hand(database, name, text)

    assert _run(monkeypatch, "--json", "--database", database, "prefs") == 0
    data = _envelope(capsys)["data"]
    assert isinstance(data, dict)
    assert data["ignored"] == [{"name": name, "text": text, "why": why}], "the JSON keeps the raw text whole"
    preferences = cast("list[dict[str, Any]]", data["preferences"])
    assert next(p for p in preferences if p["name"] == name)["source"] == "configuration"

    assert _run(monkeypatch, "--database", database, "prefs") == 0
    lines = capsys.readouterr().out.splitlines()
    shown = text if len(text) <= 80 else text[:80] + "..."
    assert f"# ignored: {name} = {shown} ({why})" in lines, lines

    assert _run(monkeypatch, "--json", "--database", database, "prefs", "unset", name) == 0
    change = _envelope(capsys)["data"]
    assert isinstance(change, dict)
    assert change["after"]["source"] == "configuration"
    assert _stored(database) == [], "the row is gone"


def test_the_human_listing_keeps_a_multi_line_row_on_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_config_layers: Path, tmp_path: Path
) -> None:
    """A row spanning lines is printed with the line break escaped, so it cannot pass for a second
    line of the listing."""
    database = str(tmp_path / "db.sqlite")
    _stored_by_hand(database, "mpd.rewind_s", "1\n2")
    assert _run(monkeypatch, "--database", database, "prefs") == 0
    assert "# ignored: mpd.rewind_s = 1\\n2 (not JSON)" in capsys.readouterr().out.splitlines()


@pytest.mark.parametrize(
    ("argv", "note"),
    [
        (("set", "volume.fade_s", "1.5"), "a running service reads it within about a second"),
        (
            ("set", "membership.consoles_allowed", '["AABBCC000012"]'),
            "a running service reads it within about a second; a console no longer allowed is let go at the "
            "next pass, one newly allowed is asked what it is playing at once and taken in if it plays the "
            "house's stream, or when it next wakes",
        ),
    ],
    ids=["fade", "consoles"],
)
def test_the_note_says_when_a_running_service_acts_on_the_change(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    isolated_config_layers: Path,
    tmp_path: Path,
    *,
    argv: tuple[str, ...],
    note: str,
) -> None:
    """A console put on the list is not always taken in "within about a second": only one already on
    the house's stream is, and any other waits for its next wake. The note promises only what the
    service does."""
    database = str(created_by_the_service(tmp_path / "db.sqlite"))
    assert _run(monkeypatch, "--json", "--database", database, "prefs", *argv) == 0
    change = _envelope(capsys)["data"]
    assert isinstance(change, dict)
    assert change["note"] == note
