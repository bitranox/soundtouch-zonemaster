#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["protobuf>=5.29", "pydantic>=2.12", "scapy>=2.6", "rich-click>=1.9.4",
#                 "lib_cli_exit_tools>=2.3.4"]
# ///
"""How a firmware master hands a joining slave its position in a running stream.

Reads a master-side zone capture and puts, per join and stream (``url_id``), everything that
bears on the joiner's timeline onto the capture clock:

* the ``TransportControl PLAY at_microseconds`` the master sent, translated from the master clock
  via the UDP clock-sync replies (``T3`` is the master's send time in that clock);
* the slave's ``ServerState`` reports: ``milliseconds`` fitted against capture time gives where
  the slave puts position 0; ``byte_offset`` (bytes its decoder consumed, counted from its first
  byte) against ``trackData.frame_offset`` gives bytes per frame, hence the size of the served
  stream relative to the source;
* the data channel: the ``byte_offset`` of the first ``AcceptAudioData`` (the joiner's first byte,
  in served-stream units) and the pulled-bytes curve, whose gap to the consumed count is the
  slave's buffer;
* the master's own fetch of the source (its non-LAN TCP connections): how many source bytes it
  held when the joiner asked, so the served point can be placed against the master's read head.

The verdict line per join compares the slave's first PLAYING report with the capture time at which
``at_microseconds + first_offset / consumption_rate`` falls.

    analyze_late_join.py --pcap captures/<run>/<master-ip>.pcap --master 192.168.0.33 --slave 192.168.0.31 \
        --firmware firmware/APServer firmware/BoseApp firmware/ClockSync firmware/libIPC.so \
                   firmware/libSoundTouch_SDK_Protobuf.so firmware/libCore.so firmware/libCommonTypes.so \
                   firmware/libProtobufMessagingIPC.so

Exit 0 report written, 2 error.
"""

from __future__ import annotations

import io
import re
import statistics
import struct
import sys
from collections import defaultdict
from contextlib import nullcontext, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

import rich_click as click
from scapy.layers.inet import IP, TCP, UDP
from scapy.utils import rdpcap

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _click import current_context, run_cli
from analyze_capture import (
    AnalysisReport,
    Options,
    build_pool,
    capture_options,
    decode_envelope,
    emit,
    emit_error,
    frames_of,
    parse_options,
    reassemble,
)
from capture_model import (
    AudioData,
    Capture,
    CumulativePoint,
    Direction,
    Endpoint,
    MsgTypeName,
    Segment,
    ServerState,
    SetUrl,
    SlaveState,
    TransportAction,
    TransportControl,
)

COMMAND = "analyze_late_join"

EXIT_CONTRACT = "Exit 0 report written, 2 error."

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from google.protobuf import descriptor_pool
    from google.protobuf.message import Message
    from scapy.packet import Packet

__all__ = ["clock_offset", "collect", "fit_position_zero", "main", "source_intake"]

_CLOCK_PACKET_BYTES = 56  # the BOSE901 sync packet; anything shorter is not one
_MIN_REPORTS_FOR_A_FIT = 2  # two points make the line a position fit needs
_MIN_STREAM_BYTES = 20_000  # below this a reassembled direction is chatter, not audio
_REPORTS_BEFORE_STEADY = 3  # the buffer is still filling across the first few reports

TRANSPORT_PORT = 40002
DATA_PORT = 40003
CLOCK_PORT = 40005
CLOCK_MAGIC = 0x0B05E901
TLS_RECORD_OVERHEAD = 5 + 16 + 1  # header, AES-GCM tag, TLS 1.3 inner content type
_TLS_RECORD_BYTES = 1400  # records are split near the MTU, so this many bytes cost one overhead


@dataclass(frozen=True, slots=True, order=True)
class StreamKey:
    """One stream of one join. A slave that leaves and comes back joins again with the same url_id."""

    join: int
    url_id: int


