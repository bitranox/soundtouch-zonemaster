"""MPD's line protocol, against the shapes a real MPD 0.24.6 produced on 2026-09-10.

Every assertion here is on what the fake RECEIVED, not on what the client returned. A fake that
answers OK to anything answers OK to a misspelled verb too, so asserting on the return value would
pass with the bytes wrong - and the bytes are the whole of what this module is.

Four of the five facts these tests pin were measured rather than reasoned, and the measurement is
``docs/measurements/2026-09-10-mpd-httpd-and-queue.md``: ``load`` APPENDS to the one queue, a
stopped ``status`` omits its position keys entirely, ``seekcur`` while stopped is refused outright,
and an unquoted argument is MISREAD rather than rejected.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import shutil
import socket
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from soundtouch_zonemaster.adapters.mpd.client import MpdControl, quote
from soundtouch_zonemaster.application.errors import MpdError, MpdRefusalError, NotInMpdError
from soundtouch_zonemaster.domain.enums import ChannelEnd, MpdState
from soundtouch_zonemaster.domain.frames import find_frame
from soundtouch_zonemaster.domain.playorder import play_order
from soundtouch_zonemaster.domain.state import Place

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from soundtouch_zonemaster.domain.mpd import MpdStatus

from mpdfake import HOST, FakeMpd

# No module-level `pytestmark = pytest.mark.asyncio`, unlike the neighbouring async suites:
# `asyncio_mode = "auto"` already runs the coroutines here, and this module holds one SYNCHRONOUS
# test (quote is a pure function), which that mark would warn about on every run.


def _silent(_kind: str, _text: str) -> None:
    """A log that says nothing: these tests are about the wire, not about the narration."""


def test_quote_escapes_what_mpd_would_otherwise_misread() -> None:
    """Measured: an unquoted name with a space made ``load`` complain about an INTEGER.

    ``ACK [2@0] {load} Integer or range expected: quoted`` - because ``load`` takes an optional
    range as its second argument, so the second word was read as one. MPD never said the name was
    bad, which is why a name somebody typed into the channel file goes through here rather than
    being trusted to be plain.
    """
    assert quote('a "quoted" name') == '"a \\"quoted\\" name"'
    assert quote("back\\slash") == '"back\\\\slash"'
    assert quote("plain") == '"plain"'


async def test_playing_an_entry_clears_first_because_load_appends() -> None:
    """Measured: ``load`` APPENDS. Without the clear the house accumulates every channel it plays."""
    fake = FakeMpd()
    await fake.start()
    client = MpdControl(HOST, fake.port, log=_silent)
    await client.connect()
    await client.play_entry("hoerbuecher", end=ChannelEnd.WRAP)
    await client.close()
    await fake.stop()

    assert fake.seen[:4] == ["clear", "repeat 1", 'load "hoerbuecher"', "play 0"]


async def test_a_channel_that_stops_at_its_end_is_played_with_repeat_off() -> None:
    """``repeat`` is MPD's one setting for the whole daemon and it survives a restart in its state
    file, so it is said on EVERY load: a channel that relied on what the previous one left would
    wrap or stop depending on which channel somebody dialled before it."""
    fake = FakeMpd()
    await fake.start()
    client = MpdControl(HOST, fake.port, log=_silent)
    await client.connect()
    await client.play_entry("buch", end=ChannelEnd.STOP)
    await client.close()
    await fake.stop()

    assert fake.seen[:4] == ["clear", "repeat 0", 'load "buch"', "play 0"]


async def test_playing_one_entry_of_the_queue_names_it() -> None:
    """A press moves to an entry the SERVICE worked out, because MPD's own next stops at the end."""
    fake = FakeMpd()
    await fake.start()
    client = MpdControl(HOST, fake.port, log=_silent)
    await client.connect()
    await client.play_at(3)
    await client.close()
    await fake.stop()

    assert fake.seen == ["play 3"]


