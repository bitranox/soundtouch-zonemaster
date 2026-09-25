"""The SIGINT path must leave a journal somebody can read (OPEN-WORK rank 26).

Measured in the flat on 2026-09-07 at 21:10:20: the service stopped correctly and dissolved the
zone, and asyncio wrote three full "Unhandled exception in client_connected_cb" tracebacks while it
did. Noise rather than a fault, standing at exactly the place a person scrolls to when looking for
a real one.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

import pytest

from soundtouch_zonemaster.adapters.soundtouch.connections import close_quietly, stop_pinging

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.asyncio


class _Said:
    """A connection as ``stop_pinging`` sees one: something to say a line about."""

    def __init__(self) -> None:
        self.label = "data"
        self.peer = "127.0.0.1"
        self.lines: list[str] = []
        self.log: Callable[[str, str], None] = lambda kind, text: self.lines.append(f"{kind}: {text}")


async def _pinging(fail_with: BaseException | None) -> asyncio.Task[None]:
    """A ping task that is running, and has either failed already or not."""

    async def loop() -> None:
        if fail_with is not None:
            raise fail_with
        while True:  # pragma: no cover - cancelled by the test, never left on its own
            await asyncio.sleep(3600)

    task = asyncio.create_task(loop())
    await asyncio.sleep(0)  # let it reach its first await, or its raise
    return task


async def test_a_ping_that_already_failed_does_not_escape_the_handler() -> None:
    """The measured one: a ping loop dies on drain, the connection is torn down, and the failure
    it died of re-raises at the await and replaces whatever the teardown was doing."""
    conn = _Said()
    ping = await _pinging(ConnectionResetError("Connection lost"))
    await stop_pinging(conn, ping)
    assert conn.lines == [], "a peer that went away is why the pings stopped, so it is not news"


async def test_a_ping_that_died_of_something_else_is_said_in_one_line() -> None:
    """Not everything is a peer going away. A defect that ends a ping loop is still a defect, and
    it must not be swallowed with the connection errors - said once, without a traceback."""
    conn = _Said()
    ping = await _pinging(ZeroDivisionError("a defect"))
    await stop_pinging(conn, ping)
    assert len(conn.lines) == 1, f"exactly one line, not none and not a traceback: {conn.lines}"
    assert "ZeroDivisionError" in conn.lines[0], conn.lines[0]


async def test_stopping_the_pings_does_not_swallow_the_caller_s_own_cancellation() -> None:
    """The other trap: cancelling a task that awaits another task is delivered THROUGH the child,
    so ``await ping`` under ``suppress(CancelledError)`` catches the CALLER's cancellation as well
    as the child's, and the caller lives on past its own cancel.

    The cancel has to arrive while the caller is INSIDE the wait, and that is arranged causally
    rather than by a clock: the ping task says so itself, from its own cancellation handler, which
    only runs once ``stop_pinging`` has cancelled it and is waiting. It then holds itself open on a
    shielded sleep, which is the margin the caller's cancel has to be delivered in.
    """
    conn = _Said()
    waiting = asyncio.Event()
    survived = False

    async def stubborn() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            waiting.set()
            await asyncio.shield(asyncio.sleep(0.5))
            raise

    async def caller() -> None:
        nonlocal survived
        ping = asyncio.create_task(stubborn())
        await asyncio.sleep(0)
        await stop_pinging(conn, ping)
        survived = True

    task = asyncio.create_task(caller())
    await waiting.wait()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.cancelled(), "the caller's cancel took effect"
    assert not survived, "and it did not run on past it"


async def test_the_ping_task_is_not_left_for_the_garbage_collector_to_complain_about() -> None:
    """Waiting with :func:`asyncio.wait` never re-raises, so the exception has to be RETRIEVED or
    Python reports it later as "Task exception was never retrieved" - the same noise, moved."""
    complaints: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: complaints.append(context))
    try:
        conn = _Said()
        ping = await _pinging(ConnectionResetError("Connection lost"))
        await stop_pinging(conn, ping)
        del ping
        for _ in range(3):
            await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous)
    assert complaints == [], f"asyncio had nothing to report: {complaints}"


class _StubbornWriter:
    """A writer whose close takes for ever: the margin a cancel has to be delivered in.

    It is what :class:`~soundtouch_zonemaster.adapters.soundtouch.connections.ClosingWriter` declares
    and no more, which is the point of that Protocol - the rule is about the two calls and their
    exceptions, so proving it needs no socket.
    """

    def __init__(self) -> None:
        self.closed = False
        self.waiting = asyncio.Event()

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waiting.set()
        await asyncio.sleep(3600)  # pragma: no cover - cancelled by the test, never left on its own


async def test_closing_a_connection_does_not_swallow_the_handler_s_own_cancellation() -> None:
    """The same trap as ``stop_pinging``'s, at the line every one of the three handlers ends on.

    ``close_quietly`` runs in a ``finally``, one statement before the handler returns, and nothing
    else is being waited on inside it - so a CancelledError arriving there can only be the
    handler's OWN. Suppressed, the handler runs on past its own cancel and reports itself
    completed. The socket has already been closed by the line above, so letting the cancel through
    costs nothing that was not already lost and says what actually happened.
    """
    writer = _StubbornWriter()
    survived = False

    async def handler() -> None:
        nonlocal survived
        await close_quietly(writer)
        survived = True

    task = asyncio.create_task(handler())
    await writer.waiting.wait()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert writer.closed, "the socket is closed either way; only the wait for it is given up"
    assert task.cancelled(), "the handler's own cancel took effect"
    assert not survived, "and it did not run on past it"
