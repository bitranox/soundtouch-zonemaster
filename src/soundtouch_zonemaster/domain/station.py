"""A station to play, and the one way to read one out of a ``<ContentItem>``.

Both records are the vocabulary the rest of the program speaks about "what is playing": a preset
read off a speaker, a slave's select and :meth:`ZoneMaster.play` all name a
:class:`StationRequest`, and what comes back once the master has registered it is a
:class:`Station`. Fetching one, and the URL resolution that turns a playlist into a stream, are the
source adapter's (``adapters/soundtouch/source.py``).

The two records were already frozen dataclasses in the archive, so nothing here is converted - only
moved out of the adapter that happened to define them.

:func:`station_request_from` is the RULE a ContentItem is read by, and it is here rather than in an
adapter because BOTH halves that ``master`` split into apply it: the zone master when a slave
selects something, and the speaker HTTP helper when a preset is read off a box. What the document
SAYS is read by the adapter that owns the parser (``adapters/soundtouch/xmlmodels.station_request``),
because the document is XML and arrives from the LAN; what it MEANS is decided here.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Station", "StationRequest", "station_request_from"]


@dataclass(frozen=True)
class StationRequest:
    """A station to play, before the master gives it a ``url_id``.

    Exactly what a speaker preset or a slave's select names, and what :meth:`ZoneMaster.play`
    takes. Named fields rather than three loose strings, because all three are ``str`` and no
    checker can tell the URL from the name once they are positional.
    """

    playback_url: str
    name: str
    content_item_xml: str


@dataclass(frozen=True)
class Station:
    """One selected station: the local-service playback URL and the ContentItem the speakers use."""

    url_id: int
    playback_url: str
    name: str
    content_item_xml: str  # the <ContentItem ...>...</ContentItem> exactly as a speaker stores it

    def as_request(self) -> StationRequest:
        """This station as the request that would start it again, without its ``url_id``."""
        return StationRequest(playback_url=self.playback_url, name=self.name, content_item_xml=self.content_item_xml)


def station_request_from(*, location: str | None, name: str | None, content_item_xml: str) -> StationRequest | None:
    """What a ``<ContentItem>``'s two values mean: a station to play, or nothing at all.

    The one place this decision is made. A slave's select and a speaker's preset carry the same
    element and were read separately once, which is how they came to disagree about what an item
    with no ``itemName`` is called: it is called by its URL.

    ``None`` rather than a raise when there is no location, because the two callers answer that
    differently - a select from a slave is logged and ignored, a preset that cannot be played ends
    the run. An EMPTY location counts as none: it names nothing to fetch.

    Both values arrive as the characters they stand for, the parser having resolved what the
    document escaped, so nothing is unescaped a second time here. The document itself is kept
    verbatim, because it is what a speaker is handed to play the station again.
    """
    if not location:
        return None
    return StationRequest(
        playback_url=location,
        name=name if name else location,
        content_item_xml=content_item_xml,
    )
