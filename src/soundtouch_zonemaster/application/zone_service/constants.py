"""The service's fixed numbers, and the one question that is nothing but a number with a name.

They sit apart from the classes that use them because the service is split along its own methods
into a chain of eight parts, and several of these are read by more than one of them. A constant
defined in whichever part happened to need it first would make that part imported for its number
rather than for what it does.

The test for whether something belongs here is exactly that: read by more than one class. The two
key maps were here until M5 and are not any more, because ``KeyReading`` turned out to be the only
reader of either - and a private name imported across a module boundary is a name in the wrong file.
"""

from __future__ import annotations

__all__ = [
    "DIAL_TICK_S",
    "FADE_S",
    "FADE_STEPS",
    "JOIN_RETRY_S",
    "MIN_CHANNELS_TO_STEP",
    "MUTE_HOLD_S",
    "PORTS_BUSY_RETRY_S",
    "PRESET_KEYS",
    "wait_for_the_next_pass_s",
]


PRESET_KEYS = 6
"""How many preset keys a speaker has, and therefore how many places the seeding fills.

It is the size of the dialling alphabet for the same reason: the keys ARE the digits.
"""
PORTS_BUSY_RETRY_S = 1.0
"""How soon a pass that found a listening port busy asks to be run again.

Nothing else would ask. The switch has not moved, a house whose boxes are asleep says nothing, and
the registry poll (``application/options.py``) is thirty seconds away - so without this the zone waits for the next
registry poll, which is what happened on the real machine on 2026-09-07 when the first version of
this retry was measured and did not come back inside eight seconds.

A second, because the condition is somebody else's connection ending and there is nothing to gain
by asking faster.
"""


def wait_for_the_next_pass_s(*, ports_busy: bool) -> float | None:
    """How long the pass loop may wait before coming back although nothing asked it to.

    ``None`` is the ordinary answer: a pass happens because something happened, and waiting for an
    event costs nothing while nothing is going on.

    A busy listening port is the exception, and it is the only one so far, because it is the single
    state that nothing reports a change OUT of. The switch has not moved, a house whose boxes are
    asleep says nothing, and the registry poll (``application/options.py``) is thirty seconds away -
    so a loop that only waits to be asked will sit there. Measured on the real machine
    2026-09-07 22:45: the service
    survived the busy port exactly as intended and then did not take the zone when it freed.
    """
    return PORTS_BUSY_RETRY_S if ports_busy else None


MIN_CHANNELS_TO_STEP = 2
"""Stepping needs somewhere else to go, so a list of one channel has no next and no previous."""
DIAL_TICK_S = 0.05
"""How finely the dialling worker re-checks its deadline while a number is being typed.

It bounds how late a completed number can be acted on, and it only runs between the first digit
and the window closing - at most two seconds after a keypress, never while nothing is happening.
"""
MUTE_HOLD_S = 1.5
"""How long a box stays at zero after it has been told to join, before its volume is put back.

A box carries on playing its OWN station for a moment after ``setZone`` reaches it, and that moment
is the whole reason this exists: measured 2026-09-08 over three joins, it stopped 0.68, 0.83 and
1.25 s after the document arrived. The wait is above the longest of the three rather than at their
mean, because being late costs silence in a room that would have been silent anyway - the box needs
about three more seconds to buffer OUR stream - while being early is the sound the mute was added to
remove.
"""
FADE_S = 0.8
"""How long the volume takes to travel from zero back to what the box was on."""
FADE_STEPS = 8
"""How many steps that journey is made of. Each one is an HTTP call to the box, so this trades
smoothness against traffic; eight over 0.8 s is a step every 100 ms."""
JOIN_RETRY_S = 5.0
"""How long a box that refused to join is left alone before it is tried again.

Every event would otherwise start another attempt, and a box that is off answers by making the
whole reconcile wait out an eight-second HTTP timeout - so one unplugged speaker would slow down
everything the service does for everybody else.

**It has to be shorter than the window a woken box counts as belonging for**, by more than one of
those timeouts, and that is the whole reason it is five and not twenty. A refusal is stamped when
it ARRIVES, so the quiet period starts up to a timeout after the box was pressed: at twenty the
retry always opened at or after
:data:`soundtouch_zonemaster.domain.membership.WAKE_WINDOW_S` had expired, the box had stopped
belonging by then, and nothing asked for it again. One blink of the network and the room stayed
out until somebody walked over and switched the speaker off and on.

Five leaves room for a refusal that takes the full timeout and still lands a retry inside the
window; ``tests/test_service_loopback.py`` holds the three numbers to that rule, because they live
in three different layers and no one of them can state it. The cost is bounded by the same window:
a box that keeps refusing is tried about once every thirteen seconds while it is awake rather than
once, and then stops belonging.
"""
