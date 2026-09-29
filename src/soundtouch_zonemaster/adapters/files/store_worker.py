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

**A DAEMON thread, and a stop that is bounded.** A call can hang where nothing on the loop can
interrupt it: a PostgreSQL host that vanished leaves libpq in ``recv``, and no cancel reaches a
thread. So the close waits for the calls ahead of it for at most ``stop_bound_s``, says so, and
goes on - the stop has speakers waiting on it. The thread is a plain daemon ``threading.Thread``
draining a queue rather than a ``ThreadPoolExecutor``, because the interpreter joins every executor
worker at exit: one stuck call would hold the finished process until systemd killed it. A thread
left behind this way still runs the writes and the close queued behind the stuck call if that call
ends while the process lives. If the process exits first, the thread dies with all of them, and
the close that gave up says how many writes that loses. The writer lock is then given back by the
database, not by the close: the kernel drops SQLite's ``flock`` with the process, and PostgreSQL
drops its advisory lock when the server notices the session is gone - at once when the
connection's socket is closed with the process, and only after the server's own TCP keepalive
when its host cannot be reached at all.

**A write is queued when it is CALLED.** ``save_state``, ``save_channels`` and ``set_preference``
are plain methods that submit at once and return the pending answer, rather than coroutines that
would submit only when first awaited. That is what lets the reader - which may not wait, because
a pass can sit ten seconds on a station - ask for a save and go on, with the next save still
landing after it. A write never raises where it is asked, not even on a store that is not open:
the refusal is in its answer, like any other failure, so the reader cannot have one thrown into it.

**A write once asked for stays queued, and a failure nobody hears is said.** Each write is
shielded, so cancelling whoever awaits it does not pull it out of the queue: a stop that lands
while a join waits for its note would otherwise lose the note that puts a muted box back up on the
next start. It is written as long as the process lives long enough for the thread to reach it,
which a close that gives up cannot promise (above). A failure is raised where the write is awaited
and said by the caller, with what it was doing; only a write whose caller stopped waiting is said
here, because nobody else is left to.

**The close is the last word.** It is queued behind every write asked for before it, it too runs
when its awaiter is cancelled, and the thread ends with it. An open that fails, or is cancelled, is
followed by a close of whatever half of it happened, so a refused start holds no lock and leaves
no thread once that close has run. It is waited for within the same bound, for the same reason,
and a clean-up that gives up leaves the lock to the database as a close that gives up does.

The store's own log lines - a legacy import, the channel count - are written from the worker
thread. Each is written while the service awaits that very call, and one call is one line to the
stream, so no line of the service's own can land inside one.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import functools
import queue
import threading
from typing import TYPE_CHECKING

from ...application.errors import StoreError
from ...domain.logfn import ERROR_KIND
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

__all__ = ["STOP_BOUND_S", "THREAD_NAME", "StoreWorker"]

THREAD_NAME = "house-store"
"""What the worker thread is called, so a stack dump inside a hang names it."""

STOP_BOUND_S = 10.0
"""How long a close waits for the calls queued ahead of it, and for the thread to end.

Twice the store's own bounds (a PostgreSQL statement or connect timeout, a SQLite busy wait, all
five seconds), so a close gives up only on a call that has already outlived every limit the store
sets itself. The house's unit gives a stop sixty seconds before it kills, and this close is the
last thing the stop waits for. Ahead of it come at most three seconds for the stand-down's save
(``STAND_DOWN_SAVE_S``) and the dissolve, which waits up to eight seconds (the speaker HTTP
timeout) for each box it cannot reach, one box after another. Five unreachable boxes make
3 + 40 + 10 = 53 s, inside the sixty; six make 61 s, and the kill then lands in this close, after
every box has been told, costing only the writes the close says it lost."""

_THREAD_END_POLL_S = 0.005
"""How often a close looks whether the thread has ended: a join would be a wait on the loop."""

type _Job = Callable[[], None]


def _drain(jobs: queue.SimpleQueue[_Job | None]) -> None:
    """The thread's whole life: run each job in the order queued, and end at the ``None`` after the close."""
    while (job := jobs.get()) is not None:
        job()


def _answer[T](call: Callable[[], T], answer: concurrent.futures.Future[T]) -> None:
    """Run one call on the thread and put what it answered, or raised, where the loop waits for it."""
    if not answer.set_running_or_notify_cancel():
        # Its caller left while it was still queued: a read nobody waits for is not worth making.
        return
    try:
        result = call()
    except BaseException as exc:  # noqa: BLE001 - handed to the awaiting caller through its future, as an executor does
        answer.set_exception(exc)
    else:
        answer.set_result(result)


