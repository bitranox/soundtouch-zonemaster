"""What the master SAYS to a speaker, against a speaker that answers and one that does not.

These are the paths that only ever ran against the flat: taking a slave on, pushing the zone,
letting one leave, dissolving, announcing a selection, and reading a preset off a box. They were
the least covered half of `master.py` after the placement code moved out, and coverage did not
notice because the file average sat above the gate's floor.

Nothing is monkeypatched. The master's HTTP calls take an address, so a small server on loopback
IS a speaker as far as they are concerned, and the assertions are on what arrived there - the
request line and the body a real box would have to parse. That double is `speaker_double.py`, which
this file shares with the one that runs the whole command. The master is deliberately NOT started:
these calls do not need its own servers, and not starting it keeps port 8090 free for the double.

The refusing half matters as much. Two of these loops catch every exception on purpose, so that
one unplugged speaker cannot stop a dissolve or an announcement reaching the others, and a loop
that swallows what it must not is invisible until the day it matters.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

import httpx
import pytest
from speaker_double import FakeSpeaker

from soundtouch_zonemaster.adapters.soundtouch.speaker_http import select_station, station_from_speaker_preset
from soundtouch_zonemaster.adapters.soundtouch.zone_master import Slave, ZoneMaster
from soundtouch_zonemaster.domain.station import StationRequest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.asyncio

SPEAKER = "127.0.0.1"
DEVICE_ID = "AABBCC000002"
PRESET_ITEM = (
    '<ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" location="http://example.invalid/s" '
    'sourceAccount="" isPresetable="true"><itemName>Preset Two</itemName></ContentItem>'
)
AUX_ITEM = '<ContentItem source="AUX" sourceAccount="AUX" isPresetable="true"><itemName>AUX IN</itemName></ContentItem>'
"""A preset that is not a stream. It parses as a ContentItem and carries no location, which is
the difference between "this preset is not there" and "there is nothing here to play"."""

EMPTY_ITEM = '<ContentItem source="STANDBY" isPresetable="false" />'
"""The same element written the short way, which is legal XML and what this box writes elsewhere.

Every STANDBY nowPlaying in the capture carries exactly this, byte for byte. Whether a real
``/presets`` document ever holds it is UNCONFIRMED - none has been captured (OPEN-WORK rank 155) -
so what the test below pins is that the two spellings of one empty element are read the same way,
which is a fact about the XML and not a claim about the speaker.
"""


@pytest.fixture
async def speaker() -> AsyncIterator[FakeSpeaker]:
    box = FakeSpeaker({1: AUX_ITEM, 2: PRESET_ITEM, 3: EMPTY_ITEM}, host=SPEAKER, device_id=DEVICE_ID)
    await box.start()
    try:
        yield box
    finally:
        await box.stop()


async def _station_server(payload: bytes) -> tuple[asyncio.AbstractServer, str]:
    """A station on an arbitrary port, so play() can reach its first bytes and go on.

    Port 8090 is taken by the speaker double and both are hardcoded in the code under test, so the
    station has to live somewhere else - which is true of a real one too.
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\n\r\n" + payload)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"http://127.0.0.1:{port}/stream"


def _master(**kw: object) -> ZoneMaster:
    return ZoneMaster(bind_ip="127.0.0.1", device_id="5EB0CE000001", log=lambda _k, _t: None, **kw)  # type: ignore[arg-type]


async def test_taking_a_slave_on_reads_its_id_and_pushes_the_zone(speaker: FakeSpeaker) -> None:
    """Two calls in order: the id has to be known before the zone naming it can be built."""
    master = _master()
    await master.add_slave(SPEAKER)
    assert master.slaves[SPEAKER].device_id == DEVICE_ID, "the id comes off the box, not from us"
    assert [(method, path) for method, path, _ in speaker.requests] == [("GET", "/info"), ("POST", "/setZone")]
    zone = speaker.bodies_for("/setZone")[0]
    assert f'<member ipaddress="{SPEAKER}">{DEVICE_ID}</member>' in zone, "the box is named in its own zone"
    assert 'senderIsMaster="true"' in zone


async def test_a_box_whose_info_carries_no_id_is_refused_rather_than_half_added(speaker: FakeSpeaker) -> None:
    """A slave with no device id cannot be addressed later, so it must not enter the registry."""
    speaker._answer = staticmethod(lambda _path: "<info><name>Nameless</name></info>")  # type: ignore[method-assign]
    master = _master()
    with pytest.raises(RuntimeError, match="no deviceID"):
        await master.add_slave(SPEAKER)
    assert master.slaves == {}, "a half-added slave would be pushed zones it cannot be named in"


