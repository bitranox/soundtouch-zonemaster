"""The one event stream, read in one place: a frame a box sent, or a key a member forwarded.

Seventh in the chain, and the only class allowed to be slow at nothing. A pass can sit ten seconds
inside a station's first bytes, and an event read only afterwards is a decision made on a house
that has since changed its mind - so everything here writes something down and asks for a pass,
and nothing here talks to a speaker.

Three measured rules decide what a press means, and all three are here: a selection alone is not a
press (the master's own station change comes back looking exactly like one), a thumb acts when it
is LET GO because the same key carries two gestures, and a box that is asleep chooses the channel
only while nothing is playing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...domain.calibration import GESTURE_KEYS
from ...domain.enums import FrameKind, KeyState
from ...domain.presses import AskedAtTheFrame
from .dialling import ROTATION_OF, STEP_OF, Dialling

if TYPE_CHECKING:
    from ...domain.events import KeyPress, SpeakerEvent

__all__ = ["KeyReading"]


class KeyReading(Dialling):
    """Everything a speaker says, turned into a digit, a step, a flag or nothing at all."""

    async def _read_what_the_speakers_say(self) -> None:
        """The one stream, read in one place: a frame from a box, or a key a member forwarded.

        Reading must never wait for a pass. A pass can sit ten seconds inside a station's first
        bytes, and an event read only afterwards is a decision made on a house that has since
        changed its mind - a box switched to AUX in those seconds would be taken over anyway.
        What is fast (writing down what a box said) stays here; what is slow (talking to speakers)
        happens in the pass, which this only asks for.
        """
        while True:
            event = self._named(await self.events.get())
            self._a_selection_or_its_confirmation(event)
            self._a_volume_report(event)
            if event.key is not None:
                self._a_key(event.device_id, event.key, at=event.received_at)
                # Said out loud whatever it was, because the path it took is the one M0 had to
                # measure, and a live run has to be able to see that a press arrived and which box
                # it was read as.
                self.log("key", f"{self._name(event.device_id)}: {event.key.key} {event.key.state}")
            self._noted_switched_on(event)
            self.policy.observe(event)
            self._wanted.set()

    def _a_selection_or_its_confirmation(self, event: SpeakerEvent) -> None:
        """A box named a preset, or said that a person touched it. Only the pair is a press.

        A selection ALONE says nothing about who caused it: the master's own station change comes
        back from every slave in exactly the frame a press produces, which ran the house's first
        zone into 28 station changes in 45 seconds on 2026-09-07. ``presses`` holds the rule; this
        is where the two frames it pairs are recognised, and it is the ONE place a preset NUMBER
        arrives at all - the forwarded press on /slaveMsg carries the ContentItem and no number,
        which is why every box has an observer.

        NEITHER frame decides anything here. Which of the two arrives second depends on where the
        box is standing - outside the zone it names the preset and then confirms it, as a SLAVE it
        confirms first. So both sides are written down and the pairing happens when nothing further
        can change it, which is the user's rule of 2026-09-21: act only once it is known whether
        that was one key, several, or a key held.

        Ahead of ``policy.observe`` on purpose, and that matters MORE now rather than less. Neither
        of these two frames names a source, so the membership state is the same either way, and
        asking here keeps the answer to "was this box asleep" the one that was true when the box
        spoke. The press it belongs to may be decided seconds later, by which time the house has
        heard the box leave standby - so the answer is carried with the selection rather than asked
        again, which is what :class:`AskedAtTheFrame` is for.
        """
        if event.preset_id is None and event.kind != FrameKind.USER_ACTIVITY_UPDATE:
            return
        if event.preset_id is not None and self._claimed_as_our_selection(event.device_id, at=event.received_at):
            # Our own /select coming back from a box outside the zone. Said once per /select,
            # because it is the line that explains why a preset the journal shows was not dialled.
            self.log(
                "dial", f"{self._name(event.device_id)} named preset {event.preset_id}: our own /select, not a press"
            )
            return
        if event.preset_id is None and self._claimed_as_our_echo(event.device_id, at=event.received_at):
            # Our own volume write coming back, not a finger: a join produces nine of these and
            # one of them once confirmed a press nobody made. Not logged - a line per step of every
            # join would bury the presses somebody did make.
            return
        # Read back on BOTH sides of writing the frame down, and the order is the point. First, so
        # that a box whose deadline passed while it was silent is decided BEFORE this new frame can
        # join it. Then again, so that a frame which completes what the box was saying is acted on
        # at once rather than at some later tick - which is what keeps an ordinary press exactly as
        # quick as it was before anything was deferred.
        self._read_what_the_presses_now_say(at=event.received_at)
        if event.preset_id is not None:
            self._presses.selection(
                event.device_id,
                event.preset_id,
                at=event.received_at,
                asked=self._what_the_house_says_of(event.device_id),
            )
        else:
            self._presses.user_activity(event.device_id, at=event.received_at)
        self._read_what_the_presses_now_say(at=event.received_at)
        # The frame just written down has armed a deadline nobody is sleeping to yet.
        self._dialled.set()

    def _a_volume_report(self, event: SpeakerEvent) -> None:
        """A box said what level it is at: written down, and a house step when it is one.

        Every box's report is written down, our own writes' included, because the next house step
        a box takes starts from the level it is at. Only a report from the box whose tapped thumb
        opened the window is a step (``domain/housevolume.py``), and only while that box is in the
        zone and not fading in: a box being faded reports every level WE set, and those would come
        back as steps nobody made.
        """
        if event.volume is None:
            return
        step = self._house_volume.reported(event.device_id, event.volume, at=event.received_at)
        if step is None or event.device_id not in self._joined or event.device_id in self._muted:
            return
        self._house_stepped(event.device_id, step)

    def _what_the_house_says_of(self, device_id: str) -> AskedAtTheFrame:
        """The two answers a press needs, taken now because now is when they are true.

        Both are asked here and CARRIED rather than asked again where they are used, and the whole
        of that is in :class:`AskedAtTheFrame`: the pairing finishes later than the frame, and by
        then the house has heard this box leave standby.
        """
        return AskedAtTheFrame(
            asleep=self.policy.is_asleep(device_id),
            may_choose_the_channel=self._may_choose_the_channel(device_id),
        )

    def _a_key(self, device_id: str, key: KeyPress, *, at: float) -> None:
        """One forwarded key, written down as a gesture: nothing here decides that it was a hold.

        A key still down when the hold threshold passes becomes due in the waiting loop, which is
        the only place that can act at a moment no frame arrived at - a box sends nothing while a
        key is held, so there is no event to hang it on. What happens here is the two halves a box
        DOES send: the press starts the gesture, and a release inside the threshold ends it as a tap.

        Nothing at all while a calibration runs, not even the recording. The person is pressing to
        be measured, and a press that could still become a hold would fire an action between the
        four presses of the gesture that started it.
        """
        if key.state == KeyState.PRESS:
            if self._calibration.is_running():
                # Ignored, never a reason to end the calibration (user, 2026-09-22), but said once
                # per press: the person pressing cannot otherwise tell a key the calibration
                # swallowed from a box that stopped listening, and presses again.
                self.log("dial", f"{self._name(device_id)}: {key.key} does nothing while a calibration runs")
                return
            self._longpresses.pressed(device_id, key.key, at=at)
            # The loop sleeps to the earliest deadline and this press just made a new one.
            self._dialled.set()
            self._a_key_press(device_id, key.key, at=at)
            return
        # A RELEASE is read even while a calibration runs, because it closes a press taken before
        # the calibration began - the fourth press of the gesture is exactly that, and swallowing
        # its release left a key the service believed was still down, which fired half a second
        # later and moved the house (caught by the gesture test, 2026-09-20).
        tap = self._longpresses.released(device_id, key.key, at=at)
        if tap is None:
            return
        if self._calibration.is_running():
            return
        self._a_tapped_key(device_id, key.key, pressed_at=at - tap.seconds, released_at=at)

    def _a_tapped_key(self, device_id: str, key: str, *, pressed_at: float, released_at: float) -> None:
        """A key let go inside the hold threshold, and only the thumbs act on one.

        A step key's tap was already collected on its way DOWN, which is what keeps a tap exactly
        as quick as it has always been - the window it waits out is the one it always waited out.
        A thumb collects nothing at the press, because a tap and a hold on it mean two unrelated
        things and neither may be guessed before the threshold decides.
        """
        if key in ROTATION_OF:
            self._thumb_tapped(device_id, key, pressed_at=pressed_at, released_at=released_at)

    def _a_key_press(self, device_id: str, key: str, *, at: float) -> None:
        """A key on its way down: a step collected, or the fourth press of the gesture.

        Every key comes through here, thumbs included, and a key that is not one of the gesture's
        two breaks the run rather than joining it - which is what a thumb pressed in the middle of
        four alternating steps has always done.
        """
        if key not in GESTURE_KEYS:
            self._gesture.forget(device_id)
            return
        self._dialler.step(device_id, STEP_OF[key], at=at)
        self._dialled.set()
        if self._gesture.saw(device_id, key, at=at):
            # The four presses were a gesture and not four jumps. They sum to zero, but only if
            # all four land in one jump, so what was collected is dropped rather than trusted.
            self._dialler.forget_steps(device_id)
            # And whatever is under a finger right now: the four presses are over, and a key still
            # down would otherwise become a hold in the middle of the measurement.
            self._longpresses.forget(device_id)
            self._calibration.begin(device_id, at=at)
            self.log("dial", f"{self._name(device_id)}: calibration started, press the number keys as you would")
