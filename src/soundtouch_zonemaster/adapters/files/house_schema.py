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

__all__ = ["CHANNEL", "MEMBER", "METADATA", "MUTED", "OUT_OF_MULTIROOM", "OWED_VOLUME", "PLACE", "SWITCH", "ZONE"]

METADATA = MetaData()

_REAL = Float().with_variant(REAL(), "sqlite")

ZONE = Table(
    "zone",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("channel", Text),
    Column("dial_window_s", _REAL),
    Column("hold_threshold_s", _REAL),
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
