"""Where the settings are read from, and the merged answer with its provenance kept.

The settings are read through ``lib_layered_config``, which merges six layers in a fixed precedence
and remembers which file each value came from:

    defaults -> app -> host -> user -> dotenv -> env

and the command line sits above all six. An option a person actually typed always wins, so a unit
whose ``ExecStart`` names settings on the command line keeps meaning exactly that, whatever any
file says.

The defaults layer is ``defaultconfig.toml`` beside this module plus every ``.toml`` in the
``defaultconfig.d`` directory beside it, merged in sorted order. The tracked files there are the
public defaults, one scope per file (``10-zone.toml`` ... ``90-mpd.toml``). A checkout that needs
values of its own writes them into a higher-numbered file in the same directory named
``9N-<scope>-rnhome.toml``: it sorts after every public default, so its values win within the
defaults layer, and the ``-rnhome`` suffix keeps it out of git and out of the wheel. The tracked
``9N-<scope>-rnhome.toml.example`` beside each one shows the shape; the library never reads it,
because ``.example`` is not a suffix it loads.

Which defaults file is read is a seam, :func:`defaults_from`, so the test suite can point every test
at a copy of the tracked files alone and a developer's private file cannot make a local run differ
from CI.

The identifiers are declared once in ``__init__conf__.py`` and imported both here and by
``tests/e2e_config.py``, which reads the hardware tests' settings through the same vendor, app and
slug. The two uses do not collide: the hardware tests' keys are lowercase top-level keys
(``e2e_host``) and everything this package reads lives under a section.

A refusal raised here is translated into an exit code by whichever command is running, not here.
"""

from __future__ import annotations

import fnmatch
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from lib_layered_config import Config, default_env_prefix, read_config
from lib_layered_config import ConfigError as LayeredConfigError

from ...__init__conf__ import LAYEREDCONF_APP, LAYEREDCONF_SLUG, LAYEREDCONF_VENDOR
from .errors import ConfigInputError

if TYPE_CHECKING:
    from collections.abc import Generator

__all__ = [
    "ENV_PREFIX",
    "PACKAGED_DEFAULTS",
    "PRIVATE_FILE_PATTERN",
    "clear_config_cache",
    "default_config_path",
    "defaults_from",
    "get_config",
    "is_private_file",
]


ENV_PREFIX = default_env_prefix(LAYEREDCONF_SLUG)
"""``SOUNDTOUCH_ZONEMASTER___``. Computed rather than typed out, because the rule that produces it is
the library's: the slug uppercased with hyphens as underscores, then a TRIPLE underscore. The
segments of a key path are then joined by DOUBLE underscores, so ``zone.bind_ip`` is set by
``SOUNDTOUCH_ZONEMASTER___ZONE__BIND_IP``."""


PACKAGED_DEFAULTS = Path(__file__).parent / "defaultconfig.toml"
"""The defaults file that ships inside the wheel, whatever it was installed from. Its companion
directory ``defaultconfig.d`` is found by the library from this name."""


PRIVATE_FILE_PATTERN = "*-rnhome.*"
"""The name of a file that holds one machine's own values: the ``.gitignore`` rule, the wheel's
exclude and ``config --redact`` all key on it, and :func:`is_private_file` is its one reading."""

_TRACKED_EXAMPLE_PATTERN = "*-rnhome.*.example"


def is_private_file(name: str) -> bool:
    """Whether a file NAME is a private override, as git sees it: the tracked ``.example`` copy is not."""
    return fnmatch.fnmatch(name, PRIVATE_FILE_PATTERN) and not fnmatch.fnmatch(name, _TRACKED_EXAMPLE_PATTERN)


@dataclass
class _DefaultsInUse:
    """Which defaults file the loader reads. One instance, changed only through :func:`defaults_from`."""

    path: Path


_IN_USE = _DefaultsInUse(PACKAGED_DEFAULTS)


def default_config_path() -> Path:
    """The defaults file the loader reads: :data:`PACKAGED_DEFAULTS`, unless :func:`defaults_from`
    has pointed it elsewhere."""
    return _IN_USE.path


@contextmanager
def defaults_from(path: Path) -> Generator[Path]:
    """Read the defaults layer from ``path`` (and the ``.d`` directory beside it) until the block ends.

    This is the seam the test suite uses to read a copy of the tracked defaults without any private
    ``-rnhome`` file. The merged configuration is forgotten on the way in and on the way out, so
    neither side of the block sees an answer merged from the other side's files. ``config-deploy``
    copies from the same place, so inside the block it writes the redirected tree.
    """
    before = _IN_USE.path
    _IN_USE.path = path
    clear_config_cache()
    try:
        yield path
    finally:
        _IN_USE.path = before
        clear_config_cache()


@lru_cache(maxsize=4)
def get_config(*, profile: str | None = None, start_dir: str | None = None, dotenv_path: str | None = None) -> Config:
    """The six layers, merged, with the bundled defaults underneath and provenance kept.

    Cached, because a command reads it once and a long-running service reads it at startup; a test
    that writes a config file and expects it to be seen calls :func:`clear_config_cache` first.

    ``start_dir`` and ``dotenv_path`` are the library's own seams and are passed straight through:
    with neither given it walks up from the working directory looking for a ``.env``, which is the
    library's documented default.

    Every way this can fail - an unparseable file, an unreadable one, a profile name that is not a
    profile name - arrives as :class:`ConfigInputError` carrying the library's own message, which
    names the file and the line. The translation happens here so that no caller has to know which
    of the library's five exception types it might meet.
    """
    try:
        return read_config(
            vendor=LAYEREDCONF_VENDOR,
            app=LAYEREDCONF_APP,
            slug=LAYEREDCONF_SLUG,
            profile=profile,
            default_file=default_config_path(),
            start_dir=start_dir,
            dotenv_path=dotenv_path,
        )
    except LayeredConfigError as exc:
        raise ConfigInputError(f"refused: {exc}") from exc


def clear_config_cache() -> None:
    """Forget the merged configuration, so the next read goes back to the files."""
    get_config.cache_clear()
