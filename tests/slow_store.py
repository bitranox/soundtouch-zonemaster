"""A real house store that takes its time, and remembers which thread asked it what.

The service's database calls are meant to run on one thread of their own and never on the event
loop that also times the zone. Two things have to be visible to say so from the outside: WHERE each
call ran, and whether the loop went on turning while one was slow. This wrapper gives both without
touching the store's internals - it is handed to the service at the ``open_store`` port, the seam
the service already takes a store through, and everything it is asked goes on to a real
:class:`SqlHouseStore` underneath once it has written the call down and, if asked to, slept.

``time.sleep`` rather than anything asynchronous, on purpose: a store call blocks whatever thread it
runs on, and a sleep is the one stand-in for a slow disk or a slow network that blocks the same way.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from soundtouch_zonemaster.application.errors import StoreError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from soundtouch_zonemaster.application.options import ChannelsExport, LegacyFiles
    from soundtouch_zonemaster.application.ports import HouseStore
    from soundtouch_zonemaster.domain.channellist import ChannelList
    from soundtouch_zonemaster.domain.preferences import (
        PreferenceName,
        PreferenceRow,
        PreferenceSource,
        PreferenceValue,
    )
    from soundtouch_zonemaster.domain.state import ZoneState

__all__ = ["Call", "SlowStore"]


@dataclass(frozen=True, slots=True)
class Call:
    """One call the store was asked, and the thread that asked it."""

    name: str
    thread: threading.Thread


class SlowStore:
    """Every call written down with its thread, slept on for ``stall_s``, then passed to ``real``.

    ``save_stalls`` overrides ``stall_s`` for the next saves, one entry per save, so a test can make
    the FIRST of two saves the slow one. ``saved`` holds each state in the order its save FINISHED,
    which is what tells two saves written out of order from two written in order: a record taken
    when a call starts would read the same either way once two threads can run them at once.
    """

    def __init__(self, real: HouseStore, *, stall_s: float = 0.0) -> None:
        self._real = real
        self.where = real.where
        self.stall_s = stall_s
        self.save_stalls: list[float] = []
        self.calls: list[Call] = []
        self.saved: list[ZoneState] = []
        self.stalls = 0
        """How many calls were actually slept on: the liveness half of any test built on this."""
        self.gates: dict[str, threading.Event] = {}
        """A call of that name waits for its event before it goes on: a call that never ends, until
        the test lets it. The test must set every gate it arms, or the thread waits for ever."""
        self.fails: set[str] = set()
        """A call of that name raises ``StoreError`` instead of reaching the real store."""

    def _called(self, name: str, *, stall_s: float | None = None) -> None:
        self.calls.append(Call(name, threading.current_thread()))
        pause = self.stall_s if stall_s is None else stall_s
        if pause > 0:
            time.sleep(pause)
            self.stalls += 1
        gate = self.gates.get(name)
        if gate is not None:
            gate.wait()
        if name in self.fails:
            message = f"simulated: the house database refused {name}"
            raise StoreError(message)

    def named(self, name: str) -> list[Call]:
        """Every call of one kind, in the order they were asked."""
        return [call for call in self.calls if call.name == name]

    def open(self, *, exclusive: bool, create: bool = True) -> None:
        self._called("open")
        self._real.open(exclusive=exclusive, create=create)

    def close(self) -> None:
        self._called("close")
        self._real.close()

    def import_legacy(self, files: LegacyFiles) -> None:
        self._called("import_legacy")
        self._real.import_legacy(files)

    def load_state(self) -> ZoneState:
        self._called("load_state")
        return self._real.load_state()

    def save_state(self, state: ZoneState) -> None:
        self._called("save_state", stall_s=self.save_stalls.pop(0) if self.save_stalls else None)
        self._real.save_state(state)
        self.saved.append(state)

    def load_channels(self) -> ChannelList:
        self._called("load_channels")
        return self._real.load_channels()

    def save_channels(self, channels: ChannelList) -> None:
        self._called("save_channels")
        self._real.save_channels(channels)

    def export_channels(self, path: Path) -> ChannelsExport:
        self._called("export_channels")
        return self._real.export_channels(path)

    def import_channels(self, path: Path) -> ChannelList:
        self._called("import_channels")
        return self._real.import_channels(path)

    def is_on(self) -> bool:
        self._called("is_on")
        return self._real.is_on()

    def set_switch(self, *, on: bool) -> bool:
        self._called("set_switch")
        return self._real.set_switch(on=on)

    def load_preferences(self) -> tuple[PreferenceRow, ...]:
        self._called("load_preferences")
        return self._real.load_preferences()

    def set_preference(
        self, name: PreferenceName, value: PreferenceValue, *, source: PreferenceSource
    ) -> PreferenceRow | None:
        self._called("set_preference")
        return self._real.set_preference(name, value, source=source)

    def set_preferences(self, values: Mapping[PreferenceName, PreferenceValue], *, source: PreferenceSource) -> None:
        self._called("set_preferences")
        self._real.set_preferences(values, source=source)

    def unset_preference(self, name: PreferenceName) -> PreferenceRow | None:
        self._called("unset_preference")
        return self._real.unset_preference(name)
