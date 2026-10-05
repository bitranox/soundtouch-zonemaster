"""A ``--set`` read, and merged over everything the files and the environment said.

One override is one dotted path and one value, typed by the SAME rule lib_layered_config applies to
an environment variable and a ``.env`` line, so the two top layers never read one spelling two ways:
a number, a boolean, a list or a table where the library would type it, the text otherwise, and a
quoted value is always the text inside the quotes.

:func:`write_at` is public because the settings map writes with it too: both build a nested mapping
from dotted paths, and one of the two would otherwise be importing the other's private name.
"""

from __future__ import annotations

import json
import math
import re
from typing import TYPE_CHECKING, Any, Final, NamedTuple, cast

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

    ``=1.2`` is a number, ``=true`` a boolean and ``=["A","B"]`` a list; ``=1.50`` and ``=0640``
    stay text because a number is taken only where it reads back as the same text; anything else
    stays the text that was typed, which is what makes ``--set zone.bind_ip=203.0.113.190`` do the
    obvious thing. ``=null`` is no value, on every key.
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


_QUOTES: Final = ('"', "'")
_NO_VALUE: Final = frozenset({"null", "none"})
_INT_TEXT: Final = re.compile(r"0|-?[1-9][0-9]{0,18}")
"""Exactly the texts ``str(int(v))`` gives back unchanged: no leading zero, no sign but minus."""
_MAX_FLOAT_TEXT: Final = 32


def _coerce(text: str) -> Any:
    """The value lib_layered_config's ``.env`` layer would make of this text.

    Mirrored rather than imported, because the library keeps the rule in a private module; what
    keeps the copy honest is tests/test_config.py, which feeds every spelling through the
    library's own dotenv layer and requires the same value back. Quoting is the dotenv layer's way
    to keep text (the environment layer has no quotes to strip), and a command line needs it for
    the same reason: ``--set database.password='"8675309"'`` is a password, not a number.

    One deliberate difference: the library keeps ``null``/``none`` as TEXT on a secret's key, and
    this reads them as no value on every key. Somebody who types ``--set database.password=null``
    did not mean a password spelled null, so the boundary refuses it by name instead of handing
    the driver a wrong password that fails only at login (user decision, 2026-10-05).
    """
    if len(text) >= 2 and text[0] == text[-1] and text[0] in _QUOTES:  # noqa: PLR2004 - two quotes
        return text[1:-1]
    if text.startswith(("[", "{")):
        try:
            return json.loads(text)
        except ValueError:
            pass
    lowered = text.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in _NO_VALUE:
        return None
    return _number(text)


def _number(text: str) -> Any:
    """An int or a float only where it reads back as the same text, else the text unchanged."""
    if _INT_TEXT.fullmatch(text):
        return int(text)
    if len(text) > _MAX_FLOAT_TEXT or not text.isascii():
        return text
    try:
        number = float(text)
    except ValueError:
        return text
    return number if math.isfinite(number) and str(number) == text else text


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
