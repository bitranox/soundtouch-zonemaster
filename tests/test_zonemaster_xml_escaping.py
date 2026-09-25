"""The documents the master serves must stay XML when the values in them are not tame.

Every assertion here parses the result with a real XML parser rather than matching substrings: a
string check passes on output no slave could read, which is the failure this file exists to catch.
An ampersand in a station name is not an attack, it is Tuesday - "Simon & Garfunkel" is enough to
end a slave's parse at the ampersand.
"""

from __future__ import annotations

from xml.etree import ElementTree

import pytest

from soundtouch_zonemaster.adapters.soundtouch.http_api import status_xml
from soundtouch_zonemaster.adapters.soundtouch.zone_master import Slave, ZoneMaster
from soundtouch_zonemaster.domain import xmlfmt
from soundtouch_zonemaster.domain.station import Station

HOSTILE = "&<>\"'"
NAMES = ["Simon & Garfunkel", "AC/DC & Friends", "</track><injected>x", 'quote " inside', "plain name"]


def _master(**kw: object) -> ZoneMaster:
    return ZoneMaster(bind_ip="10.0.0.1", device_id="AABBCC000001", log=lambda _k, _t: None, **kw)  # type: ignore[arg-type]


def _station(name: str) -> Station:
    return Station(url_id=1, playback_url="http://example.invalid/s", name=name, content_item_xml="<ContentItem />")


@pytest.mark.parametrize("name", NAMES)
def test_now_playing_stays_parseable_whatever_the_station_is_called(name: str) -> None:
    m = _master()
    m.station = _station(name)
    root = ElementTree.fromstring(m.now_playing_xml())  # noqa: S314 - our own output, and parsing IS the assertion
    assert root.findtext("track") == name, "the name must survive escaping unchanged"
    assert root.findtext("stationName") == name


def test_a_station_name_that_closes_the_tag_does_not_close_the_tag() -> None:
    """The concrete injection: unescaped, this ends <track> early and adds an element."""
    m = _master()
    m.station = _station("</track><injected>PWNED")
    root = ElementTree.fromstring(m.now_playing_xml())  # noqa: S314 - our own output
    assert root.find("injected") is None, "the injected element must not exist"
    assert root.findtext("track") == "</track><injected>PWNED"


def test_the_content_item_is_still_echoed_as_markup() -> None:
    """The deliberate exception: a real master echoes the slave's ContentItem back as markup."""
    m = _master()
    m.station = Station(
        url_id=1,
        playback_url="http://example.invalid/s",
        name="n",
        content_item_xml='<ContentItem source="X"><itemName>N</itemName></ContentItem>',
    )
    root = ElementTree.fromstring(m.now_playing_xml())  # noqa: S314 - our own output
    item = root.find("ContentItem")
    assert item is not None and item.get("source") == "X", "the fragment stays a fragment"


def test_the_zone_document_survives_a_slave_with_a_hostile_id() -> None:
    m = _master(slaves={"10.0.0.9": Slave(ip=f'10.0.0.9" evil="{HOSTILE}', device_id=f"id{HOSTILE}")})
    root = ElementTree.fromstring(m.zone_xml())  # noqa: S314 - our own output
    member = root.find("member")
    assert member is not None
    assert member.get("evil") is None, "an attribute must not be injectable through a value"
    assert member.text == f"id{HOSTILE}"


def test_the_catch_all_status_reply_escapes_the_request_path() -> None:
    """status_xml answers every unhandled path, and the path comes straight off the request line."""
    root = ElementTree.fromstring(status_xml("/x</status><injected>y"))  # noqa: S314 - our own output
    assert root.tag == "status"
    assert root.text == "/x</status><injected>y"


def test_info_and_standby_documents_parse() -> None:
    m = _master()
    assert ElementTree.fromstring(m.info_xml()) is not None  # noqa: S314 - our own output
    assert ElementTree.fromstring(m.now_playing_xml()) is not None, "no station selected"  # noqa: S314