async def test_a_slave_leaving_says_nothing_at_all_to_the_ones_that_stay(speaker: FakeSpeaker) -> None:
    """A box that is playing must never be handed a zone document.

    Measured on the hardware on 2026-09-08 in two runs, the second judged by ear: a slave STOPS,
    reconnects and re-buffers on ANY ``/setZone`` - 3.553 s in one room, 4.081 s in the other -
    and it does so even when the document is byte-identical to the one it already holds, which is
    exactly what the box that stays used to be sent. The member lists that push used to correct
    are inaudible: every box carries a list naming only itself and the house still plays in sync,
    because the audio path hangs on the ``master`` field, which they all agree about. So the
    correction cost four seconds of silence to fix nothing anybody can hear.
    """
    master = _master(
        slaves={SPEAKER: Slave(ip=SPEAKER, device_id=DEVICE_ID), "10.0.0.9": Slave(ip="10.0.0.9", device_id="DEAD01")}
    )
    await master.slave_left("10.0.0.9")
    assert "10.0.0.9" not in master.slaves
    assert speaker.requests == [], "the box that stayed may be playing; a document silences it for four seconds"


async def test_a_slave_leaving_that_was_never_in_the_zone_talks_to_nobody(speaker: FakeSpeaker) -> None:
    """The guard: an unknown address must not provoke a round of zone pushes."""
    master = _master(slaves={SPEAKER: Slave(ip=SPEAKER, device_id=DEVICE_ID)})
    await master.slave_left("10.0.0.99")
    assert speaker.requests == []
    assert SPEAKER in master.slaves


async def test_dissolving_sends_every_slave_an_empty_zone_and_forgets_them(speaker: FakeSpeaker) -> None:
    """A real slave goes to standby on an empty zone, so this is how the zone ends (REPORT.md E6)."""
    master = _master(slaves={SPEAKER: Slave(ip=SPEAKER, device_id=DEVICE_ID)})
    await master.dissolve()
    body = speaker.bodies_for("/setZone")[0]
    assert "<member" not in body, "an empty zone is what puts the box in standby"
    assert master.slaves == {}


async def test_a_speaker_that_refuses_does_not_stop_the_others_being_dissolved(speaker: FakeSpeaker) -> None:
    """The catch-all is load-bearing: one unplugged box must not leave the rest in a dead zone."""
    logs: list[str] = []
    master = ZoneMaster(
        bind_ip="127.0.0.1",
        device_id="5EB0CE000001",
        log=lambda kind, text: logs.append(f"{kind}: {text}"),
        slaves={
            "127.0.0.9": Slave(ip="127.0.0.9", device_id="DEAD01"),  # nothing listens here
            SPEAKER: Slave(ip=SPEAKER, device_id=DEVICE_ID),
        },
    )
    await master.dissolve()
    assert speaker.bodies_for("/setZone"), "the reachable box was still dissolved"
    assert any("dissolve 127.0.0.9" in line for line in logs), "and the failure was said out loud"
    assert master.slaves == {}


async def test_playing_a_station_sends_every_slave_both_documents(speaker: FakeSpeaker) -> None:
    """A box needs the selection AND the now-playing note; one without the other leaves it stale.

    Driven through ``play()`` rather than the announcement directly, because that is the only way
    a selection is ever announced and because reaching past a public method into a private one
    tests a call that nothing makes.
    """
    station_srv, url = await _station_server(os.urandom(32_000))
    master = _master(slaves={SPEAKER: Slave(ip=SPEAKER, device_id=DEVICE_ID)})
    try:
        station = await master.play(StationRequest(url, "Radio Eins", PRESET_ITEM), first_bytes_timeout_s=5.0)
        assert station is not None
        assert [path for _method, path, _body in speaker.requests] == ["/masterMsg", "/notification"]
        assert "Radio Eins" in speaker.bodies_for("/notification")[0]
        assert "nowSelection" in speaker.bodies_for("/masterMsg")[0]
    finally:
        await master.stop()
        station_srv.close()


async def test_an_unreachable_slave_does_not_stop_the_rest_of_the_announcement() -> None:
    """Display-only traffic, so a box that is off must cost a log line and nothing else.

    In particular the SECOND document must still be attempted after the first one failed: a box
    that comes back to a zone whose selection it never heard shows the wrong station.
    """
    station_srv, url = await _station_server(os.urandom(32_000))
    logs: list[str] = []
    master = ZoneMaster(
        bind_ip="127.0.0.1",
        device_id="5EB0CE000001",
        log=lambda kind, text: logs.append(f"{kind}: {text}"),
        slaves={"127.0.0.9": Slave(ip="127.0.0.9", device_id="DEAD01")},
    )
    try:
        await master.play(StationRequest(url, "n", PRESET_ITEM), first_bytes_timeout_s=5.0)
        assert [line for line in logs if "/masterMsg -> 127.0.0.9" in line], "the failure is logged, not raised"
        assert [line for line in logs if "/notification -> 127.0.0.9" in line], "and the second one still went out"
    finally:
        await master.stop()
        station_srv.close()


