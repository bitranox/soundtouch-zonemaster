"""The alarms: when the house wakes whom, onto which channel, and how loud.

Pure rules for the service to act on: the record a person sets (:class:`Alarm`) and the one check
every source runs it through before trusting it (:func:`validated`). The service holds the clock,
the wall-time zone and the wire; none of them is reached from here, so a moment this module cares
about - ``set_at`` - arrives as an aware ``datetime``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .device_id import normalized_device_id

if TYPE_CHECKING:
    from datetime import datetime, time

__all__ = [
    "NAME_CEILING",
    "RAMP_CEILING_S",
    "RING_LIMIT_CEILING_S",
    "RING_LIMIT_DEFAULT_S",
    "RING_LIMIT_FLOOR_S",
    "SEQUENCE_CEILING",
    "SEQUENCE_DIGITS",
    "SNOOZE_CEILING_S",
    "SNOOZE_DEFAULT_S",
    "SNOOZE_FLOOR_S",
    "VOLUME_CEILING",
    "WEEKDAYS",
    "Alarm",
    "AlarmBox",
    "AlarmRefusedError",
    "validated",
]

VOLUME_CEILING = 100
"""A SoundTouch box's volume runs from 0 to 100."""

RAMP_CEILING_S = 3600.0
"""An hour from quiet to loud is already longer than anybody sleeps through an alarm."""

SNOOZE_DEFAULT_S = 540.0
"""Nine minutes, the length every bedside alarm clock has used since the 1950s."""

SNOOZE_FLOOR_S = 60.0
SNOOZE_CEILING_S = 3600.0

RING_LIMIT_DEFAULT_S = 3600.0
"""An alarm nobody answers stops after an hour, counted from when it was due, snoozes included."""

RING_LIMIT_FLOOR_S = 60.0
RING_LIMIT_CEILING_S = 4 * 3600.0
"""Past four hours an unanswered alarm is a house playing to nobody, not a wake-up."""

NAME_CEILING = 64
SEQUENCE_CEILING = 12
SEQUENCE_DIGITS = frozenset("123456")
"""The off-sequence is dialled on the six preset keys, so those are its only digits."""

WEEKDAYS = 7


