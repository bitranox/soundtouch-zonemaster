"""The preference rule: which names are preferences, what each may hold, and how stored rows win."""

from __future__ import annotations

import math

import pytest

from soundtouch_zonemaster.domain.preferences import (
    FADE_DEFAULT_S,
    HousePreferences,
    PreferenceName,
    PreferenceRefusedError,
    PreferenceRow,
    Stored,
    checked,
    resolved,
    shown,
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
        (
            PreferenceName.CONSOLES,
            ["not-hex-at-all"],
            "refused: membership.consoles_allowed holds 'not-hex-at-all', which is not a device id (12 hex digits)",
        ),
    ],
)
def test_a_value_outside_what_the_preference_may_hold_is_refused_by_name(
    name: PreferenceName, value: object, message: str
) -> None:
    with pytest.raises(PreferenceRefusedError) as refused:
        checked(name, value)
    assert str(refused.value) == message


@pytest.mark.parametrize(
    "name", [PreferenceName.WINDOW, PreferenceName.HOLD, PreferenceName.REWIND, PreferenceName.FADE]
)
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf], ids=["nan", "inf", "-inf"])
def test_a_non_finite_number_is_refused_on_every_numeric_preference(name: PreferenceName, value: float) -> None:
    """NaN and infinity would otherwise slip past rewind's bare ``< 0`` check (a stored NaN rewind
    made a resumed place seconds=0.0, restarting every audiobook silently)."""
    with pytest.raises(PreferenceRefusedError) as refused:
        checked(name, value)
    assert str(refused.value) == f"refused: {name} must be finite, not {value}"


def test_the_negative_rewind_message_is_unchanged_by_the_finite_check() -> None:
    """The one refusal message this plan promises byte-identical: a genuine negative, not NaN/inf."""
    with pytest.raises(PreferenceRefusedError) as refused:
        checked(PreferenceName.REWIND, -1)
    assert str(refused.value) == "refused: the mpd rewind must not be negative, not -1.0"


@pytest.mark.parametrize(
    ("name", "text"),
    [
        (PreferenceName.WINDOW, "NaN"),
        (PreferenceName.HOLD, "Infinity"),
        (PreferenceName.REWIND, "NaN"),
        (PreferenceName.REWIND, "-Infinity"),
        (PreferenceName.FADE, "Infinity"),
    ],
)
def test_a_row_holding_a_non_finite_json_number_is_rejected_not_used(name: PreferenceName, text: str) -> None:
    """Python's ``json`` module accepts ``NaN``/``Infinity``/``-Infinity`` even though the JSON
    spec does not, so a hand-edited or migrated row can carry one straight into ``stored``."""
    row = _row(str(name), text)
    usable, rejected = stored((row,))
    assert usable == {}
    assert len(rejected) == 1
    assert rejected[0][0] == row
    assert "must be finite" in rejected[0][1]


def test_a_legal_value_comes_back_in_the_shape_the_record_holds() -> None:
    assert checked(PreferenceName.WINDOW, 1) == 1.0
    assert isinstance(checked(PreferenceName.WINDOW, 1), float)
    assert checked(PreferenceName.FADE, 0) == 0.0
    assert checked(PreferenceName.CONSOLES, ["AABBCC000012"]) == ("AABBCC000012",)
    assert checked(PreferenceName.CONSOLES, []) == ()


def test_a_console_id_is_accepted_in_any_case_and_stored_upper_case() -> None:
    """A deployed config or an old note may spell an id lower-case; the value the record holds -
    and so what a running service compares against a speaker's own upper-case device id - is
    always upper case."""
    assert checked(PreferenceName.CONSOLES, ["a1b2c3d4e5f6"]) == ("A1B2C3D4E5F6",)
    assert checked(PreferenceName.CONSOLES, ["A1b2C3d4E5f6"]) == ("A1B2C3D4E5F6",)
    assert checked(PreferenceName.CONSOLES, ["a1b2c3d4e5f6", "A1B2C3D4E5F6"]) == (
        "A1B2C3D4E5F6",
        "A1B2C3D4E5F6",
    ), "no de-duplication happens today for a same-case pair either, so a mixed-case pair keeps both"


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


HUGE_INTEGER = "1" + "0" * 400
"""JSON a decoder reads as an integer no float can hold: ``float()`` raises OverflowError on it."""

TOO_MANY_DIGITS = "1" * 5000
"""An integer literal past Python's 4300-digit conversion limit: ``json.loads`` raises a plain ValueError."""

TOO_DEEP = "[" * 100_000
"""Nesting deep enough that the decoder raises RecursionError rather than JSONDecodeError."""


@pytest.mark.parametrize(
    ("name", "text", "why"),
    [
        (PreferenceName.REWIND, HUGE_INTEGER, "refused: mpd.rewind_s is too large to be a number of seconds"),
        (PreferenceName.WINDOW, TOO_MANY_DIGITS, "not JSON"),
        (PreferenceName.FADE, TOO_DEEP, "not JSON"),
        (
            PreferenceName.CONSOLES,
            "[" + ",".join(["[1]"] * 100_000) + "]",
            "refused: membership.consoles_allowed holds something of type list, "
            "which is not a device id (12 hex digits)",
        ),
    ],
    ids=["overflows-a-float", "past-the-digit-limit", "nested-too-deep", "a-list-where-an-id-goes"],
)
def test_a_row_the_decoder_or_float_cannot_take_is_rejected_not_raised(
    name: PreferenceName, text: str, why: str
) -> None:
    """A hand-edited row must cost one value and a line, never the service: each of these three
    raised straight through ``stored`` (OverflowError, ValueError, RecursionError), which the
    service's watch does not catch and ``prefs unset`` - the way out - died on too."""
    row = _row(str(name), text)
    resolution = resolved(BASE, (row,))
    assert resolution.preferences == BASE
    assert resolution.rejected == ((row, why),)


def test_an_integer_too_large_for_a_float_is_refused_without_echoing_it() -> None:
    """The refusal a ``prefs set`` or a config file gets, and it does not repeat 400 digits back."""
    with pytest.raises(PreferenceRefusedError) as refused:
        checked(PreferenceName.WINDOW, 10**400)
    assert str(refused.value) == "refused: dialling.window_s is too large to be a number of seconds"


def test_a_console_id_of_any_case_is_accepted_and_a_long_bad_one_is_cut_short() -> None:
    """The rule is twelve hex digits of any case, folded to upper: a lower-case id from an old
    config is accepted, not refused for its case. An enormous refused item is cut, not echoed."""
    assert checked(PreferenceName.CONSOLES, ["aabbcc000012"]) == ("AABBCC000012",)
    with pytest.raises(PreferenceRefusedError) as long:
        checked(PreferenceName.CONSOLES, ["A" * 1_000_000])
    assert len(str(long.value)) < 200
    assert f"'{'A' * 80}'..." in str(long.value)


def test_a_row_under_an_enormous_name_is_rejected_with_the_name_cut_short() -> None:
    row = _row("x" * 1_000_000, "1.0")
    _, rejected = stored((row,))
    assert rejected == ((row, f"'{'x' * 80}'... is not a preference"),)


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("9.0", "9.0"),
        ('"fast"', '"fast"'),
        ("line one\nline two", "line one\\nline two"),
        ("7" * 81, "7" * 80 + "..."),
    ],
    ids=["short", "quoted-json", "multi-line", "over-the-limit"],
)
def test_shown_keeps_a_raw_value_on_one_line_and_bounded(text: str, said: str) -> None:
    assert shown(text) == said