@dataclass(frozen=True, slots=True)
class PlayingReport:
    """One ``ServerState`` report while playing, on the capture clock."""

    at: float
    position_ms: int
    consumed_bytes: int
    frames: int


@dataclass(frozen=True, slots=True)
class PositionFit:
    """Where a slave puts position 0, and how fast its reported position advances."""

    zero_at: float
    rate_ms_per_s: float


@dataclass(frozen=True, slots=True)
class SourceFetch:
    """One inbound connection the master pulled the source over."""

    peer: str
    first_at: float
    curve: list[CumulativePoint]


@dataclass(frozen=True, slots=True)
class InboundKey:
    """An inbound connection, by the peer that opened it and the master port it landed on."""

    peer: Endpoint
    master_port: int


@dataclass(frozen=True, slots=True)
class Conversation:
    """One TCP conversation between the slave and a master port."""

    client: Endpoint
    packets: list[Packet]


@dataclass
class StreamFacts:
    """Everything the capture says about one (join, url_id), in capture seconds."""

    url_id: int
    set_url_fields: Mapping[str, object] = field(default_factory=dict[str, object])
    """The SetURL payload as protobuf reflection reports it: only the fields actually set, keyed by
    the name the schema gives them. A dynamically-keyed map rather than a record, because that is
    what ``ListFields()`` returns and the report prints it verbatim."""
    set_url_at: float | None = None
    play_at_us: int | None = None
    play_sent_at: float | None = None
    first_data_at: float | None = None
    first_data_offset: int | None = None
    first_data_len: int | None = None
    pulled: list[CumulativePoint] = field(default_factory=list[CumulativePoint])
    reports: list[PlayingReport] = field(default_factory=list[PlayingReport])


def clock_offset(cap: Capture) -> float:
    """Median of (capture time - master T3) over the clock-sync replies the master sent, in seconds."""
    packets, master, t0 = cap.packets, cap.master, cap.t0
    deltas: list[float] = []
    for p in packets:
        if IP not in p or UDP not in p or p[IP].src != master or p[UDP].sport != CLOCK_PORT:
            continue
        pl = bytes(p[UDP].payload)
        if len(pl) < _CLOCK_PACKET_BYTES:
            continue
        magic, _version, _zero, _t1, _t2, t3 = struct.unpack(">QIIQQQ", pl[:40])
        if magic != CLOCK_MAGIC or t3 == 0:
            continue
        deltas.append((float(p.time) - t0) - t3 / 1e6)
    if not deltas:
        raise ValueError("no clock-sync replies from the master in this capture")
    return statistics.median(deltas)


def _conversations(cap: Capture, port: int) -> list[Conversation]:
    """TCP conversations from the slave to one master port, oldest first."""
    packets, master, slave = cap.packets, cap.master, cap.slave
    convs: defaultdict[Endpoint, list[Packet]] = defaultdict(list)
    for p in packets:
        if IP not in p or TCP not in p or {p[IP].src, p[IP].dst} != {master, slave}:
            continue
        if p[IP].dst == master and p[TCP].dport == port:
            convs[Endpoint(p[IP].src, p[TCP].sport)].append(p)
        elif p[IP].src == master and p[TCP].sport == port:
            convs[Endpoint(p[IP].dst, p[TCP].dport)].append(p)
    ordered = sorted(convs.items(), key=lambda kv: float(kv[1][0].time))
    return [Conversation(client, pk) for client, pk in ordered]


def _set_fields(msg: Message) -> Mapping[str, object]:
    """The fields the message actually carries, by name; ``url`` is dropped as it is logged elsewhere."""
    return {f.name: v for f, v in msg.ListFields() if f.name != "url"}


def _frames_in(track_data: str) -> int:
    m = re.search(r'frame_offset="(\d+)"', track_data)
    return int(m.group(1)) if m else 0