class AlarmRefusedError(ValueError):
    """An alarm the rule does not allow, carrying the sentence that says why (exit 1 at the CLI)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AlarmBox:
    """One box an alarm wakes, with the level it starts at and the level its ramp ends at."""

    device_id: str
    start_volume: int
    max_volume: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Alarm:
    """One alarm as a person set it.

    ``times`` holds seven entries, Monday first (``date.weekday()``), and ``None`` is a day with no
    wake. ``set_at`` is when it was last saved or enabled: a firing due before then is not owed,
    which is what keeps an alarm created at 07:10 for 07:00 from ringing at once as a late firing.
    """

    name: str
    enabled: bool
    channel: str
    times: tuple[time | None, ...]
    boxes: tuple[AlarmBox, ...]
    ramp_s: float
    set_at: datetime
    snooze_s: float = SNOOZE_DEFAULT_S
    ring_limit_s: float = RING_LIMIT_DEFAULT_S
    off_sequence: str | None = None

    @property
    def takes_digits(self) -> bool:
        """Whether preset keys dial the off-sequence while it rings, rather than snoozing it."""
        return self.off_sequence is not None

    @property
    def box_ids(self) -> frozenset[str]:
        return frozenset(box.device_id for box in self.boxes)


def validated(alarm: Alarm) -> Alarm:
    """The alarm with its device ids folded to upper case, or :class:`AlarmRefusedError` naming why not.

    The one rule for every source: the CLI checks with it before writing, and the store checks every
    row it reads back with it, so a hand-edited database costs that one alarm and a line, never the
    house.
    """
    _check_name(alarm.name)
    if not alarm.channel:
        raise AlarmRefusedError(f"refused: alarm {alarm.name!r} names no channel")
    _check_times(alarm)
    _check_seconds(alarm)
    _check_sequence(alarm)
    if alarm.set_at.tzinfo is None:
        raise AlarmRefusedError(f"refused: alarm {alarm.name!r} has a set_at with no time zone")
    return replace(alarm, boxes=_checked_boxes(alarm))


def _check_name(name: str) -> None:
    if not name or len(name) > NAME_CEILING or name != name.strip() or not name.isprintable():
        message = (
            f"refused: an alarm name is 1 to {NAME_CEILING} printable characters with no space at either end, "
            f"not {name[:NAME_CEILING]!r}"
        )
        raise AlarmRefusedError(message)


def _check_times(alarm: Alarm) -> None:
    if len(alarm.times) != WEEKDAYS:
        message = f"refused: alarm {alarm.name!r} needs seven days, Monday first, not {len(alarm.times)}"
        raise AlarmRefusedError(message)
    for at in alarm.times:
        if at is not None and (at.second or at.microsecond or at.tzinfo is not None):
            message = f"refused: alarm {alarm.name!r} rings on a whole minute of wall time, not {at.isoformat()}"
            raise AlarmRefusedError(message)


def _check_seconds(alarm: Alarm) -> None:
    for what, value, floor, ceiling in (
        ("ramp", alarm.ramp_s, 0.0, RAMP_CEILING_S),
        ("snooze", alarm.snooze_s, SNOOZE_FLOOR_S, SNOOZE_CEILING_S),
        ("ring limit", alarm.ring_limit_s, RING_LIMIT_FLOOR_S, RING_LIMIT_CEILING_S),
    ):
        if not (math.isfinite(value) and floor <= value <= ceiling):
            message = f"refused: alarm {alarm.name!r}: the {what} must be between {floor} and {ceiling} s, not {value}"
            raise AlarmRefusedError(message)


def _check_sequence(alarm: Alarm) -> None:
    sequence = alarm.off_sequence
    if sequence is None:
        return
    if not 1 <= len(sequence) <= SEQUENCE_CEILING or not set(sequence) <= SEQUENCE_DIGITS:
        message = (
            f"refused: alarm {alarm.name!r}: an off-sequence is 1 to {SEQUENCE_CEILING} of the digits 1 to 6, "
            f"not {sequence[:SEQUENCE_CEILING]!r}"
        )
        raise AlarmRefusedError(message)


def _checked_boxes(alarm: Alarm) -> tuple[AlarmBox, ...]:
    if not alarm.boxes:
        raise AlarmRefusedError(f"refused: alarm {alarm.name!r} needs at least one box")
    seen: set[str] = set()
    boxes: list[AlarmBox] = []
    for box in alarm.boxes:
        device_id = normalized_device_id(box.device_id)
        if device_id is None:
            message = f"refused: alarm {alarm.name!r}: {box.device_id[:24]!r} is not a device id (12 hex digits)"
            raise AlarmRefusedError(message)
        if device_id in seen:
            raise AlarmRefusedError(f"refused: alarm {alarm.name!r} names {device_id} twice")
        seen.add(device_id)
        _check_volumes(alarm.name, box)
        boxes.append(replace(box, device_id=device_id))
    return tuple(boxes)


def _check_volumes(name: str, box: AlarmBox) -> None:
    for value in (box.start_volume, box.max_volume):
        _check_volume(name, value)
    if box.start_volume > box.max_volume:
        message = (
            f"refused: alarm {name!r}: {box.device_id} would start at {box.start_volume}, "
            f"above the {box.max_volume} it ramps to"
        )
        raise AlarmRefusedError(message)


def _check_volume(name: str, value: object) -> None:
    # The field is typed int, but a value can arrive here from deserialized, unvalidated data (a
    # hand-edited database row) that bypasses the type system, so this runtime check is real: the
    # parameter is widened to object so the check is not dismissed as always true.
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= VOLUME_CEILING:
        message = f"refused: alarm {name!r}: a volume is a whole number between 0 and {VOLUME_CEILING}, not {value!r}"
        raise AlarmRefusedError(message)
