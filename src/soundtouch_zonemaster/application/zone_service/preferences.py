"""The five preferences the service runs on: the options, with the house database's rows laid over.

Sixth in the chain, above the zone and below the dialling. Above the zone because the consoles a
person allows change who belongs in it; below the dialling because a calibration ends there and
writes what it measured as preferences, which this class then takes in.

Rewind, fade and consoles are not dialling, which is why this is a class of its own rather than
part of the one that reads numbers: the window and the hold are two of five, and the rule that
decides between a stored row and the configuration is the same for all of them.

The house database is read again every switch poll, on a worker of its own, so a value set while
the service runs is taken in without a restart. Each one lands at its natural point: the consoles
at the next pass (one first allowed asks for a registry read at once, which is where watching it is
decided, and is asked what it is playing), a fade at the next join, and a window or hold once
nobody is dialling or holding a key - which is the dialling worker's to decide, so this only wakes
it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from ...domain.logfn import ERROR_KIND
from ...domain.preferences import PreferenceName, PreferenceSource, PreferenceValue, resolved, shown, value_of
from ..errors import StoreError
from .zone import ZoneReconcile

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping

    from ...domain.preferences import HousePreferences, PreferenceRow

__all__ = ["PreferenceBook", "WhatATakeAsks", "what_a_take_asks"]


_KIND: Mapping[PreferenceName, str] = {
    PreferenceName.WINDOW: "dial",
    PreferenceName.HOLD: "dial",
    PreferenceName.REWIND: "prefs",
    PreferenceName.FADE: "prefs",
    PreferenceName.CONSOLES: "prefs",
}
"""The log kind each preference's line is written under; the two dialling ones keep the kind they always had."""


def _seconds(value: PreferenceValue) -> float:
    return cast("float", value)


def _device_ids(value: PreferenceValue) -> str:
    return ", ".join(cast("tuple[str, ...]", value)) or "none"


_SAID: Mapping[PreferenceName, Callable[[PreferenceValue], str]] = {
    PreferenceName.WINDOW: lambda v: f"the dialling window is {_seconds(v):.1f} s",
    PreferenceName.HOLD: lambda v: f"a key is held after {_seconds(v):.1f} s",
    PreferenceName.REWIND: lambda v: f"an MPD channel starts {_seconds(v):g} s back",
    PreferenceName.FADE: lambda v: f"a joining box fades in over {_seconds(v):.1f} s",
    PreferenceName.CONSOLES: lambda v: f"consoles allowed into the zone: {_device_ids(v)}",
}
"""How each preference reads in the log. The window and hold wordings are the lines the house has always printed."""


@dataclass(frozen=True, slots=True)
class WhatATakeAsks:
    """Which workers a take of the preferences wakes, decided from what moved and nothing else.

    Each worker is woken only for what it acts on, so a take that moved nothing it reads costs it
    nothing: start and every calibration take the preferences in, and a pass asked for by a fade
    would be a pass for nothing. The rewind and the fade wake nobody - they are read where they
    are used, and a fade reads its length once, when it starts.
    """

    dial_numbers: tuple[float, float] | None
    """The window and the hold to hand over, or ``None`` when neither moved.

    Asked for rather than handed over: the dialling worker does that once nobody is mid-gesture,
    so a number being typed finishes on the window it began with."""
    a_pass: bool
    """Whether the console list moved, which changes who belongs - and the pass is what acts on that."""
    a_registry_read: bool
    """Whether a console was allowed that the speaker book does not hold yet.

    The registry read is where a console is let into the book and watched, and a box new to the
    book is asked what it is playing right after it. One the book already holds (allowed earlier
    in the run, taken off, allowed again) needs neither: it has been watched all along."""


def what_a_take_asks(
    before: HousePreferences, after: HousePreferences, *, in_the_book: Collection[str]
) -> WhatATakeAsks:
    """What a take that moved ``before`` to ``after`` asks of the workers."""
    dial_numbers = (after.window_s, after.hold_threshold_s)
    allowed, allowed_before = frozenset(after.consoles_allowed), frozenset(before.consoles_allowed)
    return WhatATakeAsks(
        dial_numbers=dial_numbers if dial_numbers != (before.window_s, before.hold_threshold_s) else None,
        a_pass=allowed != allowed_before,
        a_registry_read=any(device_id not in in_the_book for device_id in allowed - allowed_before),
    )


