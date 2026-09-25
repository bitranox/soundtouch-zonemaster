"""Dump the state INSIDE a hang, then kill the run. Not loaded by default; ask for it.

    env PYTHONPATH=tests WATCHDOG_STALL_S=40 WATCHDOG_OUT=/tmp/hang.txt \
        .venv/bin/python -m pytest -p hang_watchdog -q tests/test_service_loopback.py

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

Self-test: run it over any suite with ``WATCHDOG_STALL_S=2`` and require exit 3 and all six
sections in the output file. An instrument that has stopped dumping looks exactly like a suite that
has stopped hanging.
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
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

STALL_S = float(os.environ.get("WATCHDOG_STALL_S", "60"))
OUT = os.environ.get("WATCHDOG_OUT", "/tmp/watchdog.txt")

_state = {"what": "session", "since": time.monotonic()}
_lock = threading.Lock()


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


def _tasks_on_loop(loop: asyncio.AbstractEventLoop, out: io.TextIOBase) -> None:
    """Ask the loop itself for its tasks: safe from this thread, and it proves the loop turns."""
    done = threading.Event()
    text: list[str] = []

    def collect() -> None:
        try:
            for task in asyncio.all_tasks(loop):
                buf = io.StringIO()
                task.print_stack(file=buf)
                text.append(f"--- {task!r}\n{buf.getvalue()}")
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


def _dump(reason: str) -> None:
    what, age = _phase()
    try:
        with Path(OUT).open("a", encoding="utf-8") as out:
            print(f"\n{'=' * 78}\nWATCHDOG {reason}: phase {what!r} stalled {age:.1f}s (pid {os.getpid()})", file=out)
            _section(out, "thread stacks", _dump_threads)
            _section(out, "loops and tasks", _dump_loops)
            _section(out, "servers", _dump_servers)
            _section(out, "transports", _dump_transports)
            _section(out, "httpx clients", _dump_http_clients)
            _section(out, "sockets", _dump_sockets)
    except BaseException:  # noqa: BLE001
        with Path(OUT).open("a", encoding="utf-8") as out:
            print("DUMP FAILED:\n" + traceback.format_exc(), file=out)


def _watch() -> None:
    while True:
        time.sleep(1.0)
        what, age = _phase()
        if age > STALL_S and what != "session":
            try:
                _dump("stall")
            finally:
                sys.stderr.write(f"\nWATCHDOG: {what} stalled {age:.0f}s, dump in {OUT}, killing run\n")
                sys.stderr.flush()
                os._exit(3)


def pytest_configure(config: pytest.Config) -> None:
    threading.Thread(target=_watch, daemon=True, name="watchdog").start()


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
