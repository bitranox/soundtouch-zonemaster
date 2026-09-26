"""The store verbs of the service CLI, from argv to the database and back."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from soundtouch_zonemaster.adapters.files.channel_file import save_channels
from soundtouch_zonemaster.adapters.files.house_store import SqlHouseStore
from soundtouch_zonemaster.domain.channellist import Channel, ChannelList
from soundtouch_zonemaster.domain.enums import ChannelKind
from soundtouch_zonemaster.entry import service_main as main

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from soundtouch_zonemaster.application.options import ServiceOptions

LIST = ChannelList(channels=(Channel(number="1", name="One", kind=ChannelKind.RADIO, url="http://radio.example/1"),))


async def _never(_options: ServiceOptions) -> int:
    raise AssertionError("a store verb must not start the service")


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", *argv])
    return main(run_service=_never)


def _envelope(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def test_the_switch_reads_on_in_a_new_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    rc = _run(monkeypatch, "--json", "--database", str(tmp_path / "db.sqlite"), "switch")
    assert rc == 0
    assert _envelope(capsys)["data"] == {"database": str(tmp_path / "db.sqlite"), "on": True, "changed": False}


def test_switching_off_is_read_back_and_reports_the_change(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    database = str(tmp_path / "db.sqlite")
    assert _run(monkeypatch, "--json", "--database", database, "switch", "off") == 0
    assert _envelope(capsys)["data"] == {"database": database, "on": False, "changed": True}
    assert _run(monkeypatch, "--json", "--database", database, "switch") == 0
    assert _envelope(capsys)["data"] == {"database": database, "on": False, "changed": False}


def test_the_switch_works_while_the_service_holds_the_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    service = SqlHouseStore(str(tmp_path / "db.sqlite"), log=lambda _k, _t: None)
    service.open(exclusive=True)
    try:
        assert _run(monkeypatch, "--json", "--database", str(tmp_path / "db.sqlite"), "switch", "off") == 0
        assert service.is_on() is False
    finally:
        service.close()


def test_an_import_is_refused_while_the_service_runs_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    source = tmp_path / "edited.json"
    save_channels(source, LIST)
    service = SqlHouseStore(str(tmp_path / "db.sqlite"), log=lambda _k, _t: None)
    service.open(exclusive=True)
    try:
        rc = _run(monkeypatch, "--json", "--database", str(tmp_path / "db.sqlite"), "channels", "import", str(source))
        envelope = _envelope(capsys)
        assert service.load_channels() == ChannelList()
    finally:
        service.close()
    assert rc == 1
    assert envelope["ok"] is False
    assert envelope["error"] == "StoreBusyError"


def test_export_then_import_round_trips_the_list(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    database = tmp_path / "db.sqlite"
    source = tmp_path / "edited.json"
    save_channels(source, LIST)
    assert _run(monkeypatch, "--json", "--database", str(database), "channels", "import", str(source)) == 0
    assert _envelope(capsys)["data"] == {"database": str(database), "channels": 1, "path": str(source)}
    exported = tmp_path / "out.json"
    assert (
        _run(monkeypatch, "--json", "--database", str(database), "channels", "export", "--output", str(exported)) == 0
    )
    assert exported.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")


def test_an_unusable_import_is_could_not_run_and_names_the_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    source = tmp_path / "broken.json"
    source.write_text('{"channels": [{"number": "x"}]}', encoding="utf-8")
    rc = _run(monkeypatch, "--json", "--database", str(tmp_path / "db.sqlite"), "channels", "import", str(source))
    envelope = _envelope(capsys)
    assert rc == 2
    assert envelope["error"] == "StoreError"
    assert "broken.json" in str(envelope["message"])


def test_an_export_to_a_missing_directory_is_could_not_run_and_names_the_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    output = tmp_path / "nonexistent-dir" / "out.json"
    rc = _run(
        monkeypatch, "--json", "--database", str(tmp_path / "db.sqlite"), "channels", "export", "--output", str(output)
    )
    envelope = _envelope(capsys)
    assert rc == 2
    assert envelope["error"] == "FileNotFoundError"
    assert str(output) in str(envelope["message"])


def test_the_switchs_human_output_names_the_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A mistyped ``--database`` must be visible in the human line, not only in the JSON envelope."""
    database = str(tmp_path / "db.sqlite")
    assert _run(monkeypatch, "--database", database, "switch") == 0
    assert database in capsys.readouterr().out


def test_channels_exports_human_output_names_the_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The FINAL line - the answer, as opposed to the narration ``load_channels`` logs on its own
    way to answering it - must itself name the database, so a mistyped ``--database`` is visible
    even with narration silenced (``--quiet``, a systemd unit's own log level, and so on)."""
    database = tmp_path / "db.sqlite"
    source = tmp_path / "edited.json"
    save_channels(source, LIST)
    assert _run(monkeypatch, "--json", "--database", str(database), "channels", "import", str(source)) == 0
    capsys.readouterr()  # discard the import's own (JSON) output, which also names the database
    exported = tmp_path / "out.json"
    assert _run(monkeypatch, "--database", str(database), "channels", "export", "--output", str(exported)) == 0
    last_line = capsys.readouterr().out.splitlines()[-1]
    assert str(database) in last_line
    assert str(exported) in last_line


def _url_where_differs_from_the_raw_setting(tmp_path: Path) -> tuple[str, str]:
    """A database URL whose redacted ``where`` is not byte-identical to the raw setting, without
    needing a password (refused before either report is ever built) or a real PostgreSQL server:
    a query VALUE that is not already percent-encoded is one, once SQLAlchemy renders it back."""
    path = tmp_path / "db.sqlite"
    setting = f"sqlite:///{path}?passfile=/etc/pgpass"
    where = f"sqlite:///{path}?passfile=%2Fetc%2Fpgpass"
    assert setting != where, "the fixture itself must exercise a real difference"
    return setting, where


def test_the_switchs_envelope_names_the_same_database_the_human_line_would(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The envelope's ``database`` field must be the store's redacted ``where``, not the raw
    setting: the two must never disagree about what a caller is told the database is."""
    setting, where = _url_where_differs_from_the_raw_setting(tmp_path)
    assert _run(monkeypatch, "--json", "--database", setting, "switch") == 0
    assert _envelope(capsys)["data"] == {"database": where, "on": True, "changed": False}


def test_channels_exports_envelope_names_the_same_database_the_human_line_would(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    setting, where = _url_where_differs_from_the_raw_setting(tmp_path)
    source = tmp_path / "edited.json"
    save_channels(source, LIST)
    assert _run(monkeypatch, "--json", "--database", setting, "channels", "import", str(source)) == 0
    capsys.readouterr()
    exported = tmp_path / "out.json"
    assert _run(monkeypatch, "--json", "--database", setting, "channels", "export", "--output", str(exported)) == 0
    assert _envelope(capsys)["data"] == {"database": where, "channels": 1, "path": str(exported)}


def test_channels_imports_envelope_names_the_same_database_as_the_switchs_would(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """``channels import`` keeps its own human line unchanged (no database named in it), but its
    envelope carries the same redacted ``where`` as every other verb's does."""
    setting, where = _url_where_differs_from_the_raw_setting(tmp_path)
    source = tmp_path / "edited.json"
    save_channels(source, LIST)
    assert _run(monkeypatch, "--json", "--database", setting, "channels", "import", str(source)) == 0
    assert _envelope(capsys)["data"] == {"database": where, "channels": 1, "path": str(source)}


def test_no_database_anywhere_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    rc = _run(monkeypatch, "--json", "switch")
    assert rc == 2
    assert "database" in str(_envelope(capsys)["message"])