class PreferenceBook(ZoneReconcile):
    """Which preferences the service is using, who decided each one, and what has been said about them."""

    async def _watch_the_preferences(self) -> None:
        """Read the preferences again every switch poll, and take in whatever changed.

        Its own loop rather than part of the switch's, so a preference read that fails can never
        stop the switch, which is the one thing that must keep working. A failing read keeps the
        last good set, said once when it starts failing and once when it recovers.
        """
        failing = False
        while True:
            await asyncio.sleep(self.options.switch_poll_s)
            try:
                rows = await self.store.load_preferences()
            except StoreError as exc:
                if not failing:
                    self.log(ERROR_KIND, f"{exc}; keeping the preferences already in use")
                    failing = True
                continue
            if failing:
                # Not an error, so not where errors go: the failure went to ERROR_KIND, which the
                # narration sends to stderr beside every other failure, and its end is a preference
                # line like the ones it lets through again.
                self.log("prefs", "the house database answers again")
                failing = False
            if rows != self._preference_rows:
                self._take_the_preferences(rows)

    def _take_the_preferences(self, rows: tuple[PreferenceRow, ...]) -> None:
        """Lay the stored preferences over the options, apply them, and say each one that changed.

        Called at start, after a calibration, and by the watch whenever the rows it reads differ
        from the ones last taken in. A line is written only for a value or a source that changed,
        so a start with nothing stored says nothing, as it always did.

        Each worker is woken only for what it acts on, which :func:`what_a_take_asks` decides. A
        console taken off the list is let go at the next pass, and stays watched until the next
        restart, because the speaker book never forgets a box. One put on the list that the book
        does not hold yet asks for a registry read at once, which is where it is let into the book
        and watched, and is asked what it is playing: asleep, its next wake takes it in; already on
        the house's stream, it belongs at once.
        """
        resolution = resolved(self.options.preferences, rows)
        for row, why in resolution.rejected:
            if (row.name, row.text) not in self._rejected_seen:
                self._rejected_seen.add((row.name, row.text))
                # Cut and escaped: a row is whatever somebody typed into the database, and one
                # spanning megabytes or lines would otherwise land in the journal whole.
                said = f"{shown(row.name)} = {shown(row.text)}"
                self.log("prefs", f"{said} in the house database is ignored ({why})")
        before, before_set_by = self._preferences, self._set_by
        self._preference_rows = rows
        self._preferences = resolution.preferences
        self._set_by = dict(resolution.set_by)
        self.policy.consoles_allowed = frozenset(self._preferences.consoles_allowed)
        asks = what_a_take_asks(before, self._preferences, in_the_book=self._speakers.keys())
        if asks.dial_numbers is not None:
            self._dial_numbers_wanted = asks.dial_numbers
            # Wakes the dialling worker, which is where the two are handed over once nobody is pressing.
            self._dialled.set()
        if asks.a_pass:
            self._wanted.set()
        if asks.a_registry_read:
            # Watched and placed now rather than at the next registry poll: without this a console
            # allowed while asleep was taken in only on its second wake, and one already on the
            # house's stream not until it next woke.
            self._read_the_registry_now()
        for name in PreferenceName:
            moved = value_of(before, name) != value_of(self._preferences, name)
            if moved or before_set_by.get(name) != self._set_by.get(name):
                self.log(_KIND[name], self._described(name))

    def _described(self, name: PreferenceName) -> str:
        """One preference as the log says it: its value, and who decided it."""
        row = self._set_by.get(name)
        if row is None:
            origin = "from the configuration"
        elif row.source == PreferenceSource.CALIBRATION:
            origin = "calibrated in an earlier run" if row.changed_at == "" else f"calibrated at {row.changed_at}"
        else:
            origin = f"set by {row.source} at {row.changed_at}"
        return f"{_SAID[name](value_of(self._preferences, name))}, {origin}"
