"""The speaker documents this master reads, declared as models rather than looked up by hand.

Each class here is one document shape a speaker sends, with the fields this program actually uses
and nothing else: a model ignores every element and attribute it does not name, so a firmware that
carries more is read exactly as before. Everything is optional unless the code cannot act without
it, because a speaker that answers half a document is a speaker to work with rather than an error
to raise - the one exception is :class:`TrackData`, where three counters out of four are not a
position and must not be read as one.

The reading functions take the ROOT ELEMENT rather than the text, so that :func:`xmlread.parse`
stays the only way a document is parsed and every size, DOCTYPE and depth bound applies to these
too. Each answers ``None`` when the root is not the document it describes or when a field will not
validate, which is the same answer the callers give an unreadable body.

What stays outside a model, deliberately: ``deviceID`` and ``source`` are read with
``xmlread.attribute_anywhere``, because the patterns they replace searched the whole frame and a
frame carries them at a depth that differs by kind; and the ``<content>`` element of a select is
kept as an ELEMENT, because the master hands the document on to speakers and re-serialises it
rather than reading values out of it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

from pydantic import NonNegativeInt, ValidationError
from pydantic_xml import BaseXmlModel, attr, element
from pydantic_xml.errors import ParsingError

from ...domain.station import StationRequest, station_request_from
from .xmlread import parse

if TYPE_CHECKING:
    from .xmlread import Element

__all__ = [
    "ContentItem",
    "KeyData",
    "TrackData",
    "Volume",
    "content_item",
    "key_data",
    "station_request",
    "track_data",
    "volume",
]


class ContentItem(BaseXmlModel, tag="ContentItem", search_mode="unordered"):
    """What a speaker plays, as it stores it in a preset and sends it in a select.

    ``location`` is the stream the master would fetch, and its absence is what says the item names
    nothing playable. The values arrive unescaped, since the parser resolves the entities the
    document escaped - which is the mirror of ``domain.xmlfmt.attr`` writing them.
    """

    location: str | None = attr(default=None)
    item_name: str | None = element(tag="itemName", default=None)


class KeyData(BaseXmlModel, tag="keyData"):
    """One key a slave forwarded: which key, pressed or released, and which remote sent it.

    ``state`` is REPORTED, never filtered: every key arrives twice, as ``press`` and then
    ``release``, and dropping one of them makes the double arrival invisible to whoever later has
    to count presses.
    """

    state: str = attr(default="")
    sender: str = attr(default="")
    key: str = ""


class Volume(BaseXmlModel, tag="volume", search_mode="unordered"):
    """A speaker's ``/volume`` answer. Only the level it is actually at matters here."""

    actual: int | None = element(tag="actualvolume", default=None)


class TrackData(BaseXmlModel, tag="AudioServerMsgTrackData"):
    """The decoder counters a slave sends with every state report (REPORT.md S6).

    All four are required: the timeline is placed on ``frame_offset``, and a report carrying only
    some of them is a report this master must not read as a position. They are counters, so a
    NEGATIVE one is refused with the whole record rather than read - which is also what the digits
    only pattern this replaced did, and a minus sign in a counter means the report is not one.
    """

    frame_offset: NonNegativeInt = attr()
    byte_offset: NonNegativeInt = attr()
    time_offset: NonNegativeInt = attr()
    latency_microsecs: NonNegativeInt = attr()


def key_data(element: Element) -> KeyData | None:
    """A ``keyData`` element as a record, or ``None`` when it is not one."""
    return _read(KeyData, element)


def volume(root: Element) -> Volume | None:
    """A ``/volume`` answer as a record, or ``None`` when it is not one."""
    return _read(Volume, root)


def track_data(root: Element) -> TrackData | None:
    """A ``trackData`` fragment as a record, or ``None`` when it is not one."""
    return _read(TrackData, root)


def content_item(root: Element) -> ContentItem | None:
    """A ``<ContentItem>`` as a record, or ``None`` when it is not one."""
    return _read(ContentItem, root)


def station_request(item_xml: str) -> StationRequest | None:
    """One ``<ContentItem>`` document as a station to play, or ``None`` when it names none.

    Both halves of the old master call this: the zone master when a slave selects something, and
    the speaker HTTP helper when a preset is read off a box. It sits here, in the adapter that owns
    the parser, so that neither of those two has to import the other, and it leaves the decision
    itself to ``domain.station.station_request_from``.

    A document that will not parse names nothing at all, which is the same answer as a ContentItem
    carrying no location.
    """
    root = parse(item_xml)
    item = content_item(root) if root is not None else None
    if item is None:
        return None
    return station_request_from(location=item.location, name=item.item_name, content_item_xml=item_xml)


class _ReadsAnXmlTree(Protocol):
    """What a pydantic-xml model class offers this module, with the types its own stubs omit."""

    def from_xml_tree(self, root: Element) -> BaseXmlModel: ...


def _read[ModelT: BaseXmlModel](model: type[ModelT], root: Element) -> ModelT | None:
    """One model from a parsed tree, with both ways it can refuse answered as ``None``.

    ``ParsingError`` is what pydantic-xml raises for a root element of another name, and
    ``ValidationError`` for a field that will not convert. The callers treat both the same way
    they treat a document that did not parse at all.

    :class:`_ReadsAnXmlTree` is why this indirection exists at all. pydantic-xml leaves the
    ``root`` parameter of ``from_xml_tree`` unannotated, so under pyright strict the method is
    partially unknown and every site that touched it would inherit that. Naming the shape once, as
    a protocol the model class satisfies, keeps all of them fully typed and silences nothing.
    """
    reader = cast("_ReadsAnXmlTree", model)
    try:
        return cast("ModelT", reader.from_xml_tree(root))
    except (ParsingError, ValidationError):
        return None
