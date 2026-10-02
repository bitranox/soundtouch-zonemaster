"""The alarm rules: the record, when it is due, what a key and the clock do, and the ramp."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from soundtouch_zonemaster.domain.alarm import (
    Alarm,
    AlarmBox,
    AlarmRefusedError,
    Firing,
    due_on,
    missed_firings,
    next_firing,
    validated,
)

SET_AT = datetime(2026, 1, 1, tzinfo=UTC)
WEEKDAYS_AT_7 = (time(7, 0),) * 5 + (None, None)


def _alarm(**changes: Any) -> Alarm:
    alarm = Alarm(
        name="weekdays",
        enabled=True,
        channel="5",
        times=WEEKDAYS_AT_7,
        boxes=(AlarmBox(device_id="AABBCC0000A1", start_volume=10, max_volume=35),),
        ramp_s=300.0,
        set_at=SET_AT,
    )
    return replace(alarm, **changes)


def test_a_valid_alarm_comes_back_with_its_device_ids_in_upper_case() -> None:
    alarm = _alarm(boxes=(AlarmBox(device_id="aabbcc0000a1", start_volume=0, max_volume=100),))
    assert validated(alarm).boxes[0].device_id == "AABBCC0000A1"


@pytest.mark.parametrize(
    ("changes", "says"),
    [
        ({"name": ""}, "name"),
        ({"name": " padded "}, "name"),
        ({"name": "x" * 65}, "name"),
        ({"name": "two\nlines"}, "name"),
        ({"channel": ""}, "channel"),
        ({"times": (time(7, 0),) * 6}, "seven"),
        ({"times": (time(7, 0, 30),) + (None,) * 6}, "whole minute"),
        ({"boxes": ()}, "at least one box"),
        ({"boxes": (AlarmBox(device_id="Room1", start_volume=1, max_volume=2),)}, "device id"),
        ({"boxes": (AlarmBox(device_id="AABBCC0000A1", start_volume=40, max_volume=30),)}, "start"),
        ({"boxes": (AlarmBox(device_id="AABBCC0000A1", start_volume=0, max_volume=101),)}, "0 and 100"),
        (
            {
                "boxes": (
                    AlarmBox(device_id="AABBCC0000A1", start_volume=1, max_volume=2),
                    AlarmBox(device_id="aabbcc0000a1", start_volume=1, max_volume=2),
                )
            },
            "twice",
        ),
        ({"ramp_s": -1.0}, "ramp"),
        ({"ramp_s": float("nan")}, "ramp"),
        ({"snooze_s": 30.0}, "snooze"),
        ({"ring_limit_s": 5 * 3600.0}, "ring limit"),
        ({"off_sequence": ""}, "off-sequence"),
        ({"off_sequence": "1237"}, "off-sequence"),
        ({"off_sequence": "1" * 13}, "off-sequence"),
        ({"set_at": SET_AT.replace(tzinfo=None)}, "set_at"),
    ],
)
def test_an_alarm_outside_the_rule_is_refused_naming_what(changes: dict[str, object], says: str) -> None:
    with pytest.raises(AlarmRefusedError, match=says):
        validated(_alarm(**changes))


def test_a_boolean_is_not_a_volume() -> None:
    with pytest.raises(AlarmRefusedError, match="volume"):
        validated(_alarm(boxes=(AlarmBox(device_id="AABBCC0000A1", start_volume=True, max_volume=2),)))


def test_an_alarm_with_no_day_set_is_legal_and_never_due() -> None:
    assert validated(_alarm(times=(None,) * 7)).times == (None,) * 7


def test_the_off_sequence_decides_whether_presets_dial_it() -> None:
    assert not _alarm().takes_digits
    assert _alarm(off_sequence="123456").takes_digits


VIENNA = ZoneInfo("Europe/Vienna")


def _vienna(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=VIENNA)


def test_an_alarm_is_due_at_its_wall_time_on_its_weekday() -> None:
    thursday = date(2026, 10, 1)
    assert due_on(_alarm(), thursday, zone=VIENNA) == _vienna(thursday, 7)
    assert due_on(_alarm(), date(2026, 10, 3), zone=VIENNA) is None  # a Saturday


def test_a_time_in_the_spring_gap_rings_at_the_first_minute_that_exists() -> None:
    gap_day = date(2026, 3, 29)  # 02:00 jumps to 03:00 in Vienna
    alarm = _alarm(times=(None,) * 6 + (time(2, 30),))
    due = due_on(alarm, gap_day, zone=VIENNA)
    assert due is not None
    assert due.astimezone(UTC) == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)  # 03:00 CEST


def test_a_time_in_the_doubled_autumn_hour_rings_once_at_its_first_occurrence() -> None:
    doubled_day = date(2026, 10, 25)  # 03:00 falls back to 02:00 in Vienna
    alarm = _alarm(times=(None,) * 6 + (time(2, 30),))
    due = due_on(alarm, doubled_day, zone=VIENNA)
    assert due is not None
    assert due.astimezone(UTC) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # 02:30 CEST, the first


def _next(
    alarms: tuple[Alarm, ...],
    now: datetime,
    *,
    paused_through: date | None = None,
    handled: frozenset[tuple[str, date]] = frozenset(),
) -> Firing | None:
    return next_firing(alarms, now=now, zone=VIENNA, paused_through=paused_through, handled=handled)


def test_the_earliest_firing_of_all_enabled_alarms_is_next() -> None:
    early = _alarm(name="early", times=(time(6, 0),) * 7)
    late = _alarm(name="late", times=(time(8, 0),) * 7)
    off = _alarm(name="off", enabled=False, times=(time(5, 0),) * 7)
    firing = _next((late, early, off), _vienna(date(2026, 10, 1), 5))
    assert firing is not None
    assert (firing.alarm.name, firing.due) == ("early", _vienna(date(2026, 10, 1), 6))


def test_a_handled_day_is_not_due_again_and_the_next_day_is() -> None:
    day = date(2026, 10, 1)
    firing = _next((_alarm(),), _vienna(day, 6), handled=frozenset({("weekdays", day)}))
    assert firing is not None
    assert firing.day == date(2026, 10, 2)


def test_a_pause_skips_every_day_through_its_last_day_off() -> None:
    firing = _next((_alarm(),), _vienna(date(2026, 10, 1), 6), paused_through=date(2026, 10, 5))
    assert firing is not None
    assert firing.day == date(2026, 10, 6)


def test_a_firing_missed_by_a_down_service_is_still_due_inside_its_ring_limit() -> None:
    day = date(2026, 10, 1)
    firing = _next((_alarm(),), _vienna(day, 7, 50))
    assert firing is not None
    assert firing.due == _vienna(day, 7)  # due in the past: the service fires it now


def test_past_its_ring_limit_a_firing_is_missed_not_due() -> None:
    day = date(2026, 10, 1)
    now = _vienna(day, 8, 0)  # exactly the 60-minute limit
    firing = _next((_alarm(),), now)
    assert firing is not None
    assert firing.day == date(2026, 10, 2)
    wednesday = frozenset({("weekdays", date(2026, 9, 30))})  # a service that was up then holds its row
    missed = missed_firings((_alarm(),), now=now, zone=VIENNA, paused_through=None, handled=wednesday)
    assert [(f.alarm.name, f.day) for f in missed] == [("weekdays", day)]


def test_a_service_down_for_two_days_misses_both_firings_in_order() -> None:
    now = _vienna(date(2026, 10, 1), 8, 0)
    missed = missed_firings((_alarm(),), now=now, zone=VIENNA, paused_through=None, handled=frozenset())
    assert [f.day for f in missed] == [date(2026, 9, 30), date(2026, 10, 1)]


def test_a_firing_due_before_the_alarm_was_saved_is_not_owed() -> None:
    day = date(2026, 10, 1)
    saved_late = _alarm(set_at=_vienna(day, 7, 10))
    firing = _next((saved_late,), _vienna(day, 7, 11))
    assert firing is not None
    assert firing.day == date(2026, 10, 2)
    assert missed_firings((saved_late,), now=_vienna(day, 9), zone=VIENNA, paused_through=None, handled=()) == ()


def test_a_late_firing_reaches_back_across_midnight() -> None:
    alarm = _alarm(times=(time(23, 50),) * 7)
    firing = _next((alarm,), _vienna(date(2026, 10, 2), 0, 20))
    assert firing is not None
    assert firing.day == date(2026, 10, 1)


def test_nothing_is_due_when_every_alarm_is_off() -> None:
    assert _next((_alarm(enabled=False),), _vienna(date(2026, 10, 1), 6)) is None
