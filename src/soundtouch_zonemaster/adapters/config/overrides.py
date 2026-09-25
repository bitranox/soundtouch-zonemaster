"""A ``--set`` read, and merged over everything the files and the environment said.

One override is one dotted path and one value, and the value is read as JSON where it is JSON, so
that the command line can put a number, a boolean or a list into the merge and not only text.

:func:`write_at` is public because the settings map writes with it too: both build a nested mapping
from dotted paths, and one of the two would otherwise be importing the other's private name.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from lib_layered_config import ConfigError as LayeredConfigError

from .errors import ConfigInputError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from lib_layered_config import Config

__all__ = ["Merged", "apply_set_overrides", "parse_set_override", "write_at"]


class Merged(NamedTuple):
    """A configuration and the dotted keys a ``--set`` put there.

    The second half exists because ``Config.with_overrides`` keeps each key's ORIGINAL provenance:
    a value replaced from the command line still reports the file it used to come from, which is
    precisely the wrong answer from the one command whose job is to say where a value came from.
    Nothing in the library can tell afterwards, so what was overridden is remembered here.
    """

    config: Config
    overridden: frozenset[str]


def parse_set_override(raw: str) -> tuple[tuple[str, ...], Any]:
    """``SECTION.KEY[.SUBKEY...]=VALUE`` into the path to write and the value to write there.

    The value is read as JSON when it parses as JSON, so ``=1.2`` is a number, ``=true`` is a
    boolean and ``=["A","B"]`` is a list; anything else stays the text that was typed, which is
    what makes ``--set service.bind_ip=192.168.0.190`` do the obvious thing.
    """
    if "=" not in raw:
        message = f"refused: --set {raw!r} must be SECTION.KEY=VALUE"
        raise ConfigInputError(message)
    path_text, value_text = raw.split("=", maxsplit=1)
    parts = tuple(path_text.split("."))
    if len(parts) < 2 or not all(parts):  # noqa: PLR2004 - a section and at least one key
        message = f"refused: --set {raw!r} needs a section and a key, as SECTION.KEY=VALUE"
        raise ConfigInputError(message)
    return parts, _coerce(value_text)


def _coerce(text: str) -> Any:
    """A typed value where the text is JSON, and the text itself where it is not."""
    if text == "":
        return ""
    try:
        return json.loads(text)
    except ValueError:
        return text


def apply_set_overrides(config: Config, raw_overrides: Sequence[str]) -> Merged:
    """Merge every ``--set`` over the merged configuration, deepest key last.

    Returns the configuration unchanged when nothing was set, so a caller never pays for the
    merge it did not ask for, and the provenance of every untouched value survives.
    """
    if not raw_overrides:
        return Merged(config, frozenset())
    overrides: dict[str, Any] = {}
    written: set[str] = set()
    for raw in raw_overrides:
        path, value = parse_set_override(raw)
        write_at(overrides, path, value)
        written.add(".".join(path))
    try:
        return Merged(config.with_overrides(overrides), frozenset(written))
    except LayeredConfigError as exc:
        raise ConfigInputError(f"refused: {exc}") from exc


def write_at(target: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    """Write ``value`` at ``path``, creating the tables on the way and refusing to tunnel one.

    Two ``--set`` arguments that disagree about whether a name is a table or a value is a typo
    worth saying out loud: silently replacing one with the other would apply half of what was
    asked for.
    """
    node = target
    for part in path[:-1]:
        fresh: dict[str, Any] = {}
        branch: Any = node.setdefault(part, fresh)
        if not isinstance(branch, dict):
            message = f"refused: --set cannot put a key under {part!r}, which was already given a value"
            raise ConfigInputError(message)
        node = cast("dict[str, Any]", branch)
    node[path[-1]] = value
