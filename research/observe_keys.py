#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["websockets>=15.0", "pydantic>=2.13.5", "rich-click>=1.9.9",
#                 "lib_cli_exit_tools>=2.3.4"]
# ///
"""Listen to the speakers' notification WebSocket and write down what arrives, with the clock.

Read-only by construction: it opens the notification channel and never sends a command, so it
makes no sound and can be pointed at any speaker in the house, the Lifestyle console included.
That is the whole reason it is separate from capture_zone.py, which drives speakers.

It exists to answer two questions the design could not: whether a key with no content (next,
previous, the thumbs) reaches anyone at all, and how far apart two quick presses actually land
once radio jitter has had its say. Its parser is what the service's observer will be built from,
which is why the preset number is a named field rather than a regex at a call site.

    uv run research/observe_keys.py --speaker 192.168.0.31 --speaker 192.168.0.33 \\
        --seconds 120 --out /tmp/keys.jsonl --json

Exit codes: 0 frames were recorded, 1 it ran and recorded nothing, 2 it could not run.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import rich_click as click
import websockets
from _click import current_context, option, run_cli
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

COMMAND = "observe_keys"

WS_PORT = 8080
SUBPROTOCOL = websockets.Subprotocol("gabbo")
"""The subprotocol a SoundTouch notification channel is opened with (research/capture_zone.py).

Spelled as the library's own NewType rather than a bare string because that is what ``connect``
declares it takes, so the constant carries the right type from its one definition.
"""

RECONNECT_BACKOFF_S = (1.0, 2.0, 5.0, 10.0)
"""Room2 drops out for minutes at a time, so a lost channel is normal rather than fatal."""

_DEVICE_ID = re.compile(r'deviceID="([^"]+)"')
_PRESET_ID = re.compile(r'<preset\s+id="(\d+)"')
_FIRST_UPDATE = re.compile(r"<updates[^>]*>\s*<(\w+)")
_ROOT_TAG = re.compile(r"<(\w+)")


class SpeakerEvent(BaseModel):
    """One frame from one speaker, with the time it arrived HERE.

    The arrival time is this machine's clock, not the speaker's, because the question it answers
    is how far apart two presses land at the observer once the radio has had its say.
    """

    model_config = ConfigDict(frozen=True)

    received_at: float
    speaker: str
    device_id: str
    kind: str
    preset_id: int | None
    frame: str


class ObserveReport(BaseModel):
    """What one observation run recorded."""

    model_config = ConfigDict(frozen=True)

    out: str
    seconds: float
    frames: dict[str, int]
    total: int


class ObserveEnvelope(BaseModel):
    """The machine-readable result, shaped like the other CLIs here."""

    ok: bool
    command: str
    data: ObserveReport


class ErrorEnvelope(BaseModel):
    """A refusal, in the same shape, so a caller parses one thing either way."""

    command: str
    error: str
    message: str


def parse_frame(speaker: str, frame: str, received_at: float) -> SpeakerEvent:
    """One frame as a record, keeping the frame itself whatever happens.

    Read with regexes rather than an XML parser on purpose: these frames arrive from the
    unauthenticated LAN side, and an XML parser would add the entity-expansion surface that a
    regex does not have. Nothing is dropped for being unparseable, because what a speaker sends
    for a key with no content is not known yet, and this file exists to find out.
    """
    device = _DEVICE_ID.search(frame)
    inner = _FIRST_UPDATE.search(frame)
    root = _ROOT_TAG.search(frame)
    preset = _PRESET_ID.search(frame)
    if inner is not None:
        kind = inner.group(1)
    elif root is not None:
        kind = root.group(1)
    else:
        kind = "unknown"
    return SpeakerEvent(
        received_at=received_at,
        speaker=speaker,
        device_id=device.group(1) if device else "",
        kind=kind,
        preset_id=int(preset.group(1)) if preset else None,
        frame=frame,
    )


def _progress(text: str) -> None:
    """Narration goes to stderr, so stdout carries the envelope and nothing else."""
    print(text, file=sys.stderr, flush=True)


async def _listen(
    speaker: str,
    write: Callable[[SpeakerEvent], Awaitable[None]],
    counts: dict[str, int],
    stop: asyncio.Event,
) -> None:
    """Hold one speaker's notification channel open until told to stop, reconnecting as needed."""
    url = f"ws://{speaker}:{WS_PORT}"
    attempt = 0
    while not stop.is_set():
        try:
            async with websockets.connect(url, subprotocols=[SUBPROTOCOL]) as ws:
                attempt = 0
                _progress(f"{speaker}: connected")
                while not stop.is_set():
                    frame = await ws.recv()
                    text = frame.decode("utf-8", "replace") if isinstance(frame, bytes) else frame
                    await write(parse_frame(speaker, text, time.time()))
                    counts[speaker] += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - any failure here is a retry, never the end of a run
            delay = RECONNECT_BACKOFF_S[min(attempt, len(RECONNECT_BACKOFF_S) - 1)]
            attempt += 1
            _progress(f"{speaker}: {type(exc).__name__}: {exc}; retrying in {delay:.0f}s")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), delay)


