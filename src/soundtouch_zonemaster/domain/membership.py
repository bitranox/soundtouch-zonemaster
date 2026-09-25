"""Who belongs in the zone, decided from what the speakers said and from nothing else.

A software master collapses the four-way classification the hardware one needed: a member sends
its press to us explicitly, so the only question left is who WANTS to be in. The design's four
cases are STANDBY (out, and POWER on brings it back), AUX or Bluetooth (out until the person comes
back by themselves), unreachable (left alone until a timeout), and everything else.

That last case is where the design answers itself twice: it says "anything else is a member", and
it justifies the AUX rule with "somebody listening on the aux input is not overridden". For a
source nobody has measured those two point opposite ways. Settled with the user 2026-09-07: a
speaker is a member only when it is known to be playing OURS. A wrong answer then costs a box that
could have played and did not, instead of music taken away from somebody in the room.

That choice is also why no Bluetooth spelling appears in this file. Under a blocklist its exact
string would have to be right or the rule would silently not fire, and it has never been measured
here; under this one it needs no entry at all.

**Playing our stream is not a source string.** Measured 2026-09-06: a box playing the master's
stream and a box playing its own internet radio both report ``LOCAL_INTERNET_RADIO``, and what
separates them is the device id inside the ``<nowPlaying>`` element - its own when it is alone,
the master's when it is ours.

**A wake has a lifetime, and only internet radio carries it.** A press at a sleeping box proves
somebody is there seconds before the box says anything itself - measured 2026-09-08, waits of
4.45 s, 3.5 s and 0.17 s between the press and the box's first report - so the press marks a wake
(:meth:`Membership.woke`) and the wake counts for :data:`WAKE_WINDOW_S`. What a box reports between
leaving standby and settling on a source is still uncaptured, so the window is bounded by the
sources themselves: it begins on standby-to-internet-radio, survives further internet radio (the
box's OWN station, which is what it names first), and is ended at once by anything else. A box
woken directly onto AUX is therefore never taken, where it used to be a member for one frame.

Nothing here acts. It answers which speakers should be in the zone; ``ZoneMaster.add_slave`` and
``slave_left`` remain the only things that change one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .enums import SourceName

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable, Sequence

    from .events import SpeakerEvent
    from .speakers import Speaker

__all__ = ["UNREACHABLE_TIMEOUT_S", "WAKE_WINDOW_S", "Membership"]

UNREACHABLE_TIMEOUT_S = 900.0
"""How long a speaker may say nothing before it stops counting as a member.

Fifteen minutes because one box is on radio and drops out for minutes at a time (OPEN-WORK rank
50). A dead spot must not empty the zone, and the cost of waiting is a speaker that is listed as a
member while it is off the air, which changes nothing anybody can hear.
"""


WAKE_WINDOW_S = 20.0
"""How long a wake counts as a reason to be in the zone.

