"""A checkout's private values: a gitignored ``9N-<scope>-rnhome.toml`` beside the public default.

The shipped defaults are one scope per file in ``defaultconfig.d``, and the library merges every
``.toml`` there in sorted order. A machine that needs its own values - the address it serves on,
the speaker the prototype must never reach - writes them into a higher-numbered file in the SAME
directory, named ``-rnhome`` so it is gitignored and excluded from the wheel. A tracked
``.toml.example`` of the same name shows the shape, and the library never reads it, because its
suffix is not one the library loads.

Three things are pinned here. The library behaves that way: an ``.example`` is not merged and a
``-rnhome`` file is merged over the default it follows. The tracked examples stay usable: each one
parses, names only settings this program reads, and sorts after every public default once copied.
And the defaults every test reads are the tracked ones only, through the loader's own seam, so a
developer's private file cannot make a local run differ from CI.
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any, cast

from soundtouch_zonemaster.adapters.config import loader
from soundtouch_zonemaster.adapters.config.loader import (
    PACKAGED_DEFAULTS,
    default_config_path,
    defaults_from,
    get_config,
    is_private_file,
)
from soundtouch_zonemaster.adapters.config.settings_map import PROTOTYPE_SETTINGS, SETTINGS

_EXAMPLE = "*-rnhome.*.example"


def _tracked_copy(destination: Path) -> Path:
    """The shipped defaults tree as a checkout without private files has it. Returns the base file."""
    shutil.copy2(PACKAGED_DEFAULTS, destination / PACKAGED_DEFAULTS.name)
    shutil.copytree(
        PACKAGED_DEFAULTS.with_suffix(".d"),
        destination / PACKAGED_DEFAULTS.with_suffix(".d").name,
        ignore=lambda _dir, names: [name for name in names if is_private_file(name)],
    )
    return destination / PACKAGED_DEFAULTS.name


def _examples() -> list[Path]:
    """The tracked examples beside the shipped defaults."""
    return sorted(PACKAGED_DEFAULTS.with_suffix(".d").glob(_EXAMPLE))


def test_the_packaged_defaults_are_the_file_beside_the_loader() -> None:
    assert Path(loader.__file__).parent / "defaultconfig.toml" == PACKAGED_DEFAULTS


def test_every_test_reads_a_defaults_tree_that_holds_no_private_file() -> None:
    """The conftest points the loader at a copy of the tracked defaults. Without that, a private
    ``bind_ip`` on the developer's machine would arrive in every test as if it were a default."""
    in_use = default_config_path()

    assert in_use != PACKAGED_DEFAULTS, "the suite must not read the checkout's own defaults tree"
    assert in_use.name == PACKAGED_DEFAULTS.name
    names = [path.name for path in in_use.with_suffix(".d").iterdir()]
    assert "10-zone.toml" in names, "the copy must hold the tracked defaults, not be empty"
    assert not [name for name in names if is_private_file(name)]


def test_the_defaults_seam_redirects_the_loader_and_puts_it_back(tmp_path: Path) -> None:
    before = default_config_path()
    base = _tracked_copy(tmp_path)
    (base.with_suffix(".d") / "50-dialling.toml").write_text("[dialling]\nwindow_s = 1.7\n", encoding="utf-8")

    with defaults_from(base):
        assert default_config_path() == base
        assert get_config()["dialling"]["window_s"] == 1.7

    assert default_config_path() == before
    assert get_config()["dialling"]["window_s"] != 1.7, "leaving the seam must forget the merged answer"


def test_a_private_override_file_is_merged_over_the_default_it_follows(tmp_path: Path) -> None:
    base = _tracked_copy(tmp_path)
    private = base.with_suffix(".d") / "91-zone-rnhome.toml"
    private.write_text('[zone]\nbind_ip = "192.0.2.77"\n', encoding="utf-8")

    with defaults_from(base):
        config = get_config()

    assert config["zone"]["bind_ip"] == "192.0.2.77"
    assert config.origin("zone.bind_ip") == {"layer": "defaults", "path": str(private), "key": "zone.bind_ip"}


def test_an_example_file_in_the_defaults_directory_is_not_merged(tmp_path: Path) -> None:
    """The control that makes the test above mean something: the same content under the example's
    name must arrive as nothing at all."""
    base = _tracked_copy(tmp_path)
    example = base.with_suffix(".d") / "91-zone-rnhome.toml.example"
    example.write_text('[zone]\nbind_ip = "192.0.2.77"\n', encoding="utf-8")

    with defaults_from(base):
        config = get_config()

    assert "bind_ip" not in config.get("zone", default={})


def test_there_is_a_tracked_example_for_each_scope_that_carries_machine_values() -> None:
    assert [path.name for path in _examples()] == [
        "91-zone-rnhome.toml.example",
        "92-files-rnhome.toml.example",
        "98-prototype-rnhome.toml.example",
    ]


def test_every_tracked_example_parses_and_names_only_settings_this_program_reads() -> None:
    """An example that no longer matches the settings would be copied and then refused, or worse,
    silently ignored as a stray key."""
    known = {**SETTINGS, **PROTOTYPE_SETTINGS}
    for path in _examples():
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
        assert parsed, f"{path.name} must declare its section"
        for section, table in parsed.items():
            assert isinstance(table, dict), f"{path.name}: [{section}] must be a table"
            for key in cast("dict[str, Any]", table):
                assert f"{section}.{key}" in known, f"{path.name} sets {section}.{key}, which nothing reads"


def test_every_example_once_copied_sorts_after_every_public_default() -> None:
    """The library merges the directory in sorted order, so a private file that sorted earlier would
    be overwritten by the very default it exists to replace."""
    directory = PACKAGED_DEFAULTS.with_suffix(".d")
    public = [path.name for path in directory.glob("*.toml") if not is_private_file(path.name)]
    for path in _examples():
        copied = path.name.removesuffix(".example")
        assert all(copied > name for name in public), f"{copied} must sort after {max(public)}"


def test_every_example_names_the_file_to_copy_it_to() -> None:
    for path in _examples():
        assert f"Copy this file to {path.name.removesuffix('.example')}" in path.read_text(encoding="utf-8")


def test_the_private_file_rule_is_the_one_git_applies() -> None:
    """``is_private_file`` decides what the tests copy and what ``config --redact`` masks; git
    decides what is published. They are two readings of one rule, so git is asked directly and the
    predicate must agree with it on a private file, its tracked example and a public default."""
    git = shutil.which("git")
    assert git is not None
    repo = Path(__file__).resolve().parents[1]
    for name in ("91-zone-rnhome.toml", "91-zone-rnhome.toml.example", "10-zone.toml", "notes-rnhome.txt"):
        probe = f"src/soundtouch_zonemaster/adapters/config/defaultconfig.d/{name}"
        # argv list: git resolved by which, every other element a literal or a name from the tuple above
        check = subprocess.run([git, "check-ignore", "-q", "--no-index", probe], cwd=repo, check=False)  # noqa: S603
        assert is_private_file(name) is (check.returncode == 0), name
