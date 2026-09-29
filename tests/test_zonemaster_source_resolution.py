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

from soundtouch_zonemaster.adapters.cli.prototype import parse_options
from soundtouch_zonemaster.adapters.soundtouch.clock import now_us
from soundtouch_zonemaster.adapters.soundtouch.orion import (
    BMX_REGISTRY_PATH,
    ORION_FALLBACK_PATH,
    REGISTRY_MAX_BYTES,
    REGISTRY_TIMEOUT_S,
    OrionBase,
)
from soundtouch_zonemaster.adapters.soundtouch.source import RingBuffer, StreamSource, resolve_stream_url
from soundtouch_zonemaster.adapters.soundtouch.zone_master import FIRST_BYTES_TIMEOUT_S
from soundtouch_zonemaster.composition import open_prototype_master
from soundtouch_zonemaster.domain.station import Station

pytestmark = pytest.mark.asyncio


class _Server:
    """Serves a fixed body and content type per path, and records what was asked for.

    ``statuses`` answers a path with another status than 200, ``headers`` adds lines to its
    answer (a ``Location`` for a redirect), and ``delays`` holds its answer back, which is how two
    readers are made to be inside one lookup at once.
    """

    def __init__(
        self,
        routes: dict[str, tuple[str, str]],
        *,
        statuses: dict[str, int] | None = None,
        headers: dict[str, str] | None = None,
        delays: dict[str, float] | None = None,
    ) -> None:
        self.routes = routes
        self.statuses = statuses if statuses is not None else {}
        self.headers = headers if headers is not None else {}
        self.delays = delays if delays is not None else {}
        self.asked: list[str] = []
        self.base = ""
        self._srv: asyncio.AbstractServer | None = None

    async def __aenter__(self) -> Self:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            head = await reader.readuntil(b"\r\n\r\n")
            path = head.decode().split(" ")[1]
            self.asked.append(path)
            await asyncio.sleep(self.delays.get(path, 0.0))
            ctype, body = self.routes.get(path, ("text/plain", "not found"))
            raw = body.replace("{base}", self.base).encode()
            status = self.statuses.get(path, 200)
            extra = self.headers.get(path, "").replace("{base}", self.base)
            writer.write(
                f"HTTP/1.1 {status} X\r\nContent-Type: {ctype}\r\nContent-Length: {len(raw)}\r\n{extra}\r\n".encode()
                + raw
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


def _resolver(base: str, logs: list[str], **tuning: float) -> OrionBase:
    """The real resolver against ``base``, saying what it does into ``logs``."""
    return OrionBase(base, log=lambda k, t: logs.append(f"{k} {t}"), **tuning)


async def _first_bytes_of(station_url: str, orion: OrionBase, logs: list[str]) -> StreamSource:
    """Run a real source against ``station_url`` until its first bytes, then stop it.

    Bounded by the wall clock rather than by anything the source controls, and it fails naming
    what did not happen, so a resolver that never completes reads as that and not as a hang.
    """
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
    except TimeoutError:
        pytest.fail(f"no first bytes within 5 s; the source said {logs}")
    finally:
        await source.stop()
    return source


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
        logs: list[str] = []
        source = await _first_bytes_of(RELATIVE, _resolver(server.base, logs), logs)
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
        _registry(LOCAL_INTERNET_RADIO="http:///no/host"),
        _registry(LOCAL_INTERNET_RADIO="{base}/orion?via=registry"),
        _registry(LOCAL_INTERNET_RADIO="{base}/orion#top"),
        _registry(LOCAL_INTERNET_RADIO="{base}/orion?"),
    ],
    ids=[
        "not-json",
        "not-a-registry",
        "no-radio-entry",
        "no-scheme",
        "empty-base",
        "no-host",
        "query-in-base",
        "fragment-in-base",
        "bare-question-mark",
    ],
)
async def test_an_unreadable_registry_falls_back_to_the_known_orion_path_and_says_so(
    answer: tuple[str, str],
) -> None:
    """AfterTouch's own adapter path under the service base, rather than failing the station.

    A registry that cannot answer is a neighbour having a bad moment; the Orion adapter it would
    have named sits at a fixed path under the same service, so the house keeps playing. A base
    with a query or a fragment in it is not one: the location is APPENDED, so it would land inside
    the query or after the fragment and name something the adapter never served.
    """
    routes = {BMX_REGISTRY_PATH: answer, f"{ORION_FALLBACK_PATH}{RELATIVE}": STREAM}
    async with _Server(routes) as server:
        logs: list[str] = []
        source = await _first_bytes_of(RELATIVE, _resolver(server.base, logs), logs)
        assert f"{ORION_FALLBACK_PATH}{RELATIVE}" in server.asked
        assert source.ring.end_offset > 0
        assert any("registry" in line and f"{server.base}{ORION_FALLBACK_PATH}" in line for line in logs), logs


