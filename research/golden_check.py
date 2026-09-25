#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["protobuf>=5.29", "pydantic>=2.12", "scapy>=2.6", "rich-click>=1.9.4",
#                 "lib_cli_exit_tools>=2.3.4", "lib_layered_config>=5.6.2"]
# ///
"""Golden-output check for the analysis instruments: do they still say exactly what they said?

analyze_capture.py and analyze_late_join.py are the instruments behind REPORT.md S3 and S6, and
extract_protos.py produced the 330 versioned schemas. None of them can be covered by an ordinary
unit test - their input is a 6 MB pcap and 22 MB of firmware - but each has a RECORDED output in
the tree: the two .md files beside the capture, and research/proto itself. This runs all three
and requires the output to be byte-identical to that record, which is what makes changing them
verifiable rather than hopeful.

Its inputs are gitignored (device-specific captures and firmware pulled off a speaker), so on a
fresh clone it exits 2 naming what is missing rather than passing vacuously. WHICH capture it
replays is a setting, not a constant: the ``[golden]`` scope read by research/_settings.py names
the capture directory, its pcap and the two speakers it recorded. The tracked defaults name none,
so a checkout that has not set them is told which settings to fill in, again with exit 2.

It runs each instrument with its OWN interpreter, so the dependency list above is the union of the
three scripts' PEP 723 blocks rather than empty. If an instrument takes a new dependency and this
list does not, the run fails loudly with a ModuleNotFoundError naming it.

capture_zone.py is NOT covered here: it drives real speakers and is audible in the house. Its
documents and parsers are covered by tests/test_capture_zone.py instead.

    uv run research/golden_check.py

Exit codes: 0 all identical, 1 a mismatch, 2 the check itself could not run.
"""

from __future__ import annotations

import filecmp
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import rich_click as click
from _click import current_context, option, run_cli
from _settings import GoldenSettings, SettingsError, golden_settings, load
from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Sequence

COMMAND = "golden_check"

FIRMWARE = (
    "BoseApp",
    "APServer",
    "STSCertified",
    "libSoundTouch_SDK_Protobuf.so",
    "libIPC.so",
    "libCore.so",
    "libCommonTypes.so",
    "libSTSClient.so",
    "libProtobufMessagingIPC.so",
    "WebServer",
    "ClockSync",
)
RECORDED_MASTER_SIDE = "analysis-master-side.md"
RECORDED_LATE_JOIN = "analysis-late-join.md"


class InputsMissingError(Exception):
    """The gitignored firmware or capture is absent, or none is configured, so the check cannot run (exit 2)."""


@dataclass(frozen=True)
class Outcome:
    """One instrument's verdict: what ran, whether it matched, and what to read on a mismatch."""

    name: str
    ok: bool
    detail: str


def _binary_args(research: Path) -> list[str]:
    """extract_protos takes the binaries POSITIONALLY, so it keeps the bare path list."""
    return [str(research / "firmware" / n) for n in FIRMWARE]


def _firmware_args(research: Path) -> list[str]:
    """The repeated ``--firmware <path>`` pairs the analysers now take.

    They used argparse's ``nargs="+"`` and took one flag with many values; click has no variadic
    option, so the form changed with the framework and this builds the repeated one.
    """
    out: list[str] = []
    for n in FIRMWARE:
        out += ["--firmware", str(research / "firmware" / n)]
    return out


def _run(cmd: list[str], cwd: Path, stdout: Path | None = None) -> subprocess.CompletedProcess[str]:
    with stdout.open("w", encoding="utf-8") if stdout else tempfile.TemporaryFile("w") as sink:
        return subprocess.run(
            cmd,
            cwd=cwd,
            stdout=sink,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            encoding="utf-8",
            errors="replace",
        )


def check_stdout_script(repo: Path, script: str, work: Path, *, golden: GoldenSettings) -> Outcome:
    """Run an analysis script and require its stdout to equal the recorded .md byte for byte."""
    research = repo / "research"
    recorded = research / golden.capture / RECORDED[script]
    produced = work / f"{Path(script).stem}.out"
    cmd = [
        sys.executable,
        str(research / script),
        "--pcap",
        str(research / golden.capture / golden.pcap),
        "--master",
        golden.master,
        "--slave",
        golden.slave,
        *_firmware_args(research),
    ]
    proc = _run(cmd, cwd=repo, stdout=produced)
    if proc.returncode != 0:
        return Outcome(script, ok=False, detail=f"exit {proc.returncode}: {proc.stderr.strip()[-300:]}")
    if not filecmp.cmp(produced, recorded, shallow=False):
        return Outcome(script, ok=False, detail=f"output differs from {recorded}; produced {produced}")
    return Outcome(script, ok=True, detail=f"identical to {recorded.name}")


