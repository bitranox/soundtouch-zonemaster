"""Which selections a person made, and which are the master's own voice coming back.

A speaker reports what it is playing on the same notification channel, in the same frame shape, as
a preset somebody pressed. In the hardest case the two are byte for byte identical: pressing the
preset that is already playing produces exactly the frame our own station change produces, down to
the ``location`` naming our own URL. Measured 2026-09-07 on the flat's first zone under the
service, where a service that read every selection as a press answered its own change with another
one - 28 station changes in 45 seconds, a new stream each time, audible as constant re-buffering in
four rooms.

**What separates them is that the box says when a HUMAN touched it.** A ``userActivityUpdate``
accompanies a pressed preset and never accompanies a selection we caused. Over two live runs: 26
human selections, every one with a user activity within tens of milliseconds; 62 echo selections,
not one with a user activity anywhere near it. It holds for both ways a person can press - the
remote and the buttons on the box itself, measured separately - for a POWER-on wake, for a press on
a box already playing, and for the source key cycling onto Bluetooth.

**Which SIDE the confirmation arrives on depends on where the box is standing.** A box outside the
zone names its preset and then says a person touched it, 4 to 55 ms behind. A box that is a SLAVE
in the zone does the opposite: the touch arrives first and the selection follows it by 21 to 37 ms.
The second run measured that on four presses made from inside a playing zone, and it is not a
detail of one evening - it is the room the whole feature exists for, because a person changing the
channel is standing in a room that is playing.

**Touches the house made itself are not in here.** A box answers every volume write with a touch,
so the fade after a join produces nine of them, and on 2026-09-21 they once confirmed a selection
nobody pressed. The service drops one touch per write of its own before a frame reaches this module
(``VolumeGuard._claimed_as_our_echo``). The slave half of ``burst-as-a-slave-frames.json`` is that
fade and nothing else: its presses were driven through ``/key``, which as a slave reports no touch.

**Both sides are collected, and the pairing waits until nothing further can change it** (user,
2026-09-21: "we only can act AFTER we know it's a single key, multi-key or long press"). Nothing
here displaces anything.

**The pairing rule is not the obvious one, and both obvious ones are measured wrong.** Oldest with
oldest hands a touch to an echo that arrived just in front of a real press, and the press is lost.
Newest with newest hands it to the echo that follows a press by about a second, which is our own
answer to that very press. What reads all three runs correctly is the NEAREST: every allowed pair
is ranked by how far apart it is and the closest is taken first, then the next closest of what is
left. A real press always has a confirmation closer to it than any echo does, so the echoes are
what remains over - and what remains over is discarded. A surplus selection is our own voice; a
surplus touch is our own volume fade, which makes a box report that a person touched it
(OPEN-WORK rank 185).

**Reported in time order**, because the presses of a burst are the digits of a channel number and
1 then 2 is not 2 then 1.

**One key action is TWO touches, and the second is what the next key is measured from.** A box says
a person touched it once as the key goes down and once as it comes back up, 0.23 to 0.447 s apart
over the twenty presses of the first two runs. Until 2026-09-21 one of the pair was spent on the
selection and the other thrown away, so the dialling window was armed at the press and charged a
person for however long their thumb rested on the key: a measured 0.645 s between two presses in
the flat is only about 0.31 s of idle time, and the number split at a 0.6 s window all the same.
The rule is the user's, 2026-09-21 - the window for the next key starts at the RELEASE of this one.

**Which of the pair the selection is next to is the box's STATE**, the same way the side is. A box
outside the zone names the preset and both touches follow it, so the one that confirms it is the
key going DOWN. A slave sends both touches first and names the preset 21 to 37 ms behind the
second, so the one that confirms it is the key coming UP and the press is the touch in front of it.

Nothing here reads a clock, the way ``dialling`` and ``calibration`` do not: every call takes the
moment it happened, so the boundary is assertable rather than waited out.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "CONFIRM_BACK_WINDOW_S",
    "CONFIRM_WINDOW_S",
    "HOLD_CEILING_S",
    "HOLD_FLOOR_S",
    "HOLD_WHEN_UNSEEN_S",
    "AskedAtTheFrame",
    "Press",
    "Presses",
]

CONFIRM_WINDOW_S = 1.0
"""How far behind a selection its confirmation may still arrive.

