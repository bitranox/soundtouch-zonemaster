"""Alembic's entry point: runs the house migrations on the connection the store hands it.

The store opens the transaction (``HouseDatabase.writing``, which is ``BEGIN IMMEDIATE`` on
SQLite) and holds the writer lock before it calls Alembic, so this file only says which
connection to use. ``render_as_batch`` is what lets a later revision ALTER a SQLite table, which
SQLite can only do by copying it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from alembic import context

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

connection = cast("Connection", context.config.attributes["connection"])
context.configure(connection=connection, render_as_batch=True, transactional_ddl=True)
with context.begin_transaction():
    context.run_migrations()
