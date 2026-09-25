"""What the master will read, what it refuses, and what either costs.

The documents this parser is fed arrive from an unauthenticated LAN: anything in the flat may
``POST /slaveMsg`` on 8090, and a notification frame is no better. So the tests here are of two
kinds, and both matter:

* the REFUSALS - an attack or a malformed document must come back as ``None`` rather than as an
  exception in an event loop or a body the parser works through;
* the COST - every refusal and every legal document has a ceiling, asserted at the seams a
  speaker reaches (``HttpApi.handle`` and ``parse_frame``) and not only at the helper, because
  the linear work around the helper lands in both arms and is what an attacker's body pays for.

The ceilings are deliberately loose against the measurements (every refusal here took
microseconds and is asserted under 5 ms), because a shared machine under load is what CI is. They
are still well under what the thing each guards against costs: measured 2026-09-22, the laughs
document below takes 11 ms with the DOCTYPE screen removed, and a megabyte of repeated `<keyData`
cost 0.9 to 3.3 s per run through the patterns this replaced.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from speaker_double import RecordingMaster

from soundtouch_zonemaster.adapters.soundtouch.http_api import HttpApi, Request, key_press
from soundtouch_zonemaster.adapters.soundtouch.observer import UNREADABLE, parse_frame
from soundtouch_zonemaster.adapters.soundtouch.xmlread import (
    MAX_DEPTH,
    MAX_XML_CHARS,
    attribute_anywhere,
    child_tag,
    element_anywhere,
    parse,
    serialised_as,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

FIXTURES = Path(__file__).parent / "fixtures"
PEER = "192.0.2.21"

A_MEGABYTE = 1 << 20
"""What the HTTP face accepts, so it is what a hostile body may be when it reaches the seam."""


def say_nothing(kind: str, text: str) -> None:
    """A ``LogFn`` that drops the line: what a body was logged as is another test's business."""


def laughs(*, levels: int = 11) -> str:
    """A billion-laughs document: each entity names the one below it ten times over.

    The body names the TOP entity, so the nominal expansion is 100 characters times ten to the
    power of ``levels``, times the hundred references. Getting that wrong is how this document
    stops being an attack: an undefined entity is refused in microseconds by any parser, which
    would leave the test passing while measuring nothing.
    """
    declarations = "".join(f'<!ENTITY e{i} "' + f"&e{i - 1};" * 10 + '">' for i in range(1, levels + 1))
    top = f"&e{levels};"
    return '<!DOCTYPE l [<!ENTITY e0 "' + "a" * 100 + '">' + declarations + "]><l>" + top * 100 + "</l>"


HOSTILE = {
    "billion laughs": laughs(),
    "quadratic blowup": '<!DOCTYPE q [<!ENTITY x "' + "x" * 20000 + '">]><q>' + "&x;" * 10000 + "</q>",
    "external entity": '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>',
    "external DTD": '<!DOCTYPE x SYSTEM "http://198.51.100.1/x.dtd"><x/>',
    "parameter entity": '<!DOCTYPE x [<!ENTITY % p SYSTEM "http://198.51.100.1/p"> %p;]><x/>',
    "doctype behind a comment": '<?xml version="1.0"?><!-- c --><?pi x?><!DOCTYPE x><x/>',
    "recursive entity": '<!DOCTYPE x [<!ENTITY a "&b;"><!ENTITY b "&a;">]><x>&a;</x>',
    "undefined entity": "<x>&nope;</x>",
    "a NUL character": "<x>\x00</x>",
    "a lone surrogate": "<x>\ud800</x>",
    "a duplicate attribute": '<x a="1" a="2"/>',
    "an unclosed opening tag": "<keyData" * 2000,
    "two root elements": "<x/><y/>",
    "text that is not XML at all": "not xml, not even close",
    "over the size cap": "<r>" + "x" * MAX_XML_CHARS + "</r>",
    "too deeply nested": "<a>" * (MAX_DEPTH + 1) + "</a>" * (MAX_DEPTH + 1),
}
"""One input per way a document can be hostile or unreadable. Every one of them answers None."""


