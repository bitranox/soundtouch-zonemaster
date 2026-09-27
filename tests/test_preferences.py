"""The preference rule: which names are preferences, what each may hold, and how stored rows win."""

from __future__ import annotations

import pytest

from soundtouch_zonemaster.domain.preferences import (
    FADE_DEFAULT_S,
    HousePreferences,
    PreferenceName,
    PreferenceRefused,
    PreferenceRow,
    Stored,
    checked,
    resolved,
    stored,
    value_of,
)

BASE = HousePreferences(window_s=0.8, hold_threshold_s=1.0, rewind_s=20.0, fade_s=FADE_DEFAULT_S, consoles_allowed=())


def _row(name: str, text: str, source: str = "cli") -> PreferenceRow:
    return PreferenceRow(name=name, text=text, source=source, changed_at="2026-09-27T14:40:00+00:00")


def test_the_five_names_are_the_config_paths() -> None:
    assert [str(name) for name in PreferenceName] == [
        "dialling.window_s",
        "dialling.hold_threshold_s",
        "mpd.rewind_s",
        "volume.fade_s",
        "membership.consoles_allowed",
    ]


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        (PreferenceName.WINDOW, 0.3, "refused: the dialling window must be between 0.5 and 2.0 s, not 0.3"),
        (PreferenceName.HOLD, 2.5, "refused: the hold threshold must be between 1.0 and 2.0 s, not 2.5"),
        (PreferenceName.REWIND, -1, "refused: the mpd rewind must not be negative, not -1.0"),
        (PreferenceName.FADE, 5.5, "refused: the fade-in must be between 0.0 and 5.0 s, not 5.5"),
        (PreferenceName.WINDOW, "fast", "refused: dialling.window_s must be a number, not str"),
        (PreferenceName.FADE, True, "refused: volume.fade_s must be a number, not bool"),
        (PreferenceName.CONSOLES, "AABBCC000012", "refused: membership.consoles_allowed must be a list, not str"),
        (
            PreferenceName.CONSOLES,
            ["AABBCC00001"],
            "refused: membership.consoles_allowed holds 'AABBCC00001', which is not a device id (12 hex digits)",
        ),
    ],
)
def test_a_value_outside_what_the_preference_may_hold_is_refused_by_name(
    name: PreferenceName, value: object, message: str
) -> None:
    with pytest.raises(PreferenceRefused) as refused:
        checked(name, value)
    assert str(refused.value) == message


def test_a_legal_value_comes_back_in_the_shape_the_record_holds() -> None:
    assert checked(PreferenceName.WINDOW, 1) == 1.0
    assert isinstance(checked(PreferenceName.WINDOW, 1), float)
    assert checked(PreferenceName.FADE, 0) == 0.0
    assert checked(PreferenceName.CONSOLES, ["AABBCC000012"]) == ("AABBCC000012",)
    assert checked(PreferenceName.CONSOLES, []) == ()


def test_no_rows_leave_the_base_untouched() -> None:
    resolution = resolved(BASE, ())
    assert resolution.preferences == BASE
    assert resolution.set_by == {}
    assert resolution.rejected == ()


def test_a_stored_row_wins_over_the_base_and_says_who_set_it() -> None:
    window = _row("dialling.window_s", "0.6", source="calibration")
    consoles = _row("membership.consoles_allowed", '["AABBCC000012"]')
    resolution = resolved(BASE, (window, consoles))
    assert resolution.preferences.window_s == 0.6
    assert resolution.preferences.consoles_allowed == ("AABBCC000012",)
    assert resolution.preferences.hold_threshold_s == BASE.hold_threshold_s
    assert resolution.set_by == {PreferenceName.WINDOW: window, PreferenceName.CONSOLES: consoles}


def test_stored_decodes_each_usable_row_once() -> None:
    fade = _row("volume.fade_s", "1.5")
    usable, rejected = stored((fade,))
    assert usable == {PreferenceName.FADE: Stored(value=1.5, row=fade)}
    assert rejected == ()


@pytest.mark.parametrize(
    ("row", "why"),
    [
        (_row("dialling.speed", "1.0"), "'dialling.speed' is not a preference"),
        (_row("dialling.window_s", "fast"), "not JSON"),
        (_row("dialling.window_s", "9.0"), "refused: the dialling window must be between 0.5 and 2.0 s, not 9.0"),
    ],
)
def test_a_row_that_cannot_be_used_is_skipped_and_the_base_applies(row: PreferenceRow, why: str) -> None:
    resolution = resolved(BASE, (row,))
    assert resolution.preferences == BASE
    assert resolution.set_by == {}
    assert resolution.rejected == ((row, why),)


def test_value_of_reads_each_name_off_the_record() -> None:
    assert [value_of(BASE, name) for name in PreferenceName] == [0.8, 1.0, 20.0, 0.8, ()]
