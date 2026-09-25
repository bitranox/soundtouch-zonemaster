"""The speaker API the slaves call on the master, driven through its own MasterPort seam.

No socket and no ZoneMaster: HttpApi is handed a stand-in that records what it was asked to do,
which is what MasterPort exists for. What a real slave actually sends is covered end to end in
test_zonemaster_local_e2e.
"""

from __future__ import annotations

import asyncio

from soundtouch_zonemaster.adapters.soundtouch.http_api import HttpApi, Request, parse_request, status_xml

ZONE_XML = '<?xml version="1.0" encoding="UTF-8" ?><zone master="5EB0CE000001" />'


class FakeMaster:
    """Implements MasterPort and remembers the calls, so a route can be checked without a zone."""

    device_id = "5EB0CE000001"

    def __init__(self) -> None:
        self.selected: list[tuple[str, str]] = []
        self.left: list[str] = []

    def info_xml(self) -> str:
        return "<info/>"

    def now_playing_xml(self) -> str:
        return "<nowPlaying/>"

    def zone_xml(self) -> str:
        return ZONE_XML

    async def select(self, content_item_xml: str, *, origin: str) -> None:
        self.selected.append((content_item_xml, origin))

    async def slave_left(self, ip: str) -> None:
        self.left.append(ip)


def _api() -> tuple[HttpApi, FakeMaster, list[str]]:
    logs: list[str] = []
    master = FakeMaster()
    return HttpApi(master, lambda kind, text: logs.append(f"{kind} {text}")), master, logs


def _get(path: str) -> Request:
    return Request(method="GET", path=path, body="", peer="192.168.1.21")


def _post(path: str, body: str) -> Request:
    return Request(method="POST", path=path, body=body, peer="192.168.1.21")


async def test_each_get_route_answers_from_the_master() -> None:
    api, master, _ = _api()
    assert await api.handle(_get("/info")) == "<info/>"
    assert await api.handle(_get("/now_playing")) == "<nowPlaying/>"
    assert await api.handle(_get("/nowPlaying")) == "<nowPlaying/>", "the speakers use both spellings"
    assert await api.handle(_get("/getZone")) == ZONE_XML
    assert "<presets />" in await api.handle(_get("/presets"))
    assert master.device_id in await api.handle(_get("/volume"))


async def test_an_unknown_get_echoes_its_path_and_is_logged() -> None:
    api, _, logs = _api()
    assert await api.handle(_get("/somethingElse")) == status_xml("/somethingElse")
    assert any("unhandled GET /somethingElse" in line for line in logs)


async def test_a_slave_pressing_a_preset_selects_that_station_for_the_zone() -> None:
    api, master, _ = _api()
    # The shape a speaker really sends (REPORT.md S3): the ContentItem's attributes ride on the
    # content element, and the master rebuilds a ContentItem from them.
    body = (
        '<slaveMessage action="select"><content source="LOCAL_INTERNET_RADIO" type="stationurl" '
        'location="http://example.invalid/live" isPresetable="true">'
        "<itemName>Radio</itemName></content></slaveMessage>"
    )
    await api.handle(_post("/slaveMsg", body))
    await asyncio.sleep(0)  # the handler schedules the select as a task
    assert len(master.selected) == 1, "the select reached the master"
    item, origin = master.selected[0]
    assert item.startswith("<ContentItem") and 'location="http://example.invalid/live"' in item
    assert "<itemName>Radio</itemName>" in item
    assert origin == "192.168.1.21"


async def test_a_slave_msg_that_is_not_a_select_changes_nothing() -> None:
    api, master, logs = _api()
    await api.handle(_post("/slaveMsg", '<slaveMessage action="volume"><content>x</content></slaveMessage>'))
    await asyncio.sleep(0)
    assert master.selected == []
    assert any("action=volume" in line for line in logs)


async def test_slave_msg_logs_the_whole_body_even_with_no_content_item() -> None:
    """A key with no content is the case the log has to carry; nothing parses it yet."""
    api, master, logs = _api()
    body = (
        '<?xml version="1.0" encoding="UTF-8" ?>'
        '<slaveMessage action="keyPress">'
        '<keyData state="press" sender="Gabbo">NEXT_TRACK</keyData>'
        "</slaveMessage>"
    )
    await api.handle(_post("/slaveMsg", body))
    await asyncio.sleep(0)
    assert master.selected == []
    assert any("NEXT_TRACK" in line for line in logs), logs


async def test_a_slave_leaving_the_zone_is_reported_by_its_own_address() -> None:
    api, master, _ = _api()
    await api.handle(_post("/removeZoneSlave", '<zone senderIPAddress="192.168.1.22" />'))
    await asyncio.sleep(0)
    assert master.left == ["192.168.1.21"], "the peer that called is the one that left"


async def test_a_notification_is_logged_by_its_tag_and_answered() -> None:
    api, _, logs = _api()
    answer = await api.handle(_post("/notification", "<updates deviceID='x'>\n  <nowPlayingUpdated/></updates>"))
    assert answer == status_xml("/notification")
    assert any("notification nowPlayingUpdated" in line for line in logs)


async def test_an_unknown_post_is_logged_with_its_body_and_answered() -> None:
    api, _, logs = _api()
    assert await api.handle(_post("/whatever", "hello")) == status_xml("/whatever")
    assert any("POST /whatever hello" in line for line in logs)


def test_a_request_line_is_split_into_method_path_and_body() -> None:
    raw = b"POST /slaveMsg?x=1 HTTP/1.1\r\nHost: h\r\nContent-Length: 5\r\n\r\nhello"
    req = parse_request(raw, "192.168.1.21")
    assert (req.method, req.path, req.body, req.peer) == ("POST", "/slaveMsg", "hello", "192.168.1.21")
