"""The preferences, as rows: one per preference somebody set, and nothing for one nobody did.

Every write is an UPSERT on the name. Delete-then-insert under READ COMMITTED lets two writers
collide on PostgreSQL (OPEN-WORK rank 204, the switch), and a calibration and ``prefs set`` can
arrive at the same moment. The dialect's own ``INSERT ... ON CONFLICT DO UPDATE`` is one statement
on both backends this program runs on.

Nothing is judged here: a row goes in as the caller checked it and comes out exactly as held, and
``domain/preferences.stored`` is the one place a stored row is decoded and found usable or not.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select
from sqlalchemy.dialects import postgresql, sqlite

from ...domain.preferences import PreferenceRow
from .house_schema import PREFERENCE

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Row

    from ...domain.preferences import PreferenceName, PreferenceSource, PreferenceValue

__all__ = ["delete_preference", "read_preferences", "write_preference"]

_COLUMNS = (PREFERENCE.c.name, PREFERENCE.c.value, PREFERENCE.c.source, PREFERENCE.c.changed_at)


def read_preferences(connection: Connection) -> tuple[PreferenceRow, ...]:
    """Every stored row, by name, exactly as held."""
    return tuple(_as_row(found) for found in connection.execute(select(*_COLUMNS).order_by(PREFERENCE.c.name)))


def write_preference(
    connection: Connection, name: PreferenceName, value: PreferenceValue, *, source: PreferenceSource, changed_at: str
) -> PreferenceRow | None:
    """Set one preference and hand back the row it replaced. The caller holds the transaction and checked the value.

    ``json.dumps`` writes a tuple as a JSON array, which is the shape the rule reads back.

    The row handed back comes from a SELECT run before the UPSERT, not from the UPSERT itself: the
    write is one atomic statement on both backends, but the read that reports what it replaced is
    not part of it. On PostgreSQL under READ COMMITTED, two callers writing the same name at once
    can each read the same "before" row and each report having replaced it, even though only one
    of them did - a caller that trusts the returned row to mean "this is what my write replaced"
    is trusting something the database does not guarantee here.
    """
    before = _row(connection, name)
    row = {"name": name.value, "value": json.dumps(value), "source": source.value, "changed_at": changed_at}
    replaced = {"value": row["value"], "source": row["source"], "changed_at": row["changed_at"]}
    if connection.dialect.name == "postgresql":
        connection.execute(
            postgresql.insert(PREFERENCE).values(**row).on_conflict_do_update(index_elements=["name"], set_=replaced)
        )
    else:
        connection.execute(
            sqlite.insert(PREFERENCE).values(**row).on_conflict_do_update(index_elements=["name"], set_=replaced)
        )
    return before


def delete_preference(connection: Connection, name: PreferenceName) -> PreferenceRow | None:
    """Unset one preference and hand back the row it held, or nothing when none was set."""
    before = _row(connection, name)
    connection.execute(delete(PREFERENCE).where(PREFERENCE.c.name == name.value))
    return before


def _row(connection: Connection, name: PreferenceName) -> PreferenceRow | None:
    found = connection.execute(select(*_COLUMNS).where(PREFERENCE.c.name == name.value)).one_or_none()
    return None if found is None else _as_row(found)


def _as_row(found: Row[Any, Any, Any, Any]) -> PreferenceRow:
    name, value, source, changed_at = found
    return PreferenceRow(name=str(name), text=str(value), source=str(source), changed_at=str(changed_at))
