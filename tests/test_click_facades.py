"""The three copies of the typed rich-click facade, held to being copies.

``src/soundtouch_zonemaster/adapters/cli/typed_click.py``, ``research/_click.py`` and ``tools/_click.py`` are deliberate
duplicates: the research scripts and the two shipped tools have to run where this package is not
installed, so the declaration lives once per runnable area rather than once in the package. That
is a documented decision, and this file is what makes it safe to keep.

Two things went unnoticed without it. The three are only copies for as long as nobody edits one,
and nothing said so. And the top-level module name ``_click`` is shared by two of them, so under
the whole suite whichever test inserts its directory first decides which file the OTHER one's tool
gets: measured, ``research/_click.py`` wins and ``tools/_click.py`` is never imported at all,
which is why it sat at 0 percent while its directory was inside the coverage gate. Loading each by
PATH here, under its own module name, both covers them and takes the import order out of it.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
FACADES = {
    "soundtouch_zonemaster": ROOT / "src" / "soundtouch_zonemaster" / "adapters" / "cli" / "typed_click.py",
    "research": ROOT / "research" / "_click.py",
    "tools": ROOT / "tools" / "_click.py",
}

# What every runnable area imports from its own copy. `src/soundtouch_zonemaster/__main__.py` and the research
# analysers take all three; a tool that takes fewer still gets the same file.
RE_EXPORTED = ("argument", "current_context", "option")
"""The casts: each copy hands back rich_click's own object under a typed name."""
EXPECTED_NAMES = (*RE_EXPORTED, "MACHINE_FLAGS", "run_cli")
"""The casts plus the one function every copy defines, the usage-error-aware ``run_cli``."""


def _load(area: str, path: Path) -> ModuleType:
    """Import one copy under a name of its own, so the shared ``_click`` cannot decide which."""
    spec = importlib.util.spec_from_file_location(f"_click_{area}", path)
    assert spec is not None and spec.loader is not None, f"{path} is not importable"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _code_without_the_docstring(path: Path) -> str:
    """The file with its module docstring removed, which is the only part meant to differ.

    Each copy's docstring names its siblings and says why that copy exists, so comparing whole
    files would only ever report the difference that is intended.
    """
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    first = tree.body[0]
    assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant), f"{path} opens with no docstring"
    assert first.end_lineno is not None
    return "\n".join(text.splitlines()[first.end_lineno :]).strip()


@pytest.mark.parametrize("area", sorted(FACADES))
def test_each_copy_declares_the_whole_surface_its_area_imports(area: str) -> None:
    """Loaded by path, so this reaches the copy that the suite's import order hides."""
    module = _load(area, FACADES[area])
    for name in EXPECTED_NAMES:
        assert hasattr(module, name), f"{FACADES[area]} is missing {name}"
    assert sorted(module.__all__) == sorted(EXPECTED_NAMES), f"{FACADES[area]}: __all__ disagrees with what it defines"


def test_the_three_copies_are_still_copies() -> None:
    """The duplication is a decision; drift in it is not.

    A change to one copy that never reaches the other two is exactly what nothing else here would
    notice - the suite imports one of them and the other two ship to a machine with no tests.
    """
    bodies = {area: _code_without_the_docstring(path) for area, path in FACADES.items()}
    reference = bodies["soundtouch_zonemaster"]
    for area, body in bodies.items():
        assert body == reference, f"{FACADES[area]} has drifted from {FACADES['soundtouch_zonemaster']}"


def test_every_copy_hands_back_the_same_object_the_package_does() -> None:
    """The casts are runtime-free by construction: each copy re-exports rich_click's own object.

    Compared against ``adapters/cli/typed_click.py`` rather than against ``rich_click`` directly, because
    reading ``rich_click.option`` here would reintroduce in a test the exact partially-typed access
    these facades exist to contain - the check would need the suppression the design removed. The
    package's copy is inside the type-checked tree, so identity with it says the same thing.

    A copy that grew a wrapper instead of a cast would still import and still type-check, and every
    command built on it would quietly lose its options.
    """
    from soundtouch_zonemaster.adapters.cli import typed_click as packaged

    for area, path in FACADES.items():
        module = _load(area, path)
        for name in RE_EXPORTED:
            assert getattr(module, name) is getattr(packaged, name), f"{path}: {name} is not the object it re-exports"
