"""A person's switch write held open on its own PostgreSQL connection, and the wait for a writer behind it.

The race a switch writer must survive is a person's ``switch off`` committing between the writer's
read of the switch and its own write: the writer then reports a change it did not make, and a
deploy that believes it turns the house back on over the person's word. Nothing in the code under
test can be paused between that read and that write, so the tests hold the PERSON instead. Their
write sits uncommitted on a second, real connection; the writer under test starts in a thread or a
subprocess; and the person commits only once PostgreSQL reports a backend waiting on a lock. A
writer that reads before it locks has read the old word by then, and one that locks first reads
the person's.

PostgreSQL only: a SQLite writer takes the database's one write lock (``BEGIN IMMEDIATE``) before
it reads anything, so no read can come between, and SQLite has no view of a waiting writer to
synchronise on.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from sqlalchemy.engine import Connection

__all__ = ["InThread", "a_person_writing", "wait_until_a_writer_waits"]

_CONNECT_TIMEOUT_S = 5
_WAIT_S = 10.0
"""How long a writer may take to reach the lock it waits on; far above the milliseconds it needs."""

_WAITING = text(
    "SELECT count(*) FROM pg_stat_activity "
    "WHERE datname = current_database() AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
)


@contextmanager
def a_person_writing(url: str) -> Generator[Connection]:
    """A connection whose transaction the test writes into and commits when it chooses.

    Rolled back on the way out if the test never committed, so a failed assertion cannot leave a
    writer blocked behind it for ever.
    """
    engine = create_engine(url, connect_args={"connect_timeout": _CONNECT_TIMEOUT_S})
    try:
        with engine.connect() as connection:
            try:
                yield connection
            finally:
                if connection.in_transaction():
                    connection.rollback()
    finally:
        engine.dispose()


def wait_until_a_writer_waits(url: str) -> None:
    """Return once another backend in this database waits on a lock; fail by name after ``_WAIT_S``.

    ``pg_stat_activity`` is read on an autocommit connection, because inside a transaction it
    answers from the snapshot its first read took and would never see the writer arrive.
    """
    engine = create_engine(url, connect_args={"connect_timeout": _CONNECT_TIMEOUT_S}, isolation_level="AUTOCOMMIT")
    deadline = time.monotonic() + _WAIT_S
    try:
        with engine.connect() as connection:
            while connection.scalar(_WAITING) == 0:
                if time.monotonic() > deadline:
                    message = f"no writer came to wait behind the held write within {_WAIT_S:g} s"
                    raise AssertionError(message)
                time.sleep(0.02)
    finally:
        engine.dispose()


class InThread[T]:
    """Run one call on its own thread, and hand back what it returned or raise what it raised."""

    def __init__(self, call: Callable[[], T]) -> None:
        self._call = call
        self._result: list[T] = []
        self._error: list[BaseException] = []
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self._result.append(self._call())
        except BaseException as exc:  # noqa: BLE001 - handed back to the test thread by result()
            self._error.append(exc)

    def result(self) -> T:
        self._thread.join(_WAIT_S)
        if self._thread.is_alive():
            message = f"the writer did not finish within {_WAIT_S:g} s of the person's commit"
            raise AssertionError(message)
        if self._error:
            raise self._error[0]
        return self._result[0]
