"""Who belongs in the zone, decided from what the speakers said and nothing else.

The design's four cases, plus the one it answered twice. It ends with "anything else is a member",
and it justifies the AUX rule with "somebody listening on the aux input is not overridden"; for a
source nobody has measured those two say opposite things. Settled with the user 2026-09-07: only
what is known to be OURS makes a speaker a member. A mistake then costs a box that could have
played and did not, rather than music taken away from somebody.

That is also why no Bluetooth spelling appears anywhere here. Under a blocklist its exact string
would have to be right or the rule would not fire; under this one it needs no entry at all.

The clock is injected because it is a real external edge, and the timeout is the rule with teeth:
one box drops off the radio for minutes at a time, so a speaker going quiet must NOT cost it its
membership before the timeout is up.

Nothing in here does anything. It returns the set that should be in the zone; `ZoneMaster` is
still the only thing that changes one.
"""

from __future__ import annotations

from soundtouch_zonemaster.domain.enums import ProductCode, SourceName
from soundtouch_zonemaster.domain.events import SpeakerEvent
from soundtouch_zonemaster.domain.membership import UNREACHABLE_TIMEOUT_S, WAKE_WINDOW_S, Membership
from soundtouch_zonemaster.domain.speakers import Speaker

MASTER = "BC24000000AA"
SPEAKER_ID = "AABBCC000010"
CONSOLE_ID = "AABBCC000012"
ADDRESS = "192.0.2.10"


class FakeClock:
    """A clock a test moves by hand, which is what makes the timeout boundary assertable."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def speaker(device_id: str = SPEAKER_ID, *, console: bool = False) -> Speaker:
    return Speaker(
        device_id=device_id,
        name="Bose Studio",
        ip=ADDRESS,
        mac=device_id,
        product_code=ProductCode.LIFESTYLE if console else ProductCode.SOUNDTOUCH,
        account_id="1000001",
    )


def playing(source: str, owner: str, *, at: float, device_id: str = SPEAKER_ID) -> SpeakerEvent:
    """A nowPlayingUpdated as the observer hands it over."""
    return SpeakerEvent(
        received_at=at,
        speaker=ADDRESS,
        device_id=device_id,
        kind="nowPlayingUpdated",
        source=source,
        stream_owner=owner,
        frame="<updates/>",
    )


def _policy(clock: FakeClock, *, consoles: tuple[str, ...] = ()) -> Membership:
    return Membership(master_device_id=MASTER, now=clock, consoles_allowed=consoles)


def test_a_speaker_never_heard_from_is_not_a_member() -> None:
    """The registry lists it; nothing says what it is doing. Silence is not consent."""
    clock = FakeClock()

    assert _policy(clock).target_members([speaker()]) == frozenset()


def test_a_speaker_in_standby_is_not_a_member() -> None:
    clock = FakeClock()
    policy = _policy(clock)

    policy.observe(playing(SourceName.STANDBY, SPEAKER_ID, at=clock.t))

    assert policy.target_members([speaker()]) == frozenset()


def test_a_box_that_named_standby_last_is_asleep_until_it_names_something_else() -> None:
    """What the service asks while a box is waking, and the whole reason it can be asked then.

    A woken box names its preset 101 to 199 ms before it says it has left standby (2026-09-07),
    and a selection frame names no source at all - so the answer over exactly that gap is still
    the standby the box went into. It is what separates a wake from a press at the one moment the
    service has to tell them apart.
    """
    clock = FakeClock()
    policy = _policy(clock)

    policy.observe(playing(SourceName.STANDBY, SPEAKER_ID, at=clock.t))
    assert policy.is_asleep(SPEAKER_ID)

    policy.observe(
        SpeakerEvent(
            received_at=clock.t,
            speaker=ADDRESS,
            device_id=SPEAKER_ID,
            kind="nowSelectionUpdated",
            preset_id=3,
            frame="<updates/>",
        )
    )
    assert policy.is_asleep(SPEAKER_ID), "a selection says nothing about the source, so it changes nothing"

    policy.observe(playing(SourceName.LOCAL_INTERNET_RADIO, SPEAKER_ID, at=clock.t))
    assert not policy.is_asleep(SPEAKER_ID)


def test_a_box_nobody_has_heard_a_source_from_is_not_asleep() -> None:
    """Unknown is not standby. The service asks every box what it is playing before its first
    frame, so this is a box that appeared between two registry reads - and treating it as awake
    costs at most one channel change that somebody asked for."""
    assert not _policy(FakeClock()).is_asleep(SPEAKER_ID)


def test_a_speaker_playing_the_masters_stream_is_a_member() -> None:
    clock = FakeClock()
    policy = _policy(clock)

    policy.observe(playing(SourceName.LOCAL_INTERNET_RADIO, MASTER, at=clock.t))

    assert policy.target_members([speaker()]) == {SPEAKER_ID}


def test_a_speaker_playing_its_own_internet_radio_is_not_a_member() -> None:
    """The source string is identical to ours. Only the owner of the stream tells them apart."""
    clock = FakeClock()
    policy = _policy(clock)

    policy.observe(playing(SourceName.LOCAL_INTERNET_RADIO, SPEAKER_ID, at=clock.t))

    assert policy.target_members([speaker()]) == frozenset()


def test_a_speaker_on_aux_is_not_a_member() -> None:
    clock = FakeClock()
    policy = _policy(clock)

    policy.observe(playing("AUX", SPEAKER_ID, at=clock.t))

    assert policy.target_members([speaker()]) == frozenset()


def test_a_source_nobody_has_measured_keeps_the_speaker_out() -> None:
    """The user's call: a mistake costs a box that stays silent, never music taken from somebody."""
    clock = FakeClock()
    policy = _policy(clock)

    policy.observe(playing("SOMETHING_NOBODY_HAS_SEEN", SPEAKER_ID, at=clock.t))

    assert policy.target_members([speaker()]) == frozenset()


