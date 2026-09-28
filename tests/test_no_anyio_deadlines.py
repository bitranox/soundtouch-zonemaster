"""No httpx client in the package carries a deadline of its own.

An anyio deadline that fires on the same turn of the loop as a stop swallows the stop
(``adapters/http_client.py`` says how, ``tests/test_stop_meets_a_deadline.py`` reproduces it). The
fix holds only while every client comes from ``client_without_deadline`` and every request goes
without a ``timeout=``, and a client built by hand, with httpx's default of five seconds, would
bring the hang back without a single test noticing. This is the test that notices.

The detector reads source rather than importing it, so it is proven on planted cases first: a
checker that finds nothing in the package means something only if it finds the planted ones.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "soundtouch_zonemaster"
FACTORY = PACKAGE / "adapters" / "http_client.py"

_CLIENTS = frozenset({"AsyncClient", "Client"})
_REQUESTS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "request", "stream", "send", "build_request"}
)


def _called_name(call: ast.Call) -> str | None:
    """``httpx.AsyncClient(...)`` and a bare ``AsyncClient(...)`` both name ``AsyncClient``."""
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _is_httpx(call: ast.Call) -> bool:
    func = call.func
    return isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "httpx"


def _gives_no_timeout(call: ast.Call) -> bool:
    timeout = next((kw.value for kw in call.keywords if kw.arg == "timeout"), None)
    return isinstance(timeout, ast.Constant) and timeout.value is None


def anyio_deadlines(source: str, *, is_factory: bool = False) -> list[str]:
    """Every place in ``source`` that would hand httpx a deadline, as ``line: what``."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = _called_name(node)
        if name == "Timeout" and _is_httpx(node):
            found.append(f"{node.lineno}: httpx.Timeout")
        elif name in _CLIENTS and not is_factory:
            found.append(f"{node.lineno}: a client built by hand")
        elif name in _CLIENTS and not _gives_no_timeout(node):
            found.append(f"{node.lineno}: the factory's client has a timeout")
        elif name in _REQUESTS and any(kw.arg == "timeout" for kw in node.keywords):
            found.append(f"{node.lineno}: a request with a timeout")
    return found


def test_the_detector_finds_every_way_a_deadline_gets_in() -> None:
    planted = {
        "httpx.AsyncClient()": "a client built by hand",
        "AsyncClient(timeout=5)": "a client built by hand",
        "httpx.Client(timeout=None)": "a client built by hand",
        "httpx.Timeout(read=30.0)": "httpx.Timeout",
        "client.get(url, timeout=3)": "a request with a timeout",
        "client.stream('GET', url, timeout=None)": "a request with a timeout",
    }
    for source, what in planted.items():
        assert anyio_deadlines(source) == [f"1: {what}"], source
    assert anyio_deadlines("httpx.AsyncClient()", is_factory=True) == ["1: the factory's client has a timeout"]


def test_the_detector_passes_what_the_fix_writes() -> None:
    clean = (
        "async with asyncio.timeout(8.0), client_without_deadline() as c:\n"
        "    r = await c.get(url)\n"
        "await ring.wait_for(0, 1, timeout=30.0)\n"
        "done, _ = await asyncio.wait(tasks, timeout=2.0)\n"
    )
    assert anyio_deadlines(clean) == []
    assert anyio_deadlines("httpx.AsyncClient(timeout=None, headers=h)", is_factory=True) == []


def test_no_module_in_the_package_hands_httpx_a_deadline() -> None:
    modules = sorted(PACKAGE.rglob("*.py"))
    found = {
        str(path.relative_to(PACKAGE)): hits
        for path in modules
        if (hits := anyio_deadlines(path.read_text(encoding="utf-8"), is_factory=path == FACTORY))
    }
    assert not found, f"httpx would own a deadline here, and could swallow a stop: {found}"
    # Not vacuous: the package does open clients, all of them through the factory.
    users = [path for path in modules if path != FACTORY and "client_without_deadline(" in path.read_text("utf-8")]
    assert len(users) >= 3, f"expected the source, speaker and registry modules to use the factory: {users}"