async def test_resuming_a_position_is_one_seek_and_never_a_play_then_a_seekcur() -> None:
    """The crash fix, pinned as the exact bytes: the resume names the entry AND the offset at once.

    ``play <track>`` followed by ``seekcur <seconds>`` - whether in a command list or not -
    SEGFAULTS MPD 0.24.6, because the seek arrives before the decoder has opened the song and
    there is no duration to seek within (upstream MusicPlayerDaemon/MPD issue 276, open since
    0.20.18). Measured on a container host 2026-09-21: interleaved A/B, two rounds each,
    the command list died on SIGSEGV every time and the same change without a seek survived every
    time. Every MPD channel in the house carries a position, so every channel change did it.

    ``seek {SONGPOS} {TIME}`` starts the decoder AT the offset, so there is nothing to race.
    """
    fake = FakeMpd()
    await fake.start()
    client = MpdControl(HOST, fake.port, log=_silent)
    await client.connect()
    await client.play_entry("hoerbuecher", place=Place(track=3, seconds=45.5), end=ChannelEnd.WRAP)
    await client.close()
    await fake.stop()

    assert fake.seen == ["clear", "repeat 1", 'load "hoerbuecher"', "seek 3 45.500"]
    assert not [line for line in fake.seen if line.startswith("seekcur")], (
        "seekcur is what crashes it, whatever it is wrapped in"
    )
    assert "command_list_begin" not in fake.seen, "and the list existed only to carry the seekcur"


async def _connected(fake: FakeMpd) -> MpdControl:
    await fake.start()
    client = MpdControl(HOST, fake.port, log=_silent)
    await client.connect()
    return client


async def test_the_files_under_a_directory_are_listed_with_their_paths_and_nothing_else() -> None:
    """``listall`` names directories too; a queue is built from FILES, so only those come back.

    In MPD's own order: putting them in the order a channel plays them is the domain's rule, and a
    client that sorted as well would be a second rule nobody tests against the first.
    """
    fake = FakeMpd(directories={"Buch": ["Buch/10.mp3", "Buch/2.mp3", "Buch/Bonus/01.mp3"]})
    client = await _connected(fake)
    files = await client.files_under("Buch")
    await client.close()
    await fake.stop()

    assert fake.seen == ['listall "Buch"']
    assert files == ("Buch/10.mp3", "Buch/2.mp3", "Buch/Bonus/01.mp3")


async def test_a_directory_mpd_does_not_have_is_the_house_mistake_type() -> None:
    fake = FakeMpd()
    client = await _connected(fake)
    with pytest.raises(NotInMpdError) as caught:
        await client.files_under("Nichtda")
    await client.close()
    await fake.stop()

    assert caught.value.command == "listall"


async def test_playing_files_builds_the_queue_one_add_per_file_in_the_order_given() -> None:
    """One ``add`` per file rather than one for the directory: MPD would add a directory in its
    DATABASE order, and the order a channel plays in is ours (OPEN-WORK rank 11). One command list,
    so the queue is never half built while somebody listens."""
    fake = FakeMpd()
    client = await _connected(fake)
    await client.play_files(("Buch/2.mp3", 'Buch/a "quoted" one.mp3'), end=ChannelEnd.STOP)
    await client.close()
    await fake.stop()

    assert fake.seen == [
        "clear",
        "repeat 0",
        "command_list_begin",
        'add "Buch/2.mp3"',
        'add "Buch/a \\"quoted\\" one.mp3"',
        "command_list_end",
        "play 0",
    ]
    assert fake.queue == ["Buch/2.mp3", 'Buch/a "quoted" one.mp3']


async def test_playing_files_from_a_place_is_the_one_seek_a_playlist_resume_is() -> None:
    fake = FakeMpd()
    client = await _connected(fake)
    await client.play_files(("a.mp3", "b.mp3"), place=Place(track=1, seconds=41.5), end=ChannelEnd.WRAP)
    await client.close()
    await fake.stop()

    assert fake.seen[-1] == "seek 1 41.500"
    assert "play 0" not in fake.seen


