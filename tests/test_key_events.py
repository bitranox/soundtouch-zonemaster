"""A key press forwarded by a slave, against the bodies the flat actually sent.

M0 measured that the two inputs travel different paths: next, previous and both thumbs arrive at
the master's own HTTP face as `POST /slaveMsg` with `action="key"`, while the preset NUMBER is
only ever on the speaker's notification WebSocket. Joining them anywhere later would mean two
half-streams and a per-speaker correlation nobody asked for, so both become a `SpeakerEvent` and
the service reads one queue.

The bodies are the eight recorded on 2026-09-06, lifted from the run's log into
`fixtures/slavemsg-keydata.txt` rather than retyped. A parser proved against a typed-out sample
is proved against the wrong bytes.

Driven through `MasterPort`, the seam the HTTP face already has, with no socket and no ZoneMaster.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from speaker_double import key_bodies, key_body

from soundtouch_zonemaster.adapters.soundtouch.http_api import HttpApi, Request
from soundtouch_zonemaster.domain.enums import KeyName, KeyState

if TYPE_CHECKING:
    from soundtouch_zonemaster.domain.events import SpeakerEvent

PEER = "192.168.1.21"

SELECT_BODY = (
    '<?xml version="1.0" encoding="UTF-8" ?><slaveMessage action="select">'
    '<content source="LOCAL_INTERNET_RADIO" type="stationurl" location="http://example.invalid/s">'
    "<itemName>Some Station</itemName></content></slaveMessage>"
)


class FakeMaster:
    """MasterPort, remembering what it was asked to do."""

    device_id = "5EB0CE000001"

    def __init__(self) -> None:
        self.selected: list[tuple[str, str]] = []
        self.left: list[str] = []

    def info_xml(self) -> str:
        return "<info/>"

    def now_playing_xml(self) -> str:
        return "<nowPlaying/>"

    def zone_xml(self) -> str:
        return "<zone/>"

    async def select(self, content_item_xml: str, *, origin: str) -> None:
        self.selected.append((content_item_xml, origin))

    async def slave_left(self, ip: str) -> None:
        self.left.append(ip)


def _api(*, with_queue: bool = True) -> tuple[HttpApi, FakeMaster, asyncio.Queue[SpeakerEvent] | None]:
    master = FakeMaster()
    events: asyncio.Queue[SpeakerEvent] | None = asyncio.Queue() if with_queue else None
    return HttpApi(master, lambda _kind, _text: None, events=events), master, events


def _post(body: str) -> Request:
    return Request(method="POST", path="/slaveMsg", body=body, peer=PEER)


def test_the_fixture_still_holds_all_eight_key_bodies() -> None:
    """If the fixture is ever thinned, the tests below would pass on whatever is left."""
    assert len(key_bodies()) == 8


@pytest.mark.parametrize("key", list(KeyName))
async def test_every_key_of_the_family_arrives_as_an_event(key: KeyName) -> None:
    api, master, events = _api()
    assert events is not None

    await api.handle(_post(key_body(key, KeyState.PRESS)))

    event = events.get_nowait()
    assert event.key is not None
    assert event.key.key == key
    assert event.key.state == KeyState.PRESS
    assert event.speaker == PEER
    assert master.selected == []


async def test_a_release_is_carried_rather_than_swallowed() -> None:
    """Dropping it at the parser makes the double arrival invisible to whoever later needs it."""
    api, _master, events = _api()
    assert events is not None

    await api.handle(_post(key_body(KeyName.NEXT_TRACK, KeyState.RELEASE)))

    event = events.get_nowait()
    assert event.key is not None
    assert event.key.state == KeyState.RELEASE


async def test_the_sender_is_carried_and_nothing_branches_on_it() -> None:
    """Every press in the run came from the remote; the box's own buttons are untested."""
    api, _master, events = _api()
    assert events is not None

    await api.handle(_post(key_body(KeyName.THUMBS_UP, KeyState.PRESS)))

    event = events.get_nowait()
    assert event.key is not None
    assert event.key.sender == "IrRemote"


async def test_a_key_body_never_reaches_select() -> None:
    """It carries no ContentItem at all, so a select built from it would be built from nothing."""
    api, master, _events = _api()

    for key in KeyName:
        await api.handle(_post(key_body(key, KeyState.PRESS)))

    assert master.selected == []


async def test_the_select_path_is_untouched_and_puts_no_key_event() -> None:
    api, master, events = _api()
    assert events is not None

    await api.handle(_post(SELECT_BODY))
    await asyncio.sleep(0)

    assert len(master.selected) == 1
    assert events.empty()


async def test_a_key_arrives_even_with_nobody_listening() -> None:
    """The prototype run has no service behind it, and a press must not take the HTTP face down."""
    api, _master, _events = _api(with_queue=False)

    answer = await api.handle(_post(key_body(KeyName.PREV_TRACK, KeyState.PRESS)))

    assert answer