@pytest.mark.parametrize("raw", [HOSTILE, "plain", "a&b", "<x>", 'say "hi"'])
def test_the_escapers_round_trip_through_a_parser(raw: str) -> None:
    doc = f'<e a="{xmlfmt.attr(raw)}">{xmlfmt.text(raw)}</e>'
    node = ElementTree.fromstring(doc)  # noqa: S314 - our own output
    assert node.get("a") == raw
    assert node.text == raw


def test_a_content_item_without_a_location_names_nothing() -> None:
    """Both callers ask the same question here; only their answers differ."""
    from soundtouch_zonemaster.adapters.soundtouch.xmlmodels import station_request

    assert station_request("<ContentItem><itemName>N</itemName></ContentItem>") is None


def test_a_content_item_without_a_name_falls_back_to_its_url() -> None:
    """The two parsers used to disagree here: one said the URL, the other said a bare question mark."""
    from soundtouch_zonemaster.adapters.soundtouch.xmlmodels import station_request

    request = station_request('<ContentItem location="http://example.invalid/s" />')
    assert request is not None
    assert request.name == "http://example.invalid/s"


def test_an_ampersand_in_a_station_url_is_unescaped_once_and_stays_parseable() -> None:
    """The URL arrives XML-escaped and is used raw; the name derived from it goes back through text()."""
    from soundtouch_zonemaster.adapters.soundtouch.xmlmodels import station_request

    request = station_request('<ContentItem location="http://x.invalid/s?a=1&amp;b=2" />')
    assert request is not None
    assert request.playback_url == "http://x.invalid/s?a=1&b=2", "unescaped once, for the fetch"

    m = _master()
    m.station = Station(url_id=1, playback_url=request.playback_url, name=request.name, content_item_xml="")
    root = ElementTree.fromstring(m.now_playing_xml())  # noqa: S314 - our own output
    assert root.findtext("track") == "http://x.invalid/s?a=1&b=2", "and re-escaped for the document"


@pytest.mark.parametrize("raw", ['he said "hi"\nline2', "both \" and ' with\nnewline", "tab\there"])
def test_an_attribute_keeps_its_control_characters_through_both_branches(raw: str) -> None:
    """quoteattr picks single quotes when a value holds a double quote, and that branch is ours.

    Without the entity map the branch leaves a literal newline in the attribute, which a parser
    normalises to a space - so the value comes back changed and only for values holding a quote.
    """
    root = ElementTree.fromstring(f'<e a="{xmlfmt.attr(raw)}">x</e>')  # noqa: S314 - our own output
    assert root.get("a") == raw


# --- control characters: XML 1.0 cannot carry them at all, not even as a reference ---------------

C0 = "A\x00B\x08C\x0bD\x1fE"
"""C0 control characters with the three XML allows left out. ``&#0;`` is not an escape for NUL:
XML 1.0's Char production excludes these codepoints outright, so a document holding one is not
ill-escaped, it is not XML. The only thing a builder can do with them is not write them."""


def test_a_control_character_between_tags_leaves_a_document_a_parser_accepts() -> None:
    node = ElementTree.fromstring(f"<e>{xmlfmt.text(C0)}</e>")  # noqa: S314 - our own output
    assert node.text == "ABCDE", "the illegal characters are dropped, the rest is untouched"


def test_a_control_character_in_an_attribute_leaves_a_document_a_parser_accepts() -> None:
    node = ElementTree.fromstring(f'<e a="{xmlfmt.attr(C0)}" />')  # noqa: S314 - our own output
    assert node.get("a") == "ABCDE"


def test_the_control_characters_xml_does_allow_are_not_swept_up_with_the_rest() -> None:
    """Tab and newline are legal, so dropping them would be this fix breaking working documents."""
    raw = "a\tb\nc"
    node = ElementTree.fromstring(f'<e a="{xmlfmt.attr(raw)}">{xmlfmt.text(raw)}</e>')  # noqa: S314 - our own output
    assert node.text == raw
    assert node.get("a") == raw, "quoteattr writes them as entities so an attribute keeps them"