def test_waking_out_of_standby_makes_it_a_member_before_it_plays_our_stream() -> None:
    """POWER on joins. It cannot already be playing our stream at the moment it is turned on."""
    clock = FakeClock()
    policy = _policy(clock)

    policy.observe(playing(SourceName.STANDBY, SPEAKER_ID, at=clock.t))
    clock.advance(1.0)
    policy.observe(playing(SourceName.LOCAL_INTERNET_RADIO, SPEAKER_ID, at=clock.t))

    assert policy.target_members([speaker()]) == {SPEAKER_ID}


def test_a_woken_speaker_that_is_then_put_on_aux_is_dropped_again() -> None:
    """The wake is one chance to take it, not a hold on it."""
    clock = FakeClock()
    policy = _policy(clock)

    policy.observe(playing(SourceName.STANDBY, SPEAKER_ID, at=clock.t))
    clock.advance(1.0)
    policy.observe(playing(SourceName.LOCAL_INTERNET_RADIO, SPEAKER_ID, at=clock.t))
    clock.advance(1.0)
    policy.observe(playing("AUX", SPEAKER_ID, at=clock.t))

    assert policy.target_members([speaker()]) == frozenset()


def test_a_box_that_names_its_own_station_after_waking_is_still_a_member() -> None:
    """The wake has a lifetime, and the box's own station does not end it.

    A wake used to be a single frame: set by the standby-to-something transition and cleared by
    the next source the box named. But the next source a woken box names is its OWN station, which
    is not ours - so a box that reported twice before a pass reached it fell straight back out and
    could only be taken in again by being switched off and on. That is the trap rank 16 names.
    """
    clock = FakeClock()
    policy = _policy(clock)

    policy.observe(playing(SourceName.STANDBY, SPEAKER_ID, at=clock.t))
    clock.advance(1.0)
    policy.observe(playing(SourceName.LOCAL_INTERNET_RADIO, SPEAKER_ID, at=clock.t))
    clock.advance(1.0)
    policy.observe(playing(SourceName.LOCAL_INTERNET_RADIO, SPEAKER_ID, at=clock.t))

    assert policy.target_members([speaker()]) == {SPEAKER_ID}


def test_a_press_at_a_sleeping_box_makes_it_a_member_before_it_says_it_woke() -> None:
    """Rank 16, in one assertion: the press is what proves a person is there, not the report.

    Measured 2026-09-08 in the eighth live run - a box pressed at 03:00:42.99 did not report a
    source until 03:00:46.472, and the service waited those 3.5 s with nothing to say. The press
    had already arrived.
    """
    clock = FakeClock()
    policy = _policy(clock)

    policy.observe(playing(SourceName.STANDBY, SPEAKER_ID, at=clock.t))
    policy.woke(SPEAKER_ID, at=clock.t)

    assert policy.target_members([speaker()]) == {SPEAKER_ID}


