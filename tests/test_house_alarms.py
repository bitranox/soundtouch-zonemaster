"""The alarm rows on a real database, both backends: save, replace, remove, days and the pause."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from soundtouch_zonemaster.adapters.files.house_db import database_url
from soundtouch_zonemaster.adapters.files.house_store import SqlHouseStore
from soundtouch_zonemaster.domain.alarm import Alarm, AlarmBox, AlarmDay, AlarmRefusedError, RingState

SET_AT = datetime(2026, 10, 1, 5, 0, tzinfo=UTC)
DUE = datetime(2026, 10, 2, 5, 0, tzinfo=UTC)


def _store(database: str) -> SqlHouseStore:
    store = SqlHouseStore(database, log=lambda _kind, _text: None)
    store.open(exclusive=False)
    return store


def _alarm(name: str = "weekdays", **changes: Any) -> Alarm:
    alarm = Alarm(
        name=name,
        enabled=True,
        channel="5",
        times=(time(7, 0),) * 5 + (None, None),
        boxes=(
            AlarmBox(device_id="AABBCC0000A2", start_volume=5, max_volume=30),
            AlarmBox(device_id="AABBCC0000A1", start_volume=10, max_volume=35),
        ),
        ramp_s=300.0,
        set_at=SET_AT,
        off_sequence="1234",
    )
    return replace(alarm, **changes)


def test_a_new_database_holds_no_alarm_and_no_pause(house_database: str) -> None:
    store = _store(house_database)
    try:
        book = store.load_alarm_book(since=date(2026, 10, 1))
        assert (book.alarms, book.days, book.paused_through, book.rejected) == ((), (), None, ())
    finally:
        store.close()


def test_an_alarm_comes_back_as_it_was_saved_with_its_boxes_in_order(house_database: str) -> None:
    store = _store(house_database)
    try:
        store.save_alarm(_alarm())
        assert store.load_alarm_book(since=date(2026, 10, 1)).alarms == (_alarm(),)
    finally:
        store.close()


def test_saving_again_replaces_the_days_and_boxes_rather_than_adding_to_them(house_database: str) -> None:
    store = _store(house_database)
    try:
        store.save_alarm(_alarm())
        changed = _alarm(
            times=(None,) * 6 + (time(9, 30),),
            boxes=(AlarmBox(device_id="AABBCC0000A3", start_volume=0, max_volume=20),),
            off_sequence=None,
        )
        store.save_alarm(changed)
        assert store.load_alarm_book(since=date(2026, 10, 1)).alarms == (changed,)
    finally:
        store.close()


def test_removing_an_alarm_takes_its_days_with_it_and_says_whether_it_was_there(house_database: str) -> None:
    store = _store(house_database)
    try:
        store.save_alarm(_alarm())
        store.save_alarm_day(AlarmDay(alarm="weekdays", day=date(2026, 10, 2), state=RingState.SKIPPED, due=DUE))
        assert store.remove_alarm("weekdays") is True
        assert store.remove_alarm("weekdays") is False
        book = store.load_alarm_book(since=date(2026, 10, 1))
        assert (book.alarms, book.days) == ((), ())
    finally:
        store.close()


def test_a_day_is_upserted_and_only_days_since_the_bound_come_back(house_database: str) -> None:
    store = _store(house_database)
    try:
        old = AlarmDay(alarm="weekdays", day=date(2026, 9, 1), state=RingState.DONE, due=DUE)
        on_bound = AlarmDay(alarm="weekdays", day=date(2026, 10, 1), state=RingState.SKIPPED, due=DUE)
        ringing_day = AlarmDay(
            alarm="weekdays",
            day=date(2026, 10, 2),
            state=RingState.RINGING,
            due=DUE,
            give_back="11",
            volumes_before=(("AABBCC0000A1", 22),),
        )
        store.save_alarm_day(old)
        store.save_alarm_day(on_bound)
        store.save_alarm_day(ringing_day)
        snoozed = replace(ringing_day, state=RingState.SNOOZED, snoozed_until=DUE.replace(minute=9))
        store.save_alarm_day(snoozed)
        assert store.load_alarm_book(since=date(2026, 10, 1)).days == (on_bound, snoozed)
    finally:
        store.close()


def test_the_pause_is_one_row_set_moved_and_cleared(house_database: str) -> None:
    store = _store(house_database)
    try:
        store.set_alarm_pause(date(2026, 10, 12))
        store.set_alarm_pause(date(2026, 10, 14))
        assert store.load_alarm_book(since=date(2026, 10, 1)).paused_through == date(2026, 10, 14)
        store.set_alarm_pause(None)
        assert store.load_alarm_book(since=date(2026, 10, 1)).paused_through is None
    finally:
        store.close()


def test_a_hand_edited_alarm_costs_that_alarm_and_a_reason_never_the_book(house_database: str) -> None:
    store = _store(house_database)
    try:
        store.save_alarm(_alarm("good"))
        store.save_alarm(_alarm("bad"))
    finally:
        store.close()
    engine = create_engine(database_url(house_database))
    with engine.begin() as connection:
        connection.execute(text("UPDATE alarm_box SET max_volume = 3 WHERE alarm = 'bad'"))
    engine.dispose()
    store = _store(house_database)
    try:
        book = store.load_alarm_book(since=date(2026, 10, 1))
        assert [alarm.name for alarm in book.alarms] == ["good"]
        assert [name for name, _why in book.rejected] == ["bad"]
        assert "above the 3 it ramps to" in book.rejected[0][1]
    finally:
        store.close()


def test_saving_an_alarm_the_rule_refuses_writes_nothing(house_database: str) -> None:
    store = _store(house_database)
    try:
        with pytest.raises(AlarmRefusedError, match="needs at least one box"):
            store.save_alarm(_alarm(boxes=()))
        assert store.load_alarm_book(since=date(2026, 10, 1)).alarms == ()
    finally:
        store.close()


def test_a_bad_wake_time_rejects_the_whole_alarm_with_a_reason(house_database: str) -> None:
    store = _store(house_database)
    try:
        store.save_alarm(_alarm("bad_time"))
    finally:
        store.close()
    engine = create_engine(database_url(house_database))
    with engine.begin() as connection:
        connection.execute(text("UPDATE alarm_time SET at = '25:99' WHERE alarm = 'bad_time'"))
    engine.dispose()
    store = _store(house_database)
    try:
        book = store.load_alarm_book(since=date(2026, 10, 1))
        assert book.alarms == ()
        assert [name for name, _why in book.rejected] == ["bad_time"]
        assert "not HH:MM" in book.rejected[0][1]
    finally:
        store.close()


def test_a_day_with_unparsable_volumes_is_rejected_with_a_reason(house_database: str) -> None:
    store = _store(house_database)
    try:
        store.save_alarm_day(AlarmDay(alarm="weekdays", day=date(2026, 10, 2), state=RingState.SKIPPED, due=DUE))
    finally:
        store.close()
    engine = create_engine(database_url(house_database))
    with engine.begin() as connection:
        connection.execute(text("UPDATE alarm_day SET volumes_before = '{' WHERE alarm = 'weekdays'"))
    engine.dispose()
    store = _store(house_database)
    try:
        book = store.load_alarm_book(since=date(2026, 10, 1))
        assert book.days == ()
        assert [alarm for alarm, _why in book.rejected] == ["weekdays"]
    finally:
        store.close()


def test_a_naive_due_is_rejected_with_a_reason(house_database: str) -> None:
    store = _store(house_database)
    try:
        store.save_alarm_day(AlarmDay(alarm="weekdays", day=date(2026, 10, 2), state=RingState.SKIPPED, due=DUE))
    finally:
        store.close()
    engine = create_engine(database_url(house_database))
    with engine.begin() as connection:
        connection.execute(text("UPDATE alarm_day SET due = '2026-10-02T05:00:00' WHERE alarm = 'weekdays'"))
    engine.dispose()
    store = _store(house_database)
    try:
        book = store.load_alarm_book(since=date(2026, 10, 1))
        assert book.days == ()
        assert [alarm for alarm, _why in book.rejected] == ["weekdays"]
        assert "time zone" in book.rejected[0][1]
    finally:
        store.close()


def test_an_out_of_range_volume_is_rejected_with_a_reason(house_database: str) -> None:
    store = _store(house_database)
    try:
        store.save_alarm_day(
            AlarmDay(
                alarm="weekdays",
                day=date(2026, 10, 2),
                state=RingState.RINGING,
                due=DUE,
                volumes_before=(("AABBCC0000A1", 22),),
            )
        )
    finally:
        store.close()
    engine = create_engine(database_url(house_database))
    with engine.begin() as connection:
        connection.execute(
            text("""UPDATE alarm_day SET volumes_before = '[["AABBCC0000A1", 200]]' WHERE alarm = 'weekdays'""")
        )
    engine.dispose()
    store = _store(house_database)
    try:
        book = store.load_alarm_book(since=date(2026, 10, 1))
        assert book.days == ()
        assert [alarm for alarm, _why in book.rejected] == ["weekdays"]
        assert "a volume is a whole number between 0 and 100" in book.rejected[0][1]
    finally:
        store.close()
