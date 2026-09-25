"""The composition root: one adapter per port, and nothing left unwired.

The ports themselves are Protocols, so a stand-in that does not match one is a pyright error
rather than a test failure - which is why there is no per-port contract test here. What a type
checker cannot see is the WIRING: that every field of both bundles was actually given something,
that what it was given can be called, and that the two runs the console scripts perform are
coroutines rather than something that merely looks like one.

Each of those is a way the composition root can be wrong while every module it names is right.
``build_production`` gives every field explicitly and none defaults, so a port added to either
record fails there; these tests are what says the record it fails in is the one being read.
"""

from __future__ import annotations

import dataclasses
import inspect

from soundtouch_zonemaster.application.ports import PrototypePorts, ZoneServicePorts
from soundtouch_zonemaster.composition import AppServices, build_production, hold_the_zone, run_prototype


def test_the_production_wiring_gives_every_port_of_both_programs_something() -> None:
    services = build_production()

    assert isinstance(services, AppServices)
    for bundle in (services.zone_ports, services.prototype_ports):
        for field in dataclasses.fields(bundle):
            assert getattr(bundle, field.name) is not None, f"{type(bundle).__name__}.{field.name} is unwired"


def test_every_wired_port_can_be_called() -> None:
    """A port is a callable or a class that makes one; anything else reaches the service and
    raises where the service is, rather than where the wiring is."""
    services = build_production()

    for bundle in (services.zone_ports, services.prototype_ports):
        for field in dataclasses.fields(bundle):
            assert callable(getattr(bundle, field.name)), f"{type(bundle).__name__}.{field.name} is not callable"


def test_the_two_bundles_are_the_ones_the_application_declares() -> None:
    """Named rather than counted: a count goes stale the moment a port is added, while this fails
    only if the composition root starts building something else."""
    services = build_production()

    assert isinstance(services.zone_ports, ZoneServicePorts)
    assert isinstance(services.prototype_ports, PrototypePorts)


def test_the_two_runs_the_console_scripts_perform_are_coroutines() -> None:
    """Both are awaited by ``asyncio.run`` inside a click callback. A plain function there fails
    at the moment a real house is being held, which is the worst place to find out."""
    assert inspect.iscoroutinefunction(hold_the_zone)
    assert inspect.iscoroutinefunction(run_prototype)


def test_building_the_production_services_twice_gives_two_independent_bundles() -> None:
    """Nothing here is a singleton, and a test that substituted one port would otherwise be
    substituting it for every later caller in the same process."""
    first, second = build_production(), build_production()

    assert first is not second
    assert first.zone_ports is not second.zone_ports
