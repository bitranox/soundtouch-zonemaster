"""MPD's line protocol on 6600, as much of it as a channel needs and no more.

Written rather than taken from a library (user's decision, 2026-09-10) because this project's gate
is pyright strict and ``python-mpd2`` ships no ``py.typed`` and registers its commands dynamically
through ``add_command``, so a typed facade would have had to declare every verb by hand anyway,
which is most of what a client this small is.

Every shape here was measured against a real MPD 0.24.6 on 2026-09-10 and the measurement is
``docs/measurements/2026-09-10-mpd-httpd-and-queue.md``. Four of them decide code:

* ``load`` APPENDS to the one queue, so a channel change is ``clear`` and then ``load``.
* MPD keeps no position per stored playlist, so remembering one is ours.
* ``seekcur`` while stopped is refused outright, so a resume is one command LIST.
* an unquoted argument is MISREAD rather than rejected, which is why :func:`quote` exists.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from ...application.errors import MpdError, MpdRefusalError, NotInMpdError
from ...domain.enums import ChannelEnd, MpdState
from ...domain.mpd import MpdStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ...domain.logfn import LogFn
    from ...domain.state import Place

__all__ = ["NOT_THERE", "MpdControl", "quote"]

_ACK = re.compile(r"^ACK \[(\d+)@(\d+)\] \{([^}]*)\} (.*)$")
"""A refusal, as MPD writes it: ``ACK [50@0] {load} No such playlist``."""

_GREETING_PREFIX = "OK MPD "
NOT_THERE = 50
"""MPD's code for a name it does not have: a house mistake, not a broken MPD.

One code for three things, measured on MPD 0.24.6 on 2026-09-25: ``{load} No such playlist``, and
``{listall} No such directory`` for a directory, and the same words from ``{add}`` for a FILE.

