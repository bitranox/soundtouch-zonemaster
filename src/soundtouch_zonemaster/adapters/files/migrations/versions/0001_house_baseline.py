"""The house database as rank 19 part 1 creates it: the state, the channel list and the switch.

A frozen copy of ``house_schema`` as it was then, never an import of it: a revision that read the
live MetaData would change what an old database is upgraded THROUGH every time the MetaData moved.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.sqlite import REAL

revision = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

_REAL = sa.Float().with_variant(REAL(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "zone",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("channel", sa.Text),
        sa.Column("dial_window_s", _REAL),
        sa.Column("hold_threshold_s", _REAL),
        sa.CheckConstraint("id = 1", name="zone_one_row"),
        sqlite_strict=True,
    )
    for name in ("member", "out_of_multiroom"):
        op.create_table(
            name,
            sa.Column("device_id", sa.Text, primary_key=True),
            sa.Column("position", sa.Integer, nullable=False),
            sqlite_strict=True,
        )
    op.create_table(
        "muted",
        sa.Column("device_id", sa.Text, primary_key=True),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("volume", sa.Integer, nullable=False),
        sqlite_strict=True,
    )
    op.create_table(
        "place",
        sa.Column("channel", sa.Text, primary_key=True),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("track", sa.Integer, nullable=False),
        sa.Column("seconds", _REAL, nullable=False),
        sa.Column("file", sa.Text),
        sqlite_strict=True,
    )
    op.create_table(
        "owed_volume",
        sa.Column("device_id", sa.Text, primary_key=True),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("steps", sa.Integer, nullable=False),
        sqlite_strict=True,
    )
    op.create_table(
        "channel",
        sa.Column("position", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("number", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("mpd_entry", sa.Text, nullable=False),
        sa.Column("mpd_directory", sa.Text, nullable=False),
        sa.Column("in_rotation", sa.Integer, nullable=False),
        sa.Column("at_end", sa.Text, nullable=False),
        sa.CheckConstraint("in_rotation IN (0, 1)", name="channel_in_rotation_flag"),
        sa.UniqueConstraint("number", name="channel_number_unique"),
        sqlite_strict=True,
    )
    op.create_table(
        "switch",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("word", sa.Text, nullable=False),
        sa.Column("changed_at", sa.Text, nullable=False),
        sa.CheckConstraint("id = 1", name="switch_one_row"),
        sa.CheckConstraint("word IN ('on', 'off')", name="switch_word"),
        sqlite_strict=True,
    )


def downgrade() -> None:
    for name in ("switch", "channel", "owed_volume", "place", "out_of_multiroom", "muted", "member", "zone"):
        op.drop_table(name)