async def observe(speakers: Sequence[str], seconds: float, out: Path) -> ObserveReport:
    """Listen to every named speaker for ``seconds``, one JSON object per frame, flushed as it goes.

    Flushed rather than buffered because the run it serves is watched live: a frame that is still
    in a buffer cannot be read while somebody is standing at the speaker pressing keys.
    """
    counts: dict[str, int] = dict.fromkeys(speakers, 0)
    stop = asyncio.Event()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        lock = asyncio.Lock()

        async def write(event: SpeakerEvent) -> None:
            async with lock:
                fh.write(event.model_dump_json() + "\n")
                fh.flush()

        listeners = [asyncio.create_task(_listen(ip, write, counts, stop)) for ip in speakers]
        try:
            await asyncio.sleep(seconds)
        finally:
            stop.set()
            for task in listeners:
                task.cancel()
            await asyncio.gather(*listeners, return_exceptions=True)
    return ObserveReport(out=str(out), seconds=seconds, frames=counts, total=sum(counts.values()))


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@option("--speaker", "speakers", multiple=True, required=True, help="speaker IP to listen to (repeatable)")
@option("--seconds", type=float, default=120.0, show_default=True, help="how long to listen")
@option("--out", required=True, type=click.Path(dir_okay=False, path_type=Path), help="JSONL file to write")
@option("--json", "as_json", is_flag=True, help="print a JSON envelope instead of prose")
@option("--json-bare", "as_json_bare", is_flag=True, help="as --json, on one line for jq")
def cli(*, speakers: tuple[str, ...], seconds: float, out: Path, as_json: bool, as_json_bare: bool) -> None:
    """Record every notification frame the named speakers send. Silent: it never sends a command."""
    ctx = current_context()
    indent = None if as_json_bare else 2
    try:
        report = asyncio.run(observe(speakers, seconds, out))
    except OSError as exc:
        if as_json or as_json_bare:
            print(
                ErrorEnvelope(command=COMMAND, error=type(exc).__name__, message=str(exc)).model_dump_json(
                    indent=indent
                )
            )
        else:
            print(f"cannot run: {exc}", file=sys.stderr)
        ctx.exit(2)
    if as_json or as_json_bare:
        print(ObserveEnvelope(ok=report.total > 0, command=COMMAND, data=report).model_dump_json(indent=indent))
    else:
        for speaker, count in report.frames.items():
            print(f"{speaker}: {count} frames")
        print(f"{report.total} frames in {report.seconds:.0f}s -> {report.out}")
    ctx.exit(0 if report.total > 0 else 1)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; ``lib_cli_exit_tools`` maps an unexpected exception onto an exit code."""
    return run_cli(cli, argv=argv, prog_name=COMMAND)


if __name__ == "__main__":
    raise SystemExit(main())
