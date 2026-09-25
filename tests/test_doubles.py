"""The contract of the test doubles themselves: stopping one must not be able to hang the suite.

They are shared infrastructure - six suites drive them - and one of them held the whole gate up.
A stopped box waits for every connection into it to go away, and a client that has gone without
closing its socket makes that wait endless: measured 2026-09-07, one to three of every four runs of
the service's loopback suite stopped for ever in this teardown, with a single orphaned connection
to one box and nothing left running that could ever close it. The gate then blocks instead of
going red, which is worse than a failure because nothing reports it.

The orphan is what a cancelled HTTP call leaves behind. httpx reports the client CLOSED while its
connection stays open (measured in the same run: ``is_closed=True`` on both live clients, both
transports still open), so a box cannot rely on the other end tidying up. A real speaker switched
off does not wait for its callers either - it drops them.
"""

from __future__ import annotations

import asyncio

import pytest
from registry_double import FakeRegistry, devices_at
from speaker_double import SPEAKER_PORT, FakeSpeaker

pytestmark = pytest.mark.asyncio

HOST = "127.0.0.9"
"""Its own loopback address, so this test cannot collide with a suite that uses .1 to .4."""

PATIENCE_S = 5.0
"""Long enough that a slow machine cannot fail this, short enough to report rather than hang."""


async def test_a_box_that_is_switched_off_drops_a_client_that_is_still_holding_a_connection() -> None:
    """A connection that has sent nothing and will send nothing must not outlive the box."""
    speaker = FakeSpeaker({}, host=HOST)
    await speaker.start()
    _reader, writer = await asyncio.open_connection(HOST, SPEAKER_PORT)
    try:
        try:
            await asyncio.wait_for(speaker.stop(), PATIENCE_S)
        except TimeoutError:
            pytest.fail("stopping a box waited for a client it cannot close, so the suite would hang here")
    finally:
        writer.close()


async def test_a_registry_that_is_stopped_drops_a_client_that_is_still_holding_a_connection() -> None:
    """The same for the device list: it is the same shape, and one fix has to cover both."""
    registry = FakeRegistry(devices_at({"AABBCC000010": HOST}))
    await registry.start()
    _reader, writer = await asyncio.open_connection("127.0.0.1", registry.port)
    try:
        try:
            await asyncio.wait_for(registry.stop(), PATIENCE_S)
        except TimeoutError:
            pytest.fail("stopping the registry waited for a client it cannot close")
    finally:
        writer.close()
