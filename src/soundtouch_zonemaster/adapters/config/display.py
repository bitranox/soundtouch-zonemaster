"""The merged configuration as something a person reads: dotted keys, and where each came from.

Only the shaping. Which of the two output modes a command prints, and the envelope it prints in,
belong to the command; what is here is the part that would otherwise be written twice.
"""

from __future__ import annotations

from pathlib import PurePath
from typing import TYPE_CHECKING, Any

from .errors import ConfigInputError
from .loader import is_private_file

if TYPE_CHECKING:
    from collections.abc import Collection, Iterator, Mapping, Sequence

__all__ = ["flatten", "mask_values_from_layer", "mask_values_from_private_files", "where"]


def where(origin: Mapping[str, Any] | None) -> str:
    """A value's origin as one readable phrase; a value with none is one nothing claimed."""
    if origin is None:
        return "unknown"
    path = origin.get("path")
    layer = origin.get("layer", "unknown")
    return f"{layer}: {path}" if path else str(layer)


def mask_values_from_layer(
    values: Sequence[tuple[str, Any]],
    provenance: Mapping[str, Mapping[str, Any] | None],
    *,
    layer: str,
    mask: str,
) -> list[tuple[str, Any]]:
    """The same pairs, with every value that came from ``layer`` replaced by ``mask``.

    Masking by ORIGIN rather than by key name is the point. A name list only knows the names
    somebody thought of, and the one this view inherits masks ``e2e_key`` while printing
    ``e2e_host``, ``e2e_user`` and ``e2e_speakers`` - addresses - in full. Which layer is
    per-machine is the caller's decision, so it is an argument rather than a constant here.

    A key with no origin keeps its value, because nothing claimed it and so nothing says it came
    from ``layer``. That origin is read through a declared type rather than an ``or {}`` fallback:
    the fallback widens the expression to ``Any`` and takes the whole test out of the type
    checker's sight, which is what pyright strict refuses here.
    """

    def came_from_layer(key: str) -> bool:
        origin = provenance.get(key)
        return origin is not None and origin.get("layer") == layer

    return [(key, mask if came_from_layer(key) else value) for key, value in values]


def mask_values_from_private_files(
    values: Sequence[tuple[str, Any]],
    provenance: Mapping[str, Mapping[str, Any] | None],
    *,
    mask: str,
) -> list[tuple[str, Any]]:
    """The same pairs, with every value a private override FILE set replaced by ``mask``.

    A private ``9N-<scope>-rnhome.toml`` sits in the defaults directory, so its values report the
    defaults layer and :func:`mask_values_from_layer` cannot tell them from the public defaults
    beside them. The file's name can, which is why this keys on the provenance path instead.
    """

    def came_from_private_file(key: str) -> bool:
        origin = provenance.get(key)
        path = None if origin is None else origin.get("path")
        return isinstance(path, str) and is_private_file(PurePath(path).name)

    return [(key, mask if came_from_private_file(key) else value) for key, value in values]


def flatten(
    data: Mapping[str, Any], *, prefix: str | None = None, known_names: Collection[str] = ()
) -> list[tuple[str, Any]]:
    """The configuration as dotted key and value pairs, in the order the merge produced them.

    A ``prefix`` that matches nothing is two different questions wearing one face, and they are
    answered differently. A name in ``known_names`` is a scope or a setting this program reads, so
    nothing having set it is an ANSWER and comes back as no pairs. Any other name is a typo, and
    that stays a refusal rather than an empty listing: silence there would read as "it is empty"
    rather than as "there is no such section", which is what this split exists for.

    The distinction is not academic. The two scopes whose every setting describes ONE machine ship
    with every line commented out, so they are empty on a host nobody has configured yet - and
    that is precisely the host somebody is asking about.

    ``known_names`` is passed in rather than imported, so this module stays about the shaping:
    which names a program owns is what its settings map says, and there is only one of those.
    """
    pairs = list(_walk(data, ()))
    if prefix is None:
        return pairs
    wanted = [(key, value) for key, value in pairs if key == prefix or key.startswith(f"{prefix}.")]
    if not wanted and prefix not in known_names:
        message = f"refused: no section or key called {prefix!r} in the merged configuration"
        raise ConfigInputError(message)
    return wanted


def _walk(data: Mapping[str, Any], path: tuple[str, ...]) -> Iterator[tuple[str, Any]]:
    """Every leaf under ``data``, keyed by its dotted path. A table is walked, a list is a leaf."""
    for key, value in data.items():
        here = (*path, str(key))
        if isinstance(value, dict):
            yield from _walk(value, here)  # pyright: ignore[reportUnknownArgumentType] - Any by contract
        else:
            yield ".".join(here), value
