"""The house store off the event loop: one thread, in the order asked, and nothing lost at the close.

The service hands every database call to a :class:`StoreWorker`, which runs it on one thread of
its own so the loop that times the zone never waits for a disk or a network. What that must not
cost is anything the synchronous store gave for free: two saves written in the order they were
asked for, a save that was asked for being written even when nobody waits for it or the one who
waited was cancelled, and a close that comes after all of it and leaves no thread behind.

The store underneath is the real one, on SQLite and on PostgreSQL when the checkout names a
server (``house_database``), wrapped in a :class:`SlowStore` so a call can be made slow enough for
the next one to queue behind it. PostgreSQL is the backend where the thread matters most: the
writer lock lives on one server session, taken by the open and given back by the close, and both
must happen on the worker for the lock to be released at all. What was written is read back
afterwards through a second store of its own, the way a restart would read it.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

import pytest
from slow_store import SlowStore

from soundtouch_zonemaster.adapters.files.house_store import SqlHouseStore
from soundtouch_zonemaster.adapters.files.store_worker import StoreWorker
from soundtouch_zonemaster.application.errors import StoreBusyError, StoreError
from soundtouch_zonemaster.domain.channellist import ChannelList
from soundtouch_zonemaster.domain.state import ZoneState

if TYPE_CHECKING:
    import threading


def _quiet(_kind: str, _text: str) -> None:
    return None


def _worker_over(database: str, *, stall_s: float = 0.0) -> tuple[StoreWorker, SlowStore]:
    slow = SlowStore(SqlHouseStore(database, log=_quiet), stall_s=stall_s)
    return StoreWorker(slow, log=_quiet), slow


def _state_in(database: str) -> ZoneState:
    """What a restart would read, through a store of its own."""
    reader = SqlHouseStore(database, log=_quiet)
    reader.open(exclusive=False)
    try:
        return reader.load_state()
    finally:
        reader.close()


async def _ended(thread: threading.Thread, *, timeout: float = 5.0) -> bool:
    """Whether the thread has ended, waited for on a clock the thread does not control."""
    deadline = time.monotonic() + timeout
    while thread.is_alive() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    return not thread.is_alive()


async def test_two_saves_are_written_in_the_order_they_were_asked_for(house_database: str) -> None:
    """The first save is the slow one, so a worker that could run the second beside it would
    finish the second first and leave the house on the older state."""
    database = house_database
    worker, slow = _worker_over(database)
    await worker.open(exclusive=True)
    slow.save_stalls = [0.3, 0.0]

    first = worker.save_state(ZoneState(channel="1"))
    second = worker.save_state(ZoneState(channel="2"))
    await asyncio.gather(first, second)
    await worker.close()

    assert [state.channel for state in slow.saved] == ["1", "2"]
    assert _state_in(database).channel == "2", "the house is on the state asked for LAST"


async def test_a_save_nobody_waits_for_is_written_before_the_close(house_database: str) -> None:
    """The reader queues a save and goes on, because it may not wait; the close comes after it."""
    database = house_database
    worker, slow = _worker_over(database)
    await worker.open(exclusive=True)
    slow.stall_s = 0.2

    worker.save_state(ZoneState(channel="7"))
    await worker.close()

    assert [call.name for call in slow.calls] == ["open", "save_state", "close"]
    assert _state_in(database).channel == "7"


async def test_a_save_whose_caller_was_cancelled_is_written_all_the_same(house_database: str) -> None:
    """A stop that lands while a caller waits for its save must not take the save back.

    The save queues behind a slow read, so it has not started when its caller is cancelled - the
    one moment a cancel could still remove it from the queue.
    """
    database = house_database
    worker, slow = _worker_over(database)
    await worker.open(exclusive=True)
    slow.stall_s = 0.3
    reading = asyncio.ensure_future(worker.load_state())

    async def save() -> None:
        await worker.save_state(ZoneState(channel="5"))

    saving = asyncio.create_task(save())
    await asyncio.sleep(0.05)
    assert not slow.named("save_state"), "the control: the save is still queued when the cancel lands"
    saving.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await saving
    await reading
    await worker.close()

    assert _state_in(database).channel == "5"


async def test_a_close_whose_caller_was_cancelled_still_closes_and_ends_the_thread(house_database: str) -> None:
    """The close is the last word even when a second stop interrupts it: the writer lock must be
    given back, or the next start would find the house held by a process that has already gone."""
    database = house_database
    worker, slow = _worker_over(database)
    await worker.open(exclusive=True)
    thread = slow.calls[0].thread
    slow.stall_s = 0.3
    worker.save_state(ZoneState(channel="3"))

    closing = asyncio.create_task(worker.close())
    await asyncio.sleep(0.05)
    closing.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await closing

    assert await _ended(thread), "the database thread outlived the close"
    assert [call.name for call in slow.calls] == ["open", "save_state", "close"]
    again = SqlHouseStore(database, log=_quiet)
    again.open(exclusive=True)
    again.close()
    assert _state_in(database).channel == "3"


async def test_a_store_that_is_not_open_refuses_every_call(house_database: str) -> None:
    """Before the open and after the close alike, as the store underneath does: a call that
    quietly went nowhere would let a test pass while nothing it meant to exercise was written."""
    worker, _slow = _worker_over(house_database)

    with pytest.raises(StoreError, match="before open"):
        await worker.load_state()
    with pytest.raises(StoreError, match="before open"):
        worker.save_state(ZoneState())

    await worker.open(exclusive=True)
    await worker.close()

    with pytest.raises(StoreError, match="before open"):
        await worker.load_preferences()
    with pytest.raises(StoreError, match="before open"):
        worker.save_channels(ChannelList())


async def test_an_open_that_is_refused_leaves_no_thread_and_no_store_behind(house_database: str) -> None:
    """A database another service holds refuses the start; nothing may be left running after it."""
    database = house_database
    holder = SqlHouseStore(database, log=_quiet)
    holder.open(exclusive=True)
    try:
        worker, slow = _worker_over(database)
        with pytest.raises(StoreBusyError):
            await worker.open(exclusive=True)
        thread = slow.calls[0].thread
        assert await _ended(thread), "the database thread outlived a refused open"
        assert [call.name for call in slow.calls] == ["open", "close"], "the half-open store was closed"
        with pytest.raises(StoreError, match="before open"):
            await worker.load_state()
    finally:
        holder.close()