def test_a_carriage_return_survives_an_attribute() -> None:
    """Between tags a parser normalises it to a newline, which is XML's rule and not ours."""
    node = ElementTree.fromstring(f'<e a="{xmlfmt.attr("a\rb")}" />')  # noqa: S314 - our own output
    assert node.get("a") == "a\rb"


def test_a_control_character_in_a_station_name_does_not_break_every_slave_s_document() -> None:
    """The name arrives in a slave's ContentItem, so it arrives from the unauthenticated side."""
    m = _master()
    m.station = _station("Radio\x00Free")
    root = ElementTree.fromstring(m.now_playing_xml())  # noqa: S314 - our own output
    assert root.findtext("track") == "RadioFree"
    assert root.findtext("stationName") == "RadioFree"


def test_a_control_character_in_the_echoed_content_item_does_not_break_it_either() -> None:
    """The third position: the ContentItem is echoed as MARKUP, so it goes through neither escaper.

    That exception is deliberate and stays. It is not an exception from being XML, though, and the
    fragment arrives from the same unauthenticated side as the name does, so a NUL in it reaches
    every slave in the zone through /notification exactly as one in the name would.
    """
    m = _master()
    m.station = Station(
        url_id=1,
        playback_url="http://example.invalid/s",
        name="n",
        content_item_xml='<ContentItem source="X"><itemName>N\x00ame</itemName></ContentItem>',
    )
    root = ElementTree.fromstring(m.now_playing_xml())  # noqa: S314 - our own output
    item = root.find("ContentItem")
    assert item is not None and item.get("source") == "X", "it is still echoed as a fragment"
    assert item.findtext("itemName") == "Name"


def test_a_channel_url_becomes_a_content_item_that_reads_back_as_the_same_url() -> None:
    """The service builds this document from a configured URL, and the master parses it back.

    The two halves have to agree: the URL is escaped INTO the attribute and unescaped once when it
    is read out again, so a query string with an ampersand survives the round trip while the
    document a slave receives is still XML.
    """
    from soundtouch_zonemaster.adapters.soundtouch.xmlmodels import station_request
    from soundtouch_zonemaster.domain.zonexml import station_content_item

    item = station_content_item(url="http://x.invalid/s?a=1&b=2", name="Simon & Garfunkel")

    root = ElementTree.fromstring(item)  # noqa: S314 - our own output, and parsing IS the assertion
    assert root.findtext("itemName") == "Simon & Garfunkel"
    request = station_request(item)
    assert request is not None
    assert request.playback_url == "http://x.invalid/s?a=1&b=2", "unescaped once, for the fetch"
    assert request.name == "Simon & Garfunkel"


@pytest.mark.parametrize("raw", ["tab\there", "line\nbreak", "cr\rhere", 'quote " and & amp', "plain"])
def test_a_value_survives_the_round_trip_through_an_attribute(raw: str) -> None:
    """``attr`` writes more entities than the markup ones, and the reader has to bring them all back.

    quoteattr spells tab, newline and carriage return as numeric entities, and a parser normalises
    a LITERAL one of those in an attribute to a space - so a value only survives because ``attr``
    wrote the entity. The one path that matters: a station URL goes out through ``attr`` inside a
    ContentItem, a slave echoes that item back in a select, and ``station_request`` reads it, so
    anything lost here is a URL the master then fetches wrong. Driven through that real reader
    rather than a mirror function, because it is the reader whose answer is used.
    """
    from soundtouch_zonemaster.adapters.soundtouch.xmlmodels import station_request

    request = station_request(f'<ContentItem location="{xmlfmt.attr(raw)}" />')
    assert request is not None
    assert request.playback_url == raw


@pytest.mark.parametrize("raw", ["Simon & Garfunkel", "<not a tag>", 'quote " inside', "plain"])
def test_a_value_survives_the_round_trip_between_tags(raw: str) -> None:
    """The same round trip for a name, which travels between the tags rather than in an attribute."""
    from soundtouch_zonemaster.adapters.soundtouch.xmlmodels import station_request

    item = f'<ContentItem location="http://x.invalid/s"><itemName>{xmlfmt.text(raw)}</itemName></ContentItem>'
    request = station_request(item)
    assert request is not None
    assert request.name == raw
