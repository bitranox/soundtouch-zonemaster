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
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING

import pytest
from slow_store import SlowStore

from soundtouch_zonemaster.adapters.files.house_store import SqlHouseStore
from soundtouch_zonemaster.adapters.files.store_worker import StoreWorker
from soundtouch_zonemaster.application.errors import StoreBusyError, StoreError
from soundtouch_zonemaster.domain.channellist import ChannelList
from soundtouch_zonemaster.domain.logfn import ERROR_KIND
from soundtouch_zonemaster.domain.state import ZoneState

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

STOP_BOUND_S = 0.3
"""The bound the tests below give a stop: short, so a test of a call that never ends is quick."""


def _quiet(_kind: str, _text: str) -> None:
    return None


def _worker_over(
    database: str, *, stall_s: float = 0.0, stop_bound_s: float | None = None, lines: list[str] | None = None
) -> tuple[StoreWorker, SlowStore]:
    slow = SlowStore(SqlHouseStore(database, log=_quiet), stall_s=stall_s)
    heard = lines if lines is not None else []

    def log(kind: str, text: str) -> None:
        heard.append(f"{kind}: {text}")

    if stop_bound_s is None:
        return StoreWorker(slow, log=log), slow
    return StoreWorker(slow, log=log, stop_bound_s=stop_bound_s), slow


@pytest.fixture
def gate() -> Iterator[threading.Event]:
    """A gate a stuck call waits on, always opened at the end so no thread waits for ever."""
    event = threading.Event()
    yield event
    event.set()


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


class _SecondSaveReleasesTheFirst(SlowStore):
    """The first save waits until the second has been WRITTEN, or half a second, whichever is first.

    One thread can never run the second while the first waits, so there the first gives up waiting
    and is written first. A second thread would run the second beside it, write it, and let the
    first land on top of it - so the order is decided by the worker, never by a sleep that a busy
    machine could stretch either way.
    """

    def __init__(self, real: SqlHouseStore) -> None:
        super().__init__(real)
        self.second_written = threading.Event()

    def save_state(self, state: ZoneState) -> None:
        if state.channel == "1":
            self.second_written.wait(0.5)
        super().save_state(state)
        if state.channel == "2":
            self.second_written.set()


async def test_two_saves_are_written_in_the_order_they_were_asked_for(house_database: str) -> None:
    """A worker that could run the second save beside the first would leave the house on the older state."""
    database = house_database
    slow = _SecondSaveReleasesTheFirst(SqlHouseStore(database, log=_quiet))
    worker = StoreWorker(slow, log=_quiet)
    await worker.open(exclusive=True)

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
    # A write ANSWERS the refusal rather than raising it where it is asked: the reader asks for a
    # save and goes on, and must never have an exception thrown into it for asking.
    refused = worker.save_state(ZoneState())
    with pytest.raises(StoreError, match="before open"):
        await refused

    await worker.open(exclusive=True)
    await worker.close()

    with pytest.raises(StoreError, match="before open"):
        await worker.load_preferences()
    refused_too = worker.save_channels(ChannelList())
    with pytest.raises(StoreError, match="before open"):
        await refused_too


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


async def test_a_failed_write_whose_caller_stopped_waiting_is_said(house_database: str, gate: threading.Event) -> None:
    """Nobody is left to raise a failure into once the caller was cancelled, so the worker says it.

    Only then: a write that is still awaited raises where it is awaited, and the caller says it
    with what it was doing, so a generic line here as well would say one failure twice.
    """
    lines: list[str] = []
    worker, slow = _worker_over(house_database, lines=lines)
    await worker.open(exclusive=True)
    slow.gates["save_channels"] = gate
    slow.fails.add("save_channels")

    async def save() -> None:
        await worker.save_channels(ChannelList())

    saving = asyncio.create_task(save())
    await asyncio.sleep(0.05)
    saving.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await saving
    assert not lines, "the control: nothing is said while the write is still on its way"
    gate.set()
    await worker.close()

    said = [line for line in lines if line.startswith(f"{ERROR_KIND}: ") and "save_channels" in line]
    assert len(said) == 1, lines


async def test_an_awaited_write_that_fails_is_raised_and_not_said(house_database: str) -> None:
    """The other half: the caller hears the failure, so the worker says nothing of its own."""
    lines: list[str] = []
    worker, slow = _worker_over(house_database, lines=lines)
    await worker.open(exclusive=True)
    slow.fails.add("save_channels")

    with pytest.raises(StoreError, match="refused save_channels"):
        await worker.save_channels(ChannelList())
    await worker.close()

    assert lines == []


