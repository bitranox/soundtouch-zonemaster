"""Cut every test off from this machine's configuration, and from a shared coverage database.

The first half matters more than it looks. The service reads its settings through
``lib_layered_config``, so a test that asserts "this setting is missing" is really asserting
something about the machine it runs on: a developer with
``~/.config/soundtouch-zonemaster/config.toml`` would see a different answer from CI, and the
failure would read as a defect in whatever was just changed. So every test gets the layers above
the defaults pointed at an empty directory of its own.

The defaults layer needs the same care for a different reason. It is ``defaultconfig.toml`` plus
every file in ``defaultconfig.d`` beside it, and a checkout keeps its private values there, in
gitignored ``9N-<scope>-rnhome.toml`` files. So the whole session reads a COPY of that tree holding
only what git tracks, through the loader's own seam (``loader.defaults_from``): a developer's
``bind_ip`` then cannot turn up in a test as if it were a default, and a local run sees exactly what
CI sees.

The second half is the template's, and it is about where this tree LIVES: coverage.py keeps its
trace data in a SQLite database, SQLite wants POSIX locking, and this checkout sits on a network
share or a filesystem without POSIX locking. The database goes to a local temp directory instead,
and a journal left behind by a crashed run is removed rather than left to make the next one report
"database is locked".

What is deliberately NOT here: a ``sys.path`` block, because ``pythonpath = ["src"]`` in
``pyproject.toml`` does that, and ``.env`` loading, because a checkout's ``.env`` is exactly what the
first half exists to keep out. The hardware tests read their own settings through
``tests/e2e_config.py``.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from soundtouch_zonemaster.adapters.config import loader

if TYPE_CHECKING:
    from collections.abc import Iterator

_COVERAGE_BASENAME = ".coverage.soundtouch_zonemaster"

_LAYER_ROOTS = (
    # Linux: the app and host layers hang off /etc, the user layer off $XDG_CONFIG_HOME.
    ("LIB_LAYERED_CONFIG_ETC", "etc"),
    ("XDG_CONFIG_HOME", "xdg"),
    # macOS and Windows, so the suite is as isolated on a CI cell as it is here.
    ("LIB_LAYERED_CONFIG_MAC_APP_ROOT", "mac-app"),
    ("LIB_LAYERED_CONFIG_MAC_HOME_ROOT", "mac-home"),
    ("LIB_LAYERED_CONFIG_PROGRAMDATA", "programdata"),
    ("LIB_LAYERED_CONFIG_APPDATA", "appdata"),
    ("LIB_LAYERED_CONFIG_LOCALAPPDATA", "localappdata"),
)


def _purge_stale_coverage_files(cov_path: Path) -> None:
    """Delete the SQLite sidecars a crashed run leaves behind.

    An explicit suffix list rather than a glob: a glob on the same prefix could match an unrelated
    file, while these three sidecar names are SQLite's own and stable.
    """
    for suffix in ("", "-journal", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            Path(str(cov_path) + suffix).unlink()


def pytest_configure(config: pytest.Config) -> None:
    """Point the coverage database at local disk, before pytest-cov opens one.

    ``config`` is unread: pytest matches this hook by NAME and passes what the hook spec declares,
    and what this one needs is the moment rather than the argument - it has to run before
    pytest-cov builds its ``Coverage()`` in ``pytest_sessionstart``.
    """
    if "COVERAGE_FILE" not in os.environ:
        cov_path = Path(tempfile.gettempdir()) / _COVERAGE_BASENAME
        _purge_stale_coverage_files(cov_path)
        os.environ["COVERAGE_FILE"] = str(cov_path)


def _untracked(_directory: str, names: list[str]) -> list[str]:
    """The names a checkout keeps out of git, so the copy below holds what CI's checkout holds."""
    return [name for name in names if loader.is_private_file(name)]


@pytest.fixture(scope="session", autouse=True)
def tracked_defaults_only(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Read the defaults layer from a copy of the tracked files, for the whole session.

    Yields the copied base file. Its companion directory keeps the name the library looks for, so a
    provenance path still ends in ``defaultconfig.d/<file>``.
    """
    shipped = loader.PACKAGED_DEFAULTS
    copy = tmp_path_factory.mktemp("tracked-defaults") / shipped.name
    shutil.copy2(shipped, copy)
    shutil.copytree(shipped.with_suffix(".d"), copy.with_suffix(".d"), ignore=_untracked)
    with loader.defaults_from(copy):
        yield copy


@pytest.fixture(autouse=True)
def isolated_config_layers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point every config layer at an empty tree, and clear what a previous test merged.

    Yields the root, so a test that wants a file in a layer can write one: the app layer is
    ``<root>/etc/xdg/soundtouch-zonemaster/config.toml`` and the user layer is
    ``<root>/xdg/soundtouch-zonemaster/config.toml``.

    The dotenv layer is deliberately NOT redirected. Walking up from the working directory for a
    ``.env`` is the library's default, and a ``.env`` in the checkout is a layer the hardware tests
    still read; the tests below therefore assert on named keys rather than on the whole merged
    mapping.
    """
    root = tmp_path / "config-layers"
    for variable, leaf in _LAYER_ROOTS:
        monkeypatch.setenv(variable, str(root / leaf))
    for name in [name for name in os.environ if name.startswith(loader.ENV_PREFIX)]:
        monkeypatch.delenv(name, raising=False)
    loader.clear_config_cache()
    yield root
    loader.clear_config_cache()


_THE_HOUSE_S_HOST_LAYER = """[prototype]
never_touch = [{ ip = "192.168.0.30", name = "Room5", why = "the Lifestyle console" }]
"""


@pytest.fixture
def a_house_that_protects_its_console(isolated_config_layers: Path) -> Path:
    """Put the house's own entry where the deployed host keeps it: the host layer of THIS machine.

    The wheel ships ``never_touch`` empty, so a test about the refusal has to supply the house the
    way a house does - as a file in its host layer - rather than lean on a default that no longer
    names anybody's console. Returns the file it wrote.
    """
    written = isolated_config_layers / "etc" / "soundtouch-zonemaster" / "hosts" / f"{socket.gethostname()}.toml"
    written.parent.mkdir(parents=True)
    written.write_text(_THE_HOUSE_S_HOST_LAYER, encoding="utf-8")
    loader.clear_config_cache()
    return written
