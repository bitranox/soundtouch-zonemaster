"""The preference rows on a real database: set, replace, unset, and the old calibration carried over."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine, text

from soundtouch_zonemaster.adapters.files.house_db import database_url
from soundtouch_zonemaster.adapters.files.house_store import SqlHouseStore
from soundtouch_zonemaster.adapters.files.state_file import LegacyState, save_state
from soundtouch_zonemaster.application.errors import StoreError
from soundtouch_zonemaster.application.options import LegacyFiles
from soundtouch_zonemaster.domain.preferences import PreferenceName, PreferenceRow, PreferenceSource
from soundtouch_zonemaster.domain.state import ZoneState

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


def _store(database: str, *, exclusive: bool = False) -> SqlHouseStore:
    store = SqlHouseStore(database, log=lambda _kind, _text: None)
    store.open(exclusive=exclusive)
    return store


def test_a_new_database_holds_no_preference(house_database: str) -> None:
    store = _store(house_database)
    try:
        assert store.load_preferences() == ()
    finally:
        store.close()


def test_set_replace_and_unset_each_hand_back_what_was_there(house_database: str) -> None:
    store = _store(house_database)
    try:
        assert store.set_preference(PreferenceName.WINDOW, 0.7, source=PreferenceSource.CLI) is None
        first = store.load_preferences()
        assert [(row.name, row.text, row.source) for row in first] == [("dialling.window_s", "0.7", "cli")]
        assert first[0].changed_at != ""
        assert store.set_preference(PreferenceName.WINDOW, 0.9, source=PreferenceSource.CALIBRATION) == first[0]
        assert [(row.text, row.source) for row in store.load_preferences()] == [("0.9", "calibration")]
        removed = store.unset_preference(PreferenceName.WINDOW)
        assert removed is not None
        assert removed.text == "0.9"
        assert store.load_preferences() == ()
        assert store.unset_preference(PreferenceName.WINDOW) is None
    finally:
        store.close()


def test_a_list_is_stored_as_json_and_rows_come_back_by_name(house_database: str) -> None:
    store = _store(house_database)
    try:
        store.set_preference(PreferenceName.FADE, 1.5, source=PreferenceSource.CLI)
        store.set_preference(PreferenceName.CONSOLES, ("AABBCC000012",), source=PreferenceSource.CLI)
        assert [(row.name, row.text) for row in store.load_preferences()] == [
            ("membership.consoles_allowed", '["AABBCC000012"]'),
            ("volume.fade_s", "1.5"),
        ]
    finally:
        store.close()


def test_a_preference_can_be_set_while_the_service_holds_the_writer_lock(house_database: str) -> None:
    service = _store(house_database, exclusive=True)
    try:
        cli = _store(house_database)
        try:
            cli.set_preference(PreferenceName.REWIND, 5.0, source=PreferenceSource.CLI)
        finally:
            cli.close()
        assert [row.text for row in service.load_preferences()] == ["5.0"]
    finally:
        service.close()


def test_an_old_state_files_calibration_becomes_calibration_rows_with_no_time(
    house_database: str, tmp_path: Path
) -> None:
    state_file = tmp_path / "zone-state.json"
    save_state(state_file, LegacyState(state=ZoneState(channel="3"), dial_window_s=0.6, hold_threshold_s=1.4))
    store = _store(house_database, exclusive=True)
    try:
        store.import_legacy(LegacyFiles(state_file=state_file))
        assert store.load_state().channel == "3"
        assert store.load_preferences() == (
            PreferenceRow(name="dialling.hold_threshold_s", text="1.4", source="calibration", changed_at=""),
            PreferenceRow(name="dialling.window_s", text="0.6", source="calibration", changed_at=""),
        )
    finally:
        store.close()


def test_an_imported_calibration_never_replaces_a_value_somebody_already_set(
    house_database: str, tmp_path: Path
) -> None:
    state_file = tmp_path / "zone-state.json"
    save_state(state_file, LegacyState(state=ZoneState(), dial_window_s=0.6))
    store = _store(house_database, exclusive=True)
    try:
        store.set_preference(PreferenceName.WINDOW, 1.1, source=PreferenceSource.CLI)
        store.import_legacy(LegacyFiles(state_file=state_file))
        assert [(row.text, row.source) for row in store.load_preferences()] == [("1.1", "cli")]
    finally:
        store.close()


def test_several_preferences_are_stored_together_under_one_time(house_database: str) -> None:
    store = _store(house_database)
    try:
        values = {PreferenceName.WINDOW: 0.7, PreferenceName.HOLD: 1.4}
        store.set_preferences(values, source=PreferenceSource.CALIBRATION)
        rows = store.load_preferences()
        assert [(row.name, row.text, row.source) for row in rows] == [
            ("dialling.hold_threshold_s", "1.4", "calibration"),
            ("dialling.window_s", "0.7", "calibration"),
        ]
        assert len({row.changed_at for row in rows}) == 1, "one decision, one moment"
    finally:
        store.close()


_REFUSE_THE_HOLD_SQLITE = """
CREATE TRIGGER refuse_the_hold BEFORE INSERT ON preference
WHEN NEW.name = 'dialling.hold_threshold_s'
BEGIN SELECT RAISE(ABORT, 'refused by the test'); END
"""

_REFUSE_THE_HOLD_POSTGRES = (
    """
CREATE FUNCTION zonemaster_test_refuse_the_hold() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.name = 'dialling.hold_threshold_s' THEN
        RAISE EXCEPTION 'refused by the test';
    END IF;
    RETURN NEW;
END
$$
""",
    """
CREATE TRIGGER refuse_the_hold BEFORE INSERT ON preference
FOR EACH ROW EXECUTE FUNCTION zonemaster_test_refuse_the_hold()
""",
)
"""The same refusal on PostgreSQL. The function outlives the table the fixture drops, so the test
drops it itself."""


@contextmanager
def _the_database_refuses_the_hold(database: str) -> Generator[None]:
    """A real refusal of the hold's row, raised by the database inside whatever transaction writes it."""
    engine = create_engine(database_url(database))
    postgres = engine.dialect.name == "postgresql"
    try:
        with engine.begin() as connection:
            for statement in _REFUSE_THE_HOLD_POSTGRES if postgres else (_REFUSE_THE_HOLD_SQLITE,):
                connection.execute(text(statement))
        yield
    finally:
        if postgres:
            with engine.begin() as connection:
                connection.execute(text("DROP FUNCTION IF EXISTS zonemaster_test_refuse_the_hold() CASCADE"))
        engine.dispose()


def test_a_refused_second_row_leaves_no_row_stored(house_database: str) -> None:
    """All or nothing: the database accepts the window's row and refuses the hold's, and neither is
    kept. A calibration is one decision, and half of it stored is a calibration nobody made.

    The window alone is written first as the control: the same database stores a row the refusal
    does not name, so an empty table afterwards is the rollback and not a store that writes nothing.
    """
    store = _store(house_database)
    try:
        with _the_database_refuses_the_hold(house_database):
            store.set_preferences({PreferenceName.WINDOW: 0.6}, source=PreferenceSource.CLI)
            assert [row.text for row in store.load_preferences()] == ["0.6"], "the control: a row it does not refuse"
            store.unset_preference(PreferenceName.WINDOW)
            with pytest.raises(StoreError):
                store.set_preferences(
                    {PreferenceName.WINDOW: 0.7, PreferenceName.HOLD: 1.4}, source=PreferenceSource.CALIBRATION
                )
            assert store.load_preferences() == (), "the window was rolled back with the hold"
    finally:
        store.close()