def _submit[T](jobs: queue.SimpleQueue[_Job | None], call: Callable[[], T]) -> asyncio.Future[T]:
    """Queue one call behind everything already queued, and hand the loop its answer to await."""
    answer: concurrent.futures.Future[T] = concurrent.futures.Future()
    jobs.put(functools.partial(_answer, call, answer))
    return asyncio.wrap_future(answer, loop=asyncio.get_running_loop())


def _refused[T](refusal: StoreError, _call: Callable[[], T]) -> asyncio.Future[T]:
    """The answer to a call on a store that is not open: the refusal in it, never raised at the call.

    The call is not made; it is taken only so the answer is typed as the call's would have been.
    """
    refused: asyncio.Future[T] = asyncio.get_running_loop().create_future()
    refused.set_exception(refusal)
    return refused


class StoreWorker:
    """One house store, reached from the event loop and run on one thread of its own.

    The thread exists from ``open`` to ``close`` and not a moment longer: building one costs
    nothing, which keeps constructing a service a thing a test can do without starting anything.
    ``stop_bound_s`` is how long a close, or the clean-up after a refused open, waits for the
    thread before it goes on without it.
    """

    def __init__(self, store: HouseStore, /, *, log: LogFn, stop_bound_s: float = STOP_BOUND_S) -> None:
        self._store = store
        self.where = store.where
        self.log = log
        self._stop_bound_s = stop_bound_s
        self._jobs: queue.SimpleQueue[_Job | None] | None = None
        self._thread: threading.Thread | None = None
        self._writes_out = 0
        """Writes asked for and not answered yet, the one running on the thread included: what a
        close that gives up has to say would be lost if the process exits before they run."""

    async def open(self, *, exclusive: bool) -> None:
        """Start the thread and open the store on it; a refusal leaves neither behind."""
        if self._jobs is not None:
            message = f"{self.where}: the house store is already open"
            raise StoreError(message)
        jobs: queue.SimpleQueue[_Job | None] = queue.SimpleQueue()
        thread = threading.Thread(target=_drain, args=(jobs,), name=THREAD_NAME, daemon=True)
        thread.start()
        try:
            await _submit(jobs, functools.partial(self._store.open, exclusive=exclusive))
        except BaseException:
            # Queued behind the open, so it runs once the open has finished however it finished:
            # a cancel lands on the await, not on a call already running on the thread. The
            # store's close does nothing when nothing was opened, and whatever it raises here is
            # dropped in favour of the refusal the caller is about to see.
            cleanup = self._shielded(jobs, self._store.close, what="the close after a refused open")
            jobs.put(None)
            with contextlib.suppress(Exception):
                await self._within_the_stop_bound(cleanup, thread, what="closing after a refused open")
            raise
        self._jobs, self._thread = jobs, thread

    async def close(self) -> None:
        """Close the store after every call queued before it, then end the thread - or say why not."""
        jobs, thread = self._jobs, self._thread
        self._jobs, self._thread = None, None
        if jobs is None or thread is None:
            return
        closing = self._shielded(jobs, self._store.close, what="close")
        # Nothing can be queued after the close, and the thread ends once it has run - also when
        # the wait below gives up or is cancelled and nobody is left to see it end.
        jobs.put(None)
        await self._within_the_stop_bound(closing, thread, what="the close")

    async def _within_the_stop_bound(
        self, answer: asyncio.Future[None], thread: threading.Thread, *, what: str
    ) -> None:
        """Wait for the answer, then for the thread to end, for at most the stop bound; say it if not.

        The thread's end is looked for rather than joined, because a join is a wait on the loop.
        Only this bound running out is said here; whatever the call itself raised is the caller's.
        """
        bound = asyncio.timeout(self._stop_bound_s)
        try:
            async with bound:
                await answer
                while thread.is_alive():
                    await asyncio.sleep(_THREAD_END_POLL_S)
        except TimeoutError:
            if not bound.expired():
                raise
            self.log(
                ERROR_KIND,
                f"{self.where}: {what} did not finish within {self._stop_bound_s:g} s, so the stop goes on "
                f"without it; {self._left_behind()} lost if the call stuck on the thread has not ended when the "
                "process exits",
            )

    def _left_behind(self) -> str:
        """What a stop that gives up leaves on the thread: the close, and every write not answered yet.

        The thread is a daemon, so the process exit ends it with whatever is still queued on it. The
        stop cannot keep those writes, and says how many there are so a lost save is not a surprise.
        """
        count = self._writes_out
        if count == 0:
            return "it is"
        return f"{count} {'write' if count == 1 else 'writes'} not written yet, and it, are"

    async def import_legacy(self, files: LegacyFiles) -> None:
        await self._run(functools.partial(self._store.import_legacy, files))

    async def load_state(self) -> ZoneState:
        return await self._run(self._store.load_state)

    def save_state(self, state: ZoneState) -> asyncio.Future[None]:
        return self._write(functools.partial(self._store.save_state, state), what="save_state")

    async def load_channels(self) -> ChannelList:
        return await self._run(self._store.load_channels)

    def save_channels(self, channels: ChannelList) -> asyncio.Future[None]:
        return self._write(functools.partial(self._store.save_channels, channels), what="save_channels")

    async def load_preferences(self) -> tuple[PreferenceRow, ...]:
        return await self._run(self._store.load_preferences)

    def set_preference(
        self, name: PreferenceName, value: PreferenceValue, *, source: PreferenceSource
    ) -> asyncio.Future[PreferenceRow | None]:
        return self._write(
            functools.partial(self._store.set_preference, name, value, source=source), what="set_preference"
        )

    async def is_on(self) -> bool:
        return await self._run(self._store.is_on)

    def switch(self, *, poll_s: float, ignored_file: Path | None) -> DbSwitch:
        """The switch, read through this worker like everything else, each poll on the thread."""
        return DbSwitch(self.is_on, where=self.where, log=self.log, poll_s=poll_s, ignored_file=ignored_file)

    def _run[T](self, call: Callable[[], T]) -> asyncio.Future[T]:
        """Queue one call on the thread now, and answer what it answers, or raise what it raises."""
        if self._jobs is None:
            return _refused(self._not_open(), call)
        return _submit(self._jobs, call)

    def _write[T](self, call: Callable[[], T], *, what: str) -> asyncio.Future[T]:
        """Queue a write now, and keep it queued whatever happens to whoever awaits it."""
        if self._jobs is None:
            return _refused(self._not_open(), call)
        self._writes_out += 1
        return self._shielded(self._jobs, call, what=what, counted=True)

    def _shielded[T](
        self, jobs: queue.SimpleQueue[_Job | None], call: Callable[[], T], *, what: str, counted: bool = False
    ) -> asyncio.Future[T]:
        """Queue a call its caller cannot take back, and say its failure if the caller stopped waiting.

        The caller awaits ``outer``; the call itself is ``inner``, which nothing on the loop cancels.
        A shield of its own rather than ``asyncio.shield``, because what that does with the failure
        of a call whose awaiter left depends on the Python: 3.12 marks it retrieved and drops it
        without a word, 3.13 and later hand it to the loop's exception handler, which writes a
        traceback beside the house's own log. Here it is said once, in the log, on every version.

        ``counted`` marks a write, which leaves ``_writes_out`` when the call is answered - by the
        call, not by its caller, who may have stopped waiting long before.
        """
        inner = _submit(jobs, call)
        outer: asyncio.Future[T] = inner.get_loop().create_future()
        if counted:
            inner.add_done_callback(self._a_write_answered)
        inner.add_done_callback(functools.partial(self._settle, what, outer))
        return outer

    def _a_write_answered[T](self, _inner: asyncio.Future[T]) -> None:
        self._writes_out -= 1

    def _settle[T](self, what: str, outer: asyncio.Future[T], inner: asyncio.Future[T]) -> None:
        """Hand the call's answer to its caller, or say its failure when nobody is waiting for it any more."""
        if outer.cancelled():
            failure = None if inner.cancelled() else inner.exception()
            if failure is not None:
                said = f"{type(failure).__name__}: {failure}"
                self.log(ERROR_KIND, f"{self.where}: {what} failed after its caller had stopped waiting ({said})")
            return
        if inner.cancelled():
            outer.cancel()
            return
        failure = inner.exception()
        if failure is not None:
            outer.set_exception(failure)
            return
        outer.set_result(inner.result())

    def _not_open(self) -> StoreError:
        return StoreError(f"{self.where}: the house store was used before open()")
