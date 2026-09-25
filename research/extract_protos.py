#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["protobuf>=5.29", "pydantic>=2.12", "rich-click>=1.9.4", "lib_cli_exit_tools>=2.3.4"]
# ///
"""Recover .proto schemas embedded in protoc-generated binaries.

protoc's C++ code generator embeds every compiled .proto as a serialized ``FileDescriptorProto``
string that is registered at startup. This tool finds those blobs in an ELF (or any byte blob),
walks the protobuf wire format from each embedded ``<name>.proto`` filename to the end of the
descriptor, parses it, and renders it back to .proto text.

Usage:
    extract_protos.py --out research/proto research/firmware/APServer research/firmware/BoseApp

Exit codes: 0 schemas written, 1 nothing found, 2 error. Prints a JSON envelope on stdout.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import rich_click as click
from _click import argument, current_context, option, run_cli
from google.protobuf import descriptor_pb2, descriptor_pool
from google.protobuf.message import DecodeError
from pydantic import BaseModel, ConfigDict, field_validator

if TYPE_CHECKING:
    from collections.abc import Sequence

COMMAND = "extract_protos"

__all__ = [
    "ErrorEnvelope",
    "ExtractEnvelope",
    "ExtractReport",
    "Found",
    "Options",
    "extract",
    "find_descriptors",
    "parse_options",
    "render_proto",
    "walk_file_descriptor",
]


class Varint(NamedTuple):
    """A decoded varint and where the next field begins. A NamedTuple: this is read per wire field."""

    value: int
    next_pos: int


class Candidate(NamedTuple):
    """A possible descriptor: where it starts and the .proto filename that anchored it."""

    start: int
    name: bytes


class ExtractReport(BaseModel):
    """What one sweep over the given binaries found."""

    per_source: dict[str, int]
    files: list[str]
    messages: int
    inexact: list[str]
    pool_failures: list[str]
    conflicts: list[str]


class ExtractEnvelope(BaseModel):
    """The machine-readable result this tool prints on success."""

    ok: bool
    command: str
    data: ExtractReport
    skipped: list[str] = []


class ErrorEnvelope(BaseModel):
    """The failure envelope every tool here prints, so a caller parses one format either way.

    ``error`` is the exception CLASS name and nothing else, because that is what a caller branches
    on; the human text goes in ``message``. Packing both into ``error`` made this the one tool of
    the eight a caller had to special-case.
    """

    ok: bool = False
    command: str
    error: str
    message: str


class Options(BaseModel):
    """One validated run: the binaries to sweep and where the .proto files go."""

    model_config = ConfigDict(frozen=True)

    binaries: tuple[Path, ...]
    out: Path

    @field_validator("binaries")
    @classmethod
    def _binaries_must_exist(cls, value: tuple[Path, ...]) -> tuple[Path, ...]:
        missing = [str(b) for b in value if not b.is_file()]
        if missing:
            raise ValueError(f"no such binary: {', '.join(missing)}")
        return value


class WireType(IntEnum):
    """The protobuf wire types this walker meets. A tag's low three bits carry one of these."""

    VARINT = 0
    LENGTH_DELIMITED = 2


# Top-level FileDescriptorProto field numbers and the wire type each one carries. The numbers stay
# literals on purpose: they are the format being walked, not this program's data, and protobuf does
# not expose them as constants on the Python class.
_FILE_DESCRIPTOR_FIELDS: dict[int, WireType] = {
    1: WireType.LENGTH_DELIMITED,
    2: WireType.LENGTH_DELIMITED,
    3: WireType.LENGTH_DELIMITED,
    4: WireType.LENGTH_DELIMITED,
    5: WireType.LENGTH_DELIMITED,
    6: WireType.LENGTH_DELIMITED,
    7: WireType.LENGTH_DELIMITED,
    8: WireType.LENGTH_DELIMITED,
    9: WireType.LENGTH_DELIMITED,
    10: WireType.VARINT,
    11: WireType.VARINT,
    12: WireType.LENGTH_DELIMITED,
    13: WireType.LENGTH_DELIMITED,
    14: WireType.VARINT,
}
_PROTO_NAME_RE = re.compile(rb"[A-Za-z0-9_./-]+\.proto")
# The .proto spelling of each scalar type, keyed by protobuf's own closed Type enum rather than by
# the bare wire numbers: the names are rendering data that cannot be derived from the enum, but the
# keys can be, and spelling them out as 1..18 duplicates a set protobuf already defines. The three
# composite types (message, enum, group) are absent on purpose - _type_name renders those from
# type_name instead.
_FD = descriptor_pb2.FieldDescriptorProto
_SCALAR_TYPE_NAMES = {
    _FD.TYPE_DOUBLE: "double",
    _FD.TYPE_FLOAT: "float",
    _FD.TYPE_INT64: "int64",
    _FD.TYPE_UINT64: "uint64",
    _FD.TYPE_INT32: "int32",
    _FD.TYPE_FIXED64: "fixed64",
    _FD.TYPE_FIXED32: "fixed32",
    _FD.TYPE_BOOL: "bool",
    _FD.TYPE_STRING: "string",
    _FD.TYPE_BYTES: "bytes",
    _FD.TYPE_UINT32: "uint32",
    _FD.TYPE_SFIXED32: "sfixed32",
    _FD.TYPE_SFIXED64: "sfixed64",
    _FD.TYPE_SINT32: "sint32",
    _FD.TYPE_SINT64: "sint64",
}
_LABELS = {_FD.LABEL_OPTIONAL: "optional", _FD.LABEL_REQUIRED: "required", _FD.LABEL_REPEATED: "repeated"}