The number lives HERE, with the rest of the wire, and goes no further up: the service is handed
:class:`~soundtouch_zonemaster.application.errors.NotInMpdError` and never a code to compare.
The three error types themselves are the application's, because it is the service that reacts to
each and it may not import this package to name an outcome.
"""


def _refusal(code: int, command: str, message: str) -> MpdRefusalError:
    """One ``ACK`` line as the type the caller branches on, which is the only place the code is read.

    A missing name gets its own type rather than being told apart by :data:`NOT_THERE` at the
    call site, because the call site is in ``application`` and the number is MPD's wire.
    """
    if code == NOT_THERE:
        return NotInMpdError(code, command, message)
    return MpdRefusalError(code, command, message)


def quote(value: str) -> str:
    """One argument the way MPD wants it: double quoted, with backslash and quote escaped.

    Unquoted is not merely refused, it is MISREAD. Measured: ``load a "quoted" name`` came back as
    ``ACK [2@0] {load} Integer or range expected: quoted``, because ``load`` takes an optional
    range as its second argument and the second word was read as one. MPD never said the name was
    bad. A name somebody typed into the channel file therefore goes through here rather than being
    trusted to be plain.
    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class MpdControl:
    """One connection to MPD, opened lazily and re-opened when it has gone."""

    def __init__(self, host: str, port: int, log: LogFn) -> None:
        self.host = host
        self.port = port
        self.log = log
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> str:
        """Open the connection and return the greeting, which names the PROTOCOL version.

        A real 0.24.6 greets with ``OK MPD 0.24.0``, so the number here is not the server's and is
        not worth comparing against one. What IS worth checking is that the greeting is an MPD
        greeting at all: something else listening on the port would take every command, and every
        answer would be read as pairs or as a refusal, so the house would report an MPD problem
        about a program that is not MPD.
        """
        reader, writer = await asyncio.open_connection(self.host, self.port)
        greeting = (await reader.readline()).decode("utf-8").rstrip("\n")
        if not greeting.startswith(_GREETING_PREFIX):
            writer.close()
            raise MpdError(f"not an MPD on {self.host}:{self.port}: {greeting!r}")
        self._reader, self._writer = reader, writer
        self.log("mpd", f"connected to {self.host}:{self.port}, {greeting}")
        return greeting

    async def close(self) -> None:
        """Drop the connection, and forget it before closing so a reconnect cannot race a close."""
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            writer.close()

    def peer_has_gone(self) -> bool:
        """Whether MPD has already closed this connection, answered without writing anything.

        MPD closes an idle control connection after ``connection_timeout``, which is 60 s by
        default and which this house's ``mpd.conf`` does not set. Measured against the house's own
        MPD 0.24.6 on 2026-09-20: a ping after 55 s of idleness was answered, one after 62 s got
        EOF. This service speaks to MPD only when somebody presses something, so a connection that
        has been quiet for longer than a minute is the ORDINARY case here and not an edge.

        asyncio reports it for nothing: the transport feeds EOF to the reader the moment the FIN
        arrives, so this is already true BEFORE a command is written. ``writer.is_closing()`` is
        NOT a signal for it - measured the same day, it read false on a dead connection and on a
        live one alike, so a check including it would be a line that never fires reading like
        care taken.
        """
        return self._reader is not None and self._reader.at_eof()

    async def _a_connection_to_speak_on(self) -> None:
        """Open, or REPLACE, before an exchange. Never repair after one.

        The order is the whole of it. ``load`` is not idempotent - it appends - and once a
        command's bytes are out, one MPD never read is indistinguishable from one it read and ran,
        so a client that repaired after a failure would have to choose between losing the keypress
        and doing it twice. Asking first means the choice never arises.

        Only the connection is replaced here. A command that fails once this has run is a real
        failure and is reported as one, which is what :meth:`close` and the caller's own handling
        are for.
        """
        if self._writer is None:
            await self.connect()
            return
        if self.peer_has_gone():
            await self.close()
            await self.connect()

    async def _send(self, line: str) -> None:
        if self._writer is None:
            await self.connect()
        if self._writer is None:  # pragma: no cover - connect() raises rather than returning unset
            raise MpdError("mpd connection is not open")
        self._writer.write(line.encode("utf-8") + b"\n")
        await self._writer.drain()

    async def _read_to_terminator(self) -> list[tuple[str, str]]:
        """Read until MPD says OK, or raise what it said instead.

        Pairs rather than a dict, and ordered, because several answers repeat a key - a queue
        listing names every entry with the same one - and a dict would keep the last silently.
        """
        if self._reader is None:  # pragma: no cover - only reachable by calling this before _send
            raise MpdError("mpd connection is not open")
        pairs: list[tuple[str, str]] = []
        while True:
            raw = await self._reader.readline()
            if not raw:
                raise MpdError("mpd closed the connection mid-answer")
            line = raw.decode("utf-8").rstrip("\n")
            if line == "OK":
                return pairs
            ack = _ACK.match(line)
            if ack is not None:
                raise _refusal(int(ack.group(1)), ack.group(3), ack.group(4))
            key, _, value = line.partition(": ")
            pairs.append((key, value))

    async def command(self, line: str) -> list[tuple[str, str]]:
        """One command, and its answer as ordered pairs."""
        await self._a_connection_to_speak_on()
        await self._send(line)
        return await self._read_to_terminator()

    async def command_list(self, lines: Sequence[str]) -> None:
        """Several commands MPD applies as ONE, which is how a resume avoids being two states."""
        await self._a_connection_to_speak_on()
        await self._send("command_list_begin")
        for line in lines:
            await self._send(line)
        await self._send("command_list_end")
        await self._read_to_terminator()

    async def status(self) -> MpdStatus:
        """What MPD is doing, with every key it omitted left as None rather than zeroed."""
        fields = dict(await self.command("status"))
        return MpdStatus(
            state=MpdState(fields.get("state", MpdState.STOP.value)),
            song=int(fields["song"]) if "song" in fields else None,
            elapsed=float(fields["elapsed"]) if "elapsed" in fields else None,
            duration=float(fields["duration"]) if "duration" in fields else None,
            playlist_length=int(fields.get("playlistlength", 0)),
        )

    async def play_entry(self, entry: str, *, place: Place | None = None, end: ChannelEnd) -> None:
        """Put one stored playlist on, from the start or from where the house left it.

        ``clear`` first because ``load`` APPENDS - measured, and without it the queue grows by one
        channel every time somebody dials.

        ``repeat`` on every load, from the channel's own ``end``. It is ONE setting for the whole
        daemon and MPD keeps it across a restart, so a channel that left it to whatever the previous
        one set would wrap or stop depending on what somebody had dialled before. ``end`` has no
        default for the same reason: a caller that forgot it would get ``wrap`` on a book.

        **The resume is ``seek``, one command, and that is a crash fix rather than a tidy-up.**
        It used to be a command list of ``play <track>`` then ``seekcur <seconds>``, on the
        argument that ``seekcur`` on a stopped MPD is refused (``ACK [55@0] {seekcur} Not
        playing``) so the two could not be separate exchanges. That sequence SEGFAULTS MPD 0.24.6:
        measured on a container host 2026-09-21, interleaved A/B, two rounds each, with
        and without a listener on the httpd output - the command list died on SIGSEGV every time
        and the same channel change without a seek survived every time. Every MPD channel in this
        house carries a saved position, so every channel change did it, and MPD had been dying
        every thirty to ninety minutes of use since the previous afternoon.

        The mechanism is upstream's, open since 0.20.18 as MusicPlayerDaemon/MPD issue 276: the
        seek arrives before the decoder has opened the song, so there is no duration to seek
        within. ``seek {SONGPOS} {TIME}`` does not race it, because it names the entry and the
        offset together and MPD starts the decoder AT that offset. Verified on the house's own
        audiobooks: ``seek 2 300.000`` answers OK and leaves MPD playing entry 2 at 302.9 s.

        Two other sequences also survive and were rejected: polling ``status`` until a duration
        appears and only then sending ``seekcur`` (upstream's own workaround, which adds a wait
        and a loop to every channel change), and ``seekid``, which needs the queue read first.

        The place names the TRACK the house was left in, which is the whole of it: the same resume
        with 0 in it played the first file of the list 1.5 s in while the house had been five files
        further on (heard in the flat 2026-09-20 15:43).
        """
        await self._an_empty_queue_that(end=end)
        await self.command(f"load {quote(entry)}")
        await self._start(place)

    async def play_files(self, files: Sequence[str], *, place: Place | None = None, end: ChannelEnd) -> None:
        """Put these files on, in THIS order, from the start or from where the house left them.

        One ``add`` per file rather than one for their directory: MPD adds a directory in its
        DATABASE order (measured 2026-09-25: ``Buch/10.mp3`` before ``Buch/2.mp3``), and the order
        a channel plays in is the domain's rule, not MPD's. All of them in one command list, so the
        queue is never half built while somebody is listening.

        No files is still a channel change: the queue is emptied and nothing plays, rather than the
        stream carrying on with the channel somebody just dialled away from. The resume is the same
        single ``seek`` as :meth:`play_entry`, for the crash reason given there.
        """
        await self._an_empty_queue_that(end=end)
        if not files:
            return
        await self.command_list([f"add {quote(one)}" for one in files])
        await self._start(place)

    async def _an_empty_queue_that(self, *, end: ChannelEnd) -> None:
        """``clear``, and the channel's ``repeat``, which every way of loading one begins with."""
        await self.command("clear")
        await self.command(f"repeat {1 if end is ChannelEnd.WRAP else 0:d}")

    async def _start(self, place: Place | None) -> None:
        if place is None:
            await self.command("play 0")
            return
        await self.command(f"seek {place.track:d} {place.seconds:.3f}")

    async def files_under(self, directory: str) -> tuple[str, ...]:
        """Every file MPD's database holds under that directory, subdirectories included.

        In MPD's order, which is not the channel's; the caller puts them in play order. ``listall``
        names each directory as well, and only the files are kept. It lists what MPD can PLAY,
        not whatever sits in the directory: a cover image is not in its answer (measured).
        """
        return tuple(value for key, value in await self.command(f"listall {quote(directory)}") if key == "file")

    async def queue_files(self) -> tuple[str, ...]:
        """The path of every entry in the queue, in queue order, which is what a held key steps by."""
        return tuple(value for key, value in await self.command("playlistinfo") if key == "file")

    async def play_at(self, position: int) -> None:
        """Play one entry of the queue, counting from zero.

        Not ``next`` and ``previous``: those stop at the end of the queue unless ``repeat`` is on,
        and a press past the end wraps on every channel, including one whose ``repeat`` is off. The
        service works out which entry that is (``domain.mpd.entry_after``) and names it.
        """
        await self.command(f"play {position:d}")

    async def stop(self) -> None:
        await self.command("stop")
