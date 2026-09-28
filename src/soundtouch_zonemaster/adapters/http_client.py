"""Every httpx client this program opens, built without a deadline of its own.

httpx runs on anyio, and anyio tells its own cancellation from anybody else's by the message it
cancels with. When a deadline anyio armed fires on the same turn of the event loop as a stop, the
task wakes with anyio's message: anyio takes the stop for its own timeout, swallows it, and raises a
timeout instead. A loop that retries timeouts then never ends, and neither does the stop waiting for
it - measured 2026-09-28, a stop waited seven hours on a station fetch that reconnected every thirty
seconds (``tests/test_stop_meets_a_deadline.py`` reproduces it).

So no client is given a timeout, and every caller bounds its call with ``asyncio.timeout``, which
counts a stop that lands with its deadline and lets it through. ``tests/test_no_anyio_deadlines.py``
fails if a client anywhere else in the package is built by hand, or a request is given a timeout.
"""

from __future__ import annotations

import httpx

__all__ = ["client_without_deadline"]


def client_without_deadline(
    *, headers: dict[str, str] | None = None, follow_redirects: bool = False
) -> httpx.AsyncClient:
    """An AsyncClient that enforces no deadline; the caller's ``asyncio.timeout`` is the deadline."""
    # The request-without-timeout rule exists so nothing can hang for ever. Nothing here can: every
    # caller holds this client inside asyncio.timeout, and the guard test above enforces that no
    # client is built anywhere else.
    return httpx.AsyncClient(timeout=None, headers=headers, follow_redirects=follow_redirects)  # noqa: S113  # nosec B113