def collect(  # noqa: PLR0912 - one pass over one capture; splitting it would re-walk the packets
    pool: descriptor_pool.DescriptorPool, cap: Capture
) -> dict[StreamKey, StreamFacts]:
    """Walk the transport and data channels; file every relevant frame under (join, url_id).

    A slave that leaves and comes back opens new connections, and its second join of a running
    stream is exactly the late-join case, so joins are numbered in transport-connection order.
    """
    master, t0 = cap.master, cap.t0
    facts: dict[StreamKey, StreamFacts] = {}

    def of(join: int, url_id: int) -> StreamFacts:
        return facts.setdefault(StreamKey(join, url_id), StreamFacts(url_id))

    transport = _conversations(cap, TRANSPORT_PORT)
    join_started = [float(conv.packets[0].time) - t0 for conv in transport]
    for join, conv in enumerate(transport, start=1):
        streams = reassemble(conv.packets, conv.client, Endpoint(master, TRANSPORT_PORT))
        for frame in frames_of(streams.of(Direction.MASTER_TO_SLAVE)):
            payload = decode_envelope(pool, frame.body).payload
            if payload is None:
                continue
            name = payload.DESCRIPTOR.name
            if name == MsgTypeName.SET_URL:
                set_url = cast("SetUrl", payload)
                s = of(join, set_url.url_id)
                s.set_url_fields, s.set_url_at = _set_fields(payload), frame.at - t0
            elif name == MsgTypeName.TRANSPORT_CONTROL:
                control = cast("TransportControl", payload)
                if control.control != TransportAction.PLAY:
                    continue
                s = of(join, control.url_id)
                s.play_at_us, s.play_sent_at = control.at_microseconds, frame.at - t0
        for frame in frames_of(streams.of(Direction.SLAVE_TO_MASTER)):
            payload = decode_envelope(pool, frame.body).payload
            if payload is None or payload.DESCRIPTOR.name != MsgTypeName.SERVER_STATE:
                continue
            report_msg = cast("ServerState", payload)
            if report_msg.state == SlaveState.PLAYING:
                of(join, report_msg.url_id).reports.append(
                    PlayingReport(
                        frame.at - t0,
                        report_msg.milliseconds,
                        report_msg.byte_offset,
                        _frames_in(report_msg.trackData),
                    )
                )

    for conv in _conversations(cap, DATA_PORT):
        opened = float(conv.packets[0].time) - t0
        join = max((i + 1 for i, ts in enumerate(join_started) if ts <= opened + 0.5), default=1)
        streams = reassemble(conv.packets, conv.client, Endpoint(master, DATA_PORT))
        for frame in frames_of(streams.of(Direction.MASTER_TO_SLAVE)):
            payload = decode_envelope(pool, frame.body).payload
            if payload is None or payload.DESCRIPTOR.name != MsgTypeName.ACCEPT_AUDIO_DATA:
                continue
            chunk = cast("AudioData", payload)
            s = of(join, chunk.stream_id)
            if s.first_data_at is None:
                s.first_data_at = frame.at - t0
                s.first_data_offset, s.first_data_len = chunk.byte_offset, len(chunk.data)
            served = (s.pulled[-1].total if s.pulled else 0) + len(chunk.data)
            s.pulled.append(CumulativePoint(frame.at - t0, served))
    return facts


def fit_position_zero(reports: Sequence[PlayingReport]) -> PositionFit:
    """Least-squares fit of the reported position against capture time."""
    if len(reports) < _MIN_REPORTS_FOR_A_FIT:
        raise ValueError("need at least two PLAYING reports to fit")
    ts = [r.at for r in reports]
    ms = [r.position_ms for r in reports]
    t_mean, ms_mean = statistics.fmean(ts), statistics.fmean(ms)
    var = sum((t - t_mean) ** 2 for t in ts)
    slope = sum((t - t_mean) * (m - ms_mean) for t, m in zip(ts, ms, strict=True)) / var
    return PositionFit(t_mean - ms_mean / slope, slope)


def consumption_rate(reports: Sequence[PlayingReport]) -> float:
    """Bytes the slave's decoder consumes per second of reported position (served-stream units)."""
    first, last = reports[0], reports[-1]
    return (last.consumed_bytes - first.consumed_bytes) / ((last.position_ms - first.position_ms) / 1000.0)