**It is a bound and not a knob, and since 2026-09-21 it is barely even that.** Replaying the two
runs of real human presses - 26 presses among 88 selections - every value from 0.2 s to a minute
reads all of them identically, because a press now identifies itself as a whole key action rather
than as a lone touch: two touches with a hold between them, next to the selection. An echo has no
such action anywhere near it, so widening this reaches nothing.

The largest gap a real press has ever shown in this direction is 64 ms. A second is about fifteen
times that, and what the number really governs is how long a selection nobody confirmed keeps its
box waiting - which is why it is not larger.
"""

CONFIRM_BACK_WINDOW_S = 0.1
"""How far in FRONT of a selection its confirmation may already have arrived.

This is the tight direction, and unlike the forward one it has a measured ceiling on both sides of
the chosen value. The frame it pairs with is the release of the key that caused the selection, 21
to 37 ms in front of it over ten presses by a slave. Two different frames lying further back must
NOT be paired: our own volume fade, which makes a box report that a person touched it, and the
release of a real press whose echo comes back 0.7 to 1.4 s later.

**Replaying all three runs puts the boundaries at 40 ms and 240 ms.** Every value between reads all
98 selections correctly. At 245 ms a fade touch 240 ms in front of an echo in the third run turns
that echo into a ninth press of an eight-press run; at 1.9 s the first run gains a press and at 5 s
it gains two and the second run one, which is the loop of 2026-09-07 coming back. So this number is
picked from the middle of a measured range, about three times the largest real gap above it and
about two and a half times itself below the first misclassification.
"""


HOLD_FLOOR_S = 0.15
"""How far apart two touches must be to be one key going down and coming back up.

Below this they are two different things. The shortest hold this house has recorded is 0.23 s, and
the shortest gap between one press coming up and the next going down is 0.134 s, measured in the
calibration burst of the second run - so the floor sits between the two.
"""

HOLD_CEILING_S = 1.0
"""How long after a press a second touch can still be that press coming back up.

Above this it is something else: our own volume fade, or the next thing somebody does. The longest
hold measured is 0.447 s, and a preset held ON PURPOSE runs to several seconds - the firmware reads
that as "store what is playing here" - so a second is clear of both.

It is also how long a press whose release never arrives waits before being reported anyway, which
is why it is not larger.
"""

HOLD_WHEN_UNSEEN_S = 0.45
"""What a press's hold is taken to be when no release frame arrives.

It is not an edge case. Measured 2026-09-21: every press a person made on the remote or at a box's
own buttons reported two touches, and every press DRIVEN through a box's ``/key`` endpoint reported
one. A digit with no release would arm no window at all.

The longest hold recorded rather than the median of them, because the two risks are not symmetric:
a window that closes early splits a number into two channel changes, and one that closes late only
delays the change.
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class AskedAtTheFrame:
    """What the house answered about a box AT THE MOMENT it spoke, carried to where it is used.

    Both questions have to be asked at the frame and not when the pair completes. A box names its
    preset before it says it has left standby, which is what lets a wake be told from a press on a
    box that is already playing - and the pairing now finishes later than the frame, by up to the
    forward window. Asked again at that point, the house would have heard the box leave standby in
    the meantime and every wake would read as an ordinary press.

    The two are carried separately because neither follows from the other: whether a box may choose
    the channel for the whole house depends on what the zone is playing as well as on whether the
    box was asleep.
    """

    asleep: bool
    may_choose_the_channel: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class Press:
    """One preset a person pressed, and the three moments that are not the same moment.

    ``named_at`` is when the box named the preset. It identifies the press and orders it against
    the others, and it is the one of the three that every recording of this house holds.

    ``pressed_at`` is when the key went down and ``released_at`` when it came back up. The
    calibration measures the gap between one release and the next press, because that is the idle
    time a person controls, and the dialling window is armed at ``released_at`` for the same
    reason. Outside the zone ``pressed_at`` is a few milliseconds behind ``named_at`` and the
    release follows it; as a SLAVE both are in front of it.
    """

    preset_id: int
    named_at: float
    pressed_at: float
    released_at: float
    asked: AskedAtTheFrame


