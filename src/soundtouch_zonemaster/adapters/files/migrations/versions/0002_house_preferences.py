"""Rank 197: the house preferences get a table, and the zone row's two calibrated values move into it.

A frozen description of the change, never an import of ``house_schema`` (see 0001's docstring).

A carried-over value is written with ``source = 'calibration'`` and an EMPTY ``changed_at``: the
zone row never recorded when the calibration ran, and a migration time would claim one it did.

On SQLite the two columns are dropped with SQLite's own ``ALTER TABLE ... DROP COLUMN`` rather than
Alembic's batch mode. Batch mode copies the table into a new one built from what SQLAlchemy
REFLECTS, so what this frozen revision produces would depend on how well the installed SQLAlchemy
reads back every table option and constraint - STRICT above all, which is what makes the file
refuse a value of the wrong type. The SQLAlchemy this program requires does reflect it (measured on
2.1.1), but a revision should not rest on that: the native statement alters ``zone`` in place and
keeps everything it does not name. It needs SQLite 3.35; this program already refuses anything
older than 3.37, which STRICT itself needs.
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.sqlite import REAL

revision = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None

_REAL = sa.Float().with_variant(REAL(), "sqlite")
_INSERT = sa.text("INSERT INTO preference (name, value, source, changed_at) VALUES (:name, :value, 'calibration', '')")
_READ_BACK = sa.text("SELECT value FROM preference WHERE name = :name")


def upgrade() -> None:
    op.create_table(
        "preference",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("changed_at", sa.Text, nullable=False),
        sa.CheckConstraint("source IN ('calibration', 'cli', 'app')", name="preference_source"),
        sqlite_strict=True,
    )
    bind = op.get_bind()
    zone = bind.execute(sa.text("SELECT dial_window_s, hold_threshold_s FROM zone WHERE id = 1")).one_or_none()
    if zone is not None:
        for name, value in (("dialling.window_s", zone[0]), ("dialling.hold_threshold_s", zone[1])):
            if value is not None:
                bind.execute(_INSERT, {"name": name, "value": json.dumps(float(value))})
    if bind.dialect.name == "sqlite":
        op.execute("ALTER TABLE zone DROP COLUMN dial_window_s")
        op.execute("ALTER TABLE zone DROP COLUMN hold_threshold_s")
    else:
        op.drop_column("zone", "dial_window_s")
        op.drop_column("zone", "hold_threshold_s")


def downgrade() -> None:
    """Put the stored window and hold back on the ``zone`` row, and drop the ``preference`` table.

    Only what 0001 can hold goes back, and the rest is lost with the table:

    - a window or hold row is written onto the ``zone`` row with ``id = 1``; a database with no
      ``zone`` row (never given a state) has nowhere to put it, and the value is dropped;
    - the source is not kept: a ``cli`` or ``app`` row goes down exactly as a calibration would,
      because the old columns were only ever written by one;
    - the other three preferences have no column in 0001 and are dropped;
    - both rows are read and converted with :func:`_number` BEFORE either column is added: a
      window or hold row whose text is not a JSON number (a JSON string, a JSON boolean, or
      anything ``json.loads`` cannot parse) makes the downgrade refuse before any DDL runs, so the
      database is left exactly as it was - a valid 0002 database - on every engine, the alembic
      CLI's plain ``engine.begin()`` on SQLite included. A number outside the bounds the
      application itself enforces on write goes down unchecked; this migration only guards the
      JSON type, not the application's value range.
    """
    bind = op.get_bind()
    window_text = bind.execute(_READ_BACK, {"name": "dialling.window_s"}).scalar_one_or_none()
    window = _number(window_text, name="dialling.window_s") if window_text is not None else None
    hold_text = bind.execute(_READ_BACK, {"name": "dialling.hold_threshold_s"}).scalar_one_or_none()
    hold = _number(hold_text, name="dialling.hold_threshold_s") if hold_text is not None else None
    op.add_column("zone", sa.Column("dial_window_s", _REAL))
    op.add_column("zone", sa.Column("hold_threshold_s", _REAL))
    if window is not None:
        bind.execute(sa.text("UPDATE zone SET dial_window_s = :value WHERE id = 1"), {"value": window})
    if hold is not None:
        bind.execute(sa.text("UPDATE zone SET hold_threshold_s = :value WHERE id = 1"), {"value": hold})
    op.drop_table("preference")


def _number(text: object, *, name: str) -> float:
    """A stored JSON number back as the float the old column held.

    Refuses anything that is not a JSON number: ``float()`` alone accepts a JSON string
    (``'"0.7"'`` becomes ``0.7``) and a JSON boolean (``true`` becomes ``1.0``, since ``bool`` is
    an ``int`` subclass in Python), so both are rejected explicitly before reaching ``float()``. A
    number outside the bounds the application enforces on write is not checked here.
    """
    decoded = json.loads(str(text))
    if isinstance(decoded, bool) or not isinstance(decoded, int | float):
        message = f"preference row {name!r} is not a JSON number ({text!r}); fix or delete it before downgrading"
        raise ValueError(message)
    return float(decoded)