async def test_a_registry_answering_an_error_status_falls_back_and_names_the_status() -> None:
    """The one refusal a working HTTP server gives, and the reason it gives is in the log line."""
    routes = {BMX_REGISTRY_PATH: _registry(LOCAL_INTERNET_RADIO="{base}/orion")}
    async with _Server(routes, statuses={BMX_REGISTRY_PATH: 500}) as server:
        logs: list[str] = []
        url = await _resolver(server.base, logs).absolute(RELATIVE)
    assert url == f"{server.base}{ORION_FALLBACK_PATH}{RELATIVE}", "a 500 carrying a registry is still a 500"
    assert any("answered 500" in line for line in logs), logs


async def test_an_unreachable_registry_falls_back_without_waiting_for_ever() -> None:
    """Nothing listening at all is the other way a registry fails, and it must not end the station."""
    closed = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", 0)
    port = closed.sockets[0].getsockname()[1]
    closed.close()
    await closed.wait_closed()
    logs: list[str] = []
    url = await _resolver(f"http://127.0.0.1:{port}", logs).absolute(RELATIVE)
    assert url == f"http://127.0.0.1:{port}{ORION_FALLBACK_PATH}{RELATIVE}"
    assert any("registry" in line for line in logs)


@pytest.mark.parametrize(
    "service_url",
    [
        # Neither is an httpx.HTTPError: a port past 65535 surfaces from the socket layer as an
        # OverflowError inside an exception group, and a control character as httpx.InvalidURL.
        pytest.param("http://127.0.0.1:99999", id="port-out-of-range"),
        pytest.param("http://127.0.0.1:8000/\x01", id="control-character-in-the-path"),
        pytest.param("http://\u2603\u2603..example", id="host-that-is-no-name"),
    ],
)
async def test_a_registry_url_no_request_can_be_sent_to_falls_back_and_backs_off(service_url: str) -> None:
    """A mistyped ``[registry] url`` is the registry not answering, never an exception to the caller.

    A station start sits behind this call and must not raise from it, so the address is refused
    the way an unreadable registry is: the fallback answers, the log says why, and the back-off is
    recorded, which the second lookup shows by saying nothing new.
    """
    logs: list[str] = []
    orion = _resolver(service_url, logs)
    first = await orion.absolute(RELATIVE)
    second = await orion.absolute(RELATIVE)
    assert first == second == f"{service_url}{ORION_FALLBACK_PATH}{RELATIVE}"
    refusals = [line for line in logs if "is not an address a request can be sent to" in line]
    assert len(refusals) == 1, f"refused once and then remembered for its back-off: {logs}"