def test_a_press_does_not_make_a_sleeping_box_look_awake() -> None:
    """``is_asleep`` decides whether a press may choose the channel, and the mark must not move it.

    Set the other way round, a press out of standby would read as a press at an awake box, and a
    wake in Room6 would send three other rooms to whatever Room6 last played.
    """
    clock = FakeClock()
    policy = _policy(clock)

    policy.observe(playing(SourceName.STANDBY, SPEAKER_ID, at=clock.t))
    policy.woke(SPEAKER_ID, at=clock.t)

    assert policy.is_asleep(SPEAKER_ID) is True


def test_a_box_that_was_pressed_and_then_says_nothing_is_let_go_when_the_wake_runs_out() -> None:
    """The lifetime is what bounds the guess. A press is evidence, not a lease.

    The window has to outlast the longest take-in, because ``_take_in`` waits up to
    :data:`soundtouch_zonemaster.master.FIRST_BYTES_TIMEOUT_S` for the station and then asks who belongs
    again - a window shorter than that would drop the box it was fetching the stream for.
    """
    clock = FakeClock()
    policy = _policy(clock)

    policy.observe(playing(SourceName.STANDBY, SPEAKER_ID, at=clock.t))
    policy.woke(SPEAKER_ID, at=clock.t)
    clock.advance(WAKE_WINDOW_S / 2)
    assert policy.target_members([speaker()]) == {SPEAKER_ID}, "still inside the window"

    clock.advance(WAKE_WINDOW_S)

    assert policy.target_members([speaker()]) == frozenset()


def test_a_box_pressed_and_then_put_on_aux_is_dropped_at_once() -> None:
    """The window is not a hold either: a source that is not ours ends the wake before it expires.

    This is the AUX rule surviving the lifetime. Somebody who wakes a box and puts it on its aux
    input is somebody listening, and the zone does not take a speaker away from them.
    """
    clock = FakeClock()
    policy = _policy(clock)

    policy.observe(playing(SourceName.STANDBY, SPEAKER_ID, at=clock.t))
    policy.woke(SPEAKER_ID, at=clock.t)
    clock.advance(1.0)
    policy.observe(playing("AUX", SPEAKER_ID, at=clock.t))

    assert policy.target_members([speaker()]) == frozenset()


def test_a_box_woken_straight_onto_its_aux_input_is_never_taken() -> None:
    """The wake begins on internet radio and on nothing else, and this is why.

    A wake used to be one frame, so a box that left standby onto AUX was a member for that single
    frame and was dropped by its next report. A frame is now a window, and the same rule would
    hold a box somebody is listening to for the whole of it - so the window begins only on the
    source a box switched on actually names first, its own last station. It costs the one chance a
    wake onto an unmeasured source used to have, in the direction the user settled: a box that
    could have played and did not, rather than music taken away from somebody in the room.
    """
    clock = FakeClock()
    policy = _policy(clock)

    policy.observe(playing(SourceName.STANDBY, SPEAKER_ID, at=clock.t))
    clock.advance(1.0)
    policy.observe(playing("AUX", SPEAKER_ID, at=clock.t))

    assert policy.target_members([speaker()]) == frozenset()


def test_a_press_at_a_box_nobody_has_heard_of_marks_nothing() -> None:
    """A press names a box; it does not introduce one. Liveness is still not membership."""
    clock = FakeClock()
    policy = _policy(clock)

    policy.woke(SPEAKER_ID, at=clock.t)

    assert policy.target_members([speaker()]) == frozenset()


def test_going_back_to_standby_ends_the_membership() -> None:
    clock = FakeClock()
    policy = _policy(clock)

    policy.observe(playing(SourceName.LOCAL_INTERNET_RADIO, MASTER, at=clock.t))
    clock.advance(1.0)
    policy.observe(playing(SourceName.STANDBY, SPEAKER_ID, at=clock.t))

    assert policy.target_members([speaker()]) == frozenset()


def test_the_console_stays_out_even_while_it_plays_our_stream() -> None:
    """An API POWER flips its input, so it is never taken on by accident."""
    clock = FakeClock()
    policy = _policy(clock)

    policy.observe(playing(SourceName.LOCAL_INTERNET_RADIO, MASTER, at=clock.t, device_id=CONSOLE_ID))

    assert policy.target_members([speaker(CONSOLE_ID, console=True)]) == frozenset()


def test_the_console_joins_when_it_is_named_explicitly() -> None:
    clock = FakeClock()
    policy = _policy(clock, consoles=(CONSOLE_ID,))

    policy.observe(playing(SourceName.LOCAL_INTERNET_RADIO, MASTER, at=clock.t, device_id=CONSOLE_ID))

    assert policy.target_members([speaker(CONSOLE_ID, console=True)]) == {CONSOLE_ID}


