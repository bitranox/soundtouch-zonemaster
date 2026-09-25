"""Tests for research/extract_protos.py: a planted descriptor must be found, a mangled one not."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from google.protobuf import descriptor_pb2
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))
import extract_protos as ep

if TYPE_CHECKING:
    from generate_pb import Protoc


def _zone_descriptor() -> descriptor_pb2.FileDescriptorProto:
    fdp = descriptor_pb2.FileDescriptorProto()
    fdp.name = "SoundTouchInterface/Zone.proto"
    fdp.package = "SoundTouchInterface"
    fdp.dependency.append("Common.proto")
    msg = fdp.message_type.add()
    msg.name = "zone"
    f1 = msg.field.add()
    f1.name, f1.number, f1.label, f1.type = "master", 1, f1.LABEL_OPTIONAL, f1.TYPE_STRING
    f2 = msg.field.add()
    f2.name, f2.number, f2.label, f2.type = "member", 2, f2.LABEL_REPEATED, f2.TYPE_MESSAGE
    f2.type_name = ".SoundTouchInterface.zone.Member"
    nested = msg.nested_type.add()
    nested.name = "Member"
    nf = nested.field.add()
    nf.name, nf.number, nf.label, nf.type = "ipaddress", 1, nf.LABEL_OPTIONAL, nf.TYPE_STRING
    nf.default_value = "0.0.0.0"
    enum = fdp.enum_type.add()
    enum.name = "ZoneStatus"
    for i, name in enumerate(["STANDALONE", "MASTER", "SLAVE"]):
        v = enum.value.add()
        v.name, v.number = name, i
    return fdp


def _plant(payload: bytes) -> bytes:
    """Surround the payload the way .rodata does: other strings, then a NUL, then more strings.

    Deterministic on purpose: a random byte after the descriptor is a VALID field tag about 5 %
    of the time, which would make the exact-bounds assertion flake.
    """
    before = b"%s - I am a slave and my IP hasn't changed '%s'\x00/setZone\x00"
    after = b"\x00ZoneServiceImpl::BroadcastZoneList\x00\x0a\x05junk!\x00"
    return before + payload + after


def test_planted_descriptor_is_found_with_exact_bounds() -> None:
    fdp = _zone_descriptor()
    raw = fdp.SerializeToString()
    blob = _plant(raw)
    found = ep.find_descriptors(blob, source="synthetic")
    assert [f.descriptor.name for f in found] == ["SoundTouchInterface/Zone.proto"]
    assert found[0].raw == raw, "the walk must stop exactly at the descriptor's end"
    assert [f.name for f in found[0].descriptor.message_type[0].field] == ["master", "member"]


def test_a_bare_filename_string_is_not_a_descriptor() -> None:
    # A log format string like "%s: Zone.proto" preceded by 0x0a+len looks like a field-1 tag.
    name = b"SoundTouchInterface/Zone.proto"
    decoy = b"\x0a" + bytes([len(name)]) + name + b" failed to load\x00"
    assert ep.find_descriptors(_plant(decoy), source="decoy") == []


def test_a_descriptor_followed_by_a_plausible_tag_is_still_cut_at_the_nul() -> None:
    raw = _zone_descriptor().SerializeToString()
    # 0x1a is a valid field-3 (dependency) tag; the NUL before it must end the walk first.
    blob = raw + b"\x00\x1a\x04junk"
    found = ep.find_descriptors(blob, source="tail")
    assert len(found) == 1 and found[0].raw == raw and found[0].exact


def test_exact_is_false_when_the_walk_overruns_into_a_valid_looking_field() -> None:
    raw = _zone_descriptor().SerializeToString()
    # No NUL: the junk IS parsed as a dependency (field 3). protoc serialises fields in number
    # order, so a field 3 arriving after field 4 cannot be reproduced by re-serialisation, and the
    # exactness flag catches the overrun. (A tail tag with a HIGHER number than the last real
    # field would slip through; that is the documented blind spot.)
    blob = raw + b"\x1a\x04junk\x00"
    found = ep.find_descriptors(blob, source="overrun")
    assert len(found) == 1
    assert found[0].descriptor.dependency[-1] == "junk"
    assert not found[0].exact


def test_render_round_trips_through_protoc() -> None:
    pytest.importorskip("grpc_tools")  # the runner below needs it; the rest of this file does not
    from grpc_tools import protoc as _protoc  # pyright: ignore[reportMissingTypeStubs]

    protoc = cast("Protoc", _protoc)

    fdp = _zone_descriptor()
    text = ep.render_proto(fdp)
    assert "repeated .SoundTouchInterface.zone.Member member = 2;" in text
    assert 'optional string ipaddress = 1 [default = "0.0.0.0"];' in text
    assert "enum ZoneStatus {" in text
    # Compile the rendered text (plus a stub for its import) and compare descriptors.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "SoundTouchInterface").mkdir()
        (root / "SoundTouchInterface" / "Zone.proto").write_text(text)
        (root / "Common.proto").write_text('syntax = "proto2";\n')
        out = root / "desc.pb"
        rc = protoc.main(
            [
                "protoc",
                f"-I{root}",
                f"--descriptor_set_out={out}",
                "SoundTouchInterface/Zone.proto",
            ]
        )
        assert rc == 0
        fds = descriptor_pb2.FileDescriptorSet()
        fds.ParseFromString(out.read_bytes())
        compiled = next(f for f in fds.file if f.name == fdp.name)
        assert compiled.package == fdp.package
        assert [m.name for m in compiled.message_type] == ["zone"]
        assert compiled.message_type[0].field[1].type_name == ".SoundTouchInterface.zone.Member"
        assert compiled.message_type[0].nested_type[0].field[0].default_value == "0.0.0.0"
        assert [v.name for v in compiled.enum_type[0].value] == ["STANDALONE", "MASTER", "SLAVE"]


def test_walk_rejects_a_blob_that_does_not_start_with_the_name() -> None:
    fdp = _zone_descriptor()
    raw = fdp.SerializeToString()
    # Skip the name field: the walker must refuse rather than accept a headless descriptor.
    name_len = raw[1]
    headless = raw[2 + name_len :]
    with pytest.raises(ValueError):
        ep.walk_file_descriptor(headless, 0)


def test_a_name_whose_length_byte_is_a_name_character_is_still_found() -> None:
    fdp = _zone_descriptor()
    fdp.name = "SoundTouchInterface/initializationcompleteaction.proto"  # 54 chars -> length byte '6'
    assert len(fdp.name) == 0x36
    raw = fdp.SerializeToString()
    found = ep.find_descriptors(_plant(raw), source="len-is-digit")
    assert [f.descriptor.name for f in found] == [fdp.name]
    assert found[0].raw == raw


def _common_descriptor() -> descriptor_pb2.FileDescriptorProto:
    """The dependency Zone.proto imports, so a sweep holding both resolves in one pool."""
    fdp = descriptor_pb2.FileDescriptorProto()
    fdp.name = "Common.proto"
    fdp.package = "SoundTouchInterface"
    msg = fdp.message_type.add()
    msg.name = "header"
    f = msg.field.add()
    f.name, f.number, f.label, f.type = "device", 1, f.LABEL_OPTIONAL, f.TYPE_STRING
    return fdp


def _binary(path: Path, fdp: descriptor_pb2.FileDescriptorProto) -> Path:
    """One prepared binary: a descriptor planted in the kind of noise .rodata is made of."""
    path.write_bytes(_plant(fdp.SerializeToString()))
    return path


def test_a_sweep_writes_every_schema_it_found_and_says_which_binary_held_it(tmp_path: Path) -> None:
    """The whole tool in one call: two binaries in, two .proto files on disk and a report of them.

    The two are a pair on purpose: Zone.proto imports Common.proto, so a sweep holding both is
    also the case where the pool has everything it needs and reports no failure.
    """
    out = tmp_path / "proto"
    envelope = ep.extract(
        ep.parse_options(
            (
                _binary(tmp_path / "APServer", _zone_descriptor()),
                _binary(tmp_path / "BoseApp", _common_descriptor()),
            ),
            out,
        )
    )
    assert envelope.ok
    assert envelope.data.per_source == {"APServer": 1, "BoseApp": 1}
    assert envelope.data.files == ["Common.proto", "SoundTouchInterface/Zone.proto"]
    assert envelope.data.messages == 2
    assert envelope.data.conflicts == []
    assert envelope.data.inexact == []
    assert envelope.data.pool_failures == [], "Zone's import was in the sweep, so the pool resolved it"
    assert (out / "SoundTouchInterface" / "Zone.proto").read_text(encoding="utf-8").startswith('syntax = "proto2"')
    assert "optional string device = 1;" in (out / "Common.proto").read_text(encoding="utf-8")


def test_the_same_schema_in_two_binaries_is_written_once_and_is_not_a_conflict(tmp_path: Path) -> None:
    """The normal case in real firmware: one .proto compiled into several binaries, byte for byte."""
    out = tmp_path / "proto"
    envelope = ep.extract(
        ep.parse_options(
            (
                _binary(tmp_path / "APServer", _zone_descriptor()),
                _binary(tmp_path / "BoseApp", _zone_descriptor()),
            ),
            out,
        )
    )
    assert envelope.data.per_source == {"APServer": 1, "BoseApp": 1}, "both binaries did hold a copy"
    assert envelope.data.files == ["SoundTouchInterface/Zone.proto"], "and one file was written for the two"
    assert envelope.data.conflicts == []


def test_two_copies_of_one_name_that_differ_are_a_conflict_and_the_first_binary_wins(tmp_path: Path) -> None:
    """Two firmware generations in one filesystem is the case this notices rather than silently mixes.

    Keeping the first copy is the tool's rule; what matters is that the disagreement is REPORTED,
    because a schema silently taken from the older binary would decode the newer traffic wrongly.
    """
    newer = _zone_descriptor()
    extra = newer.message_type[0].field.add()
    extra.name, extra.number, extra.label, extra.type = "extra", 3, extra.LABEL_OPTIONAL, extra.TYPE_STRING
    out = tmp_path / "proto"
    envelope = ep.extract(
        ep.parse_options(
            (
                _binary(tmp_path / "APServer", _zone_descriptor()),
                _binary(tmp_path / "OtherApp", newer),
            ),
            out,
        )
    )
    assert envelope.data.conflicts == ["SoundTouchInterface/Zone.proto: APServer vs OtherApp"]
    assert envelope.data.files == ["SoundTouchInterface/Zone.proto"]
    written = (out / "SoundTouchInterface" / "Zone.proto").read_text(encoding="utf-8")
    assert "extra" not in written, "the first copy is the one on disk"
    assert [f.startswith("SoundTouchInterface/Zone.proto:") for f in envelope.data.pool_failures] == [True], (
        "Common.proto was not in this sweep, so the pool says so rather than passing silently"
    )


def test_a_binary_holding_no_descriptor_writes_nothing_and_reports_not_ok(tmp_path: Path) -> None:
    """``ok`` false is what the CLI turns into exit 1: it ran, and the answer is nothing found."""
    binary = tmp_path / "NoProtos"
    binary.write_bytes(b"just strings\x00and %s formats\x00no descriptor here\x00")
    envelope = ep.extract(ep.parse_options((binary,), tmp_path / "proto"))
    assert not envelope.ok
    assert envelope.data.per_source == {"NoProtos": 0}
    assert envelope.data.files == []
    assert list((tmp_path / "proto").iterdir()) == [], "the directory is made, and left empty"


def test_a_binary_that_is_not_there_is_refused_before_anything_is_swept(tmp_path: Path) -> None:
    """The refusal the CLI reports as an error envelope; a missing path must not read as empty."""
    with pytest.raises(ValidationError, match="no such binary"):
        ep.parse_options((tmp_path / "absent",), tmp_path / "proto")