class _Pending:
    """What one speaker has said that has not been decided yet.

    A plain class with a typed ``__init__`` rather than a dataclass: pyright strict reads a
    ``field(default_factory=list)`` as ``list[Unknown]`` whatever the annotation says, and the
    house rule is to define the type rather than silence the checker.
    """

    __slots__ = ("selections", "touches")

    def __init__(self) -> None:
        self.selections: list[tuple[float, int, AskedAtTheFrame]] = []
        """What the box NAMED, unread: the moment, the preset, and what the house said of it then.

        Not a :class:`Press`, because a press is the answer rather than the question - it carries
        moments this box has not sent yet.
        """
        self.touches: list[float] = []


class Presses:
    """Every selection and every touch a box has made, until the pairing can no longer change."""

    def __init__(self, *, window_s: float = CONFIRM_WINDOW_S, back_window_s: float = CONFIRM_BACK_WINDOW_S) -> None:
        self.window_s = window_s
        self.back_window_s = back_window_s
        self._said: dict[str, _Pending] = {}
        """Device id to what that box has said and nobody has decided yet.

        Per speaker rather than one table, because the pairing is about one box's own two ways of
        speaking and two rooms pressing at the same moment have nothing to do with each other. It
        is bounded by the house and every entry is taken out by :meth:`due`, which every pending
        entry reaches: a group that pairs nothing still has a deadline.
        """

    def selection(self, device_id: str, preset_id: int, *, at: float, asked: AskedAtTheFrame) -> None:
        """A box named a preset. Whether a person caused it is not decided here."""
        self._said.setdefault(device_id, _Pending()).selections.append((at, preset_id, asked))

    def user_activity(self, device_id: str, *, at: float) -> None:
        """A box said a person touched it. Which press that belongs to is not decided here."""
        self._said.setdefault(device_id, _Pending()).touches.append(at)

    def pending(self, device_id: str) -> bool:
        """Whether this box has said something that has not been decided yet.

        What a caller does with it: a dialled number may not complete while it is true, because a
        press that has not been paired has no release for the window to be armed at, and the
        digit is still on its way. That is the user's rule of 2026-09-21 stated where it belongs -
        act only once it is known what was pressed - rather than re-timing the digits to fake it.
        """
        return device_id in self._said

    def deadline(self) -> float | None:
        """When the earliest box can be read, or nothing while no box has said anything.

        The service sleeps to this the way it sleeps to the dialler's and the gesture's, and
        re-reads it after every wake: a further frame from that box moves it.
        """
        pending = [self._readable_at(said) for said in self._said.values()]
        return min(pending) if pending else None

    def due(self, *, at: float) -> tuple[tuple[str, Press], ...]:
        """Every press from every box that can now be read, in the order the keys went down.

        A box is read either because nothing further can change what it said - every selection has
        a confirmation, every confirmed press has its release, and no loose touch could still
        confirm anything - or because the last moment any of those could have arrived has passed.
        The first keeps an ordinary press as quick as it ever was; the second stops an echo sitting
        in the table until the box next speaks, which in the first run was two and a half minutes.

        Whichever way it goes the box is emptied, including the frames that paired with nothing: a
        selection nobody confirmed is our own voice and a touch that confirmed nothing is our own
        volume fade, and keeping either would only offer it to whatever the box says next.
        """
        said: list[tuple[str, Press]] = []
        for device_id, pending in sorted(self._said.items()):
            if self._readable_at(pending) > at:
                continue
            said.extend((device_id, press) for press in self._read(pending)[0])
            del self._said[device_id]
        said.sort(key=lambda reported: reported[1].named_at)
        return tuple(said)

    def _readable_at(self, pending: _Pending) -> float:
        """The moment this box can be read: now, if nothing further can change what it said.

        "Now" is spelled as the last frame it sent, which is a moment already past for every
        caller. Only a frame that has NOT arrived can change a reading in which every selection is
        answered, every press has its release and no touch is left loose, and by the time anything
        has arrived this box has been read.
        """
        latest = max([named_at for named_at, _preset, _asked in pending.selections] + pending.touches)
        waiting = self._read(pending)[1]
        return max(waiting) if waiting else latest

    def _read(self, pending: _Pending) -> tuple[list[Press], list[float]]:
        """This box's presses, and every moment at which something could still change them.

        The two come out together because they are two readings of one pass: a selection with no
        confirmation yet, a press whose release may still arrive, and a loose touch that may still
        confirm the next selection are each a press that is NOT final, and each names the moment
        after which it becomes one.

        **A press claims BOTH its touches at once, and that is what keeps an echo out.** Every
        allowed selection-and-touch pair is ranked by how far apart it is, the closest taken first,
        and the key action it belongs to spends its release in the same step. Claiming the
        confirmations first and the releases afterwards leaves every release lying free for a
        moment, and an echo arriving just before a real press then adopts the release of that press
        and becomes a digit - which is the loop of 2026-09-07 coming back through a new door.
        """
        touches = pending.touches
        allowed = sorted(
            (abs(touch - named_at), selection_index, touch_index)
            for selection_index, (named_at, _preset, _asked) in enumerate(pending.selections)
            for touch_index, touch in enumerate(touches)
            if -self.back_window_s <= touch - named_at <= self.window_s
        )
        spent: set[int] = set()
        moments: dict[int, tuple[float, float | None]] = {}
        for _apart, selection_index, touch_index in allowed:
            if selection_index in moments or touch_index in spent:
                continue
            spent.add(touch_index)
            named_at = pending.selections[selection_index][0]
            waiting_selections = [
                other for index, (other, _p, _a) in enumerate(pending.selections) if index not in moments
            ]
            pressed_at, released_at, also = self._both_moments(
                named_at, touch_index, touches, spent, waiting_selections
            )
            if also is not None:
                spent.add(also)
            moments[selection_index] = (pressed_at, released_at)

        presses: list[Press] = []
        waiting: list[float] = []
        for index, (named_at, preset_id, asked) in enumerate(pending.selections):
            found = moments.get(index)
            if found is None:
                waiting.append(named_at + self.window_s)
                continue
            pressed_at, released_at = found
            if released_at is None:
                waiting.append(pressed_at + HOLD_CEILING_S)
                released_at = pressed_at + HOLD_WHEN_UNSEEN_S
            presses.append(
                Press(
                    preset_id=preset_id,
                    named_at=named_at,
                    pressed_at=pressed_at,
                    released_at=released_at,
                    asked=asked,
                )
            )
        # A loose touch has TWO possible futures and must be allowed the longer of them: it may
        # confirm a selection that has not arrived (the backward window), or it may be the key
        # going DOWN whose release is still to come. A slave sends that press 0.23 to 0.447 s
        # ahead of everything else, so a touch given only the backward window is thrown away
        # before its own release arrives and the press reads as having no press moment at all.
        waiting.extend(touch + HOLD_CEILING_S for index, touch in enumerate(touches) if index not in spent)
        presses.sort(key=lambda press: press.named_at)
        return presses, waiting

    def _both_moments(
        self,
        named_at: float,
        touch_index: int,
        touches: list[float],
        spent: set[int],
        waiting_selections: list[float],
    ) -> tuple[float, float | None, int | None]:
        """When the key went down, when it came up, and which further touch that used.

        Which of the pair confirmed the selection is decided by the SIDE it arrived on, because
        that is decided by where the box is standing and nothing else: a box outside the zone
        names the preset before either touch, so the confirmation is the key going down and the
        release is still to come; a slave sends both touches first, so the confirmation is the key
        coming up and the press is the touch in front of it.

        A release that never arrives answers ``None`` so the caller can wait for it, and a press
        that cannot be found - the slave shape with one touch - is taken to be a hold in front of
        the release, which is the same guess made in the other direction.

        **A touch goes to whoever is NEARER**, which is the same rule that decided the confirmation
        and is what keeps a key action from eating its neighbour's. A press reporting only one
        touch is not hypothetical - every press driven through a box's own ``/key`` endpoint does
        it - and without this the release search reaches past the gap and takes the NEXT press's
        confirmation, which then has nothing left to confirm it. Measured on the third run: two of
        its four presses outside the zone disappeared that way.
        """
        confirming = touches[touch_index]
        free = [
            (moment, index)
            for index, moment in enumerate(touches)
            if index not in spent
            and not any(abs(moment - other) < abs(moment - confirming) for other in waiting_selections)
        ]
        if confirming >= named_at:
            behind = [pair for pair in free if HOLD_FLOOR_S <= pair[0] - confirming <= HOLD_CEILING_S]
            if not behind:
                return confirming, None, None
            released_at, also = min(behind)
            return confirming, released_at, also
        ahead = [pair for pair in free if HOLD_FLOOR_S <= confirming - pair[0] <= HOLD_CEILING_S]
        if not ahead:
            return confirming - HOLD_WHEN_UNSEEN_S, confirming, None
        pressed_at, also = max(ahead)
        return pressed_at, confirming, also
