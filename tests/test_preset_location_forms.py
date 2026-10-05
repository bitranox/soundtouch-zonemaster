"""A pressed preset reads the same whatever form its stored location takes (OPEN-WORK rank 210).

Every captured fixture in this repo carries the legacy ``/custom/v1/playback/<base64url>`` location,
while since 2026-09-27 every preset at all three sites is stored in the Orion form, absolute or
relative. The press path is meant to be independent of it - the preset NUMBER comes from the frame's
``<preset id>`` and the pairing from timing - and these drive one recorded-shape frame per form
through the real observer parser and the real pairing to hold that.
"""

from __future__ import annotations

import base64
import math
from urllib.parse import quote

import pytest

from soundtouch_zonemaster.adapters.soundtouch.observer import parse_frame
from soundtouch_zonemaster.domain.enums import SourceName
from soundtouch_zonemaster.domain.presses import AskedAtTheFrame, Presses

DEVICE = "AABBCC0000A1"
DESCRIPTOR = '{"name":"Radio","imageUrl":"","streamUrl":"http://192.0.2.1/stream"}'
ORION_DATA = quote(base64.b64encode(DESCRIPTOR.encode()).decode(), safe="")
LEGACY = "/custom/v1/playback/" + base64.urlsafe_b64encode(b'{"streamUrl":"http://192.0.2.1/stream"}').decode()
ORION_ABSOLUTE = f"http://192.0.2.10:8000/core02/svc-bmx-adapter-orion/prod/orion/station?data={ORION_DATA}"
ORION_RELATIVE = f"/station?data={ORION_DATA}"
AWAKE = AskedAtTheFrame(asleep=False, may_choose_the_channel=True)


def _selection(location: str, preset_id: int) -> str:
    return (
        f'<updates deviceID="{DEVICE}"><nowSelectionUpdated><preset id="{preset_id}">'
        f'<ContentItem source="{SourceName.LOCAL_INTERNET_RADIO}" type="stationurl" '
        f'location="{location}" sourceAccount="" isPresetable="true">'
        "<itemName>Radio</itemName></ContentItem>"
        "</preset></nowSelectionUpdated></updates>"
    )


LOCATIONS = [
    pytest.param(LEGACY, id="legacy-custom-playback"),
    pytest.param(ORION_ABSOLUTE, id="orion-absolute"),
    pytest.param(ORION_RELATIVE, id="orion-relative"),
]


@pytest.mark.parametrize("location", LOCATIONS)
def test_the_parser_reads_the_preset_number_whatever_the_location_form(location: str) -> None:
    event = parse_frame("127.0.0.1", _selection(location, 4), 100.0)

    assert (event.kind, event.device_id, event.preset_id) == ("nowSelectionUpdated", DEVICE, 4)
    assert location.replace("&", "&amp;") in event.frame, "the frame is carried as it came"


@pytest.mark.parametrize("location", LOCATIONS)
def test_a_selection_and_its_two_touches_are_one_press_whatever_the_location_form(location: str) -> None:
    """Outside the zone a box names the preset and both touches follow it (measured 2026-09-21)."""
    selection = parse_frame("127.0.0.1", _selection(location, 4), 100.0)
    touches = [parse_frame("127.0.0.1", f'<userActivityUpdate deviceID="{DEVICE}" />', at) for at in (100.03, 100.30)]
    presses = Presses()
    assert selection.preset_id is not None
    presses.selection(selection.device_id, selection.preset_id, at=selection.received_at, asked=AWAKE)
    for touch in touches:
        assert touch.kind == "userActivityUpdate"
        presses.user_activity(touch.device_id, at=touch.received_at)

    decided = presses.due(at=math.inf)

    assert [(device, press.preset_id) for device, press in decided] == [(DEVICE, 4)]
