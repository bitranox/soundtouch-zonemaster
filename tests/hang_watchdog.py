"""Dump the state INSIDE a hang, then kill the run. Armed by itself in CI; locally, ask for it.

    env PYTHONPATH=tests WATCHDOG_STALL_S=40 WATCHDOG_OUT=/tmp/hang.txt \
        .venv/bin/python -m pytest -p hang_watchdog -q tests/test_service_loopback.py

With ``CI=true`` (GitHub sets it) ``conftest.py`` arms it through :func:`arm_in_ci`, with a stall
budget of :data:`CI_STALL_S` and the dump on stderr, so a hang there ends the job in minutes with
its evidence in the log. ``WATCHDOG_OUT=-`` sends a local dump to stderr as well.

Exit 3 means it fired. Written for the hunt that closed OPEN-WORK rank 17, where the suite stopped
for ever on one to three of every four runs and every dump taken AFTERWARDS saw a tidy process: the
hang was in the finalizer of an async-generator fixture, so by the time pytest returned there was
nothing left to look at. This runs on its own thread, marks every phase pytest enters, and when one
outlives its budget it writes all thread stacks, every asyncio task with its stack, every server
with its live client count, every transport with its peer and protocol, the sockets the process
holds, and any httpx client still alive with who is holding it.

Two details are the whole value, and both were learned by getting them wrong first. The tasks are
enumerated ON the loop through ``call_soon_threadsafe``: it is the safe way to read them from
another thread, and it also answers a question no stack can - whether the loop is still turning at
all. And every section is guarded and flushed on its own, because the first version died mid-dump
on a dead weakproxy in ``gc.get_objects()``, wrote nothing after the thread stacks, and left the
run hanging with no report at all.

Self-test: ``tests/test_hang_watchdog.py`` runs a stalled test in a subprocess with ``CI=true`` and
``WATCHDOG_STALL_S=2``, and requires exit 3 and all six sections on stderr. An instrument that has
stopped dumping looks exactly like a suite that has stopped hanging.
"""

from __future__ import annotations

import asyncio
import faulthandler
import gc
import io
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import types
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

TO_STDERR = "-"
"""The ``WATCHDOG_OUT`` value that sends the dump to the real stderr instead of a file."""

CI_STALL_S = 300.0
"""How long one phase may run in CI before it counts as a hang.

Far above any phase this suite has (the longest waits are ten-second ``eventually`` deadlines) and
far below the six hours a CI job otherwise burns on a hang it never reports."""

_state = {"what": "session", "since": time.monotonic()}
_lock = threading.Lock()
_settings: dict[str, object] = {}


def _mark(what: str) -> None:
    with _lock:
        _state["what"] = what
        _state["since"] = time.monotonic()


def _phase() -> tuple[str, float]:
    with _lock:
        return str(_state["what"]), time.monotonic() - float(_state["since"])


def _section(out: io.TextIOBase, title: str, body: Callable[[io.TextIOBase], None]) -> None:
    print(f"\n== {title} ==", file=out)
    try:
        body(out)
    except BaseException:  # noqa: BLE001 - a dying instrument must still report
        print("SECTION FAILED:\n" + traceback.format_exc(), file=out)
    out.flush()


def _dump_threads(out: io.TextIOBase) -> None:
    faulthandler.dump_traceback(file=out, all_threads=True)


def _loops() -> list[asyncio.AbstractEventLoop]:
    """Every loop object alive. isinstance() on a dead weakproxy RAISES, so each one is guarded."""
    found: list[asyncio.AbstractEventLoop] = []
    for obj in gc.get_objects():
        try:
            if isinstance(obj, asyncio.AbstractEventLoop):
                found.append(obj)
        except ReferenceError:
            continue
    return found


_FRAME_ATTRS = (("cr_frame", "cr_await"), ("ag_frame", "ag_await"), ("gi_frame", "gi_yieldfrom"))


def _frame_and_next(obj: object) -> tuple[object, object] | None:
    """The frame a coroutine, async generator or generator is suspended in, and what it awaits."""
    for frame_attr, next_attr in _FRAME_ATTRS:
        if hasattr(obj, frame_attr):
            return getattr(obj, frame_attr), getattr(obj, next_attr, None)
    return None


def _unwrap(awaited: object) -> object | None:
    """The coroutine or generator behind an awaitable that has no frame of its own.

    ``anext(agen, default)`` and ``agen.__anext__()`` hand back builtin wrappers with no frame and
    no attribute naming the generator; the garbage collector's referents are the only way through,
    and ``anext`` is two wrappers deep (``anext_awaitable`` holds an ``async_generator_asend``,
    which holds the generator), so this searches a few levels rather than one.
    """
    level = [awaited]
    for _ in range(3):
        following: list[object] = []
        for obj in level:
            for ref in gc.get_referents(obj):
                if _frame_and_next(ref) is not None:
                    return ref
                following.append(ref)
        level = following[:64]
    return None