# Protobuf wire-format constants used while walking a descriptor blob by hand.
_FIELD1_LENGTH_DELIMITED_TAG = 0x0A  # field 1, wire type 2: the descriptor's name
_MAX_ONE_BYTE_VARINT = 128  # a longer name would need a two-byte length, which this does not read
_TAG_AND_LENGTH_BYTES = 2
_FIRST_RESERVED_FIELD_NUMBER = 536_870_912  # an extension range ending here means 'to max'


@dataclass
class Found:
    """One embedded descriptor: where it sat and what it parsed to."""

    source: str
    offset: int
    descriptor: descriptor_pb2.FileDescriptorProto
    raw: bytes = field(repr=False)
    exact: bool = True
    """Re-serialising the parsed descriptor reproduces ``raw`` byte for byte.

    protoc emits descriptors in canonical field order, so a mismatch means the walk took too
    little or too much - the one thing a successful parse cannot tell you.
    """


def _read_varint(blob: bytes, pos: int) -> Varint:
    """Decode one varint; raise ValueError on a truncated or oversized one."""
    shift = 0
    value = 0
    for _ in range(10):
        if pos >= len(blob):
            raise ValueError("truncated varint")
        byte = blob[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return Varint(value, pos)
        shift += 7
    raise ValueError("varint too long")


def walk_file_descriptor(blob: bytes, start: int) -> int:
    """Walk top-level FileDescriptorProto fields from ``start``; return the end offset.

    Stops at the first byte sequence that is not a valid FileDescriptorProto field. The first
    field must be the name (field 1) or the walk is rejected with ValueError.
    """
    pos = start
    seen_any = False
    while pos < len(blob):
        try:
            tag, after_tag = _read_varint(blob, pos)
        except ValueError:
            break
        number, wire_type = tag >> 3, tag & 0x07
        expected = _FILE_DESCRIPTOR_FIELDS.get(number)
        if expected is None or expected != wire_type:
            break
        if not seen_any and number != 1:
            raise ValueError("descriptor does not start with its name")
        if wire_type == WireType.LENGTH_DELIMITED:
            try:
                length, after_len = _read_varint(blob, after_tag)
            except ValueError:
                break
            if after_len + length > len(blob):
                break
            pos = after_len + length
        else:
            try:
                _, pos = _read_varint(blob, after_tag)
            except ValueError:
                break
        seen_any = True
    if not seen_any:
        raise ValueError("no fields")
    return pos


def _candidate_starts(blob: bytes) -> list[Candidate]:
    """Every place a .proto filename sits right after a field-1 tag, which is where one may begin."""
    out: list[Candidate] = []
    for m in _PROTO_NAME_RE.finditer(blob):
        # The length byte before the name is itself a legal name character for lengths 45..57
        # ('-', '.', '/', '0'..'9'), so the regex swallows it: try every suffix of the match.
        for name_start in range(m.start(), m.end()):
            name = blob[name_start : m.end()]
            if len(name) >= _MAX_ONE_BYTE_VARINT or name_start < _TAG_AND_LENGTH_BYTES:
                continue
            if blob[name_start - 2] == _FIELD1_LENGTH_DELIMITED_TAG and blob[name_start - 1] == len(name):
                out.append(Candidate(name_start - 2, name))
                break
    return out


def find_descriptors(blob: bytes, *, source: str) -> list[Found]:
    """Find, walk and parse every embedded FileDescriptorProto in ``blob``.

    A candidate counts only if it parses AND its ``name`` equals the anchoring filename, which
    rejects string-table hits (a bare filename inside a log format) that happen to be preceded
    by the right two bytes.
    """
    found: list[Found] = []
    for candidate in _candidate_starts(blob):
        start, name = candidate.start, candidate.name
        try:
            end = walk_file_descriptor(blob, start)
        except ValueError:
            continue
        raw = blob[start:end]
        fdp = descriptor_pb2.FileDescriptorProto()
        try:
            fdp.ParseFromString(raw)
        except DecodeError:
            continue
        has_content = fdp.message_type or fdp.enum_type or fdp.service or fdp.extension
        if fdp.name.encode() != name or not has_content:
            continue
        exact = fdp.SerializeToString() == raw
        found.append(Found(source=source, offset=start, descriptor=fdp, raw=raw, exact=exact))
    return found


# --- rendering ---------------------------------------------------------------------------------


def _default_literal(f: descriptor_pb2.FieldDescriptorProto) -> str:
    if f.type == f.TYPE_STRING:
        return '"' + f.default_value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if f.type == f.TYPE_BYTES:
        return '"' + f.default_value + '"'
    return f.default_value


def _field_options(f: descriptor_pb2.FieldDescriptorProto) -> str:
    opts: list[str] = []
    if f.HasField("default_value"):
        opts.append(f"default = {_default_literal(f)}")
    if f.options.HasField("packed"):
        opts.append(f"packed = {'true' if f.options.packed else 'false'}")
    if f.options.deprecated:
        opts.append("deprecated = true")
    return f" [{', '.join(opts)}]" if opts else ""


def _type_name(f: descriptor_pb2.FieldDescriptorProto) -> str:
    if f.type in (f.TYPE_MESSAGE, f.TYPE_ENUM, f.TYPE_GROUP):
        return f.type_name  # fully qualified with a leading dot: unambiguous for protoc
    return _SCALAR_TYPE_NAMES[f.type]


def _render_field(f: descriptor_pb2.FieldDescriptorProto, indent: str, *, in_oneof: bool) -> str:
    label = "" if in_oneof else _LABELS.get(f.label, "optional") + " "
    return f"{indent}{label}{_type_name(f)} {f.name} = {f.number}{_field_options(f)};"


def _render_enum(e: descriptor_pb2.EnumDescriptorProto, indent: str) -> list[str]:
    lines = [f"{indent}enum {e.name} {{"]
    if e.options.allow_alias:
        lines.append(f"{indent}  option allow_alias = true;")
    lines += [f"{indent}  {v.name} = {v.number};" for v in e.value]
    lines.append(f"{indent}}}")
    return lines


def _render_message(m: descriptor_pb2.DescriptorProto, indent: str) -> list[str]:
    lines = [f"{indent}message {m.name} {{"]
    inner = indent + "  "
    for nested in m.nested_type:
        if nested.options.map_entry:
            continue
        lines += _render_message(nested, inner)
    for enum in m.enum_type:
        lines += _render_enum(enum, inner)
    plain = [f for f in m.field if not f.HasField("oneof_index")]
    lines += [_render_field(f, inner, in_oneof=False) for f in plain]
    for idx, oneof in enumerate(m.oneof_decl):
        lines.append(f"{inner}oneof {oneof.name} {{")
        members = [f for f in m.field if f.HasField("oneof_index") and f.oneof_index == idx]
        lines += [_render_field(f, inner + "  ", in_oneof=True) for f in members]
        lines.append(f"{inner}}}")
    if m.extension_range:
        ranges = ", ".join(
            f"{r.start} to {'max' if r.end >= _FIRST_RESERVED_FIELD_NUMBER else r.end - 1}" for r in m.extension_range
        )
        lines.append(f"{inner}extensions {ranges};")
    lines += _render_extensions(m.extension, inner)
    lines.append(f"{indent}}}")
    return lines


def _render_extensions(exts: Sequence[descriptor_pb2.FieldDescriptorProto], indent: str) -> list[str]:
    lines: list[str] = []
    by_extendee: dict[str, list[descriptor_pb2.FieldDescriptorProto]] = {}
    for f in exts:
        by_extendee.setdefault(f.extendee, []).append(f)
    for extendee, fields in by_extendee.items():
        lines.append(f"{indent}extend {extendee} {{")
        lines += [_render_field(f, indent + "  ", in_oneof=False) for f in fields]
        lines.append(f"{indent}}}")
    return lines


def render_proto(fdp: descriptor_pb2.FileDescriptorProto) -> str:
    """Render a FileDescriptorProto as .proto source text (proto2 unless it says proto3)."""
    syntax = fdp.syntax or "proto2"
    lines = [f'syntax = "{syntax}";', ""]
    if fdp.package:
        lines += [f"package {fdp.package};", ""]
    for dep in fdp.dependency:
        lines.append(f'import "{dep}";')
    if fdp.dependency:
        lines.append("")
    for enum in fdp.enum_type:
        lines += [*_render_enum(enum, ""), ""]
    for msg in fdp.message_type:
        lines += [*_render_message(msg, ""), ""]
    lines += _render_extensions(fdp.extension, "")
    for svc in fdp.service:
        lines.append(f"service {svc.name} {{")
        for method in svc.method:
            lines.append(f"  rpc {method.name} ({method.input_type}) returns ({method.output_type});")
        lines.append("}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --- CLI ----------------------------------------------------------------------------------------


def _validate_pool(found: list[Found]) -> list[str]:
    """Add every descriptor to one pool; return the names that fail (unresolved imports/types)."""
    pool = descriptor_pool.DescriptorPool()
    # Custom options import google/protobuf/descriptor.proto, which a fresh pool does not know.
    if descriptor_pb2.DESCRIPTOR.serialized_pb:
        pool.Add(descriptor_pb2.FileDescriptorProto.FromString(descriptor_pb2.DESCRIPTOR.serialized_pb))
    by_name = {f.descriptor.name: f.descriptor for f in found}
    failures: list[str] = []
    added: set[str] = set()
    tried: set[str] = set()

    def add(name: str) -> None:
        if name in tried or name not in by_name:
            return
        tried.add(name)
        for dep in by_name[name].dependency:
            add(dep)
        try:
            pool.Add(by_name[name])
            added.add(name)
        except Exception as exc:  # noqa: BLE001 - report, do not crash the sweep
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    for name in by_name:
        add(name)
    return failures


def parse_options(binaries: tuple[Path, ...], out: Path) -> Options:
    """Validate what the CLI collected. The model is the only place a value becomes trusted."""
    return Options(binaries=binaries, out=out)


def extract(options: Options) -> ExtractEnvelope:
    """Sweep the binaries and write every recovered schema under ``options.out``."""
    found: list[Found] = []
    per_source: dict[str, int] = {}
    for path in options.binaries:
        blob = path.read_bytes()
        hits = find_descriptors(blob, source=path.name)
        per_source[path.name] = len(hits)
        found.extend(hits)

    # The same .proto is compiled into several binaries; keep the first copy but notice any
    # byte-level disagreement, which would mean two firmware generations in one filesystem.
    unique: dict[str, Found] = {}
    conflicts: list[str] = []
    for f in found:
        name = f.descriptor.name
        if name in unique and unique[name].raw != f.raw:
            conflicts.append(f"{name}: {unique[name].source} vs {f.source}")
        unique.setdefault(name, f)

    options.out.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name, f in sorted(unique.items()):
        target = options.out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_proto(f.descriptor), encoding="utf-8")
        written.append(name)

    return ExtractEnvelope(
        ok=bool(written),
        command=COMMAND,
        data=ExtractReport(
            per_source=per_source,
            files=written,
            messages=sum(len(f.descriptor.message_type) for f in unique.values()),
            inexact=sorted(f.descriptor.name for f in unique.values() if not f.exact),
            pool_failures=_validate_pool(list(unique.values())),
            conflicts=conflicts,
        ),
    )


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@argument("binaries", nargs=-1, required=True, type=click.Path(path_type=Path))
@option("--out", required=True, type=click.Path(path_type=Path), help="directory for the .proto files")
@option("--json", "as_json", is_flag=True, help="print the envelope indented (the default)")
@option("--json-bare", "as_json_bare", is_flag=True, help="print the envelope on one line for jq")
def cli(*, binaries: tuple[Path, ...], out: Path, as_json: bool, as_json_bare: bool) -> None:
    """Recover the .proto schemas embedded in the given firmware binaries.

    This tool has always printed a JSON envelope on every run, so unlike its siblings it needs no
    flag to become machine-readable; ``--json`` is accepted for symmetry and ``--json-bare`` puts
    the same envelope on one line.
    """
    indent: int | None = None if as_json_bare else 2
    ctx = current_context()
    try:
        envelope = extract(parse_options(binaries, out))
    except Exception as exc:  # noqa: BLE001 - CLI edge: typed envelope instead of a traceback
        failure = ErrorEnvelope(command=COMMAND, error=type(exc).__name__, message=str(exc))
        print(failure.model_dump_json(indent=indent))
        ctx.exit(2)
    print(envelope.model_dump_json(indent=indent))
    ctx.exit(0 if envelope.ok else 1)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; ``lib_cli_exit_tools`` maps an unexpected exception onto an exit code."""
    return run_cli(cli, argv=argv, prog_name=COMMAND)


if __name__ == "__main__":
    raise SystemExit(main())
