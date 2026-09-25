#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pydantic>=2.13.5", "rich-click>=1.9.9", "lib_cli_exit_tools>=2.3.4",
#                 "lib_layered_config>=5.6.2"]
# ///
"""Which channel-start sequence kills MPD, and with which signal: an interleaved A/B on a spare MPD.

MPD 0.24.6 died on the house's container ten times in a day, as SIGSEGV and as SIGABRT
(``terminate called after throwing an instance of 'std::system_error', what(): Invalid argument``),
each time within milliseconds of the service putting it on a channel with a saved position. The
service used to resume with a command list of ``play <track>`` and ``seekcur <seconds>``; it sends
one ``seek <track> <seconds>`` since 0.3.10. This measures both sequences, with and without a
listener on the httpd output, because the aborts all happened with the house on another channel.

It never touches the house. Each arm starts its OWN mpd, on its own control and httpd ports bound
to 127.0.0.1, with its own state and a copy of the live tag database, reading the music directory
the house's MPD reads. Where the live configuration and database are is the ``[mpd_ab]`` setting
read by research/_settings.py; the tracked default is Debian's ``/etc/mpd.conf`` and
``/var/lib/mpd/tag_cache``. The house's mpd.service keeps running, no speaker is told about the spare
port, and nothing is audible. Each verdict is the PROCESS's wait status and what it wrote to
stderr, never the socket's: a closed socket reads as success.

    uv run mpd_seek_ab.py --rounds 3 --json          (on the service host, as root,
                                                      _click.py and _settings.py beside it)

Exit codes: 0 every arm ran, 1 it ran but an arm could not be set up (no two playlists, the spare
MPD never answered), 2 it could not run.
"""

from __future__ import annotations

import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import rich_click as click
from _click import current_context, option, run_cli
from _settings import MpdAbSettings, SettingsError, load, mpd_ab_settings
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["Arm", "StartSequence", "Verdict", "build_config", "verdict_of"]

COMMAND = "mpd_seek_ab.py"
CONTROL_PORT = 6611
HTTPD_PORT = 8111
ABORT_TEXT = "terminate called after throwing"
RESUME_TRACK = 2
RESUME_SECONDS = 13.0


class StartSequence(StrEnum):
    """How an arm puts the second channel on, which is the variable under test."""

    LIST = "list"
    """``play <track>`` and ``seekcur <seconds>`` as one command list: the service before 0.3.10."""
    SEEK = "seek"
    """``seek <track> <seconds>``: the service since 0.3.10."""


class Arm(BaseModel):
    """One cell of the A/B: a sequence, and whether somebody listens on the httpd output."""

    model_config = ConfigDict(frozen=True)

    sequence: StartSequence
    listener: bool

    @property
    def label(self) -> str:
        return f"{self.sequence.value}, {'listener' if self.listener else 'nobody listening'}"


class Verdict(StrEnum):
    """How the spare MPD ended, read from the process."""

    SURVIVED = "survived"
    ABORTED = "SIGABRT"
    SEGFAULTED = "SIGSEGV"
    OTHER = "other"
    NOT_RUN = "not run"


class Outcome(BaseModel):
    """One run of one arm."""

    model_config = ConfigDict(frozen=True)

    round: int
    arm: str
    verdict: Verdict
    returncode: int | None
    abort_text: bool
    """Whether stderr carried the uncaught-exception line the house's journal showed."""
    note: str = ""


class Report(BaseModel):
    """Every run, in the order it ran, and the tally per arm."""

    model_config = ConfigDict(frozen=True)

    rounds: int
    outcomes: list[Outcome]
    tally: dict[str, dict[str, int]]


class Envelope(BaseModel):
    ok: bool
    command: str
    data: Report


class ErrorEnvelope(BaseModel):
    ok: bool = False
    command: str
    error: str
    message: str


ARMS = (
    Arm(sequence=StartSequence.LIST, listener=False),
    Arm(sequence=StartSequence.SEEK, listener=False),
    Arm(sequence=StartSequence.LIST, listener=True),
    Arm(sequence=StartSequence.SEEK, listener=True),
)
"""Interleaved in this order every round, so no arm is confounded with the wall clock."""