def _await_chain(task: asyncio.Task[object]) -> list[str]:
    """Every frame from the task's coroutine down to what it is really waiting on.

    ``Task.print_stack`` shows ONE frame for a suspended coroutine, which named ``_fetch_once`` and
    nothing inside it when the CI hang of 2026-09-28 needed the line the stop was lost on.
    """
    lines: list[str] = []
    obj: object | None = task.get_coro()
    for _ in range(64):
        if obj is None:
            break
        found = _frame_and_next(obj)
        if found is None:
            inner = _unwrap(obj)
            if inner is None:
                lines.append(f"    awaiting {obj!r}")
                break
            lines.append(f"    via {type(obj).__qualname__}")
            obj = inner
            continue
        frame, obj = found
        if not isinstance(frame, types.FrameType):
            lines.append("    (finished frame)")
            break
        code = frame.f_code
        lines.append(f'    File "{code.co_filename}", line {frame.f_lineno}, in {code.co_name}')
    return lines


def _tasks_on_loop(loop: asyncio.AbstractEventLoop, out: io.TextIOBase) -> None:
    """Ask the loop itself for its tasks: safe from this thread, and it proves the loop turns."""
    done = threading.Event()
    text: list[str] = []

    def collect() -> None:
        try:
            for task in asyncio.all_tasks(loop):
                buf = io.StringIO()
                task.print_stack(file=buf)
                chain = "\n".join(_await_chain(task))
                text.append(f"--- {task!r}\n{buf.getvalue()}  await chain:\n{chain}\n")
        except BaseException:  # noqa: BLE001
            text.append(traceback.format_exc())
        finally:
            done.set()

    loop.call_soon_threadsafe(collect)
    if not done.wait(10.0):
        print("  the loop did NOT run a callback within 10 s: it is BLOCKED, not waiting", file=out)
        return
    print(f"  {len(text)} pending task(s)", file=out)
    for chunk in text:
        print(chunk, file=out)


def _dump_loops(out: io.TextIOBase) -> None:
    loops = _loops()
    print(f"{len(loops)} event loop object(s)", file=out)
    for loop in loops:
        running, closed = loop.is_running(), loop.is_closed()
        print(f"\nloop {id(loop):#x} running={running} closed={closed}", file=out)
        if running and not closed:
            _tasks_on_loop(loop, out)


def _class_name(obj: object) -> str:
    """``module.QualName`` of anything, taken through a signature that keeps the type known."""
    cls = type(obj)
    return f"{cls.__module__}.{cls.__qualname__}"


def _dump_servers(out: io.TextIOBase) -> None:
    for obj in gc.get_objects():
        try:
            name = _class_name(obj)
        except ReferenceError:
            continue
        if name == "asyncio.base_events.Server":
            sockets = getattr(obj, "_sockets", None)
            addrs = [s.getsockname() for s in sockets] if sockets else None
            print(
                f"asyncio Server {id(obj):#x} addrs={addrs} clients={len(getattr(obj, '_clients', None) or ())} "
                f"waiters={len(getattr(obj, '_waiters', None) or [])} serving={obj.is_serving()}",
                file=out,
            )
        elif name.startswith("websockets.") and name.endswith(".Server"):
            conns = getattr(obj, "connections", None)
            print(f"websockets Server {id(obj):#x} ({name}) connections={len(conns or ())}", file=out)


def _dump_transports(out: io.TextIOBase) -> None:
    """Every live socket transport with its peer, its protocol, and who holds that protocol.

    A connection nobody is awaiting has no task to show up in the task dump, so this is the only
    place it is visible at all.
    """
    for obj in gc.get_objects():
        try:
            if not isinstance(obj, asyncio.Transport):
                continue
        except ReferenceError:
            continue
        try:
            peer = obj.get_extra_info("peername")
            local = obj.get_extra_info("sockname")
            closing = obj.is_closing()
        except Exception as exc:  # noqa: BLE001
            print(f"transport {id(obj):#x}: unreadable ({exc})", file=out)
            continue
        proto = getattr(obj, "_protocol", None)
        kind = _class_name(proto) if proto is not None else "none"
        print(f"transport {id(obj):#x} local={local} peer={peer} closing={closing} protocol={kind}", file=out)
        if proto is not None:
            holders = [_class_name(r) for r in gc.get_referrers(proto)]
            print(f"    protocol held by: {holders[:8]}", file=out)


