"""The test log sink: what a test asserts on, and what a failing test shows (OPEN-WORK rank 176)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from service_log import recording_into

if TYPE_CHECKING:
    import pytest

TESTS = Path(__file__).parent


def test_a_line_is_kept_for_the_assertions_and_printed_with_the_time_it_was_said(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A test keeps asserting on the list; pytest shows the printed copy only when the test fails.

    The stamp is ``time.monotonic``, which is the clock an asyncio loop's ``time()`` reads on this
    platform, so a line can be placed against the loop time a timed-out ``eventually`` prints.
    """
    logs: list[str] = []
    before = time.monotonic()
    recording_into(logs)("zone", "Bose Room1 joined; 1 in the zone")
    after = time.monotonic()

    assert logs == ["zone: Bose Room1 joined; 1 in the zone"]
    printed = capsys.readouterr().out.splitlines()
    assert len(printed) == 1
    stamp, line = printed[0].split(" ", 1)
    assert before <= float(stamp) <= after
    assert line == "zone: Bose Room1 joined; 1 in the zone"


def test_no_test_builds_its_own_service_log_sink() -> None:
    """A sink written inline keeps its lines in a list nobody sees when the test fails, which is
    why six timeouts in ``_both_wake`` named what never happened and nothing of why."""
    inline = [
        f"{path.name}:{number}"
        for path in sorted(TESTS.glob("test_*.py"))
        if path.name != Path(__file__).name
        for number, text in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if 'logs.append(f"{kind}: {text}")' in text
    ]
    assert inline == []