def test_a_member_that_goes_quiet_keeps_its_membership_until_the_timeout() -> None:
    """One box drops off the radio for minutes. A dead spot must not empty the zone."""
    clock = FakeClock()
    policy = _policy(clock)
    policy.observe(playing(SourceName.LOCAL_INTERNET_RADIO, MASTER, at=clock.t))

    clock.advance(UNREACHABLE_TIMEOUT_S - 60.0)

    assert policy.target_members([speaker()]) == {SPEAKER_ID}


def test_a_member_that_stays_quiet_past_the_timeout_loses_it() -> None:
    clock = FakeClock()
    policy = _policy(clock)
    policy.observe(playing(SourceName.LOCAL_INTERNET_RADIO, MASTER, at=clock.t))

    clock.advance(UNREACHABLE_TIMEOUT_S + 60.0)

    assert policy.target_members([speaker()]) == frozenset()


def test_the_timeout_boundary_is_asserted_from_both_sides() -> None:
    """A timeout tested from one side only passes for an off-by-a-lot as happily as an exact one."""
    clock = FakeClock()
    policy = _policy(clock)
    policy.observe(playing(SourceName.LOCAL_INTERNET_RADIO, MASTER, at=clock.t))
    start = clock.t

    clock.t = start + UNREACHABLE_TIMEOUT_S - 0.001
    assert policy.target_members([speaker()]) == {SPEAKER_ID}

    clock.t = start + UNREACHABLE_TIMEOUT_S + 0.001
    assert policy.target_members([speaker()]) == frozenset()


def test_a_speaker_that_comes_back_after_a_gap_is_a_member_again() -> None:
    clock = FakeClock()
    policy = _policy(clock)
    policy.observe(playing(SourceName.LOCAL_INTERNET_RADIO, MASTER, at=clock.t))
    clock.advance(UNREACHABLE_TIMEOUT_S + 60.0)
    assert policy.target_members([speaker()]) == frozenset()

    policy.observe(playing(SourceName.LOCAL_INTERNET_RADIO, MASTER, at=clock.t))

    assert policy.target_members([speaker()]) == {SPEAKER_ID}


def test_an_event_that_says_nothing_about_playback_leaves_the_verdict_alone() -> None:
    """A key press carries no source, and must not read as "this box now plays nothing"."""
    clock = FakeClock()
    policy = _policy(clock)
    policy.observe(playing(SourceName.LOCAL_INTERNET_RADIO, MASTER, at=clock.t))

    policy.observe(
        SpeakerEvent(
            received_at=clock.t,
            speaker=ADDRESS,
            device_id=SPEAKER_ID,
            kind="userActivityUpdate",
            frame="<updates/>",
        )
    )

    assert policy.target_members([speaker()]) == {SPEAKER_ID}


def test_a_speaker_the_registry_does_not_list_is_not_in_the_answer() -> None:
    """The registry decides who exists; this decides which of those belong in the zone."""
    clock = FakeClock()
    policy = _policy(clock)
    policy.observe(playing(SourceName.LOCAL_INTERNET_RADIO, MASTER, at=clock.t))

    assert policy.target_members([]) == frozenset()


def test_a_member_that_only_reports_on_its_data_channel_keeps_its_place() -> None:
    """A box playing our stream has nothing to say on its notification channel, and says nothing.

    Its liveness arrives on the transport channel instead: a state report every second, which the
    master already receives. Without counting those, a zone that nobody touches empties itself the
    moment the unreachable timeout runs out - the music stops with nobody having done anything,
    and every member is dropped for being silent while it is playing what we sent it.
    """
    clock = FakeClock()
    policy = _policy(clock)
    policy.observe(playing(SourceName.LOCAL_INTERNET_RADIO, MASTER, at=clock.t))
    assert policy.target_members([speaker()]) == frozenset({SPEAKER_ID})

    clock.advance(UNREACHABLE_TIMEOUT_S - 1)
    policy.seen(SPEAKER_ID, at=clock.t)
    clock.advance(2)

    assert policy.target_members([speaker()]) == frozenset({SPEAKER_ID}), "the report kept it alive"


def test_a_report_from_a_box_nobody_has_heard_of_does_not_invent_a_member() -> None:
    """Liveness is not membership: it says a box is there, never that it belongs to us."""
    clock = FakeClock()
    policy = _policy(clock)

    policy.seen(SPEAKER_ID, at=clock.t)

    assert policy.target_members([speaker()]) == frozenset()
