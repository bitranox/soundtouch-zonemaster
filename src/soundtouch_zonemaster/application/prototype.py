"""One run of the zone master: bring it up, hold it for a duration, take it down again.

The other program. Where the service holds a house for days and decides for itself who belongs,
this takes a list of addresses, plays a preset, waits out a clock and dissolves - and it exists
because that is what ANSWERS a protocol question: a run either proves a speaker accepts a non-Bose
master and unobfuscated data, or it refutes it, and it does so in sixty seconds with somebody
listening.

``--join-mode schedule`` is the S6 rule, the joiner started at the byte the zone reaches three
seconds later; ``restart`` is the control arm, the stream restarted for everyone at the join with
its one-second gap. Keeping both is what makes the measurement a comparison rather than an
anecdote.

The dissolve is in a ``finally`` here for the same reason it is in the service: leaving a real
speaker in a zone whose master has gone is the one outcome that needs a person to undo it by hand.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from ..domain.enums import JoinMode
from .outcome import ExitCode

if TYPE_CHECKING:
    from ..domain.logfn import LogFn
    from .options import Options
    from .ports import PrototypePorts, StationSource, ZonePort

__all__ = ["POLL_INTERVAL_S", "join_slaves", "late_join", "run", "run_until_done"]


POLL_INTERVAL_S = 1.0
"""How often the run loop looks at the clock while holding the zone."""


async def run(options: Options, *, log: LogFn, ports: PrototypePorts) -> int:
    """One whole run: bring the zone up, hold it, and take it down again.

    The dissolve is in a ``finally`` because leaving a real speaker in a zone whose master
    has gone is the one outcome that needs a person to undo it by hand.
    """
    master = ports.open_master(
        bind_ip=options.bind_ip,
        device_id=options.device_id,
        log=log,
        encryption=options.encryption,
        ignore_selects=options.ignore_selects,
    )
    await master.start()
    try:
        station = await ports.station_source(options.preset_from, options.preset)
        log("master", f"station: {station.name} <- {station.playback_url}")
        await master.play(station)
        if not await join_slaves(master, options.slaves, log=log):
            log("master", "no slave joined; nothing to play to")
            return ExitCode.REFUSED
        await run_until_done(master, options, log=log, station_source=ports.station_source)
        return ExitCode.OK
    finally:
        log("master", "dissolving and shutting down")
        await master.dissolve()
        await master.stop()


async def join_slaves(master: ZonePort, ips: tuple[str, ...], *, log: LogFn) -> int:
    """Join each slave, counting the ones that made it; one unreachable speaker is not fatal."""
    joined = 0
    for ip in ips:
        try:
            await master.add_slave(ip)
            joined += 1
        except Exception as exc:  # noqa: BLE001 - one unreachable speaker must not end the run
            log("master", f"slave {ip} not joined: {type(exc).__name__}: {exc}")
    return joined


async def run_until_done(
    master: ZonePort,
    options: Options,
    *,
    log: LogFn,
    station_source: StationSource,
    poll_s: float = POLL_INTERVAL_S,
) -> None:
    """Hold the zone for ``--duration``, firing the station switch and the late join on the way."""
    t_start = time.monotonic()
    t_end = t_start + options.duration
    switched = late_joined = False
    while time.monotonic() < t_end:
        await asyncio.sleep(poll_s)
        if options.switch_after and not switched and time.monotonic() > t_start + options.switch_after:
            switched = True
            station = await station_source(options.preset_from, options.preset2)
            log("master", f"switching to: {station.name}")
            await master.play(station)
        if options.late_slaves and not late_joined and time.monotonic() > t_start + options.join_after:
            late_joined = True
            await late_join(master, options, log=log)


async def late_join(master: ZonePort, options: Options, *, log: LogFn) -> None:
    """Bring the late slaves in, and on the control arm restart the stream for everyone."""
    for ip in options.late_slaves:
        log("master", f"late join of {ip} ({options.join_mode})")
        try:
            await master.add_slave(ip)
        except Exception as exc:  # noqa: BLE001 - keep the run going for the others
            log("master", f"late slave {ip} not joined: {type(exc).__name__}: {exc}")
    if options.join_mode is JoinMode.RESTART and master.station is not None:
        log("master", "control arm: restarting the stream for everyone")
        await master.play(master.station.as_request())
