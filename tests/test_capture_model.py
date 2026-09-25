"""The vocabulary a capture is read in, pinned where a silent drift would misread a run.

``research/capture_model.py`` needs no firmware, no capture and no speaker, so unlike the rest of
``research/`` there is nothing stopping it being tested - and nothing else inside ``make test``
holds it: ``golden_check.py`` reaches it only when the gitignored inputs are present.

What is worth pinning here is not that a frozen dataclass stores its fields. It is the values that
have to match something outside this file - the reassembler's direction keys, the envelope's
``msg_type`` numbers and its clear-text ``msg_typename`` (REPORT.md S3), and the two wire states
the analysers branch on. A rename or a renumber there still parses, still passes type checking,
and produces a report that is quietly wrong.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))
from capture_model import (
    DirectedSegments,
    Direction,
    MsgKind,
    MsgTypeName,
    Segment,
    SlaveState,
    TransportAction,
)

# --- directions ----------------------------------------------------------------------------------


def test_direction_values_are_the_reassemblers_keys():
    """The reassembler indexes its streams by these two strings; renaming them loses the payloads."""
    assert Direction.SLAVE_TO_MASTER == "a2b"
    assert Direction.MASTER_TO_SLAVE == "b2a"


@pytest.mark.parametrize(
    ("direction", "expected"),
    [(Direction.SLAVE_TO_MASTER, "slave->master"), (Direction.MASTER_TO_SLAVE, "master->slave")],
)
def test_direction_label_is_what_the_report_prints(direction: Direction, expected: str):
    assert direction.label == expected


def test_directed_segments_hands_back_the_stream_running_that_way():
    """``of`` is a dispatch, and swapping its two branches would silently transpose the report."""
    up = [Segment(at=0.5, payload=b"up")]
    down = [Segment(at=0.6, payload=b"down")]
    both = DirectedSegments(slave_to_master=up, master_to_slave=down)
    assert both.of(Direction.SLAVE_TO_MASTER) is up
    assert both.of(Direction.MASTER_TO_SLAVE) is down


# --- envelope kinds ------------------------------------------------------------------------------


def test_msg_kind_numbers_are_the_wire_values():
    """``msg_type`` as the envelope carries it. A renumber reclassifies every frame in the report."""
    assert (MsgKind.EVENT, MsgKind.REQUEST, MsgKind.RESPONSE) == (1, 2, 3)


@pytest.mark.parametrize(
    ("kind", "expected"), [(MsgKind.EVENT, "EVT"), (MsgKind.REQUEST, "REQ"), (MsgKind.RESPONSE, "RSP")]
)
def test_msg_kind_label_is_the_three_letter_form(kind: MsgKind, expected: str):
    assert kind.label == expected


@pytest.mark.parametrize(("value", "expected"), [(1, "EVT"), (2, "REQ"), (3, "RSP")])
def test_label_for_names_a_kind_it_knows(value: int, expected: str):
    assert MsgKind.label_for(value) == expected


@pytest.mark.parametrize("value", [0, 4, 99, -1])
def test_label_for_falls_back_to_the_number_on_a_fourth_kind(value: int):
    """The documented contract: a capture may name a kind this enum does not, and the report says
    the number rather than raising. This is the branch a real capture reaches first."""
    assert MsgKind.label_for(value) == str(value)


# --- typenames and states ------------------------------------------------------------------------


def test_msg_typename_compares_equal_to_the_bare_literal():
    """The docstring's own claim: a decoded frame keeps its typename as ``str`` and is compared
    against these members, so the comparison must be the one the bare literal made."""
    assert MsgTypeName.SET_URL == "AudioServerMsgSetURL"
    assert MsgTypeName.ACCEPT_AUDIO_DATA == "AudioServerMsgAcceptAudioData"
    assert MsgTypeName.TRANSPORT_CONTROL == "AudioServerMsgTransportControl"
    assert MsgTypeName.SERVER_STATE == "AudioServerMsgServerState"


def test_the_two_states_the_analysers_branch_on_keep_their_wire_values():
    """PLAYING selects which reports count; PLAY is the control that carries at_microseconds."""
    assert SlaveState.PLAYING == 4
    assert TransportAction.PLAY == 1