async def test_playing_no_files_empties_the_queue_and_plays_nothing() -> None:
    """An empty directory still takes the house OFF the previous channel's queue: left alone, the
    stream would go on playing the channel somebody just dialled away from, under the new name."""
    fake = FakeMpd(queue=["old/1.mp3"])
    client = await _connected(fake)
    await client.play_files((), end=ChannelEnd.WRAP)
    await client.close()
    await fake.stop()

    assert fake.seen == ["clear", "repeat 1"]
    assert fake.queue == []


async def test_the_queue_is_read_back_as_its_file_paths_in_queue_order() -> None:
    fake = FakeMpd(queue=["Buch/2.mp3", "Buch/10.mp3"])
    client = await _connected(fake)
    files = await client.queue_files()
    await client.close()
    await fake.stop()

    assert fake.seen == ["playlistinfo"]
    assert files == ("Buch/2.mp3", "Buch/10.mp3")


async def test_a_missing_playlist_is_its_own_type_and_still_carries_the_code() -> None:
    """A missing playlist is a house configuration mistake and must be told from a dead MPD.

    One is somebody's typo in the channel file and the other is a service that needs restarting,
    and a caller that cannot tell them apart can only report the wrong one. It is a TYPE rather
    than a code the caller compares, because the caller is in ``application`` and the code is
    MPD's wire; the code is still carried for a message a person reads.
    """
    fake = FakeMpd(refuse={"load": "No such playlist"})
    await fake.start()
    client = MpdControl(HOST, fake.port, log=_silent)
    await client.connect()
    with pytest.raises(NotInMpdError) as caught:
        await client.play_entry("gone", end=ChannelEnd.WRAP)
    await client.close()
    await fake.stop()

    assert caught.value.code == 50
    assert caught.value.command == "load"
    assert "No such playlist" in caught.value.message


async def test_a_refusal_that_is_not_a_missing_playlist_is_not_reported_as_one() -> None:
    """The control the mapping needs: a code that must come out the OTHER way.

    Without it, a mapping that answered ``NotInMpdError`` to every refusal would pass the
    test above, and the service would report a mistyped channel name for a command it got wrong.
    """
    fake = FakeMpd(refuse={"load": "unknown command"}, refuse_code=5)
    await fake.start()
    client = MpdControl(HOST, fake.port, log=_silent)
    await client.connect()
    with pytest.raises(MpdRefusalError) as caught:
        await client.play_entry("anything", end=ChannelEnd.WRAP)
    await client.close()
    await fake.stop()

    assert not isinstance(caught.value, NotInMpdError)
    assert caught.value.code == 5


async def test_a_stopped_status_reports_no_position_rather_than_zero() -> None:
    """Measured: a stopped status carries no ``elapsed`` key AT ALL, and no ``song``.

    Reading an absent key as 0.0 would save a position of zero over a real one the next time the
    house changed channel, and nothing would report it - the channel would simply start from the
    beginning next time somebody dialled it.
    """
    fake = FakeMpd(status_lines=["state: stop", "playlistlength: 0"])
    await fake.start()
    client = MpdControl(HOST, fake.port, log=_silent)
    await client.connect()
    status = await client.status()
    await client.close()
    await fake.stop()

    assert status.state is MpdState.STOP
    assert status.elapsed is None
    assert status.song is None
    assert status.playlist_length == 0


async def test_a_playing_status_reads_every_key_it_was_given() -> None:
    """The control for the test above: the same reader must report the values when they ARE there.

    Without it, a status() that returned None for everything would pass the stopped case, which is
    the one assertion that could be satisfied by a reader that does nothing at all.
    """
    fake = FakeMpd(
        status_lines=[
            "state: play",
            "song: 0",
            "elapsed: 45.593",
            "duration: 120.592",
            "playlistlength: 1",
        ]
    )
    await fake.start()
    client = MpdControl(HOST, fake.port, log=_silent)
    await client.connect()
    status = await client.status()
    await client.close()
    await fake.stop()

    assert status.state is MpdState.PLAY
    assert status.song == 0
    assert status.elapsed == pytest.approx(45.593)
    assert status.duration == pytest.approx(120.592)
    assert status.playlist_length == 1


