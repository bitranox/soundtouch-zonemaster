#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pydantic>=2.13.5", "rich-click>=1.9.9", "lib_cli_exit_tools>=2.3.4"]
# ///
"""Summarise the ``sync`` lines of a zone-master run: where each slave rendered against the zone.

The ``sync`` line is the instrument this house judges a placement change by, because it agreed
with the ear on every run the ear judged (REPORT.md S7). It was summarised by three throwaway
scripts living beside the captures, in a gitignored directory - no test, no tracking, and a regex
that had to match ``placement.py`` exactly. When it did not match, they printed NOTHING and exited
0, which reads exactly like a run in which every slave was perfectly placed.

So this refuses instead. A log holding no sync line at all is the answer "I cannot tell you", and
it leaves through exit code 1 rather than through an empty report.

    uv run research/sync_report.py --log run-abn1.log --json

Exit codes: 0 every log had sync lines and they are summarised, 1 it ran and at least one log had
none, 2 it could not run.
"""

from __future__ import annotations

import re
import statistics
import sys
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import rich_click as click
from _click import current_context, option, run_cli
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Sequence

COMMAND = "sync_report"


class LineShape(StrEnum):
    """Which shape of ``sync`` line a sample was read from.

    Two have been emitted by this master. Naming them is the point: a line matching NEITHER is not
    a third dialect to be tolerated, it is the format change this tool exists to notice.
    """

    AGAINST_THE_ZONE = "against-the-zone"
    """The current one: the frame plan's offset, with the slave's own counter as a cross-check."""

    AGAINST_A_PEER = "against-a-peer"
    """The byte-plan one, which compares two slaves and carries no counter column.

    Still emitted, not historical: it is what ``placement.py`` falls back to whenever no slave has
    reported a frame count yet, which is every run's opening seconds and the whole of a run whose
    boxes never send ``trackData``. It carries a trailing ``(bytes)`` - or ``(bytes, read-ahead
    unmeasured)`` - which this tool's first regex did not allow for, having been written from an
    excerpt of a run older than that suffix. Both forms are matched: logs on disk hold both.
    """


_AGAINST_THE_ZONE = re.compile(
    r"^\S+\s+sync\s+(?P<peer>\S+) renders (?P<clock>[+-]\d+) ms vs zone "
    r"\(frames; counter says (?P<counter>[+-]\d+) ms\)\s*$"
)
_AGAINST_A_PEER = re.compile(
    r"^\S+\s+sync\s+(?P<peer>\S+) renders (?P<clock>[+-]\d+) ms vs (?P<other>\S+)"
    r"(?: \(bytes(?:, read-ahead unmeasured)?\))?\s*$"
)


class SyncSample(BaseModel):
    """One slave's rendered position at one instant, in milliseconds against the zone."""

    model_config = ConfigDict(frozen=True)

    peer: str
    clock_ms: int
    counter_ms: int | None
    shape: LineShape


class PeerSummary(BaseModel):
    """What one slave's samples add up to over a whole run.

    Median rather than mean, because a single late report skews a mean and the question is where
    the box SAT, not what its worst sample was; minimum and maximum are reported beside it so a
    reader can see whether the median is hiding a spread.
    """

    model_config = ConfigDict(frozen=True)

    peer: str
    samples: int
    median_ms: float
    min_ms: int
    max_ms: int
    counter_median_ms: float | None
    counter_min_ms: int | None
    counter_max_ms: int | None


class LogSummary(BaseModel):
    """One log file's verdict."""

    model_config = ConfigDict(frozen=True)

    path: str
    samples: int
    shapes: list[LineShape]
    peers: list[PeerSummary]


class SyncReport(BaseModel):
    """Every log that was read, and whether all of them said anything at all."""

    model_config = ConfigDict(frozen=True)

    logs: list[LogSummary]
    silent: list[str]


class SyncEnvelope(BaseModel):
    """The machine-readable result, shaped like the other CLIs here."""

    ok: bool
    command: str
    data: SyncReport


class ErrorEnvelope(BaseModel):
    """A refusal, in the same shape, so a caller parses one thing either way.

    ``ok`` is the field that makes that sentence true, and it was the one missing: every other CLI
    here declares it, so a caller reading ``payload["ok"]`` got a KeyError from this tool alone -
    on the failure path, which is the one it is reading the field to detect. The exit-code test
    that covered this path asserted the code and never opened the envelope.
    """

    ok: bool = False
    command: str
    error: str
    message: str


