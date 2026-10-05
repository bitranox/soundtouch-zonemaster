"""The house database's tables, as one MetaData: what the migrations must build, and what the rows go through.

Portable on purpose, because the same tables run on SQLite and on PostgreSQL. Three things
follow from that. Order is a ``position`` column, because only SQLite has a ``rowid`` and even
there a covering index can hand rows back sorted by key rather than as written. A value that is a
fraction is ``Float`` everywhere but ``REAL`` on SQLite, because a STRICT table accepts only
INTEGER, REAL, TEXT, BLOB and ANY and SQLAlchemy would otherwise write FLOAT. And a flag is an
INTEGER held to 0 or 1 by a CHECK, for the same reason a BOOLEAN would not survive STRICT.

STRICT is kept on SQLite because it is what makes the FILE refuse a value of the wrong type,
rather than whoever reads it next. This is not the schema's history: ``migrations/versions`` is,
and ``tests/test_house_db.py`` holds the two to each other.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Column, Float, Integer, MetaData, Table, Text, UniqueConstraint
from sqlalchemy.dialects.sqlite import REAL

__all__ = [
    "ALARM",
    "ALARM_BOX",
    "ALARM_DAY",
    "ALARM_PAUSE",
    "ALARM_TIME",
    "CHANNEL",
    "MEMBER",
    "METADATA",
    "MUTED",
    "OUT_OF_MULTIROOM",
    "OWED_VOLUME",
    "PLACE",
    "PREFERENCE",
    "SWITCH",
    "ZONE",
]

METADATA = MetaData()

_REAL = Float().with_variant(REAL(), "sqlite")

ZONE = Table(
    "zone",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("channel", Text),
    CheckConstraint("id = 1", name="zone_one_row"),
    sqlite_strict=True,
)
"""One row. Its presence is what tells a database never given a state from one given an EMPTY state."""

MEMBER = Table(
    "member",
    METADATA,
    Column("device_id", Text, primary_key=True),
    Column("position", Integer, nullable=False),
    sqlite_strict=True,
)

MUTED = Table(
    "muted",
    METADATA,
    Column("device_id", Text, primary_key=True),
    Column("position", Integer, nullable=False),
    Column("volume", Integer, nullable=False),
    sqlite_strict=True,
)

OUT_OF_MULTIROOM = Table(
    "out_of_multiroom",
    METADATA,
    Column("device_id", Text, primary_key=True),
    Column("position", Integer, nullable=False),
    sqlite_strict=True,
)

PLACE = Table(
    "place",
    METADATA,
    Column("channel", Text, primary_key=True),
    Column("position", Integer, nullable=False),
    Column("track", Integer, nullable=False),
    Column("seconds", _REAL, nullable=False),
    Column("file", Text),
    sqlite_strict=True,
)

OWED_VOLUME = Table(
    "owed_volume",
    METADATA,
    Column("device_id", Text, primary_key=True),
    Column("position", Integer, nullable=False),
    Column("steps", Integer, nullable=False),
    sqlite_strict=True,
)

CHANNEL = Table(
    "channel",
    METADATA,
    Column("position", Integer, primary_key=True, autoincrement=False),
    Column("number", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("url", Text, nullable=False),
    Column("mpd_entry", Text, nullable=False),
    Column("mpd_directory", Text, nullable=False),
    Column("in_rotation", Integer, nullable=False),
    Column("at_end", Text, nullable=False),
    CheckConstraint("in_rotation IN (0, 1)", name="channel_in_rotation_flag"),
    UniqueConstraint("number", name="channel_number_unique"),
    sqlite_strict=True,
)
"""``at_end`` rather than ``end``: END is an SQL keyword on every backend this runs on."""

SWITCH = Table(
    "switch",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("word", Text, nullable=False),
    Column("changed_at", Text, nullable=False),
    CheckConstraint("id = 1", name="switch_one_row"),
    CheckConstraint("word IN ('on', 'off')", name="switch_word"),
    sqlite_strict=True,
)
"""``changed_at`` is ISO 8601 text: a timezone-aware timestamp type differs per backend, and STRICT refuses DATETIME."""

PREFERENCE = Table(
    "preference",
    METADATA,
    Column("name", Text, primary_key=True),
    Column("value", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("changed_at", Text, nullable=False),
    CheckConstraint("source IN ('calibration', 'cli', 'app')", name="preference_source"),
    sqlite_strict=True,
)
"""One row per preference somebody SET (``domain/preferences.py``); no row means the layers decide.

``value`` is JSON text, because four of the five hold a number and one a list, and a column per
preference would make every new one a migration. ``changed_at`` is ISO 8601 text, or empty for a
calibration carried over from before the table existed, whose time was never recorded."""

ALARM = Table(
    "alarm",
    METADATA,
    Column("name", Text, primary_key=True),
    Column("enabled", Integer, nullable=False),
    Column("channel", Text, nullable=False),
    Column("ramp_s", _REAL, nullable=False),
    Column("snooze_s", _REAL, nullable=False),
    Column("ring_limit_s", _REAL, nullable=False),
    Column("off_sequence", Text),
    Column("set_at", Text, nullable=False),
    CheckConstraint("enabled IN (0, 1)", name="alarm_enabled_flag"),
    sqlite_strict=True,
)
"""One row per alarm (``domain/alarm.py``). ``set_at`` is ISO 8601 text, like ``switch.changed_at``."""

ALARM_TIME = Table(
    "alarm_time",
    METADATA,
    Column("alarm", Text, primary_key=True),
    Column("weekday", Integer, primary_key=True, autoincrement=False),
    Column("at", Text, nullable=False),
    CheckConstraint("weekday BETWEEN 0 AND 6", name="alarm_time_weekday"),
    sqlite_strict=True,
)
"""One row per day the alarm wakes, Monday 0; a day with no row has no wake. ``at`` is ``HH:MM``."""

ALARM_BOX = Table(
    "alarm_box",
    METADATA,
    Column("alarm", Text, primary_key=True),
    Column("device_id", Text, primary_key=True),
    Column("position", Integer, nullable=False),
    Column("start_volume", Integer, nullable=False),
    Column("max_volume", Integer, nullable=False),
    sqlite_strict=True,
)

ALARM_DAY = Table(
    "alarm_day",
    METADATA,
    Column("alarm", Text, primary_key=True),
    Column("day", Text, primary_key=True),
    Column("state", Text, nullable=False),
    Column("due", Text),
    Column("snoozed_until", Text),
    Column("give_back", Text),
    Column("volumes_before", Text, nullable=False),
    CheckConstraint("state IN ('ringing', 'snoozed', 'done', 'skipped')", name="alarm_day_state"),
    sqlite_strict=True,
)
"""One alarm's progress on one local day, so a restart carries on from it. ``volumes_before`` is
JSON text, a list of ``[device_id, volume]`` pairs; the times are ISO 8601 text."""

ALARM_PAUSE = Table(
    "alarm_pause",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("through", Text, nullable=False),
    CheckConstraint("id = 1", name="alarm_pause_one_row"),
    sqlite_strict=True,
)
"""The house-wide pause: no alarm fires on any day up to and including ``through``. No row, no pause."""
