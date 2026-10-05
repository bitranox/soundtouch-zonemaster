"""The alarms: when the house wakes whom, onto which channel, and how loud.

Pure rules for the service to act on: the record a person sets (:class:`Alarm`) and the one check
every source runs it through before trusting it (:func:`validated`). The service holds the clock,
the wall-time zone and the wire; none of them is reached from here, so a moment this module cares
about - ``set_at`` - arrives as an aware ``datetime``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from .device_id import normalized_device_id

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Iterator
    from datetime import date, time, tzinfo

__all__ = [
    "GAP_SEARCH_MINUTES",
    "LOOKAHEAD_DAYS",
    "LOOKBACK_DAYS",
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
    "AlarmDay",
    "AlarmPress",
    "AlarmRefusedError",
    "Firing",
    "RingAction",
    "RingState",
    "RingStep",
    "dialled",
    "due_on",
    "limit_at",
    "missed",
    "missed_firings",
    "next_firing",
    "pressed",
    "ramp_volume",
    "ringing",
    "rung_again",
    "skipped",
    "ticked",
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


LOOKBACK_DAYS = 1
"""How far back a late firing can reach: the ring limit is at most four hours, so yesterday covers it."""

LOOKAHEAD_DAYS = 7
"""A week ahead finds the next firing of any alarm that has a day set at all."""

GAP_SEARCH_MINUTES = 180
"""How far past a nonexistent wall time to look for the first one that exists.

The gaps a daylight-saving change opens are an hour (thirty minutes on Lord Howe); a whole day a
zone skipped, as Samoa did on 2011-12-30, has no minute that exists and the alarm is simply not
due that day."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Firing:
    """One alarm due on one local day, at one moment."""

    alarm: Alarm
    day: date
    due: datetime


def limit_at(alarm: Alarm, due: datetime) -> datetime:
    """When this firing stops on its own: the ring limit, counted in REAL seconds from the SCHEDULED time.

    One rule for an on-time firing, a late one and a restart mid-ring: a 07:00 alarm fired late at
    07:50 with a 60-minute limit still stops at 08:00. ``due`` is normalised to UTC before the
    limit is added: a ``timedelta`` added to an aware local datetime is wall-clock arithmetic, so
    across a daylight-saving change a 60-minute limit would last 120 real minutes in the autumn or
    only the clock change short of 4 hours in the spring. Counting from the UTC instant keeps the
    limit a fixed REAL duration whichever side of a change it falls on.
    """
    return due.astimezone(UTC) + timedelta(seconds=alarm.ring_limit_s)


def due_on(alarm: Alarm, day: date, *, zone: tzinfo) -> datetime | None:
    """When ``alarm`` is due on ``day`` in ``zone``, or ``None`` when it has no wake that day.

    ``fold=0`` is what ``datetime`` builds, which is the FIRST of a doubled autumn hour, so a time
    there rings once. A time inside a spring gap does not exist; it rings at the first minute after
    it that does.
    """
    at = alarm.times[day.weekday()]
    if at is None:
        return None
    local = datetime.combine(day, at, tzinfo=zone)
    for _ in range(GAP_SEARCH_MINUTES + 1):
        if _exists(local):
            return local
        local += timedelta(minutes=1)
    return None


def _in_time_order(firing: Firing) -> tuple[datetime, str]:
    """Sort key: the firing's due instant in UTC, then its alarm's name to break a tie."""
    return firing.due.astimezone(UTC), firing.alarm.name


def next_firing(
    alarms: Iterable[Alarm],
    *,
    now: datetime,
    zone: tzinfo,
    paused_through: date | None,
    handled: Collection[tuple[str, date]],
) -> Firing | None:
    """The earliest firing still owed, which may already be due (a late firing), or ``None``."""
    instant = now.astimezone(UTC)
    owed = [
        firing
        for firing in _open_firings(alarms, now=instant, zone=zone, paused_through=paused_through, handled=handled)
        if limit_at(firing.alarm, firing.due) > instant
    ]
    return min(owed, key=_in_time_order, default=None)


def missed_firings(
    alarms: Iterable[Alarm],
    *,
    now: datetime,
    zone: tzinfo,
    paused_through: date | None,
    handled: Collection[tuple[str, date]],
) -> tuple[Firing, ...]:
    """Every firing within the lookback whose ring limit passed before anything rang it, in time order."""
    instant = now.astimezone(UTC)
    missed = [
        firing
        for firing in _open_firings(alarms, now=instant, zone=zone, paused_through=paused_through, handled=handled)
        if limit_at(firing.alarm, firing.due) <= instant
    ]
    return tuple(sorted(missed, key=_in_time_order))


def _open_firings(
    alarms: Iterable[Alarm],
    *,
    now: datetime,
    zone: tzinfo,
    paused_through: date | None,
    handled: Collection[tuple[str, date]],
) -> Iterator[Firing]:
    today = now.astimezone(zone).date()
    for alarm in alarms:
        if alarm.enabled:
            yield from _firings_of(alarm, today=today, zone=zone, paused_through=paused_through, handled=handled)


def _firings_of(
    alarm: Alarm, *, today: date, zone: tzinfo, paused_through: date | None, handled: Collection[tuple[str, date]]
) -> Iterator[Firing]:
    for offset in range(-LOOKBACK_DAYS, LOOKAHEAD_DAYS + 1):
        day = today + timedelta(days=offset)
        if (paused_through is not None and day <= paused_through) or (alarm.name, day) in handled:
            continue
        due = due_on(alarm, day, zone=zone)
        if due is not None and due.astimezone(UTC) >= alarm.set_at.astimezone(UTC):
            yield Firing(alarm=alarm, day=day, due=due)


