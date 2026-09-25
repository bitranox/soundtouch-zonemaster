"""The built packages ship no file that belongs to one machine.

A checkout keeps its own values in gitignored files named ``<anything>-rnhome.<ext>``, beside the
public defaults they override - which puts one INSIDE the package directory, where hatchling
collects every file it finds. Two things keep it out, and each is tested on its own:

- ``.gitignore``, which hatchling honours when it builds from a checkout. That covers the build
  somebody runs here today, and it is the first test.
- The explicit ``exclude`` in ``pyproject.toml``, which does not depend on a VCS file being present
  (an sdist unpacked somewhere, a copy of the tree). The second and third tests build from a copy
  that has NO ``.gitignore`` and a planted private file at several depths, so the exclude is the
  only thing standing between that file and the archive.

Each test also requires a tracked file to BE in the archive: an empty or failed build would
otherwise pass every "nothing private" check for the wrong reason. The tracked ``.example`` files
share the pattern and are excluded with it; they document a checkout, not an installation.
"""

from __future__ import annotations

import shutil
import tarfile
import zipfile
from pathlib import Path

from hatchling.builders.sdist import SdistBuilder
from hatchling.builders.wheel import WheelBuilder

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS_DIR = "soundtouch_zonemaster/adapters/config/defaultconfig.d"
_PLANTED = (
    f"src/{DEFAULTS_DIR}/91-zone-rnhome.toml",
    f"src/{DEFAULTS_DIR}/91-zone-rnhome.toml.example",
    "src/soundtouch_zonemaster/notes-rnhome.txt",
    "tests/e2e_defaults.d/90-e2e-rnhome.toml",
    "private-rnhome.txt",
)


def _wheel_names(project: Path, out: Path) -> list[str]:
    """Every member of the wheel hatchling builds from ``project``."""
    (artifact,) = WheelBuilder(str(project)).build(directory=str(out), versions=["standard"])
    with zipfile.ZipFile(artifact) as archive:
        return archive.namelist()


def _sdist_names(project: Path, out: Path) -> list[str]:
    """Every member of the sdist hatchling builds from ``project``."""
    (artifact,) = SdistBuilder(str(project)).build(directory=str(out), versions=["standard"])
    with tarfile.open(artifact) as archive:
        return archive.getnames()


def _copy_without_vcs(destination: Path) -> Path:
    """The files a build reads, with no ``.gitignore`` beside them and a private file planted."""
    project = destination / "project"
    project.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(ROOT / name, project / name)
    shutil.copytree(ROOT / "src", project / "src", ignore=shutil.ignore_patterns("__pycache__"))
    for relative in _PLANTED:
        planted = project / relative
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text('[zone]\nbind_ip = "192.0.2.1"\n', encoding="utf-8")
    assert not (project / ".gitignore").exists()
    return project


def _private(names: list[str]) -> list[str]:
    return [name for name in names if "-rnhome" in name]


def test_the_wheel_built_from_this_checkout_ships_no_private_file(tmp_path: Path) -> None:
    names = _wheel_names(ROOT, tmp_path)

    assert f"{DEFAULTS_DIR}/10-zone.toml" in names, "the tracked defaults must ship"
    assert _private(names) == []


def test_the_wheel_excludes_a_private_file_even_with_no_gitignore_to_hide_it(tmp_path: Path) -> None:
    project = _copy_without_vcs(tmp_path)

    names = _wheel_names(project, tmp_path / "out")

    assert f"{DEFAULTS_DIR}/10-zone.toml" in names, "the tracked defaults must ship"
    assert _private(names) == []


def test_the_sdist_excludes_a_private_file_even_with_no_gitignore_to_hide_it(tmp_path: Path) -> None:
    project = _copy_without_vcs(tmp_path)

    names = _sdist_names(project, tmp_path / "out")

    assert any(name.endswith(f"{DEFAULTS_DIR}/10-zone.toml") for name in names), "the tracked defaults must ship"
    assert _private(names) == []
