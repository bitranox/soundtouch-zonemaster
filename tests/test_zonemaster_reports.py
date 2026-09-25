"""What a slave's ServerState carries beyond state/ms/byte: the trackData XML with the decoder's
frame count (REPORT.md S6). Parsed from the exact string a speaker sends."""

from __future__ import annotations

from soundtouch_zonemaster.adapters.soundtouch.reports import TrackData, parse_track_data

CAPTURED = (
    '<?xml version="1.0" encoding="UTF-8" ?><AudioServerMsgTrackData frame_offset="380" '
    'byte_offset="134181" time_offset="32568" latency_microsecs="0" />'
)


def test_the_captured_track_data_parses_to_its_four_numbers() -> None:
    assert parse_track_data(CAPTURED) == TrackData(
        frame_offset=380, byte_offset=134181, time_offset=32568, latency_microsecs=0
    )


def test_an_empty_or_malformed_track_data_is_none_not_an_exception() -> None:
    assert parse_track_data("") is None
    assert parse_track_data('<AudioServerMsgTrackData frame_offset="x" />') is None
    assert parse_track_data('<Other frame_offset="3" />') is None
