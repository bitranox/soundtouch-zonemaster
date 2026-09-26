"""Composition root: the one place the adapters are wired to the application's ports.

Everything above this is written against a Protocol; everything below it talks to a speaker, a
file or the service next door. This module is the only one that names both, which is what lets
``application`` be read - and tested - without a network, a disk or a protobuf runtime anywhere
near it.

It stays a wiring file. Nothing here chooses any behaviour: :func:`build_production`
names one adapter per port, and the two ``*_main``-facing runs hand the real narrator and the real
ports to a use case that was already complete without them. A rule that lived here would be a rule
no test could reach except through the real world.

Two ports get a function rather than the adapter itself. The prototype's master, because the
application may not see the value the wire carries: a prototype run asks for an encryption by its
domain NAME and ``adapters.soundtouch.wire`` looks up the protobuf enum. And the house store,
because the service's CLI opens the same store and should not have to name the adapter to do it.
Every other port here is the adapter, handed over by name, so what a type checker compares against
the Protocol is the real thing rather than a wrapper standing in front of it; the store's own
conformance is pinned at the bottom for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..adapters.aftertouch import registry
from ..adapters.files.house_store import SqlHouseStore
from ..adapters.logging.narration import log
from ..adapters.mpd.client import MpdControl
from ..adapters.soundtouch import observer, speaker_http, wire
from ..adapters.soundtouch.zone_master import ZoneMaster
from ..application.outcome import ExitCode
from ..application.ports import PrototypePorts, ZoneServicePorts
from ..application.prototype import run
from ..application.zone_service import ZoneService

if TYPE_CHECKING:
    from ..application import ports as port_types
    from ..application.options import Options, ServiceOptions
    from ..domain.enums import Encryption
    from ..domain.logfn import LogFn
    from ..domain.secret import Secret

__all__ = ["AppServices", "build_production", "hold_the_zone", "open_house_store", "run_prototype"]


@dataclass(frozen=True, slots=True, kw_only=True)
class AppServices:
    """One bundle per program, built together so neither can be wired from a different world.

    The two share adapters - the same ``speaker_http`` answers a prototype's preset read and a
    service's - and building them in one place is what keeps that a fact rather than a coincidence.
    """

    zone_ports: ZoneServicePorts
    prototype_ports: PrototypePorts


def open_prototype_master(
    *,
    bind_ip: str,
    device_id: str,
    log: LogFn,
    encryption: Encryption,
    ignore_selects: bool,
) -> ZoneMaster:
    """The master one run holds, with the encryption name turned into the value the wire carries.

    This mapping is the whole reason the port takes a domain enum: ``application`` may not import
    the generated protobuf at all, so the option set carries the NAME and the translation happens
    at the edge that sends it.
    """
    return ZoneMaster(
        bind_ip=bind_ip,
        device_id=device_id,
        log=log,
        encryption=wire.encryption_type(encryption),
        ignore_selects=ignore_selects,
    )


def open_house_store(database: str, *, password: Secret | None, log: LogFn) -> port_types.HouseStore:
    """The house database both the service and the store verbs of its CLI reach through.

    A function rather than the class handed over by name, because the CLI names it too and
    neither should have to know which adapter answers: it returns a store not yet opened, and
    opening it is the caller's first touch of the disk.
    """
    return SqlHouseStore(database, password=password, log=log)


def build_production() -> AppServices:
    """One adapter per port, named once.

    Every field is given explicitly and none defaults, so a port added to either record fails
    HERE - at the one place that knows what the real world's answer is - rather than silently
    keeping whatever a default happened to be.
    """
    return AppServices(
        zone_ports=ZoneServicePorts(
            open_store=open_house_store,
            fetch_speakers=registry.fetch_speakers,
            watch_speaker=observer.SpeakerObserver,
            open_zone_master=ZoneMaster,
            read_volume=speaker_http.read_volume,
            set_volume=speaker_http.set_volume,
            select_station=speaker_http.select_station,
            ask_now_playing=speaker_http.speaker_now_playing,
            read_preset=speaker_http.station_from_speaker_preset,
            open_mpd=MpdControl,
        ),
        prototype_ports=PrototypePorts(
            open_master=open_prototype_master,
            station_source=speaker_http.station_from_speaker_preset,
        ),
    )


async def hold_the_zone(options: ServiceOptions) -> int:
    """Hold the house until the service is stopped.

    It only ever returns because the run was cancelled, which is what a SIGINT does, and the
    service dissolves the zone on its way out. There is no duration here on purpose: a run ends,
    a service is ended.
    """
    await ZoneService(options, log=log, ports=build_production().zone_ports).run()
    return ExitCode.OK


async def run_prototype(options: Options) -> int:
    """One whole run of the prototype, against real speakers, with the real narrator."""
    return await run(options, log=log, ports=build_production().prototype_ports)


if TYPE_CHECKING:
    # Conformance, stated rather than inferred. Every port above is checked where it is assigned
    # into its typed field; these are not assigned anywhere, so without this block the two
    # commands, the master's two shapes and the store itself would reach the flat with nothing
    # having compared them to anything. The store's pair states what the factory's return only
    # implies: that the ADAPTER, whose switch() hands back a DbSwitch, is a HouseStore whose
    # switch the service watches as a SwitchReader - and it keeps saying so if the factory is ever
    # annotated with the adapter's own type. A run of pyright reads this; nothing executes it.
    _run_service: port_types.RunService = hold_the_zone
    _run_zone: port_types.RunZone = run_prototype
    _master_is_a_zone_master_port: port_types.ZoneMasterPort = ZoneMaster(bind_ip="", device_id="", log=log)
    _master_is_a_prototype_master: port_types.PrototypeMaster = ZoneMaster(bind_ip="", device_id="", log=log)
    _open_store: port_types.OpenHouseStore = open_house_store
    _store_is_a_house_store: port_types.HouseStore = SqlHouseStore("", log=log)
