"""A port the protocol fixes, held by somebody else at the moment the zone starts.

Measured in the flat on 2026-09-07 22:07: switching the service on killed it three times with
``[Errno 98] address already in use`` on 40002 before the fourth start bound. The host hands
out ephemeral ports from 32768 to 60999 and the protocol's 40002, 40003 and 40005 all sit inside
that range, so any outbound connection can be holding one when the master binds.

Nothing here is monkeypatched. The port is taken by a real socket, which is what took it in the
flat, and the retry parameters are constructor arguments rather than module state so a test can
ask for a bound it is willing to wait for.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import socket

import pytest

from soundtouch_zonemaster.adapters.soundtouch.zone_master import ZoneMaster
from soundtouch_zonemaster.application.errors import PortsBusyError
from soundtouch_zonemaster.application.zone_service.constants import PORTS_BUSY_RETRY_S, wait_for_the_next_pass_s

pytestmark = pytest.mark.asyncio

BIND = "127.0.0.1"
TRANSPORT_PORT = 40002


def _hold(port: int) -> socket.socket:
    """Take a port the way an outbound connection takes one: a real socket, really bound.

    SO_REUSEADDR because the port is the protocol's own and the suite has just used it: a slave's
    connection to 40002 leaves a TIME-WAIT socket whose LOCAL port is 40002 for about a minute,
    and a plain bind over that raises EADDRINUSE (measured 2026-09-08). That failure lands in
    setup, reads as "address already in use" from whatever was last changed, and is not the
    condition this simulates. Holding a LIVE listener still refuses the master's own bind,
    which asyncio also makes with SO_REUSEADDR - measured the same way.
    """
    held = socket.socket()
    held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    held.bind((BIND, port))
    held.listen(1)
    return held


def _answers_on(port: int) -> bool:
    probe = socket.socket()
    probe.settimeout(0.5)
    try:
        probe.connect((BIND, port))
    except OSError:
        return False
    finally:
        probe.close()
    return True


async def test_start_waits_for_a_port_somebody_else_holds_and_takes_it_when_it_frees() -> None:
    """The zone is late rather than lost.

    A caller with no loop of its own - the prototype in ``__main__`` - gets its whole retry from
    here, so this is the behaviour that keeps a hand-started master from dying on a port that is
    about to free. The service asks for far fewer attempts and retries on its own pass instead.
    """
    held = _hold(TRANSPORT_PORT)
    logs: list[str] = []
    master = ZoneMaster(
        bind_ip=BIND,
        device_id="5EB0CE000001",
        log=lambda kind, text: logs.append(f"{kind}: {text}"),
        bind_attempts=40,
        bind_retry_wait_s=0.05,
    )
    starting = asyncio.create_task(master.start())
    try:
        await asyncio.sleep(0.2)
        assert not starting.done(), "start() gave up while the port was still held"
        held.close()
        await asyncio.wait_for(starting, timeout=5.0)
        assert _answers_on(TRANSPORT_PORT), "the master did not take the port once it was free"
        # Nothing is logged about the wait here on purpose: the service retries once a second, so a
        # line per attempt would be a stream. The reporting is the caller's and is asserted there.
        assert logs == [f"master: listening on {BIND}: udp/40005 tcp/40002 tcp/40003 tcp/8090 as 5EB0CE000001"]
    finally:
        with contextlib.suppress(OSError):
            held.close()
        if starting.done() and not starting.cancelled() and starting.exception() is None:
            await master.stop()
        else:
            starting.cancel()
            with contextlib.suppress(asyncio.CancelledError, OSError):
                await starting
            await master.stop()


async def test_start_gives_up_after_its_bound_and_leaves_no_port_of_its_own_bound() -> None:
    """A bound that is reached must not leave half a master listening.

    Whatever it managed to bind before the busy one has to go, or the next attempt fails on the
    ports this master is holding itself and the log names the wrong port.
    """
    held = _hold(TRANSPORT_PORT)
    logs: list[str] = []
    master = ZoneMaster(
        bind_ip=BIND,
        device_id="5EB0CE000001",
        log=lambda kind, text: logs.append(f"{kind}: {text}"),
        bind_attempts=2,
        bind_retry_wait_s=0.01,
    )
    try:
        with pytest.raises(PortsBusyError) as raised:
            await master.start()
        assert str(TRANSPORT_PORT) in str(raised.value), f"the port is not named: {raised.value}"
        # 40005 is bound before 40002 and must have been given back, or a second master cannot start.
        assert not _answers_on(8090), "the HTTP port is still bound after giving up"
        second = _hold(40005)
        second.close()
    finally:
        held.close()
        await master.stop()


async def test_a_busy_port_brings_the_pass_back_without_anybody_asking() -> None:
    """The one state nothing else reports a change OUT of has to wake the loop itself.

    Measured on the real machine on 2026-09-07 22:45: the first version of this fix relied on "the
    next pass", and the next pass was the registry poll thirty seconds away, so the service survived
    the busy port exactly as designed and then sat there while it was free. The switch has not moved,
    sleeping boxes say nothing, and no event is coming.

    The control is the other half of the same call: every other state must answer ``None``, or the
    loop would come back on a timer for ever and this would be a change of how the service runs
    rather than a repair. That the pass loop actually asks this is proved on the machine itself
    (``docs/measurements/2026-09-07-third-live-run.md``), not here.
    """
    assert wait_for_the_next_pass_s(ports_busy=True) == PORTS_BUSY_RETRY_S
    assert wait_for_the_next_pass_s(ports_busy=False) is None


async def test_a_port_taken_for_another_reason_is_not_retried() -> None:
    """Only EADDRINUSE is worth waiting out. An address that cannot be bound at all never frees.

    Two arms, because one cannot hold both halves. ``TimeoutError`` IS an ``OSError`` - its
    ``__mro__`` names it - so a single ``pytest.raises(OSError)`` around an ``asyncio.wait_for``
    swallows the very wait that was there to tell the two cases apart: the version of this that
    did stayed green with the guard in :meth:`ZoneMaster.start` deleted, because a start still
    retrying at ten seconds an attempt timed out INSIDE the ``raises`` and satisfied it.

    So the start is a task of its own. Whether it came back at all is asserted first, and only
    then what it raised, by ``errno`` rather than by type - 203.0.113.1 is a documentation address
    this machine cannot bind, which is EADDRNOTAVAIL and never becomes free.
    """
    master = ZoneMaster(
        bind_ip="203.0.113.1",
        device_id="5EB0CE000001",
        log=lambda _kind, _text: None,
        bind_attempts=5,
        bind_retry_wait_s=10.0,
    )
    starting = asyncio.create_task(master.start())
    try:
        done, _still_trying = await asyncio.wait({starting}, timeout=2.0)
        assert done, "start() was still retrying an address that can never free"
        raised = starting.exception()
        assert isinstance(raised, OSError), f"it ended with {raised!r} rather than the bind's own failure"
        assert raised.errno == errno.EADDRNOTAVAIL, f"the bind failed for another reason: errno {raised.errno}"
        assert not isinstance(raised, PortsBusyError)
    finally:
        starting.cancel()
        with contextlib.suppress(asyncio.CancelledError, OSError):
            await starting
        await master.stop()
