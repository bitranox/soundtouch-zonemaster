"""The house's preferences: the five settings a person may change while the service runs.

Each has a default in the configuration layers, and may have a value SET in the house database -
by a person (``prefs set``), by a calibration at a speaker, or later by the web app. A value set
there wins for as long as it is set, over the files, the environment and the command line alike;
``prefs unset`` removes it and the layers decide again (user, 2026-09-27). What the database holds
is therefore only ever what somebody chose, never a copy of a default.

This module is the rule for both halves, so a value can never be legal from one source and refused
from the other: the boundary that parses the config layers, the record, the store, the CLI and the
``config`` view all check through :func:`checked`, and :func:`stored` is the one place a stored row
is decoded and judged. The rows arrive as the JSON text the database holds, and decoding them here
is computation rather than I/O.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, cast

from .dialling import WINDOW_CEILING_S, WINDOW_FLOOR_S
from .longpress import HOLD_THRESHOLD_CEILING_S, HOLD_THRESHOLD_FLOOR_S

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

__all__ = [
    "DEVICE_ID",
    "FADE_CEILING_S",
    "FADE_DEFAULT_S",
    "FADE_FLOOR_S",
    "HousePreferences",
    "PreferenceName",
    "PreferenceRefusedError",
    "PreferenceRow",
    "PreferenceSource",
    "PreferenceValue",
    "Resolution",
    "Stored",
    "checked",
    "plain_value",
    "resolved",
    "stored",
    "value_of",
]

FADE_DEFAULT_S = 0.8
"""How long a joining box's volume takes to climb from zero back to where it was.

The value the house has always run with: long enough that the box does not arrive at full level in
one step, short enough that nobody waits for it."""

FADE_FLOOR_S = 0.0
"""Zero is allowed and means no climb at all: the level is put back in one step."""

FADE_CEILING_S = 5.0
"""Past this a joining room is audibly quiet for long enough to read as a fault."""

DEVICE_ID = re.compile(r"[0-9A-F]{12}")
"""A speaker's device id: its MAC, twelve upper-case hex digits, as the registry spells it."""

PreferenceValue = float | tuple[str, ...]


class PreferenceName(StrEnum):
    """The five preferences, named by the config path each one's default is written under."""

    WINDOW = "dialling.window_s"
    HOLD = "dialling.hold_threshold_s"
    REWIND = "mpd.rewind_s"
    FADE = "volume.fade_s"
    CONSOLES = "membership.consoles_allowed"


class PreferenceSource(StrEnum):
    """Who set a stored value. The database refuses any other word (a CHECK constraint)."""

    CALIBRATION = "calibration"
    CLI = "cli"
    APP = "app"


class PreferenceRefusedError(ValueError):
    """A value a preference cannot hold, carrying the sentence that says why.

    The caller (CLI, store) reads this as a refusal, not a run failure: it means exit 1
    on a value that was rejected, never "could not run".
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class HousePreferences:
    """The five values the running service uses, whichever source each came from."""

    window_s: float
    hold_threshold_s: float
    rewind_s: float
    fade_s: float
    consoles_allowed: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class PreferenceRow:
    """One stored preference exactly as the database holds it: nothing here has been checked."""

    name: str
    text: str
    """The value as JSON text."""
    source: str
    changed_at: str
    """ISO 8601, or empty when the time was never recorded (a migrated or imported calibration)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Stored:
    """A row that passed the rule, and the value it decoded to."""

    value: PreferenceValue
    row: PreferenceRow


@dataclass(frozen=True, slots=True, kw_only=True)
class Resolution:
    """The rows laid over the layered values: what wins, who set it, and what was skipped."""

    preferences: HousePreferences
    set_by: Mapping[PreferenceName, PreferenceRow]
    rejected: tuple[tuple[PreferenceRow, str], ...]


_FIELD: Mapping[PreferenceName, str] = {
    PreferenceName.WINDOW: "window_s",
    PreferenceName.HOLD: "hold_threshold_s",
    PreferenceName.REWIND: "rewind_s",
    PreferenceName.FADE: "fade_s",
    PreferenceName.CONSOLES: "consoles_allowed",
}


