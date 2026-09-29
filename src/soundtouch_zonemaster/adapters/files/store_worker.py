"""The house store off the event loop: one thread of its own, every call awaited from the loop.

The service's loop also times the zone. The clock on UDP 40005 and the frames on TCP 40003 are
answered from it, and a database call made there takes its time out of the audio. Measured
2026-09-29 with the real store: a state save on PostgreSQL took about 2 ms at the median and up to
10.75 ms at worst, and on SQLite - the house's backend, ``synchronous = FULL`` on purpose - two of
six runs showed a single save stalling for 20 to 24 ms, most of a frame period. So nothing the
service asks of the database runs on its loop: :class:`StoreWorker` hands each call to a thread
and the loop awaits the answer.

**One thread, never a pool.** A single worker runs its jobs in the order they were queued, which is
what keeps two saves in the order they were asked for; the synchronous store had that for free and
a pool would silently lose it. It also keeps every database connection on the thread that made it,
because open, each call and close all run there. Nothing else would: SQLAlchemy turns pysqlite's
``check_same_thread`` off for a file database, so its pool hands a connection to whichever thread
asks, and PostgreSQL's writer lock lives on one session held from the open to the close. With
one thread, no connection and no session is ever touched by two.

**A write is queued when it is CALLED.** ``save_state``, ``save_channels`` and ``set_preference``
are plain methods that submit at once and return the pending answer, rather than coroutines that
would submit only when first awaited. That is what lets the reader - which may not wait, because
a pass can sit ten seconds on a station - ask for a save and go on, with the next save still
landing after it.

**A write once asked for is written.** Each one is shielded, so cancelling whoever awaits it does
not pull it out of the queue: a stop that lands while a join waits for its note would otherwise
lose the note that puts a muted box back up on the next start.

**The close is the last word.** It is queued behind every write asked for before it, it too runs
when its awaiter is cancelled, and the thread ends with it. An open that fails, or is cancelled, is
followed by a close of whatever half of it happened, so a refused start holds no lock and leaves
no thread.

The store's own log lines - a legacy import, the channel count - are written from the worker
thread. Each is written while the service awaits that very call, and one call is one line to the
stream, so no line of the service's own can land inside one.
"""

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from ...application.errors import StoreError
from .house_switch import DbSwitch

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ...application.options import LegacyFiles
    from ...application.ports import HouseStore
    from ...domain.channellist import ChannelList
    from ...domain.logfn import LogFn
    from ...domain.preferences import PreferenceName, PreferenceRow, PreferenceSource, PreferenceValue
    from ...domain.state import ZoneState

__all__ = ["THREAD_NAME", "StoreWorker"]

THREAD_NAME = "house-store"
"""What the worker thread is called, so a stack dump inside a hang names it."""


class StoreWorker:
    """One house store, reached from the event loop and run on one thread of its own.

    The thread exists from ``open`` to ``close`` and not a moment longer: building one costs
    nothing, which keeps constructing a service a thing a test can do without starting anything.
    """

    def __init__(self, store: HouseStore, /, *, log: LogFn) -> None:
        self._store = store
        self.where = store.where
        self.log = log
        self._thread: ThreadPoolExecutor | None = None

    async def open(self, *, exclusive: bool) -> None:
        """Start the thread and open the store on it; a refusal leaves neither behind."""
        if self._thread is not None:
            message = f"{self.where}: the house store is already open"
            raise StoreError(message)
        thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix=THREAD_NAME)
        try:
            await asyncio.get_running_loop().run_in_executor(
                thread, functools.partial(self._store.open, exclusive=exclusive)
            )
        except BaseException:
            # Queued behind the open, so it runs once the open has finished however it finished:
            # a cancel lands on the await, not on a call already running on the thread. The
            # store's close does nothing when nothing was opened, and whatever it raises here is
            # dropped in favour of the refusal the caller is about to see.
            thread.submit(self._store.close)
            thread.shutdown(wait=False)
            raise
        self._thread = thread

    async def close(self) -> None:
        """Close the store after every call queued before it, then end the thread."""
        thread, self._thread = self._thread, None
        if thread is None:
            return
        closing = asyncio.get_running_loop().run_in_executor(thread, self._store.close)
        # Nothing can be queued after the close, and the thread ends once the queue is done - also
        # when the await below is cancelled and nobody is left to join it.
        thread.shutdown(wait=False)
        await asyncio.shield(closing)
        # Joined off the loop too: the thread is on its way out, but a join is still a wait.
        await asyncio.to_thread(thread.shutdown)

    async def import_legacy(self, files: LegacyFiles) -> None:
        await self._run(functools.partial(self._store.import_legacy, files))

    async def load_state(self) -> ZoneState:
        return await self._run(self._store.load_state)

    def save_state(self, state: ZoneState) -> asyncio.Future[None]:
        return self._write(functools.partial(self._store.save_state, state))

    async def load_channels(self) -> ChannelList:
        return await self._run(self._store.load_channels)

    def save_channels(self, channels: ChannelList) -> asyncio.Future[None]:
        return self._write(functools.partial(self._store.save_channels, channels))

    async def load_preferences(self) -> tuple[PreferenceRow, ...]:
        return await self._run(self._store.load_preferences)

    def set_preference(
        self, name: PreferenceName, value: PreferenceValue, *, source: PreferenceSource
    ) -> asyncio.Future[PreferenceRow | None]:
        return self._write(functools.partial(self._store.set_preference, name, value, source=source))

    async def is_on(self) -> bool:
        return await self._run(self._store.is_on)

    def switch(self, *, poll_s: float, ignored_file: Path | None) -> DbSwitch:
        """The switch, read through this worker like everything else, each poll on the thread."""
        return DbSwitch(self.is_on, where=self.where, log=self.log, poll_s=poll_s, ignored_file=ignored_file)

    def _run[T](self, call: Callable[[], T]) -> asyncio.Future[T]:
        """Queue one call on the thread now, and answer what it answers, or raise what it raises."""
        if self._thread is None:
            message = f"{self.where}: the house store was used before open()"
            raise StoreError(message)
        return asyncio.get_running_loop().run_in_executor(self._thread, call)

    def _write[T](self, call: Callable[[], T]) -> asyncio.Future[T]:
        """Queue a write now, and keep it queued whatever happens to whoever awaits it."""
        return asyncio.shield(self._run(call))