def check_extract_protos(repo: Path, work: Path) -> Outcome:
    """Run the schema extractor into a temp dir and require it to equal the versioned research/proto."""
    research = repo / "research"
    out = work / "proto"
    proc = _run(
        [sys.executable, str(research / "extract_protos.py"), *_binary_args(research), "--out", str(out)],
        cwd=repo,
    )
    if proc.returncode != 0:
        return Outcome("extract_protos.py", ok=False, detail=f"exit {proc.returncode}: {proc.stderr.strip()[-300:]}")
    expected = research / "proto"
    produced_names = {p.relative_to(out).as_posix() for p in out.rglob("*.proto")}
    expected_names = {p.relative_to(expected).as_posix() for p in expected.rglob("*.proto")}
    if produced_names != expected_names:
        only_new = sorted(produced_names - expected_names)[:5]
        only_old = sorted(expected_names - produced_names)[:5]
        return Outcome("extract_protos.py", ok=False, detail=f"file set differs; new={only_new} missing={only_old}")
    differing = [n for n in sorted(expected_names) if not filecmp.cmp(out / n, expected / n, shallow=False)]
    if differing:
        return Outcome("extract_protos.py", ok=False, detail=f"{len(differing)} file(s) differ, e.g. {differing[:5]}")
    return Outcome("extract_protos.py", ok=True, detail=f"{len(expected_names)} schemas identical to research/proto")


RECORDED = {"analyze_capture.py": RECORDED_MASTER_SIDE, "analyze_late_join.py": RECORDED_LATE_JOIN}
"""Each analyser's recorded output, a file beside the capture it was produced from."""


def missing_inputs(repo: Path, golden: GoldenSettings) -> list[str]:
    """Name every setting left empty, then every gitignored input the check needs but cannot find.

    An empty setting is named as ``setting golden.<key>`` and the capture's files are not looked
    for until all four are set, because a path built from an empty name points at research/ itself.
    """
    research = repo / "research"
    fields = {"capture": golden.capture, "pcap": golden.pcap, "master": golden.master, "slave": golden.slave}
    unset = [f"setting golden.{key}" for key, value in fields.items() if not value]
    needed = [research / "firmware" / n for n in FIRMWARE]
    if not unset:
        capture = research / golden.capture
        needed = [capture / golden.pcap, capture / RECORDED_MASTER_SIDE, capture / RECORDED_LATE_JOIN, *needed]
    return unset + [str(p) for p in needed if not p.exists()]


class GoldenReport(BaseModel):
    """What one run of the three instruments found."""

    repo: str
    outcomes: list[Outcome]
    identical: int
    total: int


class GoldenEnvelope(BaseModel):
    """The machine-readable result; ``ok`` is false when any instrument drifted."""

    ok: bool
    command: str
    data: GoldenReport
    skipped: list[str] = []


class ErrorEnvelope(BaseModel):
    """The machine-readable result when the check could not run at all."""

    ok: bool = False
    command: str
    error: str
    message: str


def run_instruments(repo: Path, golden: GoldenSettings) -> GoldenReport:
    """Run all three and collect their verdicts. Raises when the inputs are not present."""
    absent = missing_inputs(repo, golden)
    if absent:
        msg = f"{len(absent)} input(s) missing, first: {absent[0]}"
        if absent[0].startswith("setting "):
            msg += " (name the capture in research/defaultconfig.d/92-golden-rnhome.toml; see its .example)"
        raise InputsMissingError(msg)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        outcomes = [
            *(check_stdout_script(repo, script, work, golden=golden) for script in RECORDED),
            check_extract_protos(repo, work),
        ]
    identical = sum(1 for o in outcomes if o.ok)
    return GoldenReport(repo=str(repo), outcomes=outcomes, identical=identical, total=len(outcomes))


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@option("--repo", type=click.Path(path_type=Path), default=None, help="repo root; defaults to the working directory")
@option("--json", "as_json", is_flag=True, help="print a JSON envelope instead of prose")
@option("--json-bare", "as_json_bare", is_flag=True, help="as --json, on one line for jq")
def cli(*, repo: Path | None, as_json: bool, as_json_bare: bool) -> None:
    """Re-run the analysis instruments and require their recorded output byte for byte."""
    machine = as_json or as_json_bare
    indent: int | None = None if as_json_bare else 2
    ctx = current_context()
    try:
        report = run_instruments((repo or Path.cwd()).resolve(), golden_settings(load()))
    except (InputsMissingError, SettingsError) as exc:
        if machine:
            print(
                ErrorEnvelope(command=COMMAND, error=type(exc).__name__, message=str(exc)).model_dump_json(
                    indent=indent
                )
            )
        else:
            print(f"cannot run: {exc}", file=sys.stderr)
        ctx.exit(2)
    if machine:
        envelope = GoldenEnvelope(ok=report.identical == report.total, command=COMMAND, data=report)
        print(envelope.model_dump_json(indent=indent))
    else:
        for o in report.outcomes:
            print(f"{'ok  ' if o.ok else 'FAIL'} {o.name}: {o.detail}")
        print(f"{report.identical}/{report.total} identical")
    ctx.exit(0 if report.identical == report.total else 1)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; ``lib_cli_exit_tools`` maps an unexpected exception onto an exit code."""
    return run_cli(cli, argv=argv, prog_name=COMMAND)


if __name__ == "__main__":
    raise SystemExit(main())
