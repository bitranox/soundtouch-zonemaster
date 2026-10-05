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

import importlib

import httpcore
import httpx

__all__ = ["client_without_deadline"]

# Everything below runs when this module is imported, which is before the event loop starts, and
# that is the point: left to httpx and anyio, each happens on the loop the first time a client is
# built or used, and the loop also answers the zone's clock and frames. Together they stall it for
# tens of milliseconds on the run's first request (tests/test_http_client_off_the_loop.py holds it
# at no import on the loop at all).
#
# httpx imports its transport, httpcore (and h11 with it), only when a client is first built;
# naming the module here is what moves that import.
_TRANSPORT = httpcore
# anyio imports its asyncio backend only when the first request asks for one. The module is
# anyio's own and private, so it is imported by name rather than bound: should anyio rename it, the
# import fails at start-up, loudly, rather than the cost quietly returning to the loop.
importlib.import_module("anyio._backends._asyncio")
_SSL_CONTEXT = httpx.create_ssl_context()
"""The one SSL context every client shares, built with httpx's own defaults (certifi, or the
SSL_CERT_FILE / SSL_CERT_DIR the environment names). A client given none builds its own, a few
milliseconds on the loop for every speaker call; a context is made to be shared."""


def client_without_deadline(
    *, headers: dict[str, str] | None = None, follow_redirects: bool = False
) -> httpx.AsyncClient:
    """An AsyncClient that enforces no deadline; the caller's ``asyncio.timeout`` is the deadline."""
    # The request-without-timeout rule exists so nothing can hang for ever. Nothing here can: every
    # caller holds this client inside asyncio.timeout, and the guard test above enforces that no
    # client is built anywhere else.
    return httpx.AsyncClient(  # nosec B113
        timeout=None,  # noqa: S113
        headers=headers,
        follow_redirects=follow_redirects,
        verify=_SSL_CONTEXT,
    )