def build_config(*, live_conf: str, workdir: Path, control_port: int, httpd_port: int) -> str:
    """The spare MPD's configuration: the house's music and playlists, nothing else of the house's.

    The music and playlist directories are taken from the live file so the arms read what the house
    reads; everything that could collide or write - ports, database, state, the user switch, the
    automatic rescan - is the spare's own.
    """
    keep = ("music_directory", "playlist_directory", "filesystem_charset")
    kept = [line.strip() for line in live_conf.splitlines() if line.strip().split(" ", 1)[0] in keep]
    return "\n".join(
        [
            *kept,
            f'db_file "{workdir / "tag_cache"}"',
            f'state_file "{workdir / "state"}"',
            'bind_to_address "127.0.0.1"',
            f'port "{control_port}"',
            'auto_update "no"',
            "audio_output {",
            '    type "httpd"',
            '    name "spare"',
            '    encoder "lame"',
            '    bind_to_address "127.0.0.1"',
            f'    port "{httpd_port}"',
            '    bitrate "128"',
            '    format "44100:16:2"',
            '    always_on "yes"',
            "}",
            "",
        ]
    )


def verdict_of(returncode: int | None) -> Verdict:
    """How the process ended: None is still running, a negative code is the signal that ended it."""
    if returncode is None:
        return Verdict.SURVIVED
    if returncode == -signal.SIGABRT:
        return Verdict.ABORTED
    if returncode == -signal.SIGSEGV:
        return Verdict.SEGFAULTED
    return Verdict.OTHER


def _talk(sock: socket.socket, lines: Sequence[str], *, timeout: float = 15.0) -> str:
    for line in lines:
        sock.sendall((line + "\n").encode())
    out = b""
    sock.settimeout(timeout)
    try:
        while not (out.endswith(b"OK\n") or b"ACK " in out):
            chunk = sock.recv(65536)
            if not chunk:
                return "<CLOSED>"
            out += chunk
    except OSError:
        return "<NO ANSWER>"
    return out.decode("utf-8", "replace")


def _connect(port: int, *, deadline_s: float = 20.0) -> socket.socket | None:
    until = time.monotonic() + deadline_s
    while time.monotonic() < until:
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        except OSError:
            time.sleep(0.3)
            continue
        sock.recv(4096)
        return sock
    return None


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _second_channel(sock: socket.socket, sequence: StartSequence) -> None:
    if sequence is StartSequence.LIST:
        _talk(sock, ["command_list_begin", f"play {RESUME_TRACK}", f"seekcur {RESUME_SECONDS:.3f}", "command_list_end"])
    else:
        _talk(sock, [f"seek {RESUME_TRACK} {RESUME_SECONDS:.3f}"])