def checked(name: PreferenceName, value: object) -> PreferenceValue:
    """The value in the shape the record holds it, or :class:`PreferenceRefusedError` naming why not.

    The three messages the options already gave (window, hold, rewind) are reproduced exactly:
    the golden options corpus holds them, and a person who has met one should meet the same words
    from ``prefs set``.
    """
    if name is PreferenceName.CONSOLES:
        return _device_ids(name, value)
    number = _number(name, value)
    if name is PreferenceName.WINDOW and not WINDOW_FLOOR_S <= number <= WINDOW_CEILING_S:
        message = (
            f"refused: the dialling window must be between {WINDOW_FLOOR_S} and {WINDOW_CEILING_S} s, not {number}"
        )
        raise PreferenceRefusedError(message)
    if name is PreferenceName.HOLD and not HOLD_THRESHOLD_FLOOR_S <= number <= HOLD_THRESHOLD_CEILING_S:
        message = (
            f"refused: the hold threshold must be between {HOLD_THRESHOLD_FLOOR_S} and "
            f"{HOLD_THRESHOLD_CEILING_S} s, not {number}"
        )
        raise PreferenceRefusedError(message)
    if name is PreferenceName.REWIND and number < 0:
        message = f"refused: the mpd rewind must not be negative, not {number}"
        raise PreferenceRefusedError(message)
    if name is PreferenceName.FADE and not FADE_FLOOR_S <= number <= FADE_CEILING_S:
        message = f"refused: the fade-in must be between {FADE_FLOOR_S} and {FADE_CEILING_S} s, not {number}"
        raise PreferenceRefusedError(message)
    return number


def stored(
    rows: Iterable[PreferenceRow],
) -> tuple[dict[PreferenceName, Stored], tuple[tuple[PreferenceRow, str], ...]]:
    """Every usable row by name, and every unusable one with the reason - never a raise.

    A bad row must not take the house's music down: somebody who edited the database by hand, or a
    newer version's name this one does not know, costs that one value and a line saying so.
    """
    usable: dict[PreferenceName, Stored] = {}
    rejected: list[tuple[PreferenceRow, str]] = []
    for row in rows:
        try:
            name = PreferenceName(row.name)
        except ValueError:
            rejected.append((row, f"{row.name!r} is not a preference"))
            continue
        try:
            value = checked(name, json.loads(row.text))
        except json.JSONDecodeError:
            rejected.append((row, "not JSON"))
            continue
        except PreferenceRefusedError as exc:
            rejected.append((row, str(exc)))
            continue
        usable[name] = Stored(value=value, row=row)
    return usable, tuple(rejected)


def resolved(base: HousePreferences, rows: Iterable[PreferenceRow]) -> Resolution:
    """Every usable row laid over ``base``: the values the service runs on, and who set each."""
    usable, rejected = stored(rows)
    changes = {_FIELD[name]: held.value for name, held in usable.items()}
    return Resolution(
        preferences=replace(base, **changes),
        set_by={name: held.row for name, held in usable.items()},
        rejected=rejected,
    )


def value_of(preferences: HousePreferences, name: PreferenceName) -> PreferenceValue:
    """One preference's value off the record, by name."""
    return cast("PreferenceValue", getattr(preferences, _FIELD[name]))


def plain_value(value: PreferenceValue) -> float | list[str]:
    """A preference's value as JSON holds it: a tuple becomes the list it was read as.

    Here rather than beside either command that prints one, so ``prefs`` and ``config`` print a
    stored console list the same way from one copy of this conversion.
    """
    return list(value) if isinstance(value, tuple) else value


def _number(name: PreferenceName, value: object) -> float:
    """A JSON number as a float. A boolean is refused: JSON's ``true`` is not the number 1.

    Refused here, for every numeric preference at once, rather than in each preference's own
    range check: NaN and infinity compare false against every bound (``lo <= nan <= hi`` is
    always false), so window, hold and fade already turn them away as "out of bounds" - but
    rewind's check is a bare ``number < 0``, which a non-finite value slips past silently. A
    stored NaN rewind reached :func:`~.dialling.Place.resumed` and gave back zero seconds, so an
    audiobook would restart from the beginning on every resume. One check here closes that for
    the whole shape: no numeric preference can ever hold a value nothing finite can be measured
    against.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        message = f"refused: {name} must be a number, not {type(value).__name__}"
        raise PreferenceRefusedError(message)
    number = float(value)
    if not math.isfinite(number):
        message = f"refused: {name} must be finite, not {number}"
        raise PreferenceRefusedError(message)
    return number


def _device_ids(name: PreferenceName, value: object) -> tuple[str, ...]:
    """A list of device ids as a tuple, each one checked, so a typo cannot allow a console by accident."""
    if not isinstance(value, list | tuple):
        message = f"refused: {name} must be a list, not {type(value).__name__}"
        raise PreferenceRefusedError(message)
    ids: list[str] = []
    for item in cast("list[object] | tuple[object, ...]", value):
        if not isinstance(item, str) or not DEVICE_ID.fullmatch(item):
            message = f"refused: {name} holds {item!r}, which is not a device id (12 hex digits)"
            raise PreferenceRefusedError(message)
        ids.append(item)
    return tuple(ids)