def _dump_http_clients(out: io.TextIOBase) -> None:
    """Any httpx client still alive, and who is keeping it from being closed and collected."""
    import httpx

    for obj in gc.get_objects():
        try:
            if not isinstance(obj, httpx.AsyncClient):
                continue
        except ReferenceError:
            continue
        holders = [_class_name(r) for r in gc.get_referrers(obj)]
        print(f"httpx.AsyncClient {id(obj):#x} is_closed={obj.is_closed} held by {holders[:8]}", file=out)


def _dump_sockets(out: io.TextIOBase) -> None:
    where = shutil.which("ss")
    if where is None:
        print("(ss is not on PATH)", file=out)
        return
    got = subprocess.run(  # noqa: S603 - argv list, the program resolved by which and one literal flag
        [where, "-tanpH"], capture_output=True, text=True, timeout=30, check=False
    )
    mine = [line.strip() for line in got.stdout.splitlines() if f"pid={os.getpid()}," in line]
    print("\n".join(mine) or "(no socket of this pid)", file=out)


@runtime_checkable
class _SuspendsCapture(Protocol):
    """The one method of pytest's capture manager this uses; ``getplugin`` hands back an untyped plugin."""

    def suspend_global_capture(self, *, in_: bool = False) -> None: ...


def _real_stderr(config: pytest.Config) -> io.TextIOBase:
    """The process's own stderr, even while pytest is capturing it.

    Mid-test, fd 2 points at pytest's capture file, which ``os._exit`` discards: a dump written
    there never reaches a CI log. Suspending the global capture puts fd 2 back; nothing resumes it,
    because the run is about to be killed.
    """
    capman = config.pluginmanager.getplugin("capturemanager")
    if isinstance(capman, _SuspendsCapture):
        capman.suspend_global_capture(in_=False)
    return io.TextIOWrapper(io.FileIO(os.dup(2), "w"), encoding="utf-8", errors="replace", write_through=True)


def _open_out(config: pytest.Config, where: str) -> io.TextIOBase:
    return _real_stderr(config) if where == TO_STDERR else Path(where).open("a", encoding="utf-8")


def _dump(config: pytest.Config, where: str, reason: str) -> None:
    what, age = _phase()
    with _open_out(config, where) as out:
        try:
            print(f"\n{'=' * 78}\nWATCHDOG {reason}: phase {what!r} stalled {age:.1f}s (pid {os.getpid()})", file=out)
            _section(out, "thread stacks", _dump_threads)
            _section(out, "loops and tasks", _dump_loops)
            _section(out, "servers", _dump_servers)
            _section(out, "transports", _dump_transports)
            _section(out, "httpx clients", _dump_http_clients)
            _section(out, "sockets", _dump_sockets)
        except BaseException:  # noqa: BLE001
            print("DUMP FAILED:\n" + traceback.format_exc(), file=out)
        print(f"\nWATCHDOG: {what} stalled {age:.0f}s, killing run", file=out)


def _watch(config: pytest.Config, stall_s: float, where: str) -> None:
    while True:
        time.sleep(1.0)
        what, age = _phase()
        if age > stall_s and what != "session":
            try:
                _dump(config, where, "stall")
            finally:
                os._exit(3)


def arm_in_ci(config: pytest.Config) -> None:
    """Register this plugin when ``CI=true`` and nobody asked for it with ``-p`` already.

    CI's command line is owned by the CI template, so this is the seam. There it defaults to
    :data:`CI_STALL_S` and to stderr, since a file on the runner dies with the runner; the two
    environment variables still win when set.
    """
    if os.environ.get("CI") != "true" or config.pluginmanager.has_plugin("hang_watchdog"):
        return
    _settings.update(stall_s=CI_STALL_S, out=TO_STDERR)
    config.pluginmanager.register(sys.modules[__name__], "hang_watchdog")


def pytest_configure(config: pytest.Config) -> None:
    stall_s = float(os.environ.get("WATCHDOG_STALL_S", str(_settings.get("stall_s", 60.0))))
    where = os.environ.get("WATCHDOG_OUT", str(_settings.get("out", "/tmp/watchdog.txt")))
    threading.Thread(target=_watch, args=(config, stall_s, where), daemon=True, name="watchdog").start()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item: pytest.Item):
    _mark(f"setup {item.nodeid}")
    yield
    _mark("between")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item):
    _mark(f"call {item.nodeid}")
    yield
    _mark("between")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None):
    _mark(f"teardown {item.nodeid}")
    yield
    _mark("between")