def _drive(sock: socket.socket, arm: Arm, playlists: tuple[str, str], *, idle_s: float) -> None:
    """The house's shape: channel A playing, the house leaves it, then channel B from a saved place."""
    first, second = playlists
    listener = None
    try:
        for line in ("clear", f"load {_quote(first)}", "play 0"):
            _talk(sock, [line])  # one answer per command, or a later read gets this one's OK
        time.sleep(3)
        if arm.listener:
            listener = subprocess.Popen(
                ["/usr/bin/curl", "-sS", "--max-time", "60", f"http://127.0.0.1:{HTTPD_PORT}/", "-o", "/dev/null"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        time.sleep(idle_s)
        _talk(sock, ["clear"])
        _talk(sock, [f"load {_quote(second)}"])
        _second_channel(sock, arm.sequence)
        time.sleep(4)
    finally:
        if listener is not None and listener.poll() is None:
            listener.terminate()


def _playlists(sock: socket.socket) -> tuple[str, str] | None:
    names = [
        line.removeprefix("playlist: ")
        for line in _talk(sock, ["listplaylists"]).splitlines()
        if line.startswith("playlist: ")
    ]
    if len(names) < len(("first", "second")):
        return None
    return names[0], names[1]


def one_run(arm: Arm, *, round_: int, idle_s: float, live: MpdAbSettings) -> Outcome:
    """Start a spare MPD, drive one channel change, and report how the process ended."""
    with tempfile.TemporaryDirectory(prefix="mpd-seek-ab-") as tmp:
        workdir = Path(tmp)
        shutil.copyfile(live.live_db, workdir / "tag_cache")
        conf = workdir / "mpd.conf"
        conf.write_text(
            build_config(
                live_conf=live.live_conf.read_text(encoding="utf-8"),
                workdir=workdir,
                control_port=CONTROL_PORT,
                httpd_port=HTTPD_PORT,
            ),
            encoding="utf-8",
        )
        stderr_path = workdir / "stderr.txt"
        with stderr_path.open("wb") as err:
            mpd = subprocess.Popen(
                ["/usr/bin/mpd", "--no-daemon", "--stderr", str(conf)], stdout=subprocess.DEVNULL, stderr=err
            )
        try:
            sock = _connect(CONTROL_PORT)
            if sock is None:
                return Outcome(
                    round=round_,
                    arm=arm.label,
                    verdict=Verdict.NOT_RUN,
                    returncode=mpd.poll(),
                    abort_text=False,
                    note="the spare MPD never answered",
                )
            with sock:
                playlists = _playlists(sock)
                if playlists is None:
                    return Outcome(
                        round=round_,
                        arm=arm.label,
                        verdict=Verdict.NOT_RUN,
                        returncode=None,
                        abort_text=False,
                        note="fewer than two stored playlists",
                    )
                _drive(sock, arm, playlists, idle_s=idle_s)
            returncode = mpd.poll()
        finally:
            if mpd.poll() is None:
                mpd.terminate()
                try:
                    mpd.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    mpd.kill()
                    mpd.wait()
        text = stderr_path.read_text(encoding="utf-8", errors="replace")
    return Outcome(
        round=round_,
        arm=arm.label,
        verdict=verdict_of(returncode),
        returncode=returncode,
        abort_text=ABORT_TEXT in text,
    )


def run(*, rounds: int, idle_s: float, live: MpdAbSettings) -> Report:
    outcomes = [one_run(arm, round_=r, idle_s=idle_s, live=live) for r in range(1, rounds + 1) for arm in ARMS]
    tally: dict[str, dict[str, int]] = {arm.label: {} for arm in ARMS}
    for o in outcomes:
        tally[o.arm][o.verdict.value] = tally[o.arm].get(o.verdict.value, 0) + 1
    return Report(rounds=rounds, outcomes=outcomes, tally=tally)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@option("--rounds", type=click.IntRange(1, 20), default=3, show_default=True, help="rounds of all four arms")
@option(
    "--idle",
    "idle_s",
    type=click.FloatRange(0.0, 60.0),
    default=6.0,
    show_default=True,
    help="seconds the first channel plays unattended before the change",
)
@option("--json", "as_json", is_flag=True, help="print a JSON envelope instead of prose")
@option("--json-bare", "as_json_bare", is_flag=True, help="as --json, on one line for jq")
def cli(*, rounds: int, idle_s: float, as_json: bool, as_json_bare: bool) -> None:
    """Run the four arms interleaved on a spare MPD and report how each run ended."""
    ctx = current_context()
    indent = None if as_json_bare else 2
    machine = as_json or as_json_bare
    try:
        report = run(rounds=rounds, idle_s=idle_s, live=mpd_ab_settings(load()))
    except (OSError, SettingsError) as exc:
        if machine:
            print(
                ErrorEnvelope(command=COMMAND, error=type(exc).__name__, message=str(exc)).model_dump_json(
                    indent=indent
                )
            )
        else:
            print(f"cannot run: {exc}", file=sys.stderr)
        ctx.exit(2)
    incomplete = any(o.verdict is Verdict.NOT_RUN for o in report.outcomes)
    if machine:
        print(Envelope(ok=not incomplete, command=COMMAND, data=report).model_dump_json(indent=indent))
    else:
        for o in report.outcomes:
            print(f"round {o.round}  {o.arm:<28} {o.verdict.value}{'  (abort text)' if o.abort_text else ''} {o.note}")
        for label, counts in report.tally.items():
            print(f"{label:<28} {counts}")
    ctx.exit(1 if incomplete else 0)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; ``lib_cli_exit_tools`` maps an unexpected exception onto an exit code."""
    return run_cli(cli, argv=argv, prog_name=COMMAND)


if __name__ == "__main__":
    raise SystemExit(main())
