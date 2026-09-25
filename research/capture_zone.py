#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["rich-click>=1.9.4", "lib_cli_exit_tools>=2.3.4", "lib_layered_config>=5.6.2"]
# ///
"""Drive a two-speaker SoundTouch zone while recording everything both speakers say and do.

Runs ON the capture host: a machine on the speakers' LAN, which the dev host is not.

This file was stdlib-only until 2026-09-06, so that it could be copied to the capture host and
run with no install. That constraint was lifted deliberately (the CLIs are driven by an LLM and
so all speak JSON, which meant rich-click here too), and the PEP 723 header above is what
replaces it: ``uv run capture_zone.py`` resolves the dependencies on the host. Beside this file
the capture host needs ``_click.py``, ``_settings.py`` and the house's settings - the
``defaultconfig.toml`` tree with its ``-rnhome`` file, or the same values in the host's own config
layer - plus ``uv`` and a route to an index the first time. NONE of that is verified from the dev
host, and it fails at capture time, with the speakers on - so check it before you rely on a
session. Without the settings a run refuses no speaker, and says so on stderr before it starts.

The boundary record below stays a frozen dataclass rather than a pydantic model: it validates
what it needs to and there is no reason to churn code that is signed off. Needs Python 3.11 for
``StrEnum``.

For each step it appends timestamped records to <out>/events.jsonl (HTTP answers, WebSocket
frames, netstat snapshots) and keeps a tcpdump running on BOTH speakers, streamed over ssh into
<out>/<box>.pcap, so nothing is written on the speakers' tiny flash.

    uv run capture_zone.py --master 192.168.0.35 --slave 192.168.0.33 --out /tmp/zone-capture

Refuses any speaker named in the ``[capture] never_touch`` setting (research/_settings.py): a box
belongs there when a POWER over its API disturbs it beyond this experiment, as it flips the input
of a Lifestyle console.
Refuses to start while either speaker is not in STANDBY (someone may be listening) unless --force.
Exit 0 finished, 1 refused, 2 error. Audible: yes, at --volume (default 12) for about 3 minutes.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import rich_click as click

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _click import current_context, option, run_cli
from _settings import CaptureSettings, capture_settings, load

COMMAND = "capture_zone"

if TYPE_CHECKING:
    from collections.abc import Sequence

RUN_SCOPE = "run"  # the pseudo-box a record about the run itself is filed under

# RFC 6455 frame fields, named so the reader sees the WebSocket spec rather than bit masks.
_WS_MIN_HEADER_BYTES = 2
_WS_LEN_16BIT = 126  # the length field escapes to the next two bytes
_WS_LEN_64BIT = 127  # ... or to the next eight
_WS_HEADER_WITH_16BIT_LEN = 4
_WS_HEADER_WITH_64BIT_LEN = 10
_WS_OPCODE_TEXT = 0x1
_WS_OPCODE_CLOSE = 0x8
_WS_OPCODE_PING = 0x9
SSH_BOX = [
    "ssh",
    "-o",
    "HostKeyAlgorithms=+ssh-rsa",
    "-o",
    "PubkeyAcceptedAlgorithms=+ssh-rsa",
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "LogLevel=ERROR",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=8",
]


class EventKind(StrEnum):
    """The kinds of record the journal holds. Every line of events.jsonl is one of these."""

    HTTP = "http"
    NETSTAT = "netstat"
    WS = "ws"
    WS_OPEN = "ws-open"
    WS_CLOSE = "ws-close"
    WS_ERROR = "ws-error"
    INFO = "info"
    STEP = "step"
    TCPDUMP = "tcpdump"
    CLEANUP_WARN = "cleanup-warn"
    FINAL = "final"


class Step(StrEnum):
    """The experiment's phases, in order. The names are what a later reader greps events.jsonl for."""

    PRESET1_BOTH = "1-preset1-both"
    SET_ZONE = "1b-setZone"
    E1_PRESET2_ON_SLAVE = "2-E1-preset2-on-slave"
    RESTORE_ZONE = "3-restore-zone"
    E2_SELECT_ON_MASTER = "3b-E2-select-on-master"
    SLAVE_POWER_OFF = "4-slave-power-off"
    SLAVE_BACK = "4b-slave-back"
    E6_MASTER_POWER_OFF = "5-E6-master-power-off"
    CLEANUP = "9-cleanup"


class KeyName(StrEnum):
    """The keys this experiment presses. POWER on the master takes the whole zone down (E6)."""

    PRESET_1 = "PRESET_1"
    PRESET_2 = "PRESET_2"
    POWER = "POWER"


