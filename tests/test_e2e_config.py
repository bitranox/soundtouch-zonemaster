"""Where the hardware tests find the speakers: tracked safe defaults, and a private override beside them.

``tests/e2e_defaults.toml`` is tracked and says "no hardware": no host, no speakers, nothing
audible. A machine that can reach the speakers writes its values into a gitignored
``tests/e2e_defaults.d/90-e2e-rnhome.toml``, TYPED - the speakers a real list, ``audible`` a real
bool. A ``.env`` stays readable as a higher layer, where every value arrives as text.

These drive :func:`load_e2e_settings` with a defaults tree and a ``.env`` of their own, so what the
developer's machine holds cannot decide them.
"""

from __future__ import annotations

import shutil
import tomllib
from typing import TYPE_CHECKING

from e2e_config import DEFAULTS, load_e2e_settings

if TYPE_CHECKING:
    from pathlib import Path

_PRIVATE_FILE = """e2e_host = "192.0.2.5"
e2e_user = "someone"
e2e_key = "/nonexistent/key"
e2e_speakers = ["192.0.2.11", "192.0.2.12"]
e2e_audible = true
"""


def _defaults_tree(tmp_path: Path, *, private: str | None = None, example: str | None = None) -> Path:
    """A copy of the tracked defaults file, with an optional private file and example beside it."""
    base = tmp_path / DEFAULTS.name
    shutil.copy2(DEFAULTS, base)
    directory = base.with_suffix(".d")
    directory.mkdir()
    if private is not None:
        (directory / "90-e2e-rnhome.toml").write_text(private, encoding="utf-8")
    if example is not None:
        (directory / "90-e2e-rnhome.toml.example").write_text(example, encoding="utf-8")
    return base


def test_the_tracked_defaults_reach_no_hardware(tmp_path: Path) -> None:
    settings = load_e2e_settings(defaults=_defaults_tree(tmp_path), dotenv=tmp_path / "absent.env")

    assert not settings.reachable
    assert settings.speakers == ()
    assert settings.audible is False
    assert settings.user == "root"


def test_a_private_file_beside_the_defaults_is_read_with_its_types(tmp_path: Path) -> None:
    base = _defaults_tree(tmp_path, private=_PRIVATE_FILE)

    settings = load_e2e_settings(defaults=base, dotenv=tmp_path / "absent.env")

    assert settings.host == "192.0.2.5"
    assert settings.user == "someone"
    assert settings.key == "/nonexistent/key"
    assert settings.speakers == ("192.0.2.11", "192.0.2.12")
    assert settings.audible is True
    assert settings.reachable


def test_an_example_beside_the_defaults_is_not_read(tmp_path: Path) -> None:
    """The control for the test above: the same content under the example's name is nothing."""
    base = _defaults_tree(tmp_path, example=_PRIVATE_FILE)

    settings = load_e2e_settings(defaults=base, dotenv=tmp_path / "absent.env")

    assert not settings.reachable
    assert settings.audible is False


def test_a_dotenv_still_overrides_the_private_file_and_arrives_as_text(tmp_path: Path) -> None:
    base = _defaults_tree(tmp_path, private=_PRIVATE_FILE)
    dotenv = tmp_path / ".env"
    dotenv.write_text("E2E_SPEAKERS=192.0.2.21, 192.0.2.22\nE2E_AUDIBLE=false\n", encoding="utf-8")

    settings = load_e2e_settings(defaults=base, dotenv=dotenv)

    assert settings.speakers == ("192.0.2.21", "192.0.2.22")
    assert settings.audible is False
    assert settings.host == "192.0.2.5", "a key the .env does not name still comes from the file"


def test_the_tracked_example_names_every_setting_with_its_type() -> None:
    example = DEFAULTS.with_suffix(".d") / "90-e2e-rnhome.toml.example"
    parsed = tomllib.loads(example.read_text(encoding="utf-8"))
    defaults = tomllib.loads(DEFAULTS.read_text(encoding="utf-8"))

    assert set(parsed) == set(defaults)
    assert isinstance(parsed["e2e_speakers"], list)
    assert parsed["e2e_audible"] is False, "the example must stay silent if copied unedited"
    assert "Copy this file to 90-e2e-rnhome.toml" in example.read_text(encoding="utf-8")
