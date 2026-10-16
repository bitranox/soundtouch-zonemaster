"""The source distribution carries the package, what its tests need, and nothing else.

A checkout holds files that must never be published: session records, review notes and local tool
state, hidden only through ``.git/info/exclude`` or a nested ``.gitignore``, neither of which
hatchling reads. The sdist is therefore built from an allowlist, and this test builds one from a
copy of the tree that also holds such decoys. The private ``*-rnhome.*`` files are a separate
guard with tests of their own (``test_wheel_contents.py``).
"""

from __future__ import annotations

import shutil
import tarfile
from pathlib import Path

import pytest
from hatchling.builders.sdist import SdistBuilder

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "soundtouch_zonemaster"

# hatchling force-includes the root .gitignore into every sdist (so a build from the unpacked
# sdist applies the same exclusions); no include/exclude setting removes it.
ROOT_FILES = ("pyproject.toml", "README.md", "LICENSE", "CHANGELOG.md", "keymappings.md", ".env.example", ".gitignore")
ROOT_DIRS = ("src", "tests", "research", "tools", "docs", "examples", "notebooks")
ALLOWED_TOP_LEVEL = frozenset({"PKG-INFO", *ROOT_FILES, *ROOT_DIRS})
REQUIRED_TOP_LEVEL = frozenset({"PKG-INFO", "pyproject.toml", "src", "tests", "research", "tools"})

# What a checkout carries beside the tracked tree: a session record, a decisions log, a hidden
# tool-state directory, a plan, a rotated handover. None of these names is in the root .gitignore.
DECOY_FILES = (
    "session-notes.md",
    "decisions.jsonl",
    ".agent/state.json",
    "PLAN-something.md",
    "handover.prev.md",
)

# The gitignored bulk under the copied directories (firmware, captures, measurements, plans) is
# kept out of the copy only to keep it cheap; the copied root .gitignore would exclude it anyway.
_SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", "firmware", "captures", "measurements", "plans")


def _copy_tree_with_decoys(destination: Path) -> Path:
    """Copy the buildable tree and plant the files a checkout carries but must not ship."""
    root = destination / "tree"
    root.mkdir()
    for name in ROOT_FILES:
        shutil.copy2(PROJECT_ROOT / name, root / name)
    for name in ROOT_DIRS:
        shutil.copytree(PROJECT_ROOT / name, root / name, ignore=_SKIP)
    for decoy in DECOY_FILES:
        path = root / decoy
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("decoy\n", encoding="utf-8")
    return root


def _strip_root(names: list[str]) -> list[str]:
    """Drop the ``<name>-<version>/`` prefix every member carries."""
    return [name.split("/", 1)[1] for name in names if "/" in name]


@pytest.fixture(scope="module")
def sdist_members(tmp_path_factory: pytest.TempPathFactory) -> list[str]:
    tree = _copy_tree_with_decoys(tmp_path_factory.mktemp("packaging"))
    built = list(SdistBuilder(str(tree)).build(directory=str(tmp_path_factory.mktemp("sdist")), versions=["standard"]))
    assert len(built) == 1, f"expected one sdist, got {built}"
    with tarfile.open(built[0], "r:gz") as archive:
        members = _strip_root(archive.getnames())
    assert members, "the sdist is empty, so nothing below would be judged"
    return members


@pytest.mark.os_agnostic
def test_the_sdist_holds_only_allowlisted_top_level_entries(sdist_members: list[str]) -> None:
    top_level = {member.split("/", 1)[0] for member in sdist_members}

    unexpected = sorted(top_level - ALLOWED_TOP_LEVEL)
    missing = sorted(REQUIRED_TOP_LEVEL - top_level)

    assert not unexpected, f"the sdist ships files outside the allowlist: {unexpected}"
    assert not missing, f"the sdist lacks required entries: {missing}"


@pytest.mark.os_agnostic
def test_the_sdist_ships_only_the_package_under_src(sdist_members: list[str]) -> None:
    under_src = {member.split("/")[1] for member in sdist_members if member.startswith("src/")}

    assert under_src == {PACKAGE}


@pytest.mark.os_agnostic
def test_the_sdist_carries_what_the_tests_import(sdist_members: list[str]) -> None:
    needed = ("tests/conftest.py", "research/capture_zone.py", "tools/export_public.py")

    assert [name for name in needed if name not in sdist_members] == []


@pytest.mark.os_agnostic
def test_the_sdist_carries_no_compiled_bytecode(sdist_members: list[str]) -> None:
    compiled = [member for member in sdist_members if "__pycache__" in member.split("/") or member.endswith(".pyc")]

    assert not compiled, f"the sdist ships bytecode: {compiled[:5]}"
