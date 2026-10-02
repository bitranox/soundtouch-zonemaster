"""The alarm rules: the record, when it is due, what a key and the clock do, and the ramp."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time
from typing import Any

import pytest

from soundtouch_zonemaster.domain.alarm import (
    Alarm,
    AlarmBox,
    AlarmRefusedError,
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
