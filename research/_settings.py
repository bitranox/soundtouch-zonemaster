"""The research scripts' settings, read through ``lib_layered_config`` rather than written into code.

Three scripts carry values that describe one particular house rather than the protocol: which
speaker a capture must never touch, which recorded capture the golden check replays, and where the
live MPD keeps its configuration. Those used to be module constants, so the only way to run the
scripts somewhere else was to edit them. They are settings now, merged from the same six layers
the service reads, under a slug of their own so the two never read each other's files:

    defaults -> app -> host -> user -> dotenv -> env

The defaults layer is ``research/defaultconfig.toml`` plus every ``*.toml`` in
``research/defaultconfig.d/``, one scope per file. The tracked files hold values that are safe
anywhere - no speaker refused, no capture named - and a house writes its own into a gitignored
``9N-<scope>-rnhome.toml`` beside them, which sorts after every tracked file and so wins. The
``.toml.example`` files beside them are documentation: the loader reads ``*.toml`` only.

Each setting also has ONE fallback here, used when no layer names it at all - which is what
happens when a script is copied to another host without this tree beside it. The tracked files
mirror these values, and ``tests/test_research_config.py`` fails if the two ever disagree.

This is a copy-style helper like ``_click.py``: the research scripts run where the package is not
installed, so they cannot import the package's own config adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from lib_layered_config import Config, default_env_prefix, read_config

__all__ = [
    "DEFAULT_FILE",
    "ENV_PREFIX",
    "CaptureSettings",
    "Config",
    "GoldenSettings",
    "MpdAbSettings",
    "SettingsError",
    "capture_settings",
    "golden_settings",
    "load",
    "mpd_ab_settings",
]

VENDOR = "bitranox"
APP = "soundtouch-zonemaster-research"
SLUG = "soundtouch-zonemaster-research"
"""Not the service's slug: a research setting must never land in, or be read from, the service's files."""

ENV_PREFIX = default_env_prefix(SLUG)
"""``SOUNDTOUCH_ZONEMASTER_RESEARCH___``; a key path joins with double underscores, as in the service."""

DEFAULT_FILE = Path(__file__).resolve().parent / "defaultconfig.toml"


class SettingsError(ValueError):
    """A layer set a research setting to a value of the wrong type."""


@dataclass(frozen=True, slots=True)
class CaptureSettings:
    """What capture_zone.py needs to know about the house before it touches a speaker."""

    never_touch: tuple[str, ...]
    """Speaker addresses a run refuses to drive, as either master or slave."""
    ssh_user: str
    """The account tcpdump and netstat are run as on a speaker."""


@dataclass(frozen=True, slots=True)
class GoldenSettings:
    """Which recorded capture golden_check.py replays through the two analysers."""

    capture: str
    """The capture directory, relative to research/. Empty means none is configured."""
    pcap: str
    """The pcap file inside it that the analysers read."""
    master: str
    slave: str


@dataclass(frozen=True, slots=True)
class MpdAbSettings:
    """Where mpd_seek_ab.py finds the live MPD it copies its music and database from."""

    live_conf: Path
    live_db: Path


CAPTURE_DEFAULTS = CaptureSettings(never_touch=(), ssh_user="root")
GOLDEN_DEFAULTS = GoldenSettings(capture="", pcap="", master="", slave="")
MPD_AB_DEFAULTS = MpdAbSettings(live_conf=Path("/etc/mpd.conf"), live_db=Path("/var/lib/mpd/tag_cache"))


def load(*, default_file: Path | None = DEFAULT_FILE, start_dir: Path | None = None) -> Config:
    """The six layers, merged. A header file that does not exist is treated as absent, not refused.

    ``start_dir`` seeds the library's search for a ``.env`` (the working directory when None);
    tests hand it a temporary directory so no checkout's own ``.env`` is read.
    """
    usable = default_file if default_file is not None and default_file.is_file() else None
    return read_config(
        vendor=VENDOR,
        app=APP,
        slug=SLUG,
        default_file=usable,
        start_dir=None if start_dir is None else str(start_dir),
    )


def _text(config: Config, key: str, fallback: str) -> str:
    value: object = config.get(key, default=fallback)
    if not isinstance(value, str):
        raise SettingsError(f"{key} must be a string, got {type(value).__name__}")
    return value


def _addresses(config: Config, key: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    value: object = config.get(key, default=list(fallback))
    if not isinstance(value, list):
        raise SettingsError(f"{key} must be a list of addresses, got {type(value).__name__}")
    items = cast("list[object]", value)  # isinstance narrows to list[Unknown]; each item is checked next
    if not all(isinstance(item, str) for item in items):
        raise SettingsError(f"{key} must hold strings only")
    return tuple(str(item) for item in items)


def capture_settings(config: Config) -> CaptureSettings:
    """The ``[capture]`` scope, with the fallback for every key no layer set."""
    return CaptureSettings(
        never_touch=_addresses(config, "capture.never_touch", CAPTURE_DEFAULTS.never_touch),
        ssh_user=_text(config, "capture.ssh_user", CAPTURE_DEFAULTS.ssh_user),
    )


def golden_settings(config: Config) -> GoldenSettings:
    """The ``[golden]`` scope, with the fallback for every key no layer set."""
    return GoldenSettings(
        capture=_text(config, "golden.capture", GOLDEN_DEFAULTS.capture),
        pcap=_text(config, "golden.pcap", GOLDEN_DEFAULTS.pcap),
        master=_text(config, "golden.master", GOLDEN_DEFAULTS.master),
        slave=_text(config, "golden.slave", GOLDEN_DEFAULTS.slave),
    )


def mpd_ab_settings(config: Config) -> MpdAbSettings:
    """The ``[mpd_ab]`` scope, with the fallback for every key no layer set."""
    return MpdAbSettings(
        live_conf=Path(_text(config, "mpd_ab.live_conf", str(MPD_AB_DEFAULTS.live_conf))),
        live_db=Path(_text(config, "mpd_ab.live_db", str(MPD_AB_DEFAULTS.live_db))),
    )