def _exists(local: datetime) -> bool:
    """Whether this wall time happens at all: a time in a spring gap does not survive a round trip."""
    back = local.astimezone(UTC).astimezone(local.tzinfo)
    return back.replace(tzinfo=None) == local.replace(tzinfo=None)


class RingState(StrEnum):
    """Where one alarm stands on one day. The database refuses any other word (a CHECK constraint)."""

    RINGING = "ringing"
    SNOOZED = "snoozed"
    DONE = "done"
    SKIPPED = "skipped"


class RingAction(StrEnum):
    """What the service must do on the wire after a step."""

    NONE = "none"
    OFF = "off"
    """Put each box's volume back while it is awake, release it, and give the zone its channel back."""
    FIRE = "fire"
    """Put the alarm's channel on the zone, take the boxes in and start their ramps."""


class AlarmPress(StrEnum):
    """What a person did while it rang, as far as the alarm cares."""

    KEY = "key"
    """Thumbs, next, previous, or a preset when no off-sequence is armed - on any box in the zone."""
    STANDBY = "standby"
    """One of the alarm's OWN boxes was switched off; a box elsewhere leaving the zone is not this."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AlarmDay:
    """One alarm on one local day, kept in the database so a restart carries on from it.

    ``volumes_before`` is captured once, at the first firing: a snooze already put the volumes back
    before its refire, and a restart mid-ring finds the boxes at alarm levels. ``give_back`` is the
    channel NUMBER the zone played (``None`` when it was empty); its MPD place is kept by the zone
    state's positions like any other channel's.
    """

    alarm: str
    day: date
    state: RingState
    due: datetime | None = None
    snoozed_until: datetime | None = None
    give_back: str | None = None
    volumes_before: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class RingStep:
    """What one event decides: the day's new state, and the action it hands the service for the wire."""

    day: AlarmDay
    action: RingAction


def ringing(firing: Firing, *, give_back: str | None, volumes_before: tuple[tuple[str, int], ...]) -> AlarmDay:
    """The day a firing opens: ringing, with what to give back when it ends."""
    return AlarmDay(
        alarm=firing.alarm.name,
        day=firing.day,
        state=RingState.RINGING,
        due=firing.due,
        give_back=give_back,
        volumes_before=volumes_before,
    )


def rung_again(day: AlarmDay, *, give_back: str | None) -> AlarmDay:
    """Ringing again - after a snooze, or after a restart (which passes the day's own ``give_back``)."""
    return replace(day, state=RingState.RINGING, snoozed_until=None, give_back=give_back)


def skipped(firing: Firing) -> AlarmDay:
    """A firing the house let pass without ringing: no boxes taken in, nothing to give back."""
    return AlarmDay(alarm=firing.alarm.name, day=firing.day, state=RingState.SKIPPED, due=firing.due)


def missed(firing: Firing) -> AlarmDay:
    """A firing whose whole ring limit passed while nothing could ring it: done, never rung."""
    return AlarmDay(alarm=firing.alarm.name, day=firing.day, state=RingState.DONE, due=firing.due)


def pressed(day: AlarmDay, alarm: Alarm, press: AlarmPress, *, now: datetime) -> RingStep:
    """A key snoozes; standby on the alarm's own box ends it, unless an off-sequence is armed."""
    if day.state is not RingState.RINGING:
        return RingStep(day=day, action=RingAction.NONE)
    if press is AlarmPress.STANDBY and not alarm.takes_digits:
        return _off(day)
    return _snoozed(day, alarm, now=now)


def dialled(day: AlarmDay, alarm: Alarm, digits: str) -> RingStep:
    """The full off-sequence ends it; anything else changes nothing, and snooze stays available."""
    if day.state is not RingState.RINGING or digits != alarm.off_sequence:
        return RingStep(day=day, action=RingAction.NONE)
    return _off(day)


def ticked(day: AlarmDay, alarm: Alarm, *, now: datetime) -> RingStep:
    """What the clock does: a snooze that ran out refires, a ring limit that passed ends it."""
    if day.state not in {RingState.RINGING, RingState.SNOOZED} or day.due is None:
        return RingStep(day=day, action=RingAction.NONE)
    now = now.astimezone(UTC)
    if now >= limit_at(alarm, day.due):
        action = RingAction.OFF if day.state is RingState.RINGING else RingAction.NONE
        return RingStep(day=replace(day, state=RingState.DONE, snoozed_until=None), action=action)
    if day.state is RingState.SNOOZED and day.snoozed_until is not None and now >= day.snoozed_until:
        return RingStep(day=day, action=RingAction.FIRE)
    return RingStep(day=day, action=RingAction.NONE)


def ramp_volume(box: AlarmBox, *, ramp_s: float, elapsed_s: float) -> int:
    """How loud ``box`` is ``elapsed_s`` into its ramp: a straight line from its start to its max."""
    if ramp_s <= 0 or elapsed_s >= ramp_s:
        return box.max_volume
    share = max(elapsed_s, 0.0) / ramp_s
    return box.start_volume + round((box.max_volume - box.start_volume) * share)


def _off(day: AlarmDay) -> RingStep:
    return RingStep(day=replace(day, state=RingState.DONE, snoozed_until=None), action=RingAction.OFF)


def _snoozed(day: AlarmDay, alarm: Alarm, *, now: datetime) -> RingStep:
    until = now.astimezone(UTC) + timedelta(seconds=alarm.snooze_s)
    if day.due is not None and until >= limit_at(alarm, day.due):
        return _off(day)
    return RingStep(day=replace(day, state=RingState.SNOOZED, snoozed_until=until), action=RingAction.OFF)