def bytes_per_frame(reports: Sequence[PlayingReport]) -> float | None:
    """Served bytes per decoded frame, from the last report's byte and frame counters."""
    last = reports[-1]
    return last.consumed_bytes / last.frames if last.frames else None


def source_intake(cap: Capture) -> list[SourceFetch]:
    """The master's inbound source fetches, one per connection, oldest first.

    Any TCP connection from outside the LAN or a VPN counts; TLS record framing is
    subtracted so the curve approximates source bytes.
    """
    packets, master, t0 = cap.packets, cap.master, cap.t0
    inbound: dict[InboundKey, dict[int, Segment]] = defaultdict(dict)
    for p in packets:
        if IP not in p or TCP not in p or p[IP].dst != master:
            continue
        if p[IP].src.startswith(("192.168.", "10.", "100.")):
            continue
        payload = bytes(p[TCP].payload)
        if payload:
            key = InboundKey(Endpoint(p[IP].src, p[TCP].sport), p[TCP].dport)
            inbound[key].setdefault(p[TCP].seq, Segment(float(p.time) - t0, payload))
    out: list[SourceFetch] = []
    for key, segs in inbound.items():
        ordered = [segs[k] for k in sorted(segs)]
        if sum(len(seg.payload) for seg in ordered) < _MIN_STREAM_BYTES:
            continue
        curve: list[CumulativePoint] = []
        cum = 0
        for seg in ordered:
            length = len(seg.payload)
            cum += length - TLS_RECORD_OVERHEAD * max(1, length // _TLS_RECORD_BYTES)
            curve.append(CumulativePoint(seg.at, cum))
        out.append(SourceFetch(f"{key.peer.host}:{key.peer.port}", curve[0].at, curve))
    return sorted(out, key=lambda f: f.first_at)


def _at(curve: Sequence[CumulativePoint], t: float) -> int:
    return max((point.total for point in curve if point.at <= t), default=0)


def report(facts: dict[StreamKey, StreamFacts], offset: float, intake: Sequence[SourceFetch]) -> None:
    print(f"master clock -> capture time: {offset:+.6f} s (median over clock-sync replies)")
    print(
        "master source fetches: "
        + "; ".join(f"{f.peer} from +{f.first_at:.2f}s ({f.curve[-1].total // 1000} kB)" for f in intake)
    )
    # A stream's fetch is the connection the master opened just before its first data chunk.
    for key in sorted(facts):
        _report_stream(key, facts[key], offset, intake)


def _report_stream(key: StreamKey, s: StreamFacts, offset: float, intake: Sequence[SourceFetch]) -> None:
    """One (join, url_id): what the master sent, and where the slave put itself as a result."""
    print(f"\n## join {key.join}, url_id {key.url_id}")
    if s.set_url_at is not None:
        print(f"  SetURL at +{s.set_url_at:.3f}s: {s.set_url_fields}")
    if s.play_at_us is None or s.first_data_at is None or s.play_sent_at is None or s.first_data_offset is None:
        print("  no PLAY or no data seen")
        return
    play_t = s.play_at_us / 1e6 + offset
    print(
        f"  PLAY sent at +{s.play_sent_at:.3f}s with at_microseconds={s.play_at_us} = capture +{play_t:.3f}s "
        f"({play_t - s.play_sent_at:+.3f}s from sending)"
    )
    print(
        f"  first AcceptAudioData at +{s.first_data_at:.3f}s: byte_offset={s.first_data_offset} len={s.first_data_len}"
    )
    if len(s.reports) < _MIN_REPORTS_FOR_A_FIT:
        print(f"  {len(s.reports)} PLAYING report(s), no fit")
        return
    fit = fit_position_zero(s.reports)
    rate = consumption_rate(s.reports)
    if not rate:
        print("  too few reports to name a consumption rate")
        return
    bpf = bytes_per_frame(s.reports)
    first = s.reports[0]
    print(
        f"  slave position 0 at +{fit.zero_at:.3f}s ({fit.zero_at - play_t:+.3f}s from at_microseconds; "
        f"{fit.rate_ms_per_s:.3f} ms/s over {len(s.reports)} reports)"
    )
    print(f"  slave consumes {rate:.0f} B/s of served stream" + (f", {bpf:.1f} B per decoded frame" if bpf else ""))
    pulled_at_start = _at(s.pulled, first.at)
    print(
        f"  first PLAYING report at +{first.at:.3f}s: position {first.position_ms} ms, "
        f"{first.consumed_bytes} B consumed, "
        f"{pulled_at_start} B pulled (buffer {(pulled_at_start - first.consumed_bytes) // 1000} kB)"
    )
    steady = [_at(s.pulled, r.at) - r.consumed_bytes for r in s.reports[_REPORTS_BEFORE_STEADY:]]
    if steady:
        print(f"  buffer while playing: {min(steady) // 1000}..{max(steady) // 1000} kB")
    scheduled = play_t + s.first_data_offset / rate
    print(
        f"  first byte scheduled by at_microseconds + offset / rate: +{scheduled:.3f}s; "
        f"first PLAYING report {first.at - scheduled:+.3f}s after that"
    )
    fetch = [f for f in intake if f.first_at <= s.first_data_at]
    if fetch and s.first_data_offset:
        head = _at(fetch[-1].curve, s.first_data_at)
        render = rate * (s.first_data_at - play_t)
        print(
            f"  at the joiner's first chunk the master held ~{head // 1000} kB of source; "
            f"the served point is {(s.first_data_offset - render) / rate:+.2f}s ahead of the zone's position "
            f"(convert the head with bytes-per-frame over the source frame size before comparing)"
        )


def analyse(options: Options) -> AnalysisReport:
    """Print the late-join document and describe what was read (S6, S7)."""
    pool = build_pool(options.firmware)
    packets = rdpcap(str(options.pcap))
    t0 = float(packets[0].time)
    print(f"# {options.pcap.name}: {len(packets)} packets; times are seconds since the first packet")
    capture = Capture(packets=packets, master=options.master, slave=options.slave, t0=t0)
    offset = clock_offset(capture)
    report(
        collect(pool, capture),
        offset,
        source_intake(capture),
    )
    return AnalysisReport(
        pcap=str(options.pcap), packets=len(packets), master=options.master, slave=options.slave, report=""
    )


@click.command(context_settings={"help_option_names": ["-h", "--help"]}, epilog=EXIT_CONTRACT)
@capture_options
def cli(  # noqa: PLR0913 - a click callback's signature IS the option list; the only shorter form is an untyped **kwargs
    *, pcap: Path, master: str, slave: str, firmware: tuple[Path, ...], as_json: bool, as_json_bare: bool
) -> None:
    """Place a joining slave's first byte, clock and buffer on the capture timeline (S6, S7).

    Without --json the document goes to stdout byte for byte as it always has; golden_check.py
    holds it to the recorded analysis-late-join.md, so the redirect below is deliberately not
    installed in that mode.
    """
    machine = as_json or as_json_bare
    indent: int | None = None if as_json_bare else 2
    ctx = current_context()
    buf = io.StringIO()
    try:
        options = parse_options(pcap, master, slave, firmware)
        with redirect_stdout(buf) if machine else nullcontext():
            report_meta = analyse(options)
    except Exception as exc:  # noqa: BLE001 - CLI edge: an envelope, not a traceback
        emit_error(exc, command=COMMAND, machine=machine, indent=indent)
        ctx.exit(2)
    emit(report_meta.model_copy(update={"report": buf.getvalue()}), command=COMMAND, machine=machine, indent=indent)
    ctx.exit(0)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; ``lib_cli_exit_tools`` maps an unexpected exception onto an exit code."""
    return run_cli(cli, argv=argv, prog_name=COMMAND)


if __name__ == "__main__":
    raise SystemExit(main())
