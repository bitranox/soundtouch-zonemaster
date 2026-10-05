"""The alarms, as rows: one per alarm, one per day it wakes, one per box, its days, and the pause.

Saving an alarm writes its parent row FIRST, as an UPSERT, and only then replaces its day and box
rows. On PostgreSQL the UPSERT takes the row lock, so a second writer saving the same alarm waits
there and its delete then sees the first one's committed rows: the replace never interleaves.

Every alarm read back goes through ``domain.alarm.validated``, the rule the CLI wrote it by, so a
row somebody edited by hand costs that one alarm and a reason, never the whole book.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, time
from typing import TYPE_CHECKING

from sqlalchemy import Table, delete, insert, select
from sqlalchemy.dialects import postgresql, sqlite

from ...domain.alarm import (
    VOLUME_CEILING,
    Alarm,
    AlarmBook,
    AlarmBox,
    AlarmDay,
    AlarmRefusedError,
    RingState,
    validated,
)
from .house_schema import ALARM, ALARM_BOX, ALARM_DAY, ALARM_PAUSE, ALARM_TIME

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.engine import Connection, RowMapping

__all__ = ["delete_alarm", "read_alarm_book", "write_alarm", "write_alarm_day", "write_alarm_pause"]

_PAUSE_ROW = 1


def read_alarm_book(connection: Connection, *, since: date) -> AlarmBook:
    """Every alarm, every day from ``since`` on, and the pause, in one read."""
    rejected: list[tuple[str, str]] = []
    alarms = _read_alarms(connection, rejected)
    days = _read_days(connection, since=since, rejected=rejected)
    through = connection.execute(select(ALARM_PAUSE.c.through)).scalar_one_or_none()
    return AlarmBook(
        alarms=alarms,
        days=days,
        paused_through=None if through is None else date.fromisoformat(str(through)),
        rejected=tuple(rejected),
    )


def write_alarm(connection: Connection, alarm: Alarm) -> None:
    """Save one alarm whole: the parent row by UPSERT first, then its days and boxes replaced.

    Run through :func:`domain.alarm.validated` first, the same rule every read checks a row
    against, so a write and a read can never disagree about what an alarm may be: a refused alarm
    raises :class:`AlarmRefusedError` naming why, and nothing is written.
    """
    alarm = validated(alarm)
    row = {
        "name": alarm.name,
        "enabled": int(alarm.enabled),
        "channel": alarm.channel,
        "ramp_s": alarm.ramp_s,
        "snooze_s": alarm.snooze_s,
        "ring_limit_s": alarm.ring_limit_s,
        "off_sequence": alarm.off_sequence,
        "set_at": alarm.set_at.isoformat(),
    }
    _upsert(connection, ALARM, row, keys=("name",))
    _delete_children(connection, alarm.name, tables=(ALARM_TIME, ALARM_BOX))
    times = [
        {"alarm": alarm.name, "weekday": weekday, "at": at.strftime("%H:%M")}
        for weekday, at in enumerate(alarm.times)
        if at is not None
    ]
    if times:
        connection.execute(insert(ALARM_TIME), times)
    boxes = [
        {
            "alarm": alarm.name,
            "device_id": box.device_id,
            "position": position,
            "start_volume": box.start_volume,
            "max_volume": box.max_volume,
        }
        for position, box in enumerate(alarm.boxes)
    ]
    if boxes:
        connection.execute(insert(ALARM_BOX), boxes)


def delete_alarm(connection: Connection, name: str) -> bool:
    """Remove an alarm with its days, boxes and day records; whether there was one to remove."""
    found = connection.execute(delete(ALARM).where(ALARM.c.name == name)).rowcount
    _delete_children(connection, name, tables=(ALARM_TIME, ALARM_BOX, ALARM_DAY))
    return bool(found)


def write_alarm_day(connection: Connection, day: AlarmDay) -> None:
    """Save one alarm's progress on one local day, upserted so a restart's first read carries on from it."""
    row = {
        "alarm": day.alarm,
        "day": day.day.isoformat(),
        "state": day.state.value,
        "due": None if day.due is None else day.due.isoformat(),
        "snoozed_until": None if day.snoozed_until is None else day.snoozed_until.isoformat(),
        "give_back": day.give_back,
        "volumes_before": json.dumps([list(pair) for pair in day.volumes_before]),
    }
    _upsert(connection, ALARM_DAY, row, keys=("alarm", "day"))


def write_alarm_pause(connection: Connection, through: date | None) -> None:
    """Set or clear the house-wide pause: one row through ``through``, or none at all for ``None``."""
    if through is None:
        connection.execute(delete(ALARM_PAUSE))
        return
    _upsert(connection, ALARM_PAUSE, {"id": _PAUSE_ROW, "through": through.isoformat()}, keys=("id",))


def _read_alarms(connection: Connection, rejected: list[tuple[str, str]]) -> tuple[Alarm, ...]:
    times, bad_times = _read_times(connection)
    boxes = _read_boxes(connection)
    alarms: list[Alarm] = []
    for found in connection.execute(select(ALARM).order_by(ALARM.c.name)).mappings():
        name = str(found["name"])
        if name in bad_times:
            rejected.append((name, bad_times[name]))
            continue
        try:
            alarms.append(validated(_as_alarm(found, times=tuple(times[name]), boxes=tuple(boxes[name]))))
        except (AlarmRefusedError, ValueError) as exc:
            rejected.append((name, str(exc)))
    return tuple(alarms)


def _read_times(connection: Connection) -> tuple[defaultdict[str, list[time | None]], dict[str, str]]:
    """Every alarm's seven wake times, Monday first, and the reason for a day whose ``at`` is unusable."""
    times: defaultdict[str, list[time | None]] = defaultdict(lambda: [None] * 7)
    bad_times: dict[str, str] = {}
    for name, weekday, at in connection.execute(select(ALARM_TIME.c.alarm, ALARM_TIME.c.weekday, ALARM_TIME.c.at)):
        try:
            times[str(name)][int(weekday)] = time.fromisoformat(str(at))
        except ValueError as exc:
            bad_times[str(name)] = f"a day's time is not HH:MM ({exc})"
    return times, bad_times


