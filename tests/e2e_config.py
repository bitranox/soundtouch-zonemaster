"""Where the end-to-end tests get the settings that are specific to one house.

A speaker's address is house data, so it is never written into a tracked file. The layout is the
same one the service's own defaults use:

- ``tests/e2e_defaults.toml`` is tracked and holds the SAFE answers: no host, no speakers, nothing
  audible. A machine with nothing else reaches no speaker at all, and the hardware tests skip
  rather than guess.
- ``tests/e2e_defaults.d/`` beside it is read after it, every ``.toml`` in sorted order. A machine
  that can reach the speakers puts its values in the gitignored
  ``tests/e2e_defaults.d/90-e2e-rnhome.toml``, typed: ``e2e_speakers`` a list of addresses and
  ``e2e_audible`` a bool. The tracked ``90-e2e-rnhome.toml.example`` shows the shape and is never
  read, because ``.example`` is not a suffix the library loads.
- A ``.env`` at the repository root is still read, above both. It is optional, and every value in
  it arrives as TEXT - the speakers comma separated, ``audible`` a word - which is why the readers
  below accept a value either way.

Read through ``lib_layered_config`` rather than by hand, so there is one precedence to reason about
(defaults file, its ``.d`` directory, the app/host/user layers, ``.env``, then a process
environment variable) and every value can say which file it came from. The keys are flat top-level
names because that is what a ``.env`` key becomes.

The vendor, app and slug are the program's own, imported rather than repeated: they decide which
directories are searched, and a second copy of them here would let the tests and the service look
in different places while both still passed. Sharing them costs nothing, because the settings
below are flat top-level keys and everything the service reads lives under a section.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from lib_layered_config import Config, read_config
from pydantic import BaseModel

from soundtouch_zonemaster.__init__conf__ import LAYEREDCONF_APP, LAYEREDCONF_SLUG, LAYEREDCONF_VENDOR

__all__ = ["DEFAULTS", "DOTENV", "E2ESettings", "load_e2e_settings"]

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = Path(__file__).resolve().parent / "e2e_defaults.toml"
"""The tracked safe defaults. The library reads ``e2e_defaults.d/`` beside it after it."""
DOTENV = ROOT / ".env"

_TRUE = frozenset({"1", "true", "yes", "on"})


class E2ESettings(BaseModel):
    """What a hardware test needs to know, validated once at the boundary.

    ``audible`` gates anything that makes a sound. It defaults to false and has to be turned on in
    an untracked file, because whether a run is acceptable depends on who is at home, which no
    committed file can know.
    """

    host: str
    user: str
    key: str
    speakers: tuple[str, ...]
    audible: bool

    @property
    def reachable(self) -> bool:
        """Whether there is anything to talk to. False on a machine with only the tracked defaults."""
        return bool(self.host and self.speakers)


def _text(config: Config, key: str) -> str:
    """One config value as text; a missing key is the empty string, which reads as "not set"."""
    value = config.get(key, default="")
    return value if isinstance(value, str) else str(value)


def _as_list(value: object) -> tuple[str, ...]:
    """The speakers as a tuple: a TOML list as it is, a ``.env`` line split on commas.

    Empty entries are dropped either way, so a trailing comma or an empty list is "no speakers".
    """
    parts = [str(item) for item in cast("list[object]", value)] if isinstance(value, list) else str(value).split(",")
    return tuple(part.strip() for part in parts if part.strip())


def _as_bool(value: object) -> bool:
    """A TOML bool as it is; text true only when it plainly says so, which is the safe way round."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE


def load_e2e_settings(*, defaults: Path = DEFAULTS, dotenv: Path = DOTENV) -> E2ESettings:
    """The settings, merged over the tracked defaults. Never raises for a missing ``.env``.

    ``defaults`` and ``dotenv`` are there so a test can hand in a tree of its own; the hardware
    tests call this with neither. The ``.env`` is always named explicitly, so a missing one is
    simply absent rather than searched for in the directories above the checkout.
    """
    config = read_config(
        vendor=LAYEREDCONF_VENDOR,
        app=LAYEREDCONF_APP,
        slug=LAYEREDCONF_SLUG,
        default_file=defaults,
        dotenv_path=dotenv,
    )
    return E2ESettings(
        host=_text(config, "e2e_host"),
        user=_text(config, "e2e_user") or "root",
        key=_text(config, "e2e_key"),
        speakers=_as_list(config.get("e2e_speakers", default="")),
        audible=_as_bool(config.get("e2e_audible", default=False)),
    )
