"""IPC framing and clock packets, proven against bytes captured from two real speakers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

import pytest

from soundtouch_zonemaster.adapters.soundtouch import clock, ipc
from soundtouch_zonemaster.adapters.soundtouch.pb import audio, audio_data
from soundtouch_zonemaster.domain.enums import MsgTypeName


class CapturedFrame(TypedDict):
    """One IPC frame as the capture recorded it."""

    typename: str
    direction: str
    msg_type: int
    msg_id: int
    sequence: int
    body_hex: str
    decoded: str


# "from" is a keyword, so this one needs the functional form.
CapturedUdp = TypedDict("CapturedUdp", {"from": str, "to": str, "hex": str})


class Capture(TypedDict):
    note: str
    frames: list[CapturedFrame]
    udp: list[CapturedUdp]


FIX: Capture = json.loads((Path(__file__).parent / "fixtures" / "zone_frames.json").read_text())


def _frame(typename: str, direction: str) -> CapturedFrame:
    return next(f for f in FIX["frames"] if f["typename"] == typename and f["direction"] == direction)


@pytest.mark.parametrize("entry", FIX["frames"], ids=lambda e: e["typename"] + "/" + e["direction"][:1])
def test_every_captured_frame_decodes_to_its_named_type(entry: CapturedFrame) -> None:
    frame = ipc.decode_frame(bytes.fromhex(entry["body_hex"]))
    assert frame.typename == entry["typename"]
    assert frame.msg_id == entry["msg_id"] == ipc.MSG_IDS[MsgTypeName(entry["typename"])]
    assert frame.sequence == entry["sequence"]
    assert frame.payload is not None, "no schema for this type"


def test_encoder_reproduces_the_captured_set_clock_master_frame_byte_for_byte() -> None:
    entry = _frame("AudioServerMsgSetClockMasterMsg", "master->slave")
    payload = audio.AudioServerMsgSetClockMasterMsg(clockMasterIp="192.168.0.33", clockMasterPort=40005)
    encoded = ipc.encode_frame(ipc.REQUEST, payload, sequence=entry["sequence"])
    assert encoded[4:] == bytes.fromhex(entry["body_hex"])
    assert int.from_bytes(encoded[:4], "big") == len(encoded) - 4


def test_encoder_reproduces_the_captured_set_url_frame_byte_for_byte() -> None:
    entry = _frame("AudioServerMsgSetURL", "master->slave")
    payload = audio.AudioServerMsgSetURL(
        url="stream://192.168.0.33:40003?no_delay&nonblocking&assuredforwarding&master=true&id=1&force_connect=true",
        passthrough=0,
        url_id=1,
        url_is_realtime=True,
        connection_timeout_in_ms=120000,
        buffering_timeout_in_ms=300000,
        source_dryup_timeout_ms=30000,
        source_retry_delay_ms=5000,
        source_retry_count=5,
        playbackstreamtype=audio.AudioServerMsgSetURL.Wifi,
    )
    assert ipc.encode_frame(ipc.REQUEST, payload, sequence=entry["sequence"])[4:] == bytes.fromhex(entry["body_hex"])


def test_encoder_reproduces_the_captured_play_frame_byte_for_byte() -> None:
    entry = _frame("AudioServerMsgTransportControl", "master->slave")
    payload = audio.AudioServerMsgTransportControl(
        control=audio.AudioServerMsgTransportControl.PLAY, at_microseconds=887326627189, url_id=1
    )
    assert ipc.encode_frame(ipc.REQUEST, payload, sequence=entry["sequence"])[4:] == bytes.fromhex(entry["body_hex"])


def test_a_mutated_payload_does_not_match_the_capture() -> None:
    # The byte-for-byte tests above could pass against a broken encoder only if the capture were
    # trivially reproducible; a one-field change must break equality.
    entry = _frame("AudioServerMsgSetClockMasterMsg", "master->slave")
    payload = audio.AudioServerMsgSetClockMasterMsg(clockMasterIp="192.168.0.33", clockMasterPort=40006)
    assert ipc.encode_frame(ipc.REQUEST, payload, sequence=entry["sequence"])[4:] != bytes.fromhex(entry["body_hex"])


def test_splitter_reassembles_frames_across_arbitrary_chunking() -> None:
    bodies = [bytes.fromhex(f["body_hex"]) for f in FIX["frames"][:4]]
    stream = b"".join(len(b).to_bytes(4, "big") + b for b in bodies)
    splitter = ipc.FrameSplitter()
    out: list[bytes] = []
    for i in range(0, len(stream), 7):  # 7-byte chunks straddle every length prefix
        out += splitter.feed(stream[i : i + 7])
    assert out == bodies


def test_data_request_and_response_round_trip() -> None:
    req = _frame("AudioServerMsgAcceptAudioDataRequest", "slave->master")
    f = ipc.decode_frame(bytes.fromhex(req["body_hex"]))
    assert isinstance(f.payload, audio_data.AudioServerMsgAcceptAudioDataRequest)
    assert (f.payload.byte_count, f.payload.min_byte_count, f.payload.stream_id) == (8192, 8192, 1)
    rsp = _frame("AudioServerMsgAcceptAudioData", "master->slave")
    g = ipc.decode_frame(bytes.fromhex(rsp["body_hex"]))
    assert isinstance(g.payload, audio_data.AudioServerMsgAcceptAudioData)
    assert g.payload.encryption_type == audio_data.AudioServerMsgAcceptAudioData.OBFUSCATED
    assert g.msg_type == ipc.RESPONSE and len(g.payload.data) == 9066


# --- clock ---------------------------------------------------------------------------------------


def test_captured_clock_request_parses_and_the_reply_layout_matches_the_speaker_reply() -> None:
    req_hex = next(u["hex"] for u in FIX["udp"] if u["to"] == "192.168.0.33")
    rep_hex = next(u["hex"] for u in FIX["udp"] if u["from"] == "192.168.0.33")
    req = clock.SyncPacket.parse(bytes.fromhex(req_hex))
    rep = clock.SyncPacket.parse(bytes.fromhex(rep_hex))
    assert req.magic == clock.CLOCK_MAGIC == 0x0B05E901 and req.version == 3
    assert req.t2 == req.t3 == 0, "a fresh request carries only T1"
    assert rep.t1 == req.t1, "the speaker echoes T1"
    assert 0 < rep.t3 - rep.t2 < 5000, "the speaker stamps T3 within a few ms of T2"
    assert rep.pack() == bytes.fromhex(rep_hex), "pack() is the inverse of parse()"


def test_our_reply_echoes_t1_and_carries_the_previous_exchange() -> None:
    state = clock.ClientState()
    r1 = clock.build_reply(clock.SyncPacket(clock.CLOCK_MAGIC, 3, 1000, 0, 0, 0, 0), state, now=5000)
    assert (r1.t1, r1.t2, r1.t3, r1.t1_prev, r1.t3_prev_precise) == (1000, 5000, 5000, 0, 0)
    r2 = clock.build_reply(clock.SyncPacket(clock.CLOCK_MAGIC, 3, 1400, 5000, 5000, 0, 0), state, now=5400)
    assert (r2.t1, r2.t1_prev, r2.t3_prev_precise) == (1400, 1000, 5000)
    assert len(r2.pack()) == 56
