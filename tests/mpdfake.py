"""An MPD that speaks the shapes measured on 2026-09-10, so the client's tests need no binary.

It is deliberately literal. Every response here was copied from a real MPD 0.24.6 rather than
written from the protocol documentation, because the one thing a fake cannot do is be more right
than the thing it stands in for - and the wire shape this client gets wrong silently is the
refusal, which no documentation example shows in full.

It records every command LINE it was sent, which is what the tests assert on. Asserting on what
the client returned would pass just as well with the bytes wrong, because a fake that answers OK
to anything answers OK to a misspelled verb too.
"""

from __future__ import annotations

import asyncio
import time

__all__ = ["GREETING", "HOST", "FakeMpd"]

HOST = "127.0.0.1"
GREETING = b"OK MPD 0.24.0\n"
"""The greeting names the PROTOCOL version, not the server's: a real 0.24.6 answers 0.24.0."""


class FakeMpd:
    """Records every command line it was sent and answers from a scripted table.

    A plain class with a typed ``__init__`` rather than a dataclass, which is what
    ``registry_double.py`` and ``speaker_double.py`` are: under pyright strict a
    ``field(default_factory=list)`` infers ``list[Unknown]`` from the bare factory and the
    annotation on the attribute does not rescue it.
    """

    def __init__(
        self,
        *,
        status_lines: list[str] | None = None,
        refuse: dict[str, str] | None = None,
        refuse_code: int = 50,
        directories: dict[str, list[str]] | None = None,
        queue: list[str] | None = None,
        playlists: dict[str, list[str]] | None = None,
    ) -> None:
        self.seen: list[str] = []
        """Every line received, in order, including the command-list brackets."""
        self.first_command_at: float | None = None
        """``time.monotonic()`` when the first line arrived, or nothing while none has.

        It exists for one assertion, and it is the measured one: MPD's ``httpd`` port does not
        listen until its output first opens, so a caller that points a player at the stream before
        it has told MPD to play is refused and backs off. Only a clock can say which came first,
        and the station side of that comparison stamps itself from the same one."""
        self.status_lines: list[str] = ["state: stop"] if status_lines is None else list(status_lines)
        self.refuse: dict[str, str] = {} if refuse is None else dict(refuse)
        """Verb to message: what this MPD declines, and what it says when it does."""
        self.refuse_code = refuse_code
        """The ``ACK`` code every refusal above carries. 50 is the measured one, ``No such
        playlist``; another value is what a test uses to prove a mapping keyed on 50 answers
        something else for everything else."""
        self.directories: dict[str, list[str]] = {} if directories is None else dict(directories)
        """Directory to the files ``listall`` names under it, in the order MPD's DATABASE holds them,
        which is not the order a channel plays them in: measured, ``Buch/10.mp3`` before
        ``Buch/2.mp3``. A directory not in here is refused the way a real one is."""
        self.queue: list[str] = [] if queue is None else list(queue)
        """The queue's file paths, kept by ``clear`` and ``add`` (inside a command list too) and read
        back by ``playlistinfo``, and extended by ``load`` from :attr:`playlists`."""
        self.playlists: dict[str, list[str]] = {} if playlists is None else dict(playlists)
        """Stored playlist to the files in it. ``load`` of one not in here adds nothing and still
        answers OK, which is what every test written before playlists had paths relies on."""
        self.port = 0
        self.connections = 0
        """How many callers have connected, ever. A client that had to open a SECOND one is
        a client that threw the first away, which is the only way to see a reconnect from
        out here - the bytes of the second connection look exactly like the first."""
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, HOST, 0)
        self.port = int(self._server.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        """Stop listening, then drop whoever is still connected, in that order.

        The drop is what keeps this bounded: ``wait_closed`` waits for every client TRANSPORT to be
        gone, so one caller that vanished without closing its socket would hang the teardown with
        nothing reported.
        """
        if self._server is not None:
            self._server.close()
        for writer in self._writers:
            writer.close()
        self._writers.clear()
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None

    def drop_connections(self) -> None:
        """Close whoever is connected and keep listening, which is MPD being restarted.

        The failure worth rehearsing: a client that kept its socket finds it dead on the next
        command, and nothing tells it in between. Stopping the whole server instead would
        test a daemon that never came back, which is a different thing."""
        for writer in self._writers:
            writer.close()
        self._writers.clear()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._writers.add(writer)
        writer.write(GREETING)
        await writer.drain()
        in_list = False
        while True:
            raw = await reader.readline()
            if not raw:
                return
            line = raw.decode("utf-8").rstrip("\n")
            if self.first_command_at is None:
                self.first_command_at = time.monotonic()
            self.seen.append(line)
            if line == "command_list_begin":
                in_list = True
                continue
            if line == "command_list_end":
                # One OK for the whole list, which is what makes a resume a single state change
                # rather than a play and a seek with a window between them.
                in_list = False
                writer.write(b"OK\n")
                await writer.drain()
                continue
            if in_list:
                self._keep_the_queue(line)
                continue
            writer.write(self._answer(line))
            await writer.drain()

    def _keep_the_queue(self, line: str) -> None:
        verb, _, argument = line.partition(" ")
        if verb == "clear":
            self.queue.clear()
        elif verb == "add":
            self.queue.append(_unquoted(argument))
        elif verb == "load":
            self.queue.extend(self.playlists.get(_unquoted(argument), []))

    def _answer(self, line: str) -> bytes:
        verb, _, argument = line.partition(" ")
        if verb in self.refuse:
            return f"ACK [{self.refuse_code}@0] {{{verb}}} {self.refuse[verb]}\n".encode()
        self._keep_the_queue(line)
        if verb == "status":
            return ("\n".join(self.status_lines) + "\nOK\n").encode()
        if verb == "listall":
            return self._listing(_unquoted(argument))
        if verb == "playlistinfo":
            # The shape measured on 0.24.6: file first, then its position and id, per entry.
            entries = [f"file: {one}\nPos: {pos}\nId: {pos + 1}\n" for pos, one in enumerate(self.queue)]
            return ("".join(entries) + "OK\n").encode()
        return b"OK\n"

    def _listing(self, directory: str) -> bytes:
        """``listall`` as measured: the directory itself, then ``file:`` and ``directory:`` lines."""
        if directory not in self.directories:
            return b"ACK [50@0] {listall} No such directory\n"
        subdirectories = sorted({one.rpartition("/")[0] for one in self.directories[directory]} - {directory})
        lines = [f"directory: {directory}"]
        lines += [f"directory: {one}" for one in subdirectories]
        lines += [f"file: {one}" for one in self.directories[directory]]
        return ("\n".join(lines) + "\nOK\n").encode()


def _unquoted(argument: str) -> str:
    """One argument as :func:`~soundtouch_zonemaster.adapters.mpd.client.quote` wrote it, undone."""
    if not (argument.startswith('"') and argument.endswith('"')):
        return argument
    return argument[1:-1].replace('\\"', '"').replace("\\\\", "\\")
