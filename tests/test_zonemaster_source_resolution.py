"""What a station URL has to be followed through before it is a stream of bytes.

A real HTTP server on loopback, not a patched client: the station in this house hands out a JSON
playback descriptor, that descriptor names a playlist, and the playlist names the stream. Each hop
is what the master has to survive before a single byte reaches a speaker.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Self

import httpx
import pytest

from soundtouch_zonemaster.adapters.soundtouch.clock import now_us
from soundtouch_zonemaster.adapters.soundtouch.orion import BMX_REGISTRY_PATH, ORION_FALLBACK_PATH, OrionBase
from soundtouch_zonemaster.adapters.soundtouch.source import RingBuffer, StreamSource, resolve_stream_url
from soundtouch_zonemaster.domain.station import Station

pytestmark = pytest.mark.asyncio


class _Server:
    """Serves a fixed body and content type per path, and records what was asked for."""

    def __init__(self, routes: dict[str, tuple[str, str]]) -> None:
        self.routes = routes
        self.asked: list[str] = []
        self.base = ""
        self._srv: asyncio.AbstractServer | None = None

    async def __aenter__(self) -> Self:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            head = await reader.readuntil(b"\r\n\r\n")
            path = head.decode().split(" ")[1]
            self.asked.append(path)
            ctype, body = self.routes.get(path, ("text/plain", "not found"))
            raw = body.replace("{base}", self.base).encode()
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Type: {ctype}\r\nContent-Length: {len(raw)}\r\n\r\n".encode() + raw
            )
            await writer.drain()
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

        self._srv = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = self._srv.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *_exc: object) -> None:
        assert self._srv is not None
        self._srv.close()
        await self._srv.wait_closed()


async def _resolve(server: _Server, start: str) -> tuple[str, list[str]]:
    logs: list[str] = []
    async with httpx.AsyncClient(timeout=5.0) as client:
        url = await resolve_stream_url(client, f"{server.base}{start}", lambda k, t: logs.append(f"{k} {t}"))
    return url, logs


DESCRIPTOR = ("application/json", json.dumps({"audio": {"streamUrl": "{base}/playlist.m3u"}}))
PLAYLIST = ("audio/x-mpegurl", "#EXTM3U\n#EXTINF:-1,Radio\n{base}/live\n")
STREAM = ("audio/mpeg", "\xff\xfb" * 8)


async def test_a_descriptor_naming_a_playlist_naming_a_stream_is_followed_to_the_stream() -> None:
    async with _Server({"/station": DESCRIPTOR, "/playlist.m3u": PLAYLIST, "/live": STREAM}) as server:
        url, logs = await _resolve(server, "/station")
        assert url == f"{server.base}/live"
        assert server.asked == ["/station", "/playlist.m3u", "/live"], "each hop was fetched once, in order"
        assert any("playback descriptor ->" in line for line in logs)
        assert any("playlist ->" in line for line in logs)


async def test_a_plain_stream_url_is_returned_untouched() -> None:
    async with _Server({"/live": STREAM}) as server:
        url, logs = await _resolve(server, "/live")
        assert url == f"{server.base}/live"
        assert logs == [], "nothing to follow, nothing to say"


async def test_a_pls_playlist_is_followed_by_its_file_entry() -> None:
    pls = ("audio/x-scpls", "[playlist]\nNumberOfEntries=1\nFile1={base}/live\nTitle1=Radio\n")
    async with _Server({"/list.pls": pls, "/live": STREAM}) as server:
        url, _ = await _resolve(server, "/list.pls")
        assert url == f"{server.base}/live", "File1= is stripped to its value"


async def test_a_descriptor_without_a_stream_url_leaves_the_url_alone() -> None:
    # A station is free to answer with any JSON at all; the master must not follow a hop that is
    # not there, and must not crash on one whose shape it did not expect.
    for body in ('{"audio": {"other": 1}}', '{"audio": "not an object"}', '{"audio": null}', "[]", '"text"'):
        async with _Server({"/station": ("application/json", body)}) as server:
            url, _ = await _resolve(server, "/station")
            assert url == f"{server.base}/station", f"stayed put for {body}"


async def test_a_playlist_with_no_usable_entry_stops_at_the_playlist() -> None:
    empty = ("audio/x-mpegurl", "#EXTM3U\n# nothing here\n")
    async with _Server({"/list.m3u": empty}) as server:
        url, _ = await _resolve(server, "/list.m3u")
        assert url == f"{server.base}/list.m3u"


async def test_a_descriptor_loop_gives_up_after_a_few_hops() -> None:
    loop = ("application/json", json.dumps({"audio": {"streamUrl": "{base}/loop"}}))
    async with _Server({"/loop": loop}) as server:
        url, _ = await _resolve(server, "/loop")
        assert url == f"{server.base}/loop"
        assert len(server.asked) == 3, "bounded by the hop limit rather than following forever"


# --- a relative Orion location, completed the way the speaker completes it -----------------------

RELATIVE = "/station?data=eyJuYW1lIjoiUmFkaW8ifQ%3D%3D"
"""A preset as AfterTouch writes it; the query is opaque to the master and travels unchanged."""


def _registry(**bases: str) -> tuple[str, str]:
    """A BMX service registry answering one entry per keyword, in the shape the speaker reads."""
    services = [{"id": {"name": name}, "baseUrl": base} for name, base in bases.items()]
    return "application/json", json.dumps({"askAgainAfter": 1234, "bmx_services": services})


async def _first_bytes_of(station_url: str, orion: OrionBase) -> tuple[StreamSource, list[str]]:
    """Run a real source against ``station_url`` until its first bytes, then stop it."""
    logs: list[str] = []
    source = StreamSource(
        Station(url_id=1, playback_url=station_url, name="Orion", content_item_xml="<ContentItem/>"),
        RingBuffer(),
        lambda k, t: logs.append(f"{k} {t}"),
        orion=orion,
    )
    source.start(now_us)
    try:
        async with asyncio.timeout(5.0):
            while source.t0_us is None:
                await asyncio.sleep(0.01)
    finally:
        await source.stop()
    return source, logs


async def test_a_relative_location_is_fetched_from_the_base_the_service_registry_names() -> None:
    """The speaker's own resolution: LOCAL_INTERNET_RADIO's baseUrl, then the location behind it.

    Another service is listed first on purpose, so a reader that took the first entry would ask
    the wrong adapter for the station.
    """
    orion_path = "/core02/svc-bmx-adapter-orion/prod/orion"
    routes = {
        BMX_REGISTRY_PATH: _registry(TUNEIN="{base}/tunein", LOCAL_INTERNET_RADIO="{base}" + orion_path),
        f"{orion_path}{RELATIVE}": DESCRIPTOR,
        "/playlist.m3u": PLAYLIST,
        "/live": STREAM,
    }
    async with _Server(routes) as server:
        source, logs = await _first_bytes_of(RELATIVE, OrionBase(server.base))
        assert server.asked[:4] == [BMX_REGISTRY_PATH, f"{orion_path}{RELATIVE}", "/playlist.m3u", "/live"]
        assert source.ring.end_offset > 0, "the stream behind the relative location reached the ring"
        assert source.station.playback_url == RELATIVE, "the station itself still names it as stored"
        assert any(f"{server.base}{orion_path}" in line and "LOCAL_INTERNET_RADIO" in line for line in logs)


@pytest.mark.parametrize(
    "answer",
    [
        ("text/plain", "not found"),
        ("application/json", "[]"),
        _registry(TUNEIN="http://192.0.2.1/tunein"),
        _registry(LOCAL_INTERNET_RADIO="/no/scheme"),
        _registry(LOCAL_INTERNET_RADIO=""),
    ],
    ids=["not-json", "not-a-registry", "no-radio-entry", "no-scheme", "empty-base"],
)
async def test_an_unreadable_registry_falls_back_to_the_known_orion_path_and_says_so(
    answer: tuple[str, str],
) -> None:
    """AfterTouch's own adapter path under the service base, rather than failing the station.

    A registry that cannot answer is a neighbour having a bad moment; the Orion adapter it would
    have named sits at a fixed path under the same service, so the house keeps playing.
    """
    routes = {BMX_REGISTRY_PATH: answer, f"{ORION_FALLBACK_PATH}{RELATIVE}": STREAM}
    async with _Server(routes) as server:
        source, logs = await _first_bytes_of(RELATIVE, OrionBase(server.base))
        assert f"{ORION_FALLBACK_PATH}{RELATIVE}" in server.asked
        assert source.ring.end_offset > 0
        assert any("registry" in line and f"{server.base}{ORION_FALLBACK_PATH}" in line for line in logs), logs


async def test_an_unreachable_registry_falls_back_without_waiting_for_ever() -> None:
    """Nothing listening at all is the other way a registry fails, and it must not end the station."""
    closed = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", 0)
    port = closed.sockets[0].getsockname()[1]
    closed.close()
    await closed.wait_closed()
    orion = OrionBase(f"http://127.0.0.1:{port}")
    logs: list[str] = []
    async with httpx.AsyncClient(timeout=5.0) as client:
        url = await orion.absolute(client, RELATIVE, lambda k, t: logs.append(f"{k} {t}"))
    assert url == f"http://127.0.0.1:{port}{ORION_FALLBACK_PATH}{RELATIVE}"
    assert any("registry" in line for line in logs)


@pytest.mark.parametrize(
    "location",
    [
        "{base}/core02/svc-bmx-adapter-orion/prod/orion/station?data=eyJuYW1lIjoiUmFkaW8ifQ%3D%3D",
        "{base}/custom/v1/playback/eyJuYW1lIjoiUmFkaW8ifQ",
    ],
    ids=["absolute-orion", "legacy-playback"],
)
async def test_an_absolute_location_is_fetched_as_it_is_and_the_registry_is_never_asked(location: str) -> None:
    """Both older spellings name the station completely; only the relative one needs a base."""
    async with _Server({}) as server:
        url = location.replace("{base}", server.base)
        server.routes[url.removeprefix(server.base)] = STREAM
        await _first_bytes_of(url, OrionBase(server.base))
        assert BMX_REGISTRY_PATH not in server.asked
        assert server.asked[0] == url.removeprefix(server.base)


async def test_the_registry_is_read_once_per_resolver_and_not_once_per_station() -> None:
    """The master holds one resolver for its whole run; every station after the first reuses the base."""
    orion_path = "/prod/orion"
    routes = {BMX_REGISTRY_PATH: _registry(LOCAL_INTERNET_RADIO="{base}" + orion_path)}
    async with _Server(routes) as server:
        orion = OrionBase(server.base)
        async with httpx.AsyncClient(timeout=5.0) as client:
            first = await orion.absolute(client, RELATIVE, lambda _k, _t: None)
            second = await orion.absolute(client, "/station?data=b3RoZXI%3D", lambda _k, _t: None)
        assert first == f"{server.base}{orion_path}{RELATIVE}"
        assert second == f"{server.base}{orion_path}/station?data=b3RoZXI%3D"
        assert server.asked.count(BMX_REGISTRY_PATH) == 1
