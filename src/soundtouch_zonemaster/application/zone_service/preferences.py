"""The five preferences the service runs on: the options, with the house database's rows laid over.

Sixth in the chain, above the zone and below the dialling. Above the zone because the consoles a
person allows change who belongs in it; below the dialling because a calibration ends there and
writes what it measured as preferences, which this class then takes in.

Rewind, fade and consoles are not dialling, which is why this is a class of its own rather than
part of the one that reads numbers: the window and the hold are two of five, and the rule that
decides between a stored row and the configuration is the same for all of them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from ...domain.preferences import PreferenceName, PreferenceSource, PreferenceValue, resolved, value_of
from .zone import ZoneReconcile

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from ...domain.preferences import PreferenceRow

__all__ = ["PreferenceBook"]


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
    PreferenceName.REWIND: lambda v: f"an MPD channel starts {_seconds(v):.0f} s back",
    PreferenceName.FADE: lambda v: f"a joining box fades in over {_seconds(v):.1f} s",
    PreferenceName.CONSOLES: lambda v: f"consoles allowed into the zone: {_device_ids(v)}",
}
"""How each preference reads in the log. The window and hold wordings are the lines the house has always printed."""


class PreferenceBook(ZoneReconcile):
    """Which preferences the service is using, who decided each one, and what has been said about them."""

    def _take_the_preferences(self, rows: tuple[PreferenceRow, ...]) -> None:
        """Lay the stored preferences over the options, apply them, and say each one that changed.

        Called at start and after a calibration. A line is written only for a value or a source
        that changed, so a start with nothing stored says nothing, as it always did.
        """
        resolution = resolved(self.options.preferences, rows)
        for row, why in resolution.rejected:
            if (row.name, row.text) not in self._rejected_seen:
                self._rejected_seen.add((row.name, row.text))
                self.log("prefs", f"{row.name} = {row.text} in the house database is ignored ({why})")
        before, before_set_by = self._preferences, self._set_by
        self._preference_rows = rows
        self._preferences = resolution.preferences
        self._set_by = dict(resolution.set_by)
        self.policy.consoles_allowed = frozenset(self._preferences.consoles_allowed)
        self._the_window_is_now(self._preferences.window_s)
        self._the_hold_is_now(self._preferences.hold_threshold_s)
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