async def test_a_preset_is_read_off_a_speaker_as_a_playable_station(speaker: FakeSpeaker) -> None:
    """How the master learns what to play at all: it reads a preset the house already set."""
    request = await station_from_speaker_preset(SPEAKER, 2)
    assert request.playback_url == "http://example.invalid/s"
    assert request.name == "Preset Two"
    assert request.content_item_xml.startswith("<ContentItem")


async def test_a_preset_number_the_speaker_does_not_have_is_refused(speaker: FakeSpeaker) -> None:
    with pytest.raises(RuntimeError, match="preset 5 not found"):
        await station_from_speaker_preset(SPEAKER, 5)


async def test_a_preset_written_as_a_self_closing_element_is_read_as_empty_and_not_as_missing(
    speaker: FakeSpeaker,
) -> None:
    """Both spellings refuse, and they send whoever reads the line to different places.

    An unset preset slot is the case with no children, so it is where the short spelling is most
    likely to turn up. Matched only in its long form, the whole ``<preset>`` element failed to
    match and the answer was "preset 3 not found" - the slot is not there - when the slot is there
    and empty. Nothing that parses today parses differently: the short form has no closing tag to
    find, so it could never have matched at all.
    """
    with pytest.raises(RuntimeError, match="preset 3 has no location to play"):
        await station_from_speaker_preset(SPEAKER, 3)


async def test_a_preset_with_no_location_is_refused_rather_than_played_as_silence(speaker: FakeSpeaker) -> None:
    """Preset 1 in the double is an AUX preset: a real preset with nothing to fetch."""
    with pytest.raises(RuntimeError, match="no location to play"):
        await station_from_speaker_preset(SPEAKER, 1)


async def test_a_box_leaving_cannot_die_on_an_unplugged_speaker_because_it_speaks_to_none(
    speaker: FakeSpeaker,
) -> None:
    """A leave reaches no speaker at all, so no speaker can make it fail.

    This used to be the opposite test: the push to the boxes that stayed had to survive one of
    them being off the air, or the service died of an unplugged speaker. There is no push any
    more, so the property is stronger and simpler - a box leaving is bookkeeping, and bookkeeping
    cannot fail on the radio. The dead box is FIRST in the zone, which is where a loop that stops
    at its first failure would have been caught.
    """
    logs: list[str] = []
    master = ZoneMaster(
        bind_ip="127.0.0.1",
        device_id="5EB0CE000001",
        log=lambda kind, text: logs.append(f"{kind}: {text}"),
        slaves={
            "127.0.0.8": Slave(ip="127.0.0.8", device_id="DEAD01"),  # nothing listens here
            SPEAKER: Slave(ip=SPEAKER, device_id=DEVICE_ID),
            "127.0.0.7": Slave(ip="127.0.0.7", device_id="GONE02"),
        },
    )

    await master.slave_left("127.0.0.7")

    assert "127.0.0.7" not in master.slaves
    assert speaker.requests == [], "the box that is still there is playing, and must not be spoken to"
    assert [line for line in logs if "127.0.0.7 left the zone; 2 slave(s) remain" in line]


async def test_selecting_a_station_posts_the_bare_content_item_to_select(speaker: FakeSpeaker) -> None:
    """The shape a real box accepted, recorded in
    research/captures/2026-09-05-room3-room1/events.jsonl: a bare ``<ContentItem>``, no XML
    declaration and no wrapper element, POSTed to ``/select``.
    """
    await select_station(SPEAKER, url="http://example.invalid/s", name="Radio Eins")
    assert speaker.paths() == ["/select"]
    assert speaker.bodies_for("/select")[0] == (
        '<ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" location="http://example.invalid/s" '
        'sourceAccount="" isPresetable="true"><itemName>Radio Eins</itemName></ContentItem>'
    )


async def test_a_box_that_refuses_the_zone_is_not_left_holding_a_place_in_it() -> None:
    """A push that fails must not leave the master holding a slave that nobody can let go of.

    ``add_slave`` records the slave BEFORE it pushes, and it has to: the document it pushes names
    the whole zone, the arriving box included. But an unguarded record survives a failed push, and
    then nothing removes it - the service never counted it as joined, so it never lets it go, while
    every later dissolve and every announcement still reaches it. A box that was never in the zone
    would be sent the document that puts a real speaker into standby.
    """
    box = FakeSpeaker({}, host=SPEAKER, device_id=DEVICE_ID, refuse=frozenset({"/setZone"}))
    await box.start()
    master = _master()
    try:
        with pytest.raises(httpx.HTTPError):
            await master.add_slave(SPEAKER)

        assert master.slaves == {}, "a box that refused the zone is not in it"
    finally:
        await box.stop()
