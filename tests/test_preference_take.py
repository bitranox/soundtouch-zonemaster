"""What a take of the preferences asks of the service's workers: decided from what moved, and nothing else.

The loopback file drives the same rule through the running service; this pins each arm of it,
including the one no loopback case can see - a dialling worker woken for nothing hands over nothing.
"""

from __future__ import annotations

from dataclasses import replace

from soundtouch_zonemaster.application.zone_service.preferences import WhatATakeAsks, what_a_take_asks
from soundtouch_zonemaster.domain.preferences import FADE_DEFAULT_S, HousePreferences

CONSOLE = "AABBCC0000A5"
OTHER_CONSOLE = "AABBCC0000A4"
BASE = HousePreferences(window_s=0.8, hold_threshold_s=1.0, rewind_s=20.0, fade_s=FADE_DEFAULT_S, consoles_allowed=())
NOTHING = WhatATakeAsks(dial_numbers=None, a_pass=False, a_registry_read=False)


def test_a_take_that_moved_nothing_asks_for_nothing() -> None:
    assert what_a_take_asks(BASE, BASE, in_the_book=()) == NOTHING


def test_the_rewind_and_the_fade_wake_nobody() -> None:
    """Both are read where they are used, so neither is any worker's business."""
    moved = replace(BASE, rewind_s=5.0, fade_s=FADE_DEFAULT_S + 1.0)
    assert what_a_take_asks(BASE, moved, in_the_book=()) == NOTHING


def test_a_moved_window_asks_the_dialling_worker_for_both_numbers_and_no_pass() -> None:
    asks = what_a_take_asks(BASE, replace(BASE, window_s=0.5), in_the_book=())
    assert asks == WhatATakeAsks(dial_numbers=(0.5, 1.0), a_pass=False, a_registry_read=False)


def test_a_moved_hold_asks_the_dialling_worker_for_both_numbers_and_no_pass() -> None:
    asks = what_a_take_asks(BASE, replace(BASE, hold_threshold_s=1.5), in_the_book=())
    assert asks == WhatATakeAsks(dial_numbers=(0.8, 1.5), a_pass=False, a_registry_read=False)


def test_a_console_the_book_does_not_hold_asks_for_a_pass_and_a_registry_read() -> None:
    """The registry read is where a console is let into the speaker book and watched."""
    asks = what_a_take_asks(BASE, replace(BASE, consoles_allowed=(CONSOLE,)), in_the_book=(OTHER_CONSOLE,))
    assert asks == WhatATakeAsks(dial_numbers=None, a_pass=True, a_registry_read=True)


def test_a_console_the_book_already_holds_asks_for_a_pass_and_no_read() -> None:
    """Allowed, taken off and allowed again: it has been watched all along, so a read adds nothing."""
    asks = what_a_take_asks(BASE, replace(BASE, consoles_allowed=(CONSOLE,)), in_the_book=(CONSOLE,))
    assert asks == WhatATakeAsks(dial_numbers=None, a_pass=True, a_registry_read=False)


def test_a_console_taken_off_the_list_asks_for_a_pass_and_no_read() -> None:
    """The pass is what lets it go; there is nothing new to watch."""
    allowed = replace(BASE, consoles_allowed=(CONSOLE,))
    asks = what_a_take_asks(allowed, BASE, in_the_book=())
    assert asks == WhatATakeAsks(dial_numbers=None, a_pass=True, a_registry_read=False)


def test_the_same_consoles_in_another_order_are_no_move() -> None:
    """A list is compared as the set of boxes it lets in: reordering it changes who belongs in nothing."""
    before = replace(BASE, consoles_allowed=(CONSOLE, OTHER_CONSOLE))
    after = replace(BASE, consoles_allowed=(OTHER_CONSOLE, CONSOLE))
    assert what_a_take_asks(before, after, in_the_book=()) == NOTHING
