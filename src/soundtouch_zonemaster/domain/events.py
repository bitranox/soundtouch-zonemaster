"""What a speaker told us, whichever of the two ways it arrived.

M0 measured that the two inputs travel different paths. A key with no content - next, previous,
both thumbs - reaches the master as ``POST /slaveMsg`` with ``action="key"`` and a ``keyData``
element. The preset NUMBER never appears there: it is on the speaker's own notification WebSocket,
which the observer holds. Both become one of these, so the service reads ONE stream instead of
joining two half-streams by speaker and by clock.

It lives down here, below both of the modules that produce it, because it is a record and nothing
else - the same place ``reports.SlaveState`` sits for what a slave says about its playback.

Both records are keyword-only. That is what a pydantic model was, so every existing call site
already passes keywords, and it is also what lets the fields keep the order they were written in:
``frame`` is required and sits after four optional ones, which a positional dataclass refuses.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["KeyPress", "SpeakerEvent"]


@dataclass(frozen=True, slots=True, kw_only=True)
class KeyPress:
    """One press or release of one key, as the speaker forwarded it.

    The three fields always arrive together, so they are one record rather than three optional
    fields on the event: a key with no state is not a thing a speaker can send, and making that
    unrepresentable is cheaper than checking for it at every reader.

    Every key arrives TWICE, once as ``press`` and once as ``release``, measured 365 to 444 ms
    apart on 2026-09-06. The state is carried rather than filtered here, because a parser that
    drops the release makes the double arrival invisible to anything that later needs to see it;
    the service filters on ``press``.

    ``sender`` read ``IrRemote`` for every press in that run, which came from the remote. Whether
    a press on the box's own buttons says the same is NOT tested, so nothing branches on it.
    """

    key: str
    state: str
    sender: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SpeakerEvent:
    """One thing one speaker did, with the time it arrived HERE.

    The arrival time is this machine's clock rather than the speaker's, because what it is used
    for is how far apart two presses land at the service once the radio has had its say. M0
    measured that spread at 211 to 398 ms across eleven pairs.

    ``preset_id`` is set by a selection on the notification channel, ``key`` by a forwarded press
    on the HTTP face; a frame that is neither still becomes an event, because a dropped frame and
    a silent speaker look identical afterwards and only one of them is a problem.

    ``source`` and ``stream_owner`` come from a ``nowPlayingUpdated`` frame and are what
    membership is decided on. They are read from the ``<nowPlaying>`` element itself, never from
    the ``ContentItem`` inside it: a selection frame carries a preset's source too, and that is
    what the box COULD play rather than what it IS playing.

    ``stream_owner`` is the device id the ``<nowPlaying>`` names, measured 2026-09-06: a box in
    standby names ITSELF, and a box playing the master's stream names the MASTER. That is what
    separates "playing our stream" from "playing its own internet radio", which no source string
    can tell apart - both read ``LOCAL_INTERNET_RADIO``.

    ``volume`` is the level a ``volumeUpdated`` frame reports the box is at. A volume key is never
    forwarded to the master - the box changes its own volume and reports the result - so this is
    the only way the house hears somebody turn one up, which is what the house-volume rule steps
    every other box by (``domain/housevolume.py``).
    """

    received_at: float
    speaker: str
    device_id: str
    kind: str
    preset_id: int | None = None
    key: KeyPress | None = None
    source: str | None = None
    stream_owner: str | None = None
    volume: int | None = None
    frame: str