class KeyState(StrEnum):
    """A key press is two requests; the speaker acts on the release."""

    PRESS = "press"
    RELEASE = "release"


class SpeakerPath(StrEnum):
    """The speaker-API paths this driver calls."""

    INFO = "/info"
    KEY = "/key"
    NOW_PLAYING = "/nowPlaying"
    GET_ZONE = "/getZone"
    SET_ZONE = "/setZone"
    PRESETS = "/presets"
    SELECT = "/select"
    VOLUME = "/volume"


class SourceName(StrEnum):
    """The ``source`` a nowPlaying document reports. A speaker names many more; only these are tested."""

    STANDBY = "STANDBY"


UNKNOWN_SOURCE = "?"  # what source_of reports when nowPlaying carries no source attribute
SPEAKER_PORT = 8090  # the speaker API; the WebSocket notification channel is 8080


@dataclass(frozen=True, slots=True)
class ZoneMember:
    """One member of a zone: the speaker's address and the device id the master addresses it by."""

    ip: str
    device_id: str


class Speakers(NamedTuple):
    """The two speakers of a run, master first.

    A NamedTuple rather than a bare pair: every one of the eighteen sites in this file either
    iterates this or tests membership in it, so the iteration protocol has to stay - but the two
    fields are both ``str`` and a type checker could never catch them being swapped while they
    were anonymous. ``tests/test_capture_zone.py`` adds five more, and those read ``master`` and
    ``slave`` by name, which is what the change was for.
    """

    master: str
    slave: str


@dataclass(frozen=True, slots=True)
class Options:
    """One validated run of the capture driver.

    A frozen dataclass rather than a pydantic model: it was written that way when this file was
    stdlib-only, and there is no reason to churn a record that is signed off and does exactly what
    it needs to. A mistyped address would otherwise be found by a connection timeout partway into
    an audible three-minute experiment.
    """

    master: str
    slave: str
    out: Path
    volume: int
    force: bool

    def __post_init__(self) -> None:
        for label, value in (("master", self.master), ("slave", self.slave)):
            try:
                ipaddress.ip_address(value)
            except ValueError as exc:
                raise ValueError(f"--{label} is not an IP address: {value}") from exc
        if not 0 <= self.volume <= 100:  # noqa: PLR2004 - the speaker's own volume scale
            raise ValueError(f"--volume out of range: {self.volume}")

    @property
    def speakers(self) -> Speakers:
        """Both speakers, master first; every step that touches them walks this pair."""
        return Speakers(master=self.master, slave=self.slave)


class Journal:
    """Append-only JSONL with one clock for every kind of record."""

    def __init__(self, path: Path) -> None:
        self._f = path.open("a", encoding="utf-8")
        self._lock = threading.Lock()

    def add(self, kind: EventKind, box: str, **detail: object) -> None:
        rec = {"t": time.time(), "kind": kind, "box": box, **detail}
        with self._lock:
            self._f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._f.flush()
        short = {k: (v[:160] if isinstance(v, str) else v) for k, v in detail.items()}
        print(f"{time.strftime('%H:%M:%S')} {kind:<12} {box:<15} {short}", flush=True)


# --- the documents a speaker exchanges ----------------------------------------------------------
#
# Building and reading these is where a typo costs a whole audible run, so they are separated from
# the transport below and tested directly.


def key_body(name: KeyName, state: KeyState) -> str:
    """One half of a key press. The speaker acts on the release, so both are sent, in this order."""
    return f'<key state="{state}" sender="Gabbo">{name}</key>'


def zone_document(master_id: str, members: Sequence[ZoneMember]) -> str:
    """The /setZone body. An empty member list is how a zone is dissolved."""
    return (
        f'<zone master="{master_id}">'
        + "".join(f'<member ipaddress="{m.ip}">{m.device_id}</member>' for m in members)
        + "</zone>"
    )


def device_id_in(info_xml: str) -> str:
    """The deviceID an /info document reports; a master addresses its members by this."""
    m = re.search(r'deviceID="([0-9A-F]+)"', info_xml)
    if not m:
        raise RuntimeError(f"no deviceID in {SpeakerPath.INFO}")
    return m.group(1)


def volume_in(volume_xml: str) -> int:
    """The actual volume, or -1 when the document does not carry one."""
    m = re.search(r"<actualvolume>(\d+)</actualvolume>", volume_xml)
    return int(m.group(1)) if m else -1


