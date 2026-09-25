"""Which config path fills which field of the record, and the way back from a field to a path.

The map is the only enumeration of the settings, which is what makes the two readers below - one
for the service, one for the prototype - complete rather than nearly complete, and it is why
``tests/test_config.py`` pins each map against its record in BOTH directions.

The reverse lookups are here rather than beside the refusal that uses them, for the same reason:
they reverse this map, and a second copy of it living somewhere else could disagree with the first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from .overrides import write_at

if TYPE_CHECKING:
    from collections.abc import Mapping

    from lib_layered_config import Config

__all__ = [
    "CONFIGURABLE_NAMES",
    "PROTOTYPE_SECTIONS",
    "PROTOTYPE_SETTINGS",
    "SECTIONS",
    "SETTINGS",
    "config_path_of",
    "env_name_of",
    "prototype_settings",
    "service_settings",
    "unknown_settings",
]


SETTINGS: Mapping[str, str] = {
    # where in a config file it is written  ->  which ServiceOptions field it fills
    "zone.bind_ip": "bind_ip",
    "zone.device_id": "device_id",
    "files.channel_file": "channel_file",
    "files.switch_file": "switch_file",
    "files.state_file": "state_file",
    "registry.url": "registry_url",
    "registry.poll_s": "registry_poll_s",
    "membership.consoles_allowed": "consoles_allowed",
    "membership.unreachable_timeout_s": "unreachable_timeout_s",
    "dialling.window_s": "dial_window_s",
    "dialling.hold_threshold_s": "hold_threshold_s",
    "switch.poll_s": "switch_poll_s",
    "observer.port": "channel_policy.port",
    "observer.backoff_s": "channel_policy.backoff_s",
    "mpd.host": "mpd_host",
    "mpd.port": "mpd_port",
    "mpd.rewind_s": "mpd_rewind_s",
}
"""Every setting a config file can carry, and the field of the record it fills.

The sections are the program's own vocabulary - ``registry``, ``membership``, ``dialling``,
``switch``, ``observer`` and ``mpd`` are the modules that consume them - so a person editing
``30-registry.toml`` is editing the thing ``registry.py`` does. The record is flat and always was,
which is why this table exists at all rather than the two shapes simply being the same: renaming
every field to match a file layout would be the tail wagging the dog.

It is the ONLY enumeration of the settings, and ``tests/test_config.py`` pins it in both
directions: every field of the record is reachable through exactly one entry here, and every entry
names a field that exists. A table that is checked one way only can gain an entry for a field
nobody added, or miss a field somebody did.
"""


SECTIONS: tuple[str, ...] = tuple(dict.fromkeys(path.split(".")[0] for path in SETTINGS))
"""The section names, in the order they first appear above. One per file in ``defaultconfig.d``."""


_ABSENT = object()
"""Distinguishes "no value in any layer" from a value that happens to be ``None``."""


def _read_at(config: Config, path: str) -> Any:
    """One dotted path out of the merged configuration, or :data:`_ABSENT` if nothing set it.

    ``Config.get`` walks a dotted path itself, but it answers a missing key with the default, so
    the sentinel is what separates "nobody said" from "somebody said nothing".
    """
    return config.get(path, default=_ABSENT)


def service_settings(config: Config) -> dict[str, Any]:
    """Everything the config files said, keyed by the record's field names rather than the file's.

    Only the settings in :data:`SETTINGS` are read. Anything else in a config file is left alone,
    which is what lets this house's ``.env`` and this program's settings share a slug without
    interfering, and it is why a section written as something other than a table costs nothing: it
    yields no values instead of stopping a service at startup over a typo in a file.
    """
    found: dict[str, Any] = {}
    for source, target in SETTINGS.items():
        value = _read_at(config, source)
        if value is not _ABSENT:
            write_at(found, tuple(target.split(".")), value)
    return found


def unknown_settings(config: Config) -> list[str]:
    """Dotted paths inside OUR sections that no setting corresponds to.

    A key the record ignores must not stop a house working, so these are reported rather than
    refused. Only our own sections are looked at: a ``.env`` key or another program's section in
    the same file is not a misspelling of anything here.
    """
    known = set(SETTINGS)
    strays: list[str] = []
    for section in SECTIONS:
        table: Any = config.get(section, default=None)
        if not isinstance(table, dict):
            continue
        strays.extend(f"{section}.{key}" for key in cast("dict[str, Any]", table) if f"{section}.{key}" not in known)
    return sorted(strays)


def config_path_of(field: str) -> str:
    """Where a record field is written in a config file, e.g. ``bind_ip`` -> ``zone.bind_ip``.

    The map is one-way by construction, so this reverses it rather than keeping a second copy that
    could disagree with the first. A field with no entry answers with its own name, which is wrong
    but harmless: it appears only inside a message telling somebody where to put a value, and the
    test that pins the map in both directions means it cannot happen.
    """
    return next((source for source, target in SETTINGS.items() if target == field), field)


def env_name_of(field: str) -> str:
    """The environment variable that sets a field, without the prefix: ``ZONE__BIND_IP``."""
    return config_path_of(field).upper().replace(".", "__")


PROTOTYPE_SETTINGS: Mapping[str, str] = {
    # where in a config file it is written  ->  which field of the prototype's settings it fills
    "prototype.never_touch": "never_touch",
}
"""Every setting the PROTOTYPE reads from a config file, and the field it fills.

One entry, and the reason there is only one is the reason the section exists at all: the prototype
is a measurement run whose whole option set is typed on the command line, and the single thing
about it that must not be typed is which addresses this house never touches. That was a constant in
the source until this rebuild, which meant the one refusal protecting a real speaker could only
be changed by editing the program.

Pinned against the record it fills the same way :data:`SETTINGS` is, in both directions, by
``tests/test_config.py``.
"""

PROTOTYPE_SECTIONS: tuple[str, ...] = tuple(dict.fromkeys(path.split(".")[0] for path in PROTOTYPE_SETTINGS))
"""The prototype's section names. One file, ``80-prototype.toml``, as the service's scopes have."""


CONFIGURABLE_NAMES: frozenset[str] = frozenset((*SECTIONS, *SETTINGS, *PROTOTYPE_SECTIONS, *PROTOTYPE_SETTINGS))
"""Every name a config file may carry that this program reads: a scope, or a setting's full path.

Derived from the two maps rather than written out, so a setting added above is a name this program
answers for with nothing else edited.

It exists to tell two questions apart that look identical in the merged configuration. A scope
whose settings all describe ONE machine ships with every line commented out, so ``zone`` and
``files`` hold nothing until somebody writes a value - and "nothing set it" is an ANSWER, while a
name nobody ever declared is a typo. Both are absent from the merge; only one of them is news.
"""


def prototype_settings(config: Config) -> dict[str, Any]:
    """What the config files said about the prototype, keyed by the field names it uses.

    The same read as :func:`service_settings` over the other map, which is what keeps one
    program's section from being able to answer for the other's: a key outside
    :data:`PROTOTYPE_SETTINGS` is not read here at all.
    """
    found: dict[str, Any] = {}
    for source, target in PROTOTYPE_SETTINGS.items():
        value = _read_at(config, source)
        if value is not _ABSENT:
            write_at(found, tuple(target.split(".")), value)
    return found