async def test_a_close_behind_a_call_that_never_ends_gives_up_within_its_bound(
    house_database: str, gate: threading.Event
) -> None:
    """A database that stopped answering must not hold the stop: the speakers are waiting on it.

    The call stuck on the thread cannot be interrupted from here, so the close says so and goes on
    without it. The thread is left to finish on its own, and once the call ends the close queued
    behind it still gives the writer lock back.
    """
    lines: list[str] = []
    worker, slow = _worker_over(house_database, stop_bound_s=STOP_BOUND_S, lines=lines)
    await worker.open(exclusive=True)
    thread = slow.calls[0].thread
    slow.gates["save_state"] = gate
    worker.save_state(ZoneState(channel="4"))

    started = time.monotonic()
    await worker.close()
    took = time.monotonic() - started

    assert took < STOP_BOUND_S + 1.0, f"the close waited {took:.1f} s for a call that never ends"
    assert any(line.startswith(f"{ERROR_KIND}: ") and "did not finish" in line for line in lines), lines
    assert thread.is_alive(), "the control: the stuck call really was still running when the close gave up"
    gate.set()
    assert await _ended(thread), "the thread ends once the stuck call does"
    assert [call.name for call in slow.calls] == ["open", "save_state", "close"]
    assert _state_in(house_database).channel == "4", "the write that was stuck still landed"


async def test_a_refused_open_whose_cleanup_never_ends_gives_up_within_its_bound(
    house_database: str, gate: threading.Event
) -> None:
    """A refused start must be refused promptly, even when closing the half-open store hangs."""
    database = house_database
    holder = SqlHouseStore(database, log=_quiet)
    holder.open(exclusive=True)
    try:
        lines: list[str] = []
        worker, slow = _worker_over(database, stop_bound_s=STOP_BOUND_S, lines=lines)
        slow.gates["close"] = gate
        started = time.monotonic()
        with pytest.raises(StoreBusyError):
            await worker.open(exclusive=True)
        took = time.monotonic() - started
        assert took < STOP_BOUND_S + 1.0, f"the refusal waited {took:.1f} s for a close that never ends"
        assert any(line.startswith(f"{ERROR_KIND}: ") and "did not finish" in line for line in lines), lines
        gate.set()
        assert await _ended(slow.calls[0].thread), "the thread ends once the close does"
    finally:
        holder.close()


async def test_an_open_cancelled_while_it_hangs_gives_up_and_holds_no_lock_once_it_ends(
    house_database: str, gate: threading.Event
) -> None:
    """A stop during a start whose database will not answer: the stop goes on at once, and the open,
    whenever it does finish, is closed again behind it - the house is never left locked."""
    database = house_database
    worker, slow = _worker_over(database, stop_bound_s=STOP_BOUND_S)
    slow.gates["open"] = gate
    opening = asyncio.create_task(worker.open(exclusive=True))
    await asyncio.sleep(0.05)
    started = time.monotonic()
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening
    took = time.monotonic() - started

    assert took < STOP_BOUND_S + 1.0, f"the cancelled open waited {took:.1f} s"
    gate.set()
    assert await _ended(slow.calls[0].thread), "the thread ends once the open and its close have run"
    assert [call.name for call in slow.calls] == ["open", "close"]
    again = SqlHouseStore(database, log=_quiet)
    again.open(exclusive=True)
    again.close()


_STUCK_PROGRAM = """
import asyncio
import threading

from soundtouch_zonemaster.adapters.files.store_worker import StoreWorker
from soundtouch_zonemaster.domain.state import ZoneState


class Stuck:
    where = "a store whose save never ends"

    def open(self, *, exclusive, create=True):
        return None

    def close(self):
        return None

    def save_state(self, state):
        threading.Event().wait()


async def main():
    worker = StoreWorker(Stuck(), log=lambda kind, text: print(kind, text, flush=True))
    await worker.open(exclusive=True)
    worker.save_state(ZoneState())
    try:
        async with asyncio.timeout(0.5):
            await worker.close()
    except TimeoutError:
        pass
    print("the run is over", flush=True)


asyncio.run(main())
"""


def test_a_call_that_never_ends_does_not_hold_up_the_process_exit(tmp_path: Path) -> None:
    """The last word on a stop: once the run is over the process ends, whatever the thread is doing.

    Held up, systemd waits out its stop timeout and then kills the process. A thread the
    interpreter joins at exit - every worker of a ``ThreadPoolExecutor`` is one - turns a stuck
    database call into exactly that, so this runs a real interpreter to its real end rather than
    asking the thread how it was built.
    """
    program = tmp_path / "stuck.py"
    program.write_text(_STUCK_PROGRAM, encoding="utf-8")
    started = time.monotonic()
    try:
        finished = subprocess.run(  # noqa: S603 - argv list: this interpreter and a file this test wrote
            [sys.executable, str(program)], capture_output=True, text=True, check=False, timeout=15
        )
    except subprocess.TimeoutExpired:
        pytest.fail("the process never exited: a thread stuck in a database call held it up")
    took = time.monotonic() - started

    assert "the run is over" in finished.stdout, finished.stderr
    assert finished.returncode == 0, finished.stderr
    assert took < 10, f"the process took {took:.1f} s to exit"
