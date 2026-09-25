#!/usr/bin/env python3
"""Regenerate src/soundtouch_zonemaster/adapters/soundtouch/pb from research/proto.

protoc writes both the runtime module (``*_pb2.py``) and its type stub (``*_pb2.pyi``); without the
stub every field access on a message is untyped, so the type checker cannot see the protocol at all.

Usage:
    uv run --with grpcio-tools python research/generate_pb.py
    uv run --with grpcio-tools python research/generate_pb.py --json

Exit 0 the modules were written, 1 protoc refused them, 2 the run could not be attempted.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

import rich_click as click
from _click import current_context, option, run_cli
from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Sequence

COMMAND = "generate_pb"

# The schemas the master speaks; each pulls in whatever it imports.
SCHEMAS = (
    "AudioServerMsgAudioData.proto",
    "AudioServerMsgDefinitions.proto",
    "IPCMessageEnvelopeBase.proto",
    "SoundTouchInterface/Zone.proto",
    "ProtoToMarkup/MarkupOptions.proto",
)


class Protoc(Protocol):
    """The two things this script uses from grpcio-tools, which ships no stubs of its own."""

    __file__: str

    def main(self, command_arguments: list[str]) -> int: ...


ROOT = Path(__file__).resolve().parent.parent
PROTO_DIR = ROOT / "research" / "proto"
OUT_DIR = ROOT / "src" / "soundtouch_zonemaster" / "adapters" / "soundtouch" / "pb"


class CannotRunError(Exception):
    """The generation could not be attempted: a schema is missing, or protoc is not importable."""


class ProtocFailedError(Exception):
    """protoc ran and refused the schemas. Its own return code is kept for the report."""

    def __init__(self, returncode: int) -> None:
        super().__init__(f"protoc failed with {returncode}")
        self.returncode = returncode


class GenerateReport(BaseModel):
    """What one regeneration wrote."""

    schemas: list[str]
    written: list[str]


class GenerateEnvelope(BaseModel):
    """The machine-readable result on success."""

    ok: bool
    command: str
    data: GenerateReport
    skipped: list[str] = []


class ErrorEnvelope(BaseModel):
    """The machine-readable result on failure; ``error`` is the class name, to branch on."""

    ok: bool = False
    command: str
    error: str
    message: str


def generate() -> GenerateReport:
    """Run protoc over ``SCHEMAS`` into ``adapters/soundtouch/pb``, or raise."""
    # grpcio-tools is a dev-only dependency. At module level this import would make the file
    # unimportable wherever the schemas are merely read rather than compiled.
    try:
        from grpc_tools import protoc as _protoc  # noqa: PLC0415  # pyright: ignore[reportMissingTypeStubs]
    except ImportError as exc:  # pragma: no cover - depends on how the script was launched
        msg = f"grpcio-tools is not available: {exc}; run with --with grpcio-tools"
        raise CannotRunError(msg) from exc

    # grpcio-tools ships no stubs; the Protoc protocol above is the type, and the cast applies it.
    protoc = cast("Protoc", _protoc)

    missing = [s for s in SCHEMAS if not (PROTO_DIR / s).exists()]
    if missing:
        msg = f"missing schemas under {PROTO_DIR}: {missing}"
        raise CannotRunError(msg)

    # grpcio-tools ships the well-known types (descriptor.proto and friends) beside its protoc;
    # MarkupOptions.proto imports one, so that directory has to be on the path too.
    well_known = Path(protoc.__file__).resolve().parent / "_proto"

    argv = [
        "protoc",
        f"--proto_path={PROTO_DIR}",
        f"--proto_path={well_known}",
        f"--python_out={OUT_DIR}",
        f"--pyi_out={OUT_DIR}",
        *SCHEMAS,
    ]
    rc = protoc.main(argv)
    if rc != 0:
        raise ProtocFailedError(rc)
    written = sorted(p.relative_to(ROOT).as_posix() for p in OUT_DIR.rglob("*_pb2.py*"))
    return GenerateReport(schemas=list(SCHEMAS), written=written)


def _render(report: GenerateReport, *, as_json: bool, indent: int | None) -> None:
    if not as_json:
        print("\n".join(report.written))
        return
    print(GenerateEnvelope(ok=True, command=COMMAND, data=report).model_dump_json(indent=indent))


def _render_error(exc: Exception, *, as_json: bool, indent: int | None) -> None:
    if not as_json:
        print(f"{exc}", file=sys.stderr)
        return
    envelope = ErrorEnvelope(command=COMMAND, error=type(exc).__name__, message=str(exc))
    print(envelope.model_dump_json(indent=indent))


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@option("--json", "as_json", is_flag=True, help="print a JSON envelope instead of the file list")
@option("--json-bare", "as_json_bare", is_flag=True, help="as --json, on one line for jq")
def cli(*, as_json: bool, as_json_bare: bool) -> None:
    """Regenerate the protobuf modules and stubs in adapters/soundtouch/pb."""
    machine = as_json or as_json_bare
    indent: int | None = None if as_json_bare else 2
    ctx = current_context()
    try:
        report = generate()
    except ProtocFailedError as exc:
        _render_error(exc, as_json=machine, indent=indent)
        ctx.exit(1)
    except CannotRunError as exc:
        _render_error(exc, as_json=machine, indent=indent)
        ctx.exit(2)
    _render(report, as_json=machine, indent=indent)
    ctx.exit(0)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; ``lib_cli_exit_tools`` maps an unexpected exception onto an exit code."""
    return run_cli(cli, argv=argv, prog_name=COMMAND)


if __name__ == "__main__":
    raise SystemExit(main())