async def _not_an_mpd() -> tuple[asyncio.AbstractServer, int]:
    """A server that greets with something no MPD ever sends."""

    async def greet(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"220 smtp ready\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(greet, HOST, 0)
    return server, int(server.sockets[0].getsockname()[1])


async def _until(condition: Callable[[], bool], what: str, *, seconds: float = 2.0) -> None:
    """Wait for something the event loop has to run to bring about, and fail BY NAME if it never does.

    A bare ``sleep`` would pass for the wrong reason on a fast machine and flake on a slow one.
    This has a deadline and names what it was waiting for, so a regression reports itself rather
    than hanging the suite.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not condition():
        if loop.time() > deadline:
            raise AssertionError(f"timed out after {seconds} s waiting until {what}")
        await asyncio.sleep(0.01)


async def test_an_exchange_on_a_connection_mpd_closed_opens_a_new_one_and_sends_the_command_once() -> None:
    """MPD closes an idle control connection, so most exchanges begin on one that may be gone.

    Measured against the house's own MPD 0.24.6 on 2026-09-20: a ping after 55 s of idleness was
    answered, one after 62 s got EOF. That is ``connection_timeout`` at its 60 s default, which
    this house's ``mpd.conf`` does not set. The service speaks to MPD only when somebody presses
    something, so a connection quiet for longer than a minute is the ORDINARY case here.

    The liveness question is settled BEFORE anything is written, never repaired afterwards, and
    that is the whole point rather than a detail. A step is not idempotent: once the bytes are
    out, a command MPD never read is indistinguishable from one it read and ran, so a client that
    repairs after a failure must choose between losing the keypress and stepping twice. Hence the
    second assertion, which is the one that would catch a fix written as a retry.
    """
    fake = FakeMpd()
    await fake.start()
    client = MpdControl(HOST, fake.port, log=_silent)
    await client.connect()
    await client.command("status")

    # MPD's idle timeout, which from out here looks the same as MPD being restarted.
    fake.drop_connections()
    await _until(client.peer_has_gone, "the client can see that mpd closed the connection")

    await client.play_at(1)
    await client.close()
    await fake.stop()

    assert fake.connections == 2, "the closed connection was written into instead of being replaced"
    assert fake.seen.count("play 1") == 1, f"the step must arrive exactly once, not {fake.seen.count('play 1')}"


async def test_a_greeting_that_is_not_mpd_is_refused_by_name() -> None:
    """Something else listening on the port must not be driven as though it were MPD.

    The failure this prevents is not a crash: every command would go out, every answer would be
    read as a refusal or as pairs, and the house would report an MPD problem about a program that
    is not MPD.
    """
    server, port = await _not_an_mpd()
    try:
        client = MpdControl(HOST, port, log=_silent)
        with pytest.raises(MpdError, match="not an MPD"):
            await client.connect()
    finally:
        server.close()
        await server.wait_closed()


# --- the contract proof against a real binary -------------------------------------------------
#
# The fake above can only ever be as right as the measurement it was written from, and the real
# binary cannot run everywhere - CI has no mpd. Neither replaces the other, so both are here and
# this one is `local_only`, which `make test` runs and CI skips by design.

MPD_BINARY = shutil.which("mpd")
FIXTURE = Path(__file__).parent / "fixtures" / "technikumcity-24k.mp3"
ENTRY = "zonemaster-test"
_TRACK_SECONDS = 90.0
"""Long enough that a seek to 45 s lands inside it with room either side."""


def _whole_frames(data: bytes) -> tuple[bytes, int]:
    """The leading run of COMPLETE frames, and how long it plays in microseconds.

    Cutting here is not tidiness. 24000 bytes is not a whole number of MP3 frames, so repeating the
    raw fixture breaks the frame chain at every join; the decoder gives up about a second in, and
    the test then measures a STOPPED mpd while looking exactly like it measured a playing one.
    """
    end, total_us = 0, 0
    while True:
        frame = find_frame(data, end)
        if frame is None or frame.start + frame.length > len(data):
            return data[:end], total_us
        end = frame.start + frame.length
        total_us += frame.duration_us


def _a_free_port() -> int:
    """A port nothing holds right now. MPD does not accept port 0, so one has to be chosen."""
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        return int(probe.getsockname()[1])


async def _wait_for(what: str, ready: Callable[[], Awaitable[bool]], tries: int = 100, pause: float = 0.05) -> None:
    """Poll a coroutine factory until it says yes, bounded by something it does not control.

    The bound is the point. A loop whose only exit is the thing under test HANGS the suite on a
    regression instead of failing it, and names no cause when it does.
    """
    for _ in range(tries):
        if await ready():
            return
        await asyncio.sleep(pause)
    raise AssertionError(f"mpd never became ready: {what} after {tries * pause:.1f}s")


@contextlib.asynccontextmanager
async def real_mpd(tmp_path: Path, *, also: tuple[str, ...] = ()) -> AsyncGenerator[tuple[int, str]]:
    """A real mpd on a free port, holding one stored playlist with one long track in it.

    ``also`` names more files to lay down under the music directory, the same track each time,
    for a test that needs a directory to list.
    """
    music, playlists = tmp_path / "music", tmp_path / "playlists"
    music.mkdir()
    playlists.mkdir()
    whole, per_repeat_us = _whole_frames(FIXTURE.read_bytes())
    assert per_repeat_us > 0, "the fixture yielded no complete frames"
    repeats = math.ceil(_TRACK_SECONDS * 1_000_000 / per_repeat_us)
    for relative in ("track.mp3", *also):
        (music / relative).parent.mkdir(parents=True, exist_ok=True)
        (music / relative).write_bytes(whole * repeats)

    port = _a_free_port()
    config = tmp_path / "mpd.conf"
    config.write_text(
        f'music_directory "{music}"\n'
        f'playlist_directory "{playlists}"\n'
        f'db_file "{tmp_path / "mpd.db"}"\n'
        f'state_file "{tmp_path / "mpd.state"}"\n'
        f'log_file "{tmp_path / "mpd.log"}"\n'
        f'bind_to_address "{HOST}"\n'
        f'port "{port}"\n'
        'audio_output {\n    type "null"\n    name "null"\n}\n',
        encoding="utf-8",
    )

    assert MPD_BINARY is not None
    process = await asyncio.create_subprocess_exec(
        MPD_BINARY,
        "--no-daemon",
        str(config),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    setup = MpdControl(HOST, port, log=_silent)
    try:

        async def listening() -> bool:
            try:
                await setup.connect()
            except (OSError, MpdError):
                return False
            return True

        await _wait_for("listening", listening)
        await setup.command("update")

        async def scanned() -> bool:
            return not any(key == "updating_db" for key, _ in await setup.command("status"))

        await _wait_for("finished scanning", scanned)
        await setup.command(f"add {quote('track.mp3')}")
        await setup.command(f"save {quote(ENTRY)}")
        await setup.command("clear")
        await setup.close()
        yield port, ENTRY
    finally:
        await setup.close()
        process.terminate()
        await process.wait()


@pytest.mark.local_only
@pytest.mark.skipif(MPD_BINARY is None, reason="this test starts a real mpd and there is none on PATH")
async def test_against_a_real_mpd_the_queue_is_replaced_and_the_position_lands(tmp_path: Path) -> None:
    """The contract proof: the fake can only be as right as the measurement it was written from.

    Two calls in a row, because the fact that decides the code is that ``load`` APPENDS. A client
    that omitted the clear would leave a queue of two here and behave correctly in every fake test,
    since a fake that answers OK cannot notice a missing command.
    """
    async with real_mpd(tmp_path) as (port, entry):
        client = MpdControl(HOST, port, log=_silent)
        await client.connect()
        await client.play_entry(entry, end=ChannelEnd.WRAP)
        await client.play_entry(entry, place=Place(track=0, seconds=45.0), end=ChannelEnd.WRAP)
        status = await client.status()
        await client.close()

    assert status.state is MpdState.PLAY
    assert status.playlist_length == 1, "load without a clear would have left two"
    assert status.elapsed is not None
    assert 44.0 < status.elapsed < 48.0, f"the seek did not land: elapsed={status.elapsed}"


async def _until_status(client: MpdControl, what: str, done: Callable[[MpdStatus], bool]) -> MpdStatus:
    """Poll ``status`` until ``done`` says yes, bounded by a clock the player does not control."""
    for _ in range(100):
        status = await client.status()
        if done(status):
            return status
        await asyncio.sleep(0.05)
    raise AssertionError(f"mpd never reached it: {what}")


@pytest.mark.local_only
@pytest.mark.skipif(MPD_BINARY is None, reason="this test starts a real mpd and there is none on PATH")
async def test_against_a_real_mpd_a_stop_channel_runs_out_with_its_queue_still_loaded(tmp_path: Path) -> None:
    """The premise ``MpdStatus.ran_out`` stands on, measured rather than assumed: at the natural
    end with ``repeat`` off MPD stops and KEEPS the queue, which is what tells the end of a book
    from a restarted MPD that lost it. And a press after the end still plays, which is what makes
    the wrap past the end work on a channel that stopped."""
    async with real_mpd(tmp_path) as (port, entry):
        client = MpdControl(HOST, port, log=_silent)
        await client.connect()
        await client.play_entry(entry, place=Place(track=0, seconds=_TRACK_SECONDS - 0.5), end=ChannelEnd.STOP)
        ended = await _until_status(client, "the end of the queue", lambda status: status.state is MpdState.STOP)
        await client.play_at(0)
        again = await _until_status(client, "playing again", lambda status: status.state is MpdState.PLAY)
        await client.close()

    assert ended.ran_out(), f"a queue that ran out must read as run out: {ended}"
    assert again.song == 0


@pytest.mark.local_only
@pytest.mark.skipif(MPD_BINARY is None, reason="this test starts a real mpd and there is none on PATH")
async def test_against_a_real_mpd_a_wrap_channel_starts_again_at_its_end(tmp_path: Path) -> None:
    """The control: the same end with ``repeat`` on goes on playing from the first entry."""
    async with real_mpd(tmp_path) as (port, entry):
        client = MpdControl(HOST, port, log=_silent)
        await client.connect()
        await client.play_entry(entry, place=Place(track=0, seconds=_TRACK_SECONDS - 0.5), end=ChannelEnd.WRAP)
        wrapped = await _until_status(
            client, "the start again", lambda status: status.elapsed is not None and status.elapsed < 5.0
        )
        await client.close()

    assert wrapped.state is MpdState.PLAY
    assert wrapped.song == 0


@pytest.mark.local_only
@pytest.mark.skipif(MPD_BINARY is None, reason="this test starts a real mpd and there is none on PATH")
async def test_against_a_real_mpd_a_directory_plays_in_our_order_and_resumes_in_it(tmp_path: Path) -> None:
    """The directory channel's contract, against the binary: ``listall`` lists the files in MPD's
    own order, the queue keeps the order WE add in, and the resume lands in that order."""
    layout = ("Buch/10.mp3", "Buch/2.mp3", "Buch/Bonus/01.mp3")
    async with real_mpd(tmp_path, also=layout) as (port, _entry):
        client = MpdControl(HOST, port, log=_silent)
        await client.connect()
        listed = await client.files_under("Buch")
        ordered = play_order(listed)
        await client.play_files(ordered, place=Place(track=1, seconds=45.0), end=ChannelEnd.WRAP)
        queue = await client.queue_files()
        status = await client.status()
        with pytest.raises(NotInMpdError):
            await client.files_under("Nichtda")
        await client.close()

    assert sorted(listed) == sorted(layout), "every file, and only the files"
    assert queue == ("Buch/2.mp3", "Buch/10.mp3", "Buch/Bonus/01.mp3"), "the order we added, not MPD's"
    assert status.song == 1
    assert status.elapsed is not None
    assert 44.0 < status.elapsed < 48.0, f"the seek did not land: elapsed={status.elapsed}"
