"""The hang watchdog, armed in CI: a hang there must end in minutes and leave its dump in the log.

CI's command line belongs to the CI template, so the watchdog is armed from ``conftest.py`` when
``CI=true`` (GitHub sets it), and it writes its dump to stderr, because a file on the runner dies
with the runner. Each test here runs a real pytest in a subprocess over :func:`test_hangs_on_purpose`
or :func:`test_reports_whether_it_is_armed`, which skip unless the subprocess asks for them. The
subprocess is bounded by its own timeout, never by the watchdog it is testing.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

ROOT = Path(__file__).resolve().parents[1]
SELFTEST = "WATCHDOG_SELFTEST"
SECTIONS = ("thread stacks", "loops and tasks", "servers", "transports", "httpx clients", "sockets")


@pytest.mark.skipif(os.environ.get(SELFTEST) != "hang", reason="the stalled test a subprocess runs on purpose")
def test_hangs_on_purpose() -> None:
    time.sleep(3600)


async def _deepest_wait() -> None:
    await asyncio.Event().wait()


async def _chunks() -> AsyncIterator[bytes]:
    await _deepest_wait()
    yield b""


@pytest.mark.skipif(os.environ.get(SELFTEST) != "hang_deep", reason="the stalled test a subprocess runs on purpose")
async def test_hangs_deep_inside_an_async_generator() -> None:
    """The shape of the CI hang: a task parked in ``anext()`` of a generator awaiting a coroutine."""
    await asyncio.create_task(anext(_chunks(), None), name="deep-probe")


@pytest.mark.skipif(os.environ.get(SELFTEST) != "report", reason="the probe a subprocess runs on purpose")
def test_reports_whether_it_is_armed(request: pytest.FixtureRequest) -> None:
    print(f"ARMED={request.config.pluginmanager.has_plugin('hang_watchdog')}")


def _pytest(
    test: str, *, selftest: str, ci: bool, extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key not in {"CI", "WATCHDOG_STALL_S", "WATCHDOG_OUT"}}
    env[SELFTEST] = selftest
    if ci:
        env["CI"] = "true"
    env.update(extra or {})
    return subprocess.run(  # noqa: S603 - argv list, this interpreter and pytest
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", f"tests/test_hang_watchdog.py::{test}"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )


@pytest.mark.os_linux
def test_a_hang_in_ci_is_killed_and_its_dump_reaches_stderr() -> None:
    """The dump is written while pytest is capturing fd 2 into a file that ``os._exit`` then
    discards, so a dump that merely went to stderr would never reach a CI log. Every section must
    arrive on the process's real stderr, and the run must end with the watchdog's own exit code."""
    done = _pytest("test_hangs_on_purpose", selftest="hang", ci=True, extra={"WATCHDOG_STALL_S": "2"})
    assert done.returncode == 3, f"the watchdog must end the run; stdout:\n{done.stdout}\nstderr:\n{done.stderr}"
    assert "phase 'call tests/test_hang_watchdog.py::test_hangs_on_purpose' stalled" in done.stderr
    missing = [title for title in SECTIONS if f"== {title} ==" not in done.stderr]
    assert missing == [], f"sections missing from stderr: {missing}\nstderr:\n{done.stderr}"


@pytest.mark.parametrize(("ci", "armed"), [(True, True), (False, False)], ids=["ci", "local"])
def test_the_watchdog_is_armed_in_ci_and_only_there(*, ci: bool, armed: bool) -> None:
    done = _pytest("test_reports_whether_it_is_armed", selftest="report", ci=ci, extra={"PYTEST_ADDOPTS": "-s"})
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"ARMED={armed}" in done.stdout


@pytest.mark.os_linux
def test_the_dump_follows_a_stuck_task_down_to_the_line_it_waits_on() -> None:
    """``Task.print_stack`` shows one frame for a suspended coroutine, so a dump built on it alone
    names the outermost function and never the line a stop was lost on. The await chain must go
    through ``anext()``'s frameless wrapper and the async generator to the innermost coroutine."""
    done = _pytest(
        "test_hangs_deep_inside_an_async_generator", selftest="hang_deep", ci=True, extra={"WATCHDOG_STALL_S": "2"}
    )
    assert done.returncode == 3, f"the watchdog must end the run; stdout:\n{done.stdout}\nstderr:\n{done.stderr}"
    probe = done.stderr[done.stderr.index("name='deep-probe'") :]
    chain = probe[probe.index("await chain:") :]
    for function in ("in _chunks", "in _deepest_wait"):
        assert function in chain, f"{function!r} missing from the deep-probe await chain:\n{chain[:2000]}"
