"""The one branch of select that a measurement run needs: accept it and do nothing.

Driven through the real ZoneMaster rather than a stand-in, because the thing under test is that
the master's own select returns before it does anything. No servers are started, start() is never
called, and nothing reaches the network.

The control uses a ContentItem with no location, which select refuses on its own. That is what
lets both arms run without play: with the flag the run stops at the guard, without it the run
reaches the parse and stops there instead, and the two log lines say which happened. Patching
play would have proved only what the patch does.
"""

from __future__ import annotations

import asyncio

from soundtouch_zonemaster.adapters.soundtouch.zone_master import ZoneMaster

NO_LOCATION = '<ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl"><itemName>X</itemName></ContentItem>'


def _master(*, ignore_selects: bool) -> tuple[ZoneMaster, list[str]]:
    logs: list[str] = []
    master = ZoneMaster(
        bind_ip="127.0.0.1",
        device_id="5EB0CE000001",
        log=lambda kind, text: logs.append(f"{kind} {text}"),
        ignore_selects=ignore_selects,
    )
    return master, logs


def test_the_flag_stops_select_at_the_guard() -> None:
    master, logs = _master(ignore_selects=True)
    asyncio.run(master.select(NO_LOCATION, origin="192.168.0.31"))
    assert master.station is None
    assert any("ignoring" in line for line in logs), logs
    assert not any("no location" in line for line in logs), logs


def test_without_the_flag_select_gets_past_the_guard() -> None:
    """The control. It must fail for a DIFFERENT reason, further along, or the guard is untested."""
    master, logs = _master(ignore_selects=False)
    asyncio.run(master.select(NO_LOCATION, origin="192.168.0.31"))
    assert master.station is None
    assert any("no location" in line for line in logs), logs
    assert not any("ignoring" in line for line in logs), logs
