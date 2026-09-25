"""What a station URL has to be followed through before it is a stream of bytes.

A real HTTP server on loopback, not a patched client: the station in this house hands out a JSON
playback descriptor, that descriptor names a playlist, and the playlist names the stream. Each hop
is what the master has to survive before a single byte reaches a speaker.
"""

from __future__ import annotations

import asyncio
import json
from typing import Self

import httpx
import pytest

from soundtouch_zonemaster.adapters.soundtouch.source import resolve_stream_url

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
