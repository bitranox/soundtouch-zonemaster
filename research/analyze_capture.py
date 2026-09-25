#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["protobuf>=5.29", "pydantic>=2.12", "scapy>=2.6", "rich-click>=1.9.4",
#                 "lib_cli_exit_tools>=2.3.4"]
# ///
"""Decode a SoundTouch zone capture: every IPC frame between master and slave, by schema.

Frames on the zone TCP ports are ``uint32 big-endian length`` + ``IPCMessageEnvelopeBase``; the
envelope names the payload type in clear text (``msg_typename``) and the payload is decoded with
the schemas recovered by extract_protos.py from the same firmware.

    analyze_capture.py --pcap captures/<run>/<master-ip>.pcap --master 192.168.0.33 --slave 192.168.0.31 \
        --firmware firmware/APServer firmware/BoseApp firmware/libIPC.so firmware/libSoundTouch_SDK_Protobuf.so

Prints a human-readable report on stdout, or the same document inside a JSON envelope with
--json / --json-bare. Exit 0 report written, 2 error.
"""

from __future__ import annotations

import io
import ipaddress
import struct
import sys
import time
from collections import Counter, defaultdict
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import rich_click as click
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from pydantic import BaseModel, ConfigDict, field_validator
from scapy.layers.inet import IP, TCP, UDP
from scapy.utils import rdpcap

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract_protos as ep
from _click import current_context, option, run_cli
from capture_model import (
    AudioData,
    Capture,
    ConversationKey,
    DecodedEnvelope,
    DecodedStream,
    DirectedSegments,
    Direction,
    Endpoint,
    Envelope,
    Frame,
    MsgKind,
    MsgTypeName,
    Segment,
    SegmentStart,
    TimedDecode,
    UdpFlow,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from google.protobuf.descriptor import Descriptor
    from google.protobuf.message import Message
    from scapy.packet import Packet

# Wire and discovery constants, named so a reader of the analysis sees the protocol.
_LENGTH_PREFIX_BYTES = 4  # every IPC frame opens with a big-endian u32 length
_DATA_PORT = 40003  # the audio data channel; 40002 is transport
_MAX_BULK_FRAMES_SHOWN = 3  # a data channel repeats itself; a few frames tell the story
_SSDP_PORT = 1900
_MDNS_PORT = 5353
_UDP_FLOWS_SHOWN = 10
_UDP_PACKETS_SHOWN = 14

COMMAND = "analyze_capture"

EXIT_CONTRACT = "Exit 0 report written, 2 error."

__all__ = [
    "AnalysisEnvelope",
    "AnalysisReport",
    "ErrorEnvelope",
    "Options",
    "build_pool",
    "capture_options",
    "decode_envelope",
    "emit",
    "emit_error",
    "frames_of",
    "parse_options",
    "reassemble",
]


class Options(BaseModel):
    """One validated run of the analyser.

    The checks are here because the alternative is silent: a mistyped ``--master`` matches no
    packet, and the analyser then prints a well-formed report about nothing at all.
    """

    model_config = ConfigDict(frozen=True)

    pcap: Path
    master: str
    slave: str
    firmware: tuple[Path, ...]

    @field_validator("master", "slave")
    @classmethod
    def _an_ip_address(cls, value: str) -> str:
        ipaddress.ip_address(value)
        return value

    @field_validator("pcap")
    @classmethod
    def _pcap_must_exist(cls, value: Path) -> Path:
        if not value.is_file():
            raise ValueError(f"no such capture: {value}")
        return value

    @field_validator("firmware")
    @classmethod
    def _binaries_must_exist(cls, value: tuple[Path, ...]) -> tuple[Path, ...]:
        missing = [str(p) for p in value if not p.is_file()]
        if missing:
            raise ValueError(f"no such firmware binary: {', '.join(missing)}")
        return value


def capture_options(func: Callable[..., Any]) -> Callable[..., Any]:
    """The four inputs both analysers take, plus the two machine-readable switches.

    analyze_late_join.py shares this, which is why it is a decorator rather than a parser object:
    each tool keeps its OWN docstring as its --help text, which is the one place a reader goes to
    find out which of the two they are holding.

    ``--firmware`` is REPEATED (``--firmware a --firmware b``) rather than variadic. argparse took
    ``nargs="+"`` here; click has no variadic option, so the invocation form changed with the
    framework and golden_check.py builds the repeated form to match.
    """
    for decorate in (
        option("--json-bare", "as_json_bare", is_flag=True, help="as --json, on one line for jq"),
        option("--json", "as_json", is_flag=True, help="wrap the report in a JSON envelope"),
        option(
            "--firmware",
            required=True,
            multiple=True,
            type=click.Path(path_type=Path),
            help="a firmware binary to recover schemas from; repeat for each",
        ),
        option("--slave", required=True, help="the slave's address in the capture"),
        option("--master", required=True, help="the master's address in the capture"),
        option("--pcap", required=True, type=click.Path(path_type=Path), help="the capture to read"),
    ):
        func = decorate(func)
    return func


def parse_options(pcap: Path, master: str, slave: str, firmware: Sequence[Path]) -> Options:
    """Validate what the CLI collected. The model is the only place a value becomes trusted."""
    return Options(pcap=pcap, master=master, slave=slave, firmware=tuple(firmware))


class AnalysisReport(BaseModel):
    """One analysed capture: what was read, and the document that was produced."""

    pcap: str
    packets: int
    master: str
    slave: str
    report: str
    """The markdown document verbatim - byte for byte what the human mode prints."""


class AnalysisEnvelope(BaseModel):
    """The machine-readable result on success."""

    ok: bool
    command: str
    data: AnalysisReport
    skipped: list[str] = []


class ErrorEnvelope(BaseModel):
    """The machine-readable result on failure; ``error`` is the class name, to branch on."""

    ok: bool = False
    command: str
    error: str
    message: str


def emit(report: AnalysisReport, *, command: str, machine: bool, indent: int | None) -> None:
    """Print the result. In human mode the document has already gone to stdout untouched."""
    if not machine:
        return
    print(AnalysisEnvelope(ok=True, command=command, data=report).model_dump_json(indent=indent))


def emit_error(exc: Exception, *, command: str, machine: bool, indent: int | None) -> None:
    """Print the failure; prose to stderr, JSON to stdout so a pipeline still parses it."""
    if not machine:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return
    print(ErrorEnvelope(command=command, error=type(exc).__name__, message=str(exc)).model_dump_json(indent=indent))


def build_pool(binaries: Sequence[Path]) -> descriptor_pool.DescriptorPool:
    """One pool holding every schema found in the given binaries (first copy of a name wins)."""
    pool = descriptor_pool.DescriptorPool()
    pool.Add(descriptor_pb2.FileDescriptorProto.FromString(descriptor_pb2.DESCRIPTOR.serialized_pb))
    by_name: dict[str, descriptor_pb2.FileDescriptorProto] = {}
    for b in binaries:
        for f in ep.find_descriptors(b.read_bytes(), source=b.name):
            by_name.setdefault(f.descriptor.name, f.descriptor)
    added: set[str] = set()

    def add(name: str) -> None:
        if name in added or name not in by_name:
            return
        added.add(name)
        for dep in by_name[name].dependency:
            add(dep)
        pool.Add(by_name[name])

    for name in by_name:
        add(name)
    return pool


def reassemble(packets: Iterable[Packet], a: Endpoint, b: Endpoint) -> DirectedSegments:
    """Per-direction ordered payload segments of one TCP conversation, de-duplicated by seq."""
    out: dict[Direction, dict[int, Segment]] = {d: {} for d in Direction}
    for p in packets:
        if IP not in p or TCP not in p:
            continue
        src, dst = Endpoint(p[IP].src, p[TCP].sport), Endpoint(p[IP].dst, p[TCP].dport)
        payload = bytes(p[TCP].payload)
        if not payload:
            continue
        if (src, dst) == (a, b):
            out[Direction.SLAVE_TO_MASTER].setdefault(p[TCP].seq, Segment(float(p.time), payload))
        elif (src, dst) == (b, a):
            out[Direction.MASTER_TO_SLAVE].setdefault(p[TCP].seq, Segment(float(p.time), payload))
    ordered = {d: [segs[k] for k in sorted(segs)] for d, segs in out.items()}
    return DirectedSegments(ordered[Direction.SLAVE_TO_MASTER], ordered[Direction.MASTER_TO_SLAVE])


def frames_of(segments: Sequence[Segment]) -> list[Frame]:
    """Split a byte stream into length-prefixed frames; each frame gets the time of its first byte."""
    buf = b""
    starts: list[SegmentStart] = []
    frames: list[Frame] = []
    consumed = 0
    for segment in segments:
        starts.append(SegmentStart(consumed + len(buf), segment.at))
        buf += segment.payload
        while len(buf) >= _LENGTH_PREFIX_BYTES:
            (length,) = struct.unpack(">I", buf[:4])
            if len(buf) < 4 + length:
                break
            start = consumed
            at = max((s.at for s in starts if s.offset <= start), default=segment.at)
            frames.append(Frame(at, buf[4 : 4 + length]))
            buf = buf[4 + length :]
            consumed += 4 + length
    return frames


def decode_envelope(pool: descriptor_pool.DescriptorPool, body: bytes) -> DecodedEnvelope:
    """Parse the IPC envelope, and the payload inside it when its ``msg_typename`` resolves."""
    env_cls = message_factory.GetMessageClass(pool.FindMessageTypeByName("IPCMessageEnvelopeBase"))
    env = cast("Envelope", env_cls())
    env.ParseFromString(body)
    payload = None
    err = ""
    name = env.msg_typename
    if name:
        try:
            desc = pool.FindMessageTypeByName(name)
        except KeyError:
            desc = _find_by_simple_name(pool, name)
        if desc is not None:
            try:
                payload = message_factory.GetMessageClass(desc)()
                payload.ParseFromString(env.msg_contents)
            except Exception as exc:  # noqa: BLE001 - report the decode failure inline
                err = f"decode failed: {exc}"
        else:
            err = "no schema"
    return DecodedEnvelope(env, payload, err)


def _find_by_simple_name(pool: descriptor_pool.DescriptorPool, name: str) -> Descriptor | None:
    # The pool has no enumeration API; walk the files we know via a private but stable attribute.
    for fd in getattr(pool, "_file_descriptors", {}).values():  # pragma: no cover - fallback path
        for m in fd.message_types_by_name.values():
            if m.name == name:
                return m
    return None


def _short(msg: Message, limit: int = 220) -> str:
    text = " ".join(str(msg).split())
    if len(text) > limit:
        text = text[:limit] + " ..."
    return text


def _clock(t: float, t0: float) -> str:
    return f"+{t - t0:7.3f}s"


def report_tcp(pool: descriptor_pool.DescriptorPool, cap: Capture) -> None:
    packets, master, slave, t0 = cap.packets, cap.master, cap.slave, cap.t0
    convs: defaultdict[ConversationKey, list[Packet]] = defaultdict(list)
    for p in packets:
        if IP in p and TCP in p and {p[IP].src, p[IP].dst} == {master, slave}:
            if p[IP].dst == master:
                convs[ConversationKey(p[TCP].dport, Endpoint(p[IP].src, p[TCP].sport))].append(p)
            else:
                convs[ConversationKey(p[TCP].sport, Endpoint(p[IP].dst, p[TCP].dport))].append(p)
    for conv, pk in sorted(convs.items(), key=lambda kv: float(kv[1][0].time)):
        streams = reassemble(pk, conv.client, Endpoint(master, conv.master_port))
        print(
            f"\n## TCP {conv.client.host}:{conv.client.port} -> {master}:{conv.master_port}  ({len(pk)} packets, "
            f"{time.strftime('%H:%M:%S', time.localtime(float(pk[0].time)))} .. "
            f"{time.strftime('%H:%M:%S', time.localtime(float(pk[-1].time)))} speaker clock)"
        )
        for direction in Direction:
            _report_direction(pool, streams.of(direction), direction, conv, t0)


def _decode_all(pool: descriptor_pool.DescriptorPool, frames: Sequence[Frame]) -> DecodedStream:
    """Decode every frame, counting what each one turned out to be."""
    counts: Counter[str] = Counter()
    decoded: list[TimedDecode] = []
    for frame in frames:
        try:
            result = decode_envelope(pool, frame.body)
        except Exception as exc:  # noqa: BLE001 - an undecodable envelope is a finding, not a crash
            counts["<undecodable envelope>"] += 1
            decoded.append(TimedDecode(frame.at, None, None, f"envelope: {exc}"))
            continue
        counts[result.envelope.msg_typename or f"id={result.envelope.msg_id}"] += 1
        decoded.append(TimedDecode(frame.at, result.envelope, result.payload, result.error))
    return DecodedStream(decoded, counts)


def _report_direction(
    pool: descriptor_pool.DescriptorPool,
    segments: Sequence[Segment],
    direction: Direction,
    conv: ConversationKey,
    t0: float,
) -> None:
    """One direction of one conversation: the type histogram, then the frames themselves."""
    frames = frames_of(segments)
    stream = _decode_all(pool, frames)
    print(f"\n### {direction.label}: {len(frames)} frames, {sum(len(f.body) for f in frames)} bytes")
    for name, n in stream.counts.most_common():
        print(f"    {n:6d}  {name}")
    # The data channel repeats one payload thousands of times; a few of them tell the story.
    bulk = conv.master_port == _DATA_PORT and direction is Direction.MASTER_TO_SLAVE
    shown = 0
    for entry in stream.entries:
        if entry.envelope is None:
            print(f"  {_clock(entry.at, t0)}  {entry.error}")
            continue
        if bulk and entry.envelope.msg_typename == MsgTypeName.ACCEPT_AUDIO_DATA and shown >= _MAX_BULK_FRAMES_SHOWN:
            continue
        shown += 1 if bulk else 0
        print(
            f"  {_clock(entry.at, t0)}  {MsgKind.label_for(entry.envelope.msg_type)} "
            f"id={entry.envelope.msg_id:<3} seq={entry.envelope.sequence:<4} "
            f"{entry.envelope.msg_typename or '?':<38} {_body_text(entry)}"
        )


def _body_text(entry: TimedDecode) -> str:
    """What the frame carried: an audio chunk spelled out, another payload shortened, else the error."""
    if entry.payload is None:
        return entry.error
    if entry.payload.DESCRIPTOR.name == MsgTypeName.ACCEPT_AUDIO_DATA:
        chunk = cast("AudioData", entry.payload)
        data = chunk.data
        return (
            f"stream_id={chunk.stream_id} byte_offset={chunk.byte_offset} "
            f"encryption_type={chunk.encryption_type} endofstream={chunk.endofstream} "
            f"slave_underflow={chunk.slave_underflow} data={len(data)}B head={data[:16].hex(' ')}"
        )
    return _short(entry.payload)


def report_udp(cap: Capture) -> None:
    packets, master, slave, t0 = cap.packets, cap.master, cap.slave, cap.t0
    pk = [
        p
        for p in packets
        if IP in p
        and UDP in p
        and {p[IP].src, p[IP].dst} == {master, slave}
        and _SSDP_PORT not in (p[UDP].sport, p[UDP].dport)
        and _MDNS_PORT not in (p[UDP].sport, p[UDP].dport)
    ]
    print(f"\n## UDP between master and slave (excluding SSDP/mDNS): {len(pk)} packets")
    flows = Counter(
        UdpFlow(
            Endpoint(p[IP].src, p[UDP].sport),
            Endpoint(p[IP].dst, p[UDP].dport),
            len(bytes(p[UDP].payload)),
        )
        for p in pk
    )
    for flow, n in flows.most_common(_UDP_FLOWS_SHOWN):
        print(f"    {n:5d}  {flow.src.host}:{flow.src.port} -> {flow.dst.host}:{flow.dst.port}  {flow.length} bytes")
    last = None
    for p in pk[:_UDP_PACKETS_SHOWN]:
        t = float(p.time)
        gap = f"(+{t - last:.3f})" if last else ""
        last = t
        pl = bytes(p[UDP].payload)
        words = " ".join(f"{w:08x}" for w in struct.unpack(f">{len(pl) // 4}I", pl[: len(pl) // 4 * 4]))
        print(f"  {_clock(t, t0)} {gap:>9}  {p[IP].src}->{p[IP].dst}  {words}")


def analyse(options: Options) -> AnalysisReport:
    """Print the master-side document and describe what was read.

    The document goes to stdout exactly as it always has. ``report`` is filled by the caller,
    which is the only place that knows whether stdout was being captured.
    """
    pool = build_pool(options.firmware)
    packets = rdpcap(str(options.pcap))
    t0 = float(packets[0].time)
    print(f"# {options.pcap.name}: {len(packets)} packets; times are seconds since the first packet")
    capture = Capture(packets=packets, master=options.master, slave=options.slave, t0=t0)
    report_tcp(pool, capture)
    report_udp(capture)
    return AnalysisReport(
        pcap=str(options.pcap), packets=len(packets), master=options.master, slave=options.slave, report=""
    )


@click.command(context_settings={"help_option_names": ["-h", "--help"]}, epilog=EXIT_CONTRACT)
@capture_options
def cli(  # noqa: PLR0913 - a click callback's signature IS the option list; the only shorter form is an untyped **kwargs
    *, pcap: Path, master: str, slave: str, firmware: tuple[Path, ...], as_json: bool, as_json_bare: bool
) -> None:
    """Decode a zone capture frame by frame and print the master-side document.

    Without --json the document goes to stdout byte for byte as it always has; golden_check.py
    holds it to the recorded analysis-master-side.md, so the redirect below is deliberately not
    installed in that mode.
    """
    machine = as_json or as_json_bare
    indent: int | None = None if as_json_bare else 2
    ctx = current_context()
    buf = io.StringIO()
    try:
        options = parse_options(pcap, master, slave, firmware)
        with redirect_stdout(buf) if machine else nullcontext():
            report = analyse(options)
    except Exception as exc:  # noqa: BLE001 - CLI edge: an envelope, not a traceback
        emit_error(exc, command=COMMAND, machine=machine, indent=indent)
        ctx.exit(2)
    emit(report.model_copy(update={"report": buf.getvalue()}), command=COMMAND, machine=machine, indent=indent)
    ctx.exit(0)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; ``lib_cli_exit_tools`` maps an unexpected exception onto an exit code."""
    return run_cli(cli, argv=argv, prog_name=COMMAND)


if __name__ == "__main__":
    raise SystemExit(main())
