"""Rank 236: the alarm tables.

A frozen description of the change, never an import of ``house_schema`` (see 0001's docstring).
Nothing existing is altered, so a downgrade is the five tables dropped and every alarm lost with
them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.sqlite import REAL

revision = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None

_REAL = sa.Float().with_variant(REAL(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "alarm",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("enabled", sa.Integer, nullable=False),
        sa.Column("channel", sa.Text, nullable=False),
        sa.Column("ramp_s", _REAL, nullable=False),
        sa.Column("snooze_s", _REAL, nullable=False),
        sa.Column("ring_limit_s", _REAL, nullable=False),
        sa.Column("off_sequence", sa.Text),
        sa.Column("set_at", sa.Text, nullable=False),
        sa.CheckConstraint("enabled IN (0, 1)", name="alarm_enabled_flag"),
        sqlite_strict=True,
    )
    op.create_table(
        "alarm_time",
        sa.Column("alarm", sa.Text, primary_key=True),
        sa.Column("weekday", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("at", sa.Text, nullable=False),
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="alarm_time_weekday"),
        sqlite_strict=True,
    )
    op.create_table(
        "alarm_box",
        sa.Column("alarm", sa.Text, primary_key=True),
        sa.Column("device_id", sa.Text, primary_key=True),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("start_volume", sa.Integer, nullable=False),
        sa.Column("max_volume", sa.Integer, nullable=False),
        sqlite_strict=True,
    )
    op.create_table(
        "alarm_day",
        sa.Column("alarm", sa.Text, primary_key=True),
        sa.Column("day", sa.Text, primary_key=True),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("due", sa.Text),
        sa.Column("snoozed_until", sa.Text),
        sa.Column("give_back", sa.Text),
        sa.Column("volumes_before", sa.Text, nullable=False),
        sa.CheckConstraint("state IN ('ringing', 'snoozed', 'done', 'skipped')", name="alarm_day_state"),
        sqlite_strict=True,
    )
    op.create_table(
        "alarm_pause",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("through", sa.Text, nullable=False),
        sa.CheckConstraint("id = 1", name="alarm_pause_one_row"),
        sqlite_strict=True,
    )


def downgrade() -> None:
    for table in ("alarm_pause", "alarm_day", "alarm_box", "alarm_time", "alarm"):
        op.drop_table(table)