def preset_item_in(presets_xml: str, number: int) -> str:
    """The raw <ContentItem ...>...</ContentItem> of one preset, as the speaker stores it."""
    m = re.search(rf'<preset id="{number}"[^>]*>(<ContentItem.*?</ContentItem>)</preset>', presets_xml, re.DOTALL)
    if not m:
        raise RuntimeError(f"preset {number} not found")
    return m.group(1)


def source_of(now_playing_xml: str) -> str:
    """The ``source`` attribute, compared against :class:`SourceName`; a speaker names many values."""
    m = re.search(r'<nowPlaying[^>]*source="([A-Z_]+)"', now_playing_xml)
    return m.group(1) if m else UNKNOWN_SOURCE


# --- speaker HTTP -------------------------------------------------------------------------------


def http(ip: str, path: SpeakerPath, body: str | None = None, timeout: float = 6.0) -> str:
    data = body.encode() if body is not None else None
    req = urllib.request.Request(f"http://{ip}:{SPEAKER_PORT}{path}", data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/xml")
    # The scheme is the literal http:// built two lines above; only the host varies. urllib rather
    # than httpx because this file ships to the capture host, and every dependency it does not
    # take is one that cannot fail to resolve there.
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return r.read().decode(errors="replace")


def device_id(ip: str) -> str:
    try:
        return device_id_in(http(ip, SpeakerPath.INFO))
    except RuntimeError as exc:
        raise RuntimeError(f"{ip}: {exc}") from exc


def key(ip: str, name: KeyName, j: Journal) -> None:
    for state in KeyState:
        out = http(ip, SpeakerPath.KEY, key_body(name, state))
        j.add(EventKind.HTTP, ip, req=f"{SpeakerPath.KEY} {name} {state}", resp=out)


def now_playing(ip: str, j: Journal) -> str:
    out = http(ip, SpeakerPath.NOW_PLAYING)
    j.add(EventKind.HTTP, ip, req=SpeakerPath.NOW_PLAYING, resp=out)
    return out


def get_zone(ip: str, j: Journal) -> str:
    out = http(ip, SpeakerPath.GET_ZONE)
    j.add(EventKind.HTTP, ip, req=SpeakerPath.GET_ZONE, resp=out)
    return out


def set_zone(master_ip: str, master_id: str, members: Sequence[ZoneMember], j: Journal) -> str:
    body = zone_document(master_id, members)
    out = http(master_ip, SpeakerPath.SET_ZONE, body)
    j.add(EventKind.HTTP, master_ip, req=SpeakerPath.SET_ZONE, body=body, resp=out)
    return out


def preset_item(ip: str, number: int, j: Journal) -> str:
    """Return the raw <ContentItem ...>...</ContentItem> of preset <number> as the speaker stores it."""
    out = http(ip, SpeakerPath.PRESETS)
    j.add(EventKind.HTTP, ip, req=SpeakerPath.PRESETS, resp=out)
    try:
        return preset_item_in(out, number)
    except RuntimeError as exc:
        raise RuntimeError(f"{ip}: {exc}") from exc


def select(ip: str, content_item: str, j: Journal) -> None:
    out = http(ip, SpeakerPath.SELECT, content_item)
    j.add(EventKind.HTTP, ip, req=SpeakerPath.SELECT, body=content_item, resp=out)


def get_volume(ip: str, j: Journal) -> int:
    out = http(ip, SpeakerPath.VOLUME)
    j.add(EventKind.HTTP, ip, req=SpeakerPath.VOLUME, resp=out)
    return volume_in(out)


def set_volume(ip: str, level: int, j: Journal) -> None:
    out = http(ip, SpeakerPath.VOLUME, f"<volume>{level}</volume>")
    j.add(EventKind.HTTP, ip, req=f"{SpeakerPath.VOLUME} {level}", resp=out)


# --- speaker shell ------------------------------------------------------------------------------


def ssh_argv(user: str, ip: str, command: str) -> list[str]:
    """The ssh command line that runs ``command`` on a speaker as ``user``."""
    return [*SSH_BOX, f"{user}@{ip}", command]


def box_sh(ip: str, command: str, *, user: str, timeout: float = 20.0) -> str:
    p = subprocess.run(
        ssh_argv(user, ip, command),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return p.stdout + (f"\n[stderr] {p.stderr.strip()}" if p.stderr.strip() else "")


def snapshot(ip: str, label: str, j: Journal, *, user: str) -> None:
    out = box_sh(
        ip,
        "netstat -tunap 2>/dev/null | grep -v 127.0.0.1 | grep -v ':22 '; echo ---; "
        "ps | grep -i 'clocksync\\|APServer' | grep -v grep",
        user=user,
    )
    j.add(EventKind.NETSTAT, ip, label=label, out=out)


class Tcpdump:
    """tcpdump on a speaker, streamed over ssh into a local pcap; never touches the speaker's flash."""

    def __init__(self, ip: str, out: Path, *, user: str) -> None:
        self.ip, self.out = ip, out
        self.fh = out.open("wb")
        # -i any: a WLAN speaker carries its traffic on wlan0, a wired one on eth0.
        cmd = "tcpdump -i any -s 0 -U -w - 'not port 22'"
        self.p = subprocess.Popen(ssh_argv(user, ip, cmd), stdout=self.fh, stderr=subprocess.PIPE)

    def stop(self) -> str:
        self.p.terminate()
        try:
            self.p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.p.kill()
        self.fh.close()
        err = self.p.stderr.read().decode(errors="replace") if self.p.stderr else ""
        return f"{self.out.name}: {self.out.stat().st_size} bytes; {err.strip()[-200:]}"


# --- WebSocket listener (gabbo), stdlib ---------------------------------------------------------


class WsListener(threading.Thread):
    def __init__(self, ip: str, j: Journal) -> None:
        super().__init__(daemon=True)
        self.ip, self.j, self.stop_flag = ip, j, threading.Event()

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:  # noqa: BLE001 - a probe thread reports, never crashes the run
            self.j.add(EventKind.WS_ERROR, self.ip, error=repr(exc))

    def _run(self) -> None:
        s = socket.create_connection((self.ip, 8080), timeout=5)
        k = base64.b64encode(os.urandom(16)).decode()
        s.sendall(
            (
                f"GET / HTTP/1.1\r\nHost: {self.ip}:8080\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {k}\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: gabbo\r\n\r\n"
            ).encode()
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += s.recv(4096)
        head, _, buf = buf.partition(b"\r\n\r\n")
        self.j.add(EventKind.WS_OPEN, self.ip, status=head.decode(errors="replace").splitlines()[0])
        s.settimeout(1.0)
        while not self.stop_flag.is_set():
            try:
                chunk = s.recv(65536)
            except TimeoutError:
                continue
            if not chunk:
                self.j.add(EventKind.WS_CLOSE, self.ip)
                return
            buf += chunk
            buf = self._drain(buf, s)
        s.close()

    def _drain(self, buf: bytes, s: socket.socket) -> bytes:
        while len(buf) >= _WS_MIN_HEADER_BYTES:
            fin_op, b1 = buf[0], buf[1]
            opcode, masked, length, pos = fin_op & 0x0F, b1 & 0x80, b1 & 0x7F, 2
            if length == _WS_LEN_16BIT:
                if len(buf) < _WS_HEADER_WITH_16BIT_LEN:
                    return buf
                length, pos = int.from_bytes(buf[2:4], "big"), 4
            elif length == _WS_LEN_64BIT:
                if len(buf) < _WS_HEADER_WITH_64BIT_LEN:
                    return buf
                length, pos = int.from_bytes(buf[2:10], "big"), 10
            if masked:
                pos += 4
            if len(buf) < pos + length:
                return buf
            payload, buf = buf[pos : pos + length], buf[pos + length :]
            if opcode == _WS_OPCODE_PING:  # ping -> pong (client frames must be masked)
                s.sendall(b"\x8a\x80" + b"\x00\x00\x00\x00" + payload)
            elif opcode == _WS_OPCODE_TEXT:
                self.j.add(EventKind.WS, self.ip, frame=payload.decode(errors="replace"))
            elif opcode == _WS_OPCODE_CLOSE:
                self.j.add(EventKind.WS_CLOSE, self.ip)
        return buf


# --- the experiment -----------------------------------------------------------------------------


def wait(seconds: float, why: str) -> None:
    print(f"           ... {seconds:.0f} s: {why}", flush=True)
    time.sleep(seconds)


def refused_speaker(speakers: Speakers, never_touch: Sequence[str]) -> str | None:
    """The first speaker of the run that the house said never to touch, or None when there is none."""
    return next((ip for ip in speakers if ip in never_touch), None)


def run(options: Options, settings: CaptureSettings) -> int:  # noqa: PLR0912, PLR0915 - the experiment's steps in the order they happen
    out = options.out
    out.mkdir(parents=True, exist_ok=True)
    j = Journal(out / "events.jsonl")
    master, slave = options.master, options.slave
    refused = refused_speaker(options.speakers, settings.never_touch)
    if refused is not None:
        print(f"refused: {refused} is in [capture] never_touch; a POWER over its API disturbs it", file=sys.stderr)
        return 1
    if not settings.never_touch:
        print("note: [capture] never_touch is empty, so no speaker is refused", file=sys.stderr)
    user = settings.ssh_user

    ids = {ip: device_id(ip) for ip in options.speakers}
    j.add(
        EventKind.INFO,
        RUN_SCOPE,
        master=master,
        slave=slave,
        ids=ids,
        volume=options.volume,
        never_touch=list(settings.never_touch),
    )
    start_state = {ip: source_of(now_playing(ip, j)) for ip in options.speakers}
    if not options.force and any(src != SourceName.STANDBY for src in start_state.values()):
        print(f"refused: not all speakers in STANDBY ({start_state}); pass --force to override", file=sys.stderr)
        return 1
    volumes = {ip: get_volume(ip, j) for ip in options.speakers}
    for ip in options.speakers:
        get_zone(ip, j)
        snapshot(ip, "baseline", j, user=user)

    # Built INSIDE the try: each Tcpdump spawns an ssh on a real speaker as it is constructed,
    # so one that started before a later one raised would otherwise keep running with nothing
    # left holding a reference to stop it. The cleanup below iterates whatever got made.
    dumps: list[Tcpdump] = []
    listeners: list[WsListener] = []
    try:
        for ip in options.speakers:
            # A comprehension is what this used to be, and that was the defect: constructing a
            # Tcpdump SPAWNS a remote process, so a failure partway through must leave the ones
            # already started in the list for the cleanup below to stop. PERF401 is carved here
            # for that reason, and only here.
            dumps.append(Tcpdump(ip, out / f"{ip}.pcap", user=user))  # noqa: PERF401
        for ip in options.speakers:
            listener = WsListener(ip, j)
            listeners.append(listener)
            listener.start()
        wait(3, "tcpdump and websocket listeners settle")

        # 1. Both play preset 1, standalone; then a zone master -> slave.
        j.add(EventKind.STEP, RUN_SCOPE, name=Step.PRESET1_BOTH)
        for ip in options.speakers:
            key(ip, KeyName.PRESET_1, j)
        wait(12, "both speakers start preset 1")
        for ip in options.speakers:
            set_volume(ip, options.volume, j)
            now_playing(ip, j)
        j.add(EventKind.STEP, RUN_SCOPE, name=Step.SET_ZONE)
        set_zone(master, ids[master], [ZoneMember(slave, ids[slave])], j)
        wait(15, "zone forms, slave joins")
        for ip in options.speakers:
            get_zone(ip, j)  # E5: does the slave list itself? what does it report as master?
            now_playing(ip, j)  # E5: slave's own nowPlaying while a slave
            snapshot(ip, "in-zone", j, user=user)
        wait(20, "steady state: audio + clock traffic")

        # 2. E1: preset pressed ON THE SLAVE.
        j.add(EventKind.STEP, RUN_SCOPE, name=Step.E1_PRESET2_ON_SLAVE)
        key(slave, KeyName.PRESET_2, j)
        wait(15, "E1: what does the zone do?")
        for ip in options.speakers:
            get_zone(ip, j)
            now_playing(ip, j)

        # 3. Restore the zone if it broke, then E2: /select on the master.
        j.add(EventKind.STEP, RUN_SCOPE, name=Step.RESTORE_ZONE)
        set_zone(master, ids[master], [ZoneMember(slave, ids[slave])], j)
        wait(12, "zone re-formed")
        item2 = preset_item(master, 2, j)
        j.add(EventKind.STEP, RUN_SCOPE, name=Step.E2_SELECT_ON_MASTER)
        select(master, item2, j)
        wait(20, "E2: station change propagates")
        for ip in options.speakers:
            get_zone(ip, j)
            now_playing(ip, j)

        # 4. Slave leaves by POWER; then E6: master POWER with the slave back in the zone.
        j.add(EventKind.STEP, RUN_SCOPE, name=Step.SLAVE_POWER_OFF)
        key(slave, KeyName.POWER, j)
        wait(12, "slave leaves")
        for ip in options.speakers:
            get_zone(ip, j)
            now_playing(ip, j)
        j.add(EventKind.STEP, RUN_SCOPE, name=Step.SLAVE_BACK)
        key(slave, KeyName.PRESET_1, j)
        wait(10, "slave back on")
        set_zone(master, ids[master], [ZoneMember(slave, ids[slave])], j)
        wait(12, "zone re-formed")
        vol_before = {ip: get_volume(ip, j) for ip in options.speakers}
        j.add(EventKind.STEP, RUN_SCOPE, name=Step.E6_MASTER_POWER_OFF, volumes=vol_before)
        key(master, KeyName.POWER, j)
        wait(15, "E6: what happens to the slave?")
        for ip in options.speakers:
            get_zone(ip, j)
            now_playing(ip, j)
    finally:
        # Cleanup: dissolve whatever zone exists, everyone to standby, volumes restored.
        j.add(EventKind.STEP, RUN_SCOPE, name=Step.CLEANUP)
        for ip in options.speakers:
            try:
                set_zone(ip, ids[ip], [], j)
            except Exception as exc:  # noqa: BLE001 - cleanup must continue
                j.add(EventKind.CLEANUP_WARN, ip, error=repr(exc))
        for ip in options.speakers:
            try:
                if source_of(now_playing(ip, j)) != SourceName.STANDBY:
                    key(ip, KeyName.POWER, j)
                set_volume(ip, volumes[ip] if volumes[ip] > 0 else options.volume, j)
            except Exception as exc:  # noqa: BLE001
                j.add(EventKind.CLEANUP_WARN, ip, error=repr(exc))
        wait(5, "let the last events arrive")
        for ws in listeners:
            ws.stop_flag.set()
        for d in dumps:
            j.add(EventKind.TCPDUMP, d.ip, result=d.stop())
        for ip in options.speakers:
            try:
                j.add(EventKind.FINAL, ip, now_playing=now_playing(ip, j), zone=get_zone(ip, j))
            except Exception as exc:  # noqa: BLE001
                j.add(EventKind.CLEANUP_WARN, ip, error=repr(exc))
    return 0


DEFAULT_VOLUME = 12
"""The listening level a run uses. One source of truth for the CLI default and the record."""


def parse_options(*, master: str, slave: str, out: Path, volume: int = DEFAULT_VOLUME, force: bool = False) -> Options:
    """Validate what the CLI collected. The record is the only place a value becomes trusted."""
    return Options(master=master, slave=slave, out=out, volume=volume, force=force)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@option("--master", required=True, help="the speaker that leads the zone")
@option("--slave", required=True, help="the speaker that joins it")
@option("--out", required=True, type=click.Path(path_type=Path), help="directory for the capture")
@option("--volume", type=int, default=DEFAULT_VOLUME, show_default=True, help="listening level during the run")
@option("--force", is_flag=True, help="run even if a speaker is not in STANDBY")
@option("--json", "as_json", is_flag=True, help="print a JSON envelope instead of prose")
@option("--json-bare", "as_json_bare", is_flag=True, help="as --json, on one line for jq")
def cli(  # noqa: PLR0913 - a click callback's signature IS the option list; the only shorter form is an untyped **kwargs
    *, master: str, slave: str, out: Path, volume: int, force: bool, as_json: bool, as_json_bare: bool
) -> None:
    """Drive a real two-speaker zone and record everything it says. AUDIBLE for about 3 minutes."""
    machine = as_json or as_json_bare
    indent: int | None = None if as_json_bare else 2
    ctx = current_context()
    try:
        options = parse_options(master=master, slave=slave, out=out, volume=volume, force=force)
        rc = run(options, capture_settings(load()))
    except Exception as exc:  # noqa: BLE001 - CLI edge: an envelope, not a traceback
        if machine:
            failure = {"ok": False, "command": COMMAND, "error": type(exc).__name__, "message": str(exc)}
            print(json.dumps(failure, indent=indent))
        else:
            print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        ctx.exit(2)
    if machine:
        # Built with stdlib json rather than a model: this file ships to the capture host, and
        # every dependency it does not take is one that cannot fail to resolve there.
        skipped: list[str] = []
        envelope: dict[str, object] = {
            "ok": rc == 0,
            "command": COMMAND,
            "data": {"out": str(out), "master": master, "slave": slave, "volume": volume, "refused": rc != 0},
            "skipped": skipped,
        }
        print(json.dumps(envelope, indent=indent))
    ctx.exit(rc)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; ``lib_cli_exit_tools`` maps an unexpected exception onto an exit code."""
    return run_cli(cli, argv=argv, prog_name=COMMAND)


if __name__ == "__main__":
    raise SystemExit(main())