@pytest.mark.parametrize("name", sorted(HOSTILE), ids=sorted(HOSTILE))
def test_a_hostile_document_is_refused_rather_than_worked_through(name: str) -> None:
    """Refused, and refused fast: the two halves of "it cannot hang" are one assertion each.

    5 ms is far above what any of these measured (microseconds) and well under what the thing
    each stands for costs unguarded - the laughs document alone reaches expat's own amplification
    limit only after 11 ms, and a stronger one costs more before that limit stops it.
    """
    started = time.perf_counter()
    answer = parse(HOSTILE[name])
    spent = time.perf_counter() - started

    assert answer is None, f"{name} was parsed rather than refused"
    assert spent < 0.005, f"{name} cost {spent * 1000:.1f} ms"


def test_the_doctype_screen_is_what_makes_an_entity_attack_cheap() -> None:
    """The control for the screen: the same document, refused by expat's limit instead.

    Written because "refused" alone is not the point - expat refuses a laughs document too, after
    11 ms of expanding it. Parsing an entity-free body of the same length here measures the same
    path WITHOUT the screen firing, so the ceiling above is known to be the screen's doing rather
    than the document's size.
    """
    entity_free = "<l>" + "a" * len(laughs()) + "</l>"
    started = time.perf_counter()
    parsed = parse(entity_free)
    baseline = time.perf_counter() - started

    assert parsed is not None, "the control must be a document this master reads"
    assert baseline < 0.005


def test_a_document_at_the_depth_limit_is_read_and_one_deeper_is_not() -> None:
    """The boundary itself, because an off-by-one here is a refusal of real traffic."""
    assert parse("<a>" * MAX_DEPTH + "</a>" * MAX_DEPTH) is not None
    assert parse("<a>" * (MAX_DEPTH + 1) + "</a>" * (MAX_DEPTH + 1)) is None


def test_a_document_at_the_size_limit_is_read_and_one_over_it_is_not() -> None:
    """The other boundary. The cap counts characters, so the document is built to the character."""
    body = "<r>" + "x" * (MAX_XML_CHARS - len("<r></r>")) + "</r>"

    assert len(body) == MAX_XML_CHARS
    assert parse(body) is not None
    assert parse(body + " ") is None


def test_a_deep_content_element_cannot_reach_the_recursion_in_serialising_it() -> None:
    """Why the depth check earns its place: ``ET.tostring`` recurses, and a select is serialised.

    Four thousand nested elements inside ``<content>`` fit in the size cap comfortably. Without
    the depth check this body parses, and rebuilding its ContentItem raises RecursionError inside
    the request handler.
    """
    nested = "<a>" * 4000 + "</a>" * 4000
    body = f'<slaveMessage action="select"><content source="X">{nested}</content></slaveMessage>'

    assert len(body) < MAX_XML_CHARS
    assert parse(body) is None


def recorded_documents() -> Iterator[tuple[str, str]]:
    """Every speaker document this repository has on record, with where it came from.

    The frames of four recorded runs and the eight keyData bodies of 2026-09-06. A parser this
    master ships has to read all of them: a refusal here is a frame the house would lose.
    """
    for fixture in sorted(FIXTURES.glob("*frames*.json")):
        loaded: dict[str, Any] = json.loads(fixture.read_text(encoding="utf-8"))
        for index, entry in enumerate(loaded["frames"]):
            if "frame" in entry:
                yield f"{fixture.name}[{index}]", entry["frame"]
    for index, line in enumerate((FIXTURES / "slavemsg-keydata.txt").read_text(encoding="utf-8").splitlines()):
        if line.startswith("<"):
            yield f"slavemsg-keydata.txt[{index}]", line


RECORDED = list(recorded_documents())


def test_the_recorded_corpus_is_not_empty() -> None:
    """A parametrisation that silently found nothing would pass every case below."""
    assert len(RECORDED) > 600


@pytest.mark.parametrize(("origin", "document"), RECORDED, ids=[origin for origin, _ in RECORDED])
def test_every_recorded_document_is_read(origin: str, document: str) -> None:
    """Measured 2026-09-22 before the conversion: all of them parse, none of them is refused."""
    assert parse(document) is not None, f"{origin} would be lost"