async def test_a_registry_that_accepts_and_never_answers_costs_its_timeout_and_no_more() -> None:
    """The failure a refused port cannot show: a connection that is taken and then left silent.

    Only the resolver's own asyncio deadline ends this, since the client it reads through has none
    (``client_without_deadline``). The wait around it is the test's own and longer, so a resolver
    that lost its deadline fails here by name instead of hanging the suite.
    """
    held: list[asyncio.StreamWriter] = []

    async def say_nothing(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        held.append(writer)

    wedged = await asyncio.start_server(say_nothing, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{wedged.sockets[0].getsockname()[1]}"
    logs: list[str] = []
    started = asyncio.get_running_loop().time()
    try:
        async with asyncio.timeout(3.0):
            url = await _resolver(base, logs, timeout_s=0.3).absolute(RELATIVE)
    except TimeoutError:
        pytest.fail("the registry read has no deadline of its own: a silent registry held it for ever")
    finally:
        for writer in held:
            writer.close()
        wedged.close()
        await wedged.wait_closed()
    elapsed = asyncio.get_running_loop().time() - started
    assert url == f"{base}{ORION_FALLBACK_PATH}{RELATIVE}"
    assert elapsed < 1.5, f"a 0.3 s registry deadline took {elapsed:.2f} s"
    assert any("could not be reached (TimeoutError)" in line for line in logs), logs


async def test_the_registry_deadline_leaves_the_station_most_of_its_first_bytes_window() -> None:
    """The registry is read INSIDE the master's wait for a station's first bytes, so its deadline
    must be a small part of that window rather than nearly all of it."""
    assert REGISTRY_TIMEOUT_S * 4 <= FIRST_BYTES_TIMEOUT_S


async def test_a_registry_that_could_not_be_read_is_not_asked_again_on_the_next_station() -> None:
    """A stalled or broken registry costs its delay once, not on every start and every reconnect."""
    async with _Server({}, statuses={BMX_REGISTRY_PATH: 500}) as server:
        logs: list[str] = []
        orion = _resolver(server.base, logs)
        first = await orion.absolute(RELATIVE)
        second = await orion.absolute("/station?data=b3RoZXI%3D")
    assert first.startswith(f"{server.base}{ORION_FALLBACK_PATH}")
    assert second == f"{server.base}{ORION_FALLBACK_PATH}/station?data=b3RoZXI%3D"
    assert server.asked.count(BMX_REGISTRY_PATH) == 1


async def test_a_registry_that_could_not_be_read_is_asked_again_once_its_back_off_is_over() -> None:
    """The fallback is not remembered for good: a neighbour that recovers is found again."""
    async with _Server({}, statuses={BMX_REGISTRY_PATH: 500}) as server:
        orion = _resolver(server.base, [], retry_after_s=0.0)
        await orion.absolute(RELATIVE)
        await orion.absolute(RELATIVE)
    assert server.asked.count(BMX_REGISTRY_PATH) == 2


async def test_a_registry_body_past_the_bound_is_refused_rather_than_read_whole() -> None:
    """A valid registry padded past 64 KiB: read whole it would name its base, bounded it is refused."""
    ctype, body = _registry(LOCAL_INTERNET_RADIO="{base}/orion")
    padded = body + " " * (REGISTRY_MAX_BYTES + 1)
    async with _Server({BMX_REGISTRY_PATH: (ctype, padded)}) as server:
        logs: list[str] = []
        url = await _resolver(server.base, logs).absolute(RELATIVE)
    assert url == f"{server.base}{ORION_FALLBACK_PATH}{RELATIVE}"
    assert any(f"more than {REGISTRY_MAX_BYTES} bytes" in line for line in logs), logs


async def test_a_registry_body_just_under_the_bound_is_still_read() -> None:
    """The control for the bound: the same document, padded to just inside it, names its base."""
    ctype, body = _registry(LOCAL_INTERNET_RADIO="{base}/orion")
    async with _Server({}) as server:
        raw = body.replace("{base}", server.base)
        server.routes[BMX_REGISTRY_PATH] = (ctype, raw + " " * (REGISTRY_MAX_BYTES - len(raw)))
        url = await _resolver(server.base, []).absolute(RELATIVE)
    assert url == f"{server.base}/orion{RELATIVE}"


async def test_a_registry_that_redirects_is_not_followed() -> None:
    """The registry is the service's own, at the address it was configured with; an answer sending
    the master elsewhere is not the registry answering, however good the document at the far end."""
    routes = {"/elsewhere": _registry(LOCAL_INTERNET_RADIO="{base}/moved")}
    async with _Server(
        routes,
        statuses={BMX_REGISTRY_PATH: 302},
        headers={BMX_REGISTRY_PATH: "Location: {base}/elsewhere\r\n"},
    ) as server:
        logs: list[str] = []
        url = await _resolver(server.base, logs).absolute(RELATIVE)
    assert url == f"{server.base}{ORION_FALLBACK_PATH}{RELATIVE}"
    assert "/elsewhere" not in server.asked
    assert any("answered 302" in line for line in logs), logs


async def test_a_base_ending_in_a_slash_is_joined_without_a_double_slash() -> None:
    """``baseUrl`` is somebody else's document, and a trailing slash in it is an ordinary spelling."""
    routes = {BMX_REGISTRY_PATH: _registry(LOCAL_INTERNET_RADIO="{base}/prod/orion/")}
    async with _Server(routes) as server:
        url = await _resolver(server.base, []).absolute(RELATIVE)
    assert url == f"{server.base}/prod/orion{RELATIVE}"


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
        logs: list[str] = []
        await _first_bytes_of(url, _resolver(server.base, logs), logs)
        assert BMX_REGISTRY_PATH not in server.asked
        assert server.asked[0] == url.removeprefix(server.base)


async def test_the_registry_is_read_once_per_resolver_and_not_once_per_station() -> None:
    """The service holds one resolver for its whole run; every station after the first reuses the base."""
    orion_path = "/prod/orion"
    routes = {BMX_REGISTRY_PATH: _registry(LOCAL_INTERNET_RADIO="{base}" + orion_path)}
    async with _Server(routes) as server:
        orion = _resolver(server.base, [])
        first = await orion.absolute(RELATIVE)
        second = await orion.absolute("/station?data=b3RoZXI%3D")
        assert first == f"{server.base}{orion_path}{RELATIVE}"
        assert second == f"{server.base}{orion_path}/station?data=b3RoZXI%3D"
        assert server.asked.count(BMX_REGISTRY_PATH) == 1


async def test_two_stations_starting_at_once_ask_the_registry_once_between_them() -> None:
    """Two lookups inside one slow registry read: the second waits for the first's answer."""
    routes = {BMX_REGISTRY_PATH: _registry(LOCAL_INTERNET_RADIO="{base}/orion")}
    async with _Server(routes, delays={BMX_REGISTRY_PATH: 0.2}) as server:
        orion = _resolver(server.base, [])
        first, second = await asyncio.gather(orion.absolute(RELATIVE), orion.absolute("/station?data=Mg%3D%3D"))
    assert first == f"{server.base}/orion{RELATIVE}"
    assert second == f"{server.base}/orion/station?data=Mg%3D%3D"
    assert server.asked.count(BMX_REGISTRY_PATH) == 1


# --- what a SPEAKER is handed: absolute only against a base the registry has already named --------


async def _until_the_registry_was_asked(server: _Server) -> None:
    """Wait, bounded by the clock, for a read nobody in the test started: the background one."""
    try:
        async with asyncio.timeout(3.0):
            while BMX_REGISTRY_PATH not in server.asked:
                await asyncio.sleep(0.01)
    except TimeoutError:
        pytest.fail("nothing read the registry in the background")


async def test_a_speaker_is_handed_the_location_as_stored_until_the_registry_has_named_a_base() -> None:
    """The first document goes out relative and at once; the read it starts makes the next absolute.

    The registry is slow on purpose: a document that waited for it would be as slow, and a speaker
    document built on a person's press has no business waiting on a neighbour. The later
    ``absolute`` call waits for the background read under the resolver's lock, which is also what
    proves the two share one read.
    """
    routes = {BMX_REGISTRY_PATH: _registry(LOCAL_INTERNET_RADIO="{base}/orion")}
    async with _Server(routes, delays={BMX_REGISTRY_PATH: 0.3}) as server:
        logs: list[str] = []
        orion = _resolver(server.base, logs)
        first = orion.for_a_speaker(RELATIVE)
        await _until_the_registry_was_asked(server)
        fetched = await orion.absolute(RELATIVE)
        second = orion.for_a_speaker(RELATIVE)
    assert first == RELATIVE, "nothing named a base yet, so the speaker gets the location as stored"
    assert second == fetched == f"{server.base}/orion{RELATIVE}", "and once the registry named one, the absolute"
    assert server.asked.count(BMX_REGISTRY_PATH) == 1, "the background read and the fetch shared one read"
    assert any("as stored" in line for line in logs), logs


async def test_a_speaker_is_never_handed_the_fallback_base() -> None:
    """The fallback is a guess about the service's layout, good enough for the master's own fetch.

    A speaker completes a relative location through its own registry, so handing it a guessed
    absolute one could only be worse - with the shipped loopback ``[registry] url`` it names the
    speaker's OWN loopback, where nothing answers, and a box that stores it keeps it for good.
    """
    async with _Server({}, statuses={BMX_REGISTRY_PATH: 500}) as server:
        orion = _resolver(server.base, [])
        first = orion.for_a_speaker(RELATIVE)
        await _until_the_registry_was_asked(server)
        fetched = await orion.absolute(RELATIVE)
        second = orion.for_a_speaker(RELATIVE)
    assert fetched == f"{server.base}{ORION_FALLBACK_PATH}{RELATIVE}", "the control: the fetch did fall back"
    assert first == second == RELATIVE
    assert server.asked.count(BMX_REGISTRY_PATH) == 1, "the back-off holds for a speaker document too"


async def test_any_number_of_speaker_documents_start_one_background_read() -> None:
    """Every box on a relative channel is sent a document at once; the registry is read once."""
    routes = {BMX_REGISTRY_PATH: _registry(LOCAL_INTERNET_RADIO="{base}/orion")}
    async with _Server(routes, delays={BMX_REGISTRY_PATH: 0.2}) as server:
        orion = _resolver(server.base, [])
        sent = [orion.for_a_speaker(RELATIVE) for _ in range(5)]
        await _until_the_registry_was_asked(server)
        await orion.absolute(RELATIVE)
    assert sent == [RELATIVE] * 5
    assert server.asked.count(BMX_REGISTRY_PATH) == 1


async def test_an_absolute_location_is_handed_to_a_speaker_as_it_is_and_reads_nothing() -> None:
    async with _Server({}) as server:
        orion = _resolver(server.base, [])
        sent = orion.for_a_speaker(f"{server.base}/orion{RELATIVE}")
        await asyncio.sleep(0.1)
    assert sent == f"{server.base}/orion{RELATIVE}"
    assert server.asked == [], "only a relative location has anything to complete"


async def test_closing_the_resolver_ends_a_background_read_still_waiting_on_the_registry() -> None:
    """The end of a run must not wait out a registry that took the connection and said nothing.

    Three documents are built while the read waits, so a resolver that started a read per document
    would leave two of them behind a close that ended only the last; every task it started must be
    gone afterwards.
    """
    held: list[asyncio.StreamWriter] = []

    async def say_nothing(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        held.append(writer)

    wedged = await asyncio.start_server(say_nothing, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{wedged.sockets[0].getsockname()[1]}"
    orion = _resolver(base, [], timeout_s=30.0)
    before = asyncio.all_tasks()
    try:
        assert [orion.for_a_speaker(RELATIVE) for _ in range(3)] == [RELATIVE] * 3
        async with asyncio.timeout(3.0):
            while not held:
                await asyncio.sleep(0.01)
        closing = asyncio.create_task(orion.close())
        done, _ = await asyncio.wait({closing}, timeout=1.0)
        assert done, "close waited for the registry instead of ending the read"
        assert closing.exception() is None
        await asyncio.sleep(0)
        assert asyncio.all_tasks() - {closing} == before, "a read the resolver started outlived its close"
    finally:
        for writer in held:
            writer.close()
        wedged.close()
        await wedged.wait_closed()


async def test_a_fetch_that_fails_against_the_named_base_makes_the_next_one_ask_the_registry_again() -> None:
    """An Orion adapter that moved is found again: the base it was at is forgotten when it fails.

    The registry names ``/old``, which answers 503; by the time the source reconnects the registry
    names ``/new``, which serves the stream. Remembered for good, the old base would be asked on
    every reconnect for the rest of the run and the station would never play.
    """
    routes = {
        BMX_REGISTRY_PATH: _registry(LOCAL_INTERNET_RADIO="{base}/old"),
        f"/old{RELATIVE}": STREAM,
        f"/new{RELATIVE}": STREAM,
    }
    async with _Server(routes, statuses={f"/old{RELATIVE}": 503}) as server:
        logs: list[str] = []
        orion = _resolver(server.base, logs)

        # The move happens as soon as the old base has been asked once; the reconnect comes a
        # backoff later (one second), long after this has rewritten the route.
        async def move_when_the_old_base_is_asked() -> None:
            while f"/old{RELATIVE}" not in server.asked:
                await asyncio.sleep(0.01)
            server.routes[BMX_REGISTRY_PATH] = _registry(LOCAL_INTERNET_RADIO="{base}/new")

        mover = asyncio.create_task(move_when_the_old_base_is_asked())
        try:
            source = await _first_bytes_of(RELATIVE, orion, logs)
        finally:
            mover.cancel()
        assert source.ring.end_offset > 0
        assert server.asked.count(BMX_REGISTRY_PATH) == 2, server.asked
        assert f"/new{RELATIVE}" in server.asked
        assert any("asked again next time" in line for line in logs), logs


async def test_a_base_nobody_answers_at_is_forgotten_as_well_as_one_answering_an_error() -> None:
    """The other way a fetch fails before a byte: the connection itself is refused.

    The registry names a base on a port nothing listens on, and is rewritten to name the live one
    only once the source has been handed the dead base - so the stream plays only if the refusal
    made the resolver ask again.
    """
    closed = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", 0)
    dead = f"http://127.0.0.1:{closed.sockets[0].getsockname()[1]}"
    closed.close()
    await closed.wait_closed()
    routes = {BMX_REGISTRY_PATH: _registry(LOCAL_INTERNET_RADIO=f"{dead}/old"), f"/new{RELATIVE}": STREAM}
    async with _Server(routes) as server:
        logs: list[str] = []
        orion = _resolver(server.base, logs)

        async def move_once_the_dead_base_is_handed_out() -> None:
            while not any(f"-> {dead}/old" in line for line in logs):
                await asyncio.sleep(0.01)
            server.routes[BMX_REGISTRY_PATH] = _registry(LOCAL_INTERNET_RADIO="{base}/new")

        mover = asyncio.create_task(move_once_the_dead_base_is_handed_out())
        try:
            source = await _first_bytes_of(RELATIVE, orion, logs)
        finally:
            mover.cancel()
        assert source.ring.end_offset > 0
        assert server.asked.count(BMX_REGISTRY_PATH) == 2, server.asked
        assert any(f"a fetch against {dead}/old failed" in line for line in logs), logs


async def test_a_failed_fetch_of_a_url_the_base_did_not_complete_keeps_the_base() -> None:
    """Only a url completed against the remembered base can say anything about that base."""
    routes = {BMX_REGISTRY_PATH: _registry(LOCAL_INTERNET_RADIO="{base}/orion")}
    async with _Server(routes) as server:
        orion = _resolver(server.base, [])
        await orion.absolute(RELATIVE)
        orion.forget(f"{server.base}/orionette{RELATIVE}")
        orion.forget("http://192.0.2.1/elsewhere")
        await orion.absolute(RELATIVE)
    assert server.asked.count(BMX_REGISTRY_PATH) == 1


async def test_the_prototype_s_master_completes_against_the_configured_registry() -> None:
    """``[registry] url`` reaches the prototype's master as it reaches the service's: through the
    composition root's own builder, with the option record the CLI produces."""
    routes = {BMX_REGISTRY_PATH: _registry(LOCAL_INTERNET_RADIO="{base}/orion")}
    async with _Server(routes) as server:
        options = parse_options(
            bind_ip="127.0.0.1",
            device_id="AABBCC0000A1",
            slaves=("192.0.2.10",),
            preset_from="192.0.2.10",
            preset=1,
            preset2=2,
            switch_after=0.0,
            duration=1.0,
            encryption="none",
            late_slaves=(),
            join_after=30.0,
            join_mode="schedule",
            ignore_selects=False,
            never_touch=(),
            registry_url=server.base,
        )
        master = open_prototype_master(options, log=lambda _k, _t: None)
        url = await master.orion.absolute(RELATIVE)
    assert url == f"{server.base}/orion{RELATIVE}"
    assert server.asked == [BMX_REGISTRY_PATH]