A wake is a guess with evidence behind it - somebody pressed a key - and this is what bounds the
guess. It has to outlast the longest take-in, because ``_take_in`` waits up to
:data:`soundtouch_zonemaster.adapters.soundtouch.zone_master.FIRST_BYTES_TIMEOUT_S` (10 s) for the
station's first bytes and then asks who belongs AGAIN before it sends anything: a shorter window
would drop the very box the stream
was being fetched for. Twenty seconds costs nothing after that, because a box that joined reports
our stream and is held by :meth:`Membership._plays_our_stream` instead.
"""


@dataclass
class _Heard:
    """The last thing one speaker said about itself."""

    last_seen: float
    source: str | None = None
    stream_owner: str | None = None
    woke_at: float | None = None
    """When this box was last seen waking, or ``None`` while it is not.

    Set by the standby-to-something transition and by :meth:`Membership.woke`, which is a press a
    person made at a sleeping box. Cleared by any source that is not internet radio - so the box's
    OWN station, which is what a woken box names first, does not end its own wake.
    """
    remembered: bool = False
    """Set only by :meth:`Membership.restore`, and cleared by the first source the box names."""


class Membership:
    """The decision, and the little state it is made from. No I/O, so a test needs no speaker."""

    def __init__(
        self,
        *,
        master_device_id: str,
        now: Callable[[], float],
        unreachable_timeout_s: float = UNREACHABLE_TIMEOUT_S,
        wake_window_s: float = WAKE_WINDOW_S,
        consoles_allowed: Collection[str] = (),
    ) -> None:
        self.master_device_id = master_device_id
        self.now = now
        self.unreachable_timeout_s = unreachable_timeout_s
        self.wake_window_s = wake_window_s
        self.consoles_allowed = frozenset(consoles_allowed)
        self._heard: dict[str, _Heard] = {}

    def observe(self, event: SpeakerEvent) -> None:
        """Take in one event. Anything that names a speaker proves it is alive."""
        if not event.device_id:
            return
        heard = self._heard.get(event.device_id)
        if heard is None:
            heard = _Heard(last_seen=event.received_at)
            self._heard[event.device_id] = heard
        heard.last_seen = event.received_at
        if event.source is None:
            return
        if event.source == SourceName.LOCAL_INTERNET_RADIO:
            # Internet radio is the one source that may BEGIN a wake and the one that may not end
            # one. It begins it because that is what a box switched on names first, its own last
            # station; it may not end it because under the old single-frame flag exactly that
            # report cleared the wake, and a box that spoke twice before a pass reached it fell
            # out again and could only be taken in by being switched off and on.
            if heard.source == SourceName.STANDBY:
                heard.woke_at = event.received_at
        else:
            # Everything else ends the wake at once, the window notwithstanding. AUX is why: with
            # a lifetime instead of a single frame, a box woken straight onto its aux input would
            # otherwise be held as a member for the whole window and taken over, and the rule the
            # user settled is that music is never taken away from somebody in the room. It costs
            # the one chance a wake onto an unmeasured source used to have, in the safe direction.
            heard.woke_at = None
        heard.source = event.source
        heard.stream_owner = event.stream_owner
        heard.remembered = False

    def woke(self, device_id: str, *, at: float) -> None:
        """A person pressed something at a box that still reads STANDBY.

        A preset, usually - but a box switched on resumes whatever it had last, and that need not
        be one of its six presets. It reports such a selection as preset 0, and the press is a
        person standing there either way.

        The press is what proves somebody is there, and it arrives seconds before the box says so
        itself: measured in the eighth live run 2026-09-08, a box pressed at 03:00:42.99 named no
        source until 03:00:46.472, and the service spent those 3.5 s knowing about the press and
        waiting anyway. Three observations of that wait - 4.45 s, 3.5 s and 0.17 s - so it is not
        a constant to design around either.

        It marks the wake and NOTHING else. In particular it does not touch ``source``, because
        :meth:`is_asleep` is what decides whether a press may choose the channel for the whole
        house, and a press that made the box read as awake would let a wake in one room move every
        other room to whatever that one last played.

        A box nobody has heard of is not introduced by a press. Liveness is not membership here
        either, and the service only asks this about a box it already knows is asleep.
        """
        heard = self._heard.get(device_id)
        if heard is not None:
            heard.woke_at = at

    def seen(self, device_id: str, *, at: float) -> None:
        """Note that a speaker is there, and nothing about what it is playing.

        A member playing our stream has nothing to say on its notification channel and says
        nothing, so without this it would age out of the zone while it is playing exactly what we
        sent it - the music stopping with nobody having touched anything. What proves it is there
        is the state report it sends on the transport channel about once a second.

        A box nobody has heard of stays unknown: liveness is not membership, and a report says
        only that something is at the other end.
        """
        heard = self._heard.get(device_id)
        if heard is not None:
            heard.last_seen = at

    def is_asleep(self, device_id: str) -> bool:
        """Whether the last source this box named was STANDBY.

        Asked while a box is WAKING, which is the only moment it answers something the caller
        cannot see for itself: a selection frame names no source, so this still says STANDBY over
        the 101 to 199 ms between the frame in which a woken box names its preset and the
        ``nowPlayingUpdated`` in which it says it has left standby (four wakes, 2026-09-07).

        A box nobody has heard a source from is NOT asleep. The service asks every speaker what it
        is playing before it reads its first frame, so the unknown case is a box that appeared
        between two registry reads, and treating that as awake costs at most one channel change
        the person asked for.
        """
        heard = self._heard.get(device_id)
        return heard is not None and heard.source == SourceName.STANDBY

    def restore(self, device_ids: Iterable[str], *, at: float) -> None:
        """Take a previous run's members as members again, until the boxes themselves say otherwise.

        A restart must not empty the zone, and the boxes are under no obligation to say anything
        after one: a box playing our stream sends nothing until something changes, and a box off
        the air says nothing at all. So what was recorded counts - and the first thing a box says,
        a frame or its answer to the start-up question, replaces it.
        """
        for device_id in device_ids:
            self._heard.setdefault(device_id, _Heard(last_seen=at, remembered=True))

    def target_members(self, speakers: Sequence[Speaker]) -> frozenset[str]:
        """Which of the speakers the registry lists should be in the zone right now."""
        return frozenset(s.device_id for s in speakers if self._belongs(s))

    def _belongs(self, speaker: Speaker) -> bool:
        if speaker.is_console and speaker.device_id not in self.consoles_allowed:
            return False
        heard = self._heard.get(speaker.device_id)
        if heard is None:
            return False
        if self.now() - heard.last_seen > self.unreachable_timeout_s:
            return False
        waking = self._still_waking(heard)
        if heard.source == SourceName.STANDBY and not waking:
            # A live wake outranks the source, and only here: the box was pressed and has not yet
            # said what it is doing, which is the whole of rank 16. A box that REPORTS standby
            # after its wake has gone back to sleep, and ``observe`` has cleared the wake for it.
            return False
        return self._plays_our_stream(heard) or waking or heard.remembered

    def _still_waking(self, heard: _Heard) -> bool:
        """Whether this box's wake is recent enough to still count as a reason to be in."""
        return heard.woke_at is not None and self.now() - heard.woke_at <= self.wake_window_s

    def _plays_our_stream(self, heard: _Heard) -> bool:
        """Our stream, not merely internet radio. The owner is the whole difference."""
        return heard.source == SourceName.LOCAL_INTERNET_RADIO and heard.stream_owner == self.master_device_id