def parse_line(line: str) -> SyncSample | None:
    """One log line as a sample, or None when it is not a sync line at all.

    The two shapes are tried newest first. A line that is a sync line in neither shape returns
    None exactly as an unrelated line does, which is deliberate: the caller counts what it got,
    and zero is the answer that gets reported rather than swallowed.
    """
    zone = _AGAINST_THE_ZONE.match(line)
    if zone is not None:
        return SyncSample(
            peer=zone["peer"],
            clock_ms=int(zone["clock"]),
            counter_ms=int(zone["counter"]),
            shape=LineShape.AGAINST_THE_ZONE,
        )
    peer = _AGAINST_A_PEER.match(line)
    if peer is not None:
        return SyncSample(
            peer=peer["peer"], clock_ms=int(peer["clock"]), counter_ms=None, shape=LineShape.AGAINST_A_PEER
        )
    return None


def summarise_peer(peer: str, samples: Sequence[SyncSample]) -> PeerSummary:
    """The five numbers for one slave; the counter columns stay None when the shape had none."""
    clock = [s.clock_ms for s in samples]
    counter = [s.counter_ms for s in samples if s.counter_ms is not None]
    return PeerSummary(
        peer=peer,
        samples=len(clock),
        median_ms=statistics.median(clock),
        min_ms=min(clock),
        max_ms=max(clock),
        counter_median_ms=statistics.median(counter) if counter else None,
        counter_min_ms=min(counter) if counter else None,
        counter_max_ms=max(counter) if counter else None,
    )


def read_log(path: Path) -> LogSummary:
    """Summarise one log file, per slave.

    Undecodable bytes are replaced rather than fatal: a run log is a text file written by a program
    that also copies station names off the LAN, and losing a whole measurement to one bad byte in a
    line nobody is measuring would be the wrong trade.
    """
    samples = [
        sample
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if (sample := parse_line(line)) is not None
    ]
    peers = sorted({s.peer for s in samples})
    return LogSummary(
        path=str(path),
        samples=len(samples),
        shapes=sorted({s.shape for s in samples}),
        peers=[summarise_peer(peer, [s for s in samples if s.peer == peer]) for peer in peers],
    )


def build_report(paths: Sequence[Path]) -> SyncReport:
    """Read every log and name the ones that held no sync line."""
    logs = [read_log(path) for path in paths]
    return SyncReport(logs=logs, silent=[log.path for log in logs if log.samples == 0])


def _print_human(report: SyncReport) -> None:
    """The prose form, one line per slave, closest to what the throwaway scripts printed."""
    for log in report.logs:
        print(f"{log.path}: {log.samples} sync lines")
        for peer in log.peers:
            counter = (
                ""
                if peer.counter_median_ms is None
                else f" | counter {peer.counter_median_ms:+.0f} ms"
                f" (min {peer.counter_min_ms:+d} max {peer.counter_max_ms:+d})"
            )
            print(
                f"  {peer.peer}: n={peer.samples} median={peer.median_ms:+.0f} ms"
                f" (min {peer.min_ms:+d} max {peer.max_ms:+d}){counter}"
            )
    for path in report.silent:
        print(f"{path}: NO sync line - this log cannot answer the question", file=sys.stderr)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@option("--log", "logs", multiple=True, required=True, type=click.Path(dir_okay=False, path_type=Path), help="run log")
@option("--json", "as_json", is_flag=True, help="print a JSON envelope instead of prose")
@option("--json-bare", "as_json_bare", is_flag=True, help="as --json, on one line for jq")
def cli(*, logs: tuple[Path, ...], as_json: bool, as_json_bare: bool) -> None:
    """Summarise the sync lines of one or more run logs, and refuse a log that has none."""
    ctx = current_context()
    indent = None if as_json_bare else 2
    try:
        report = build_report(logs)
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
        print(SyncEnvelope(ok=not report.silent, command=COMMAND, data=report).model_dump_json(indent=indent))
    else:
        _print_human(report)
    ctx.exit(1 if report.silent else 0)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; ``lib_cli_exit_tools`` maps an unexpected exception onto an exit code."""
    return run_cli(cli, argv=argv, prog_name=COMMAND)


if __name__ == "__main__":
    raise SystemExit(main())
