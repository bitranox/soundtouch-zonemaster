"""The name guard: no tracked file may name this house, its machines, or the developer's paths.

The repository is public and is worked on directly, so a private name written into any tracked file
is published by the next push. The names are known only to the private name list,
``tools/public_redactions-rnhome.txt``, which is gitignored: the guard reads the left-hand side of
every rule there and fails, naming the file and the literal, if any tracked file holds one in any
case. It reads the working tree, so an edit is stopped before it is committed rather than after.

Where the list is absent - CI, a fresh clone - there is nothing to compare against and the test
skips saying so. It guards the machine where the names exist, which is the machine where they can
be written down.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import export_public as ep

# Tokens that contain a listed literal and are public by decision. The private files of this
# checkout are named ``<name>-rnhome.<ext>``, and that suffix is how .gitignore, the tracked
# ``*.example`` copies and the docs refer to them. Only the hyphenated suffix is let through; the
# bare literal is still reported wherever it stands.
PUBLIC_TOKENS = ("-rnhome",)


@pytest.mark.skipif(
    not ep.REDACTIONS.is_file(),
    reason=f"{ep.REDACTIONS.name} is private and absent here (CI, a fresh clone): no names to guard against",
)
def test_no_tracked_file_holds_a_private_name() -> None:
    needles = ep.secrets()
    assert needles, f"{ep.REDACTIONS.name} holds no rule, so this guard would pass on anything"
    found = ep.tracked_offenders(ep.ROOT, needles, allow=PUBLIC_TOKENS)
    assert not found, (
        "tracked files that name private literals (from "
        + ep.REDACTIONS.name
        + "):\n"
        + "\n".join(f"  {name}: {', '.join(hits)}" for name, hits in found.items())
    )


# Built from parts so that this file holds no bare copy of the literal it tests: the guard reads
# this file too.
_NET = "rn" + "home"


def _repo_with(tmp_path: Path, files: dict[str, str]) -> Path:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed; the guard asks git which files are tracked")
    for name, body in files.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    for argv in (["init", "-q"], ["add", "-A"]):
        subprocess.run([git, *argv], cwd=tmp_path, check=True, capture_output=True)  # noqa: S603 - argv list, literal args
    return tmp_path


def test_the_private_file_suffix_passes_the_guard(tmp_path: Path) -> None:
    """The suffix is the public naming convention, in any case and in any file."""
    repo = _repo_with(
        tmp_path,
        {
            "gitignore.txt": f"*-{_NET}.*\n!*-{_NET}.*.example\n",
            "doc.md": f"copy 91-zone-{_NET}.toml.example to 91-zone-{_NET}.toml; the -{_NET.upper()} suffix\n",
        },
    )
    assert ep.tracked_offenders(repo, [_NET], allow=PUBLIC_TOKENS) == {}


def test_a_bare_network_name_fails_the_guard_beside_the_suffix(tmp_path: Path) -> None:
    """Only the hyphenated suffix is let through; the name itself is still private."""
    repo = _repo_with(
        tmp_path,
        {
            "prose.md": f"the {_NET} box\n",
            "host.md": f"speaker.local.{_NET}\n",
            "mixed.md": f"91-zone-{_NET}.toml sits beside {_NET}.lan\n",
        },
    )
    assert ep.tracked_offenders(repo, [_NET], allow=PUBLIC_TOKENS) == {
        "host.md": [_NET],
        "mixed.md": [_NET],
        "prose.md": [_NET],
    }