def test_the_helpers_read_what_the_patterns_they_replaced_read() -> None:
    """The three "anywhere" lookups, on a frame that nests each answer at a different depth."""
    frame = (
        '<updates deviceID="AABBCC000010">'
        "<nowPlayingUpdated>"
        '<nowPlaying deviceID="OWNER" source="INTERNET_RADIO">'
        '<ContentItem source="INNER"/>'
        "</nowPlaying>"
        "</nowPlayingUpdated>"
        "</updates>"
    )
    root = parse(frame)

    assert root is not None
    assert attribute_anywhere(root, "deviceID") == "AABBCC000010", "the first in document order"
    assert child_tag(root) == "nowPlayingUpdated"
    playing = element_anywhere(root, "nowPlaying")
    assert playing is not None
    assert playing.get("source") == "INTERNET_RADIO", "and not the ContentItem's own source"


def test_a_selection_is_rebuilt_as_the_content_item_a_speaker_stores() -> None:
    """The select path's one byte-level claim: the document a speaker sent comes back unchanged.

    Measured over all 34 recorded selects on 2026-09-22. What is asserted here is the shape of
    that: the tag is the one a speaker stores, and the escaped ampersand is still escaped, because
    this string is handed to speakers rather than read.
    """
    body = '<slaveMessage action="select"><content location="http://x.invalid/s?a=1&amp;b=2"><itemName>N</itemName></content></slaveMessage>'
    root = parse(body)

    assert root is not None
    item = element_anywhere(root, "content")
    assert item is not None
    assert (
        serialised_as(item, "ContentItem")
        == '<ContentItem location="http://x.invalid/s?a=1&amp;b=2"><itemName>N</itemName></ContentItem>'
    )


SEAM_CEILING_S = 0.05
"""What a megabyte of hostile body may cost at a seam a speaker reaches.

Measured at microseconds for every shape, since all of them are over the size cap and refused
before a parser is built. The ceiling is the assertion that this stays true: through the patterns
this replaced, the same bodies cost 0.9 to 3.3 seconds each and held the event loop for every
slave in the zone.
"""


def hostile_bodies() -> dict[str, str]:
    """A megabyte of each shape that is cheap to build and expensive to read badly."""
    return {
        "unclosed opening tags": "<keyData" * (A_MEGABYTE // 8),
        "unclosed content tags": "<content " * (A_MEGABYTE // 9),
        "deep nesting": "<a>" * (A_MEGABYTE // 3),
        "many small elements": "<r>" + '<a x="1"/>' * (A_MEGABYTE // 10) + "</r>",
        "billion laughs": laughs(),
        "one long text node": "<r>" + "x" * A_MEGABYTE + "</r>",
    }


@pytest.mark.parametrize("name", sorted(hostile_bodies()), ids=sorted(hostile_bodies()))
def test_a_hostile_body_costs_the_http_face_almost_nothing(name: str) -> None:
    """The seam a stranger on the LAN reaches: one POST, measured end to end.

    Driven through ``HttpApi.handle`` rather than through the parser, because the request is where
    the cost lands: the old patterns were quadratic in the body, and everything the handler does
    around them is in both arms.
    """
    body = hostile_bodies()[name]
    api = HttpApi(RecordingMaster(), log=say_nothing)
    request = Request(method="POST", path="/slaveMsg", body=body, peer=PEER)

    started = time.perf_counter()
    asyncio.run(api.handle(request))
    spent = time.perf_counter() - started

    assert spent < SEAM_CEILING_S, f"{name} cost the HTTP face {spent * 1000:.0f} ms"


@pytest.mark.parametrize("name", sorted(hostile_bodies()), ids=sorted(hostile_bodies()))
def test_a_hostile_frame_costs_the_observer_almost_nothing(name: str) -> None:
    """The other seam: a notification frame off a speaker's WebSocket.

    A refused frame is still reported, carrying its own bytes, so a box that sends rubbish is
    visible in the log rather than silently dropped.
    """
    frame = hostile_bodies()[name]

    started = time.perf_counter()
    event = parse_frame(PEER, frame, 1.0)
    spent = time.perf_counter() - started

    assert spent < SEAM_CEILING_S, f"{name} cost the observer {spent * 1000:.0f} ms"
    assert event.kind == UNREADABLE
    assert event.frame == frame, "the frame is kept whatever happens to it"


def test_a_normal_body_is_read_at_the_same_seam() -> None:
    """The control arm: the ceiling above means nothing unless a real body still gets through."""
    body = '<slaveMessage action="key"><keyData state="press" sender="IrRemote">NEXT_TRACK</keyData></slaveMessage>'

    press = key_press(body)

    assert press is not None
    assert (press.key, press.state, press.sender) == ("NEXT_TRACK", "press", "IrRemote")