def _read_boxes(connection: Connection) -> defaultdict[str, list[AlarmBox]]:
    """Every alarm's boxes, in the order they were saved."""
    boxes: defaultdict[str, list[AlarmBox]] = defaultdict(list)
    box_rows = select(ALARM_BOX).order_by(ALARM_BOX.c.alarm, ALARM_BOX.c.position)
    for found in connection.execute(box_rows).mappings():
        boxes[str(found["alarm"])].append(
            AlarmBox(
                device_id=str(found["device_id"]),
                start_volume=found["start_volume"],
                max_volume=found["max_volume"],
            )
        )
    return boxes


def _as_alarm(found: RowMapping, *, times: tuple[time | None, ...], boxes: tuple[AlarmBox, ...]) -> Alarm:
    return Alarm(
        name=str(found["name"]),
        enabled=bool(found["enabled"]),
        channel=str(found["channel"]),
        times=times,
        boxes=boxes,
        ramp_s=float(found["ramp_s"]),
        snooze_s=float(found["snooze_s"]),
        ring_limit_s=float(found["ring_limit_s"]),
        off_sequence=None if found["off_sequence"] is None else str(found["off_sequence"]),
        set_at=datetime.fromisoformat(str(found["set_at"])),
    )


def _read_days(connection: Connection, *, since: date, rejected: list[tuple[str, str]]) -> tuple[AlarmDay, ...]:
    query = select(ALARM_DAY).where(ALARM_DAY.c.day >= since.isoformat()).order_by(ALARM_DAY.c.alarm, ALARM_DAY.c.day)
    days: list[AlarmDay] = []
    for found in connection.execute(query).mappings():
        try:
            days.append(_as_day(found))
        except (ValueError, TypeError, RecursionError) as exc:
            rejected.append((str(found["alarm"]), f"day {found['day']}: {exc}"))
    return tuple(days)


def _as_day(found: RowMapping) -> AlarmDay:
    pairs = json.loads(str(found["volumes_before"]))
    return AlarmDay(
        alarm=str(found["alarm"]),
        day=date.fromisoformat(str(found["day"])),
        state=RingState(str(found["state"])),
        due=_aware_moment(found["due"]),
        snoozed_until=_aware_moment(found["snoozed_until"]),
        give_back=None if found["give_back"] is None else str(found["give_back"]),
        volumes_before=tuple((str(device_id), _checked_volume(volume)) for device_id, volume in pairs),
    )


def _aware_moment(value: object) -> datetime | None:
    """A stored moment, with its time zone - never a naive one a later ``.astimezone(UTC)`` would misread."""
    if value is None:
        return None
    moment = datetime.fromisoformat(str(value))
    if moment.tzinfo is None:
        raise ValueError(f"{value!r} has no time zone")
    return moment


def _checked_volume(value: object) -> int:
    """A box's stored volume, the same bound ``domain.alarm`` checks a box's volume against."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= VOLUME_CEILING:
        message = f"a volume is a whole number between 0 and {VOLUME_CEILING}, not {value!r}"
        raise ValueError(message)
    return value


def _delete_children(connection: Connection, name: str, *, tables: tuple[Table, ...]) -> None:
    for table in tables:
        connection.execute(delete(table).where(table.c.alarm == name))


def _upsert(connection: Connection, table: Table, row: Mapping[str, object], *, keys: tuple[str, ...]) -> None:
    """One INSERT ... ON CONFLICT DO UPDATE on whichever backend this is (rank 204: never delete-then-insert)."""
    changed = {column: value for column, value in row.items() if column not in keys}
    dialect = postgresql if connection.dialect.name == "postgresql" else sqlite
    statement = dialect.insert(table).values(**row).on_conflict_do_update(index_elements=list(keys), set_=changed)
    connection.execute(statement)
