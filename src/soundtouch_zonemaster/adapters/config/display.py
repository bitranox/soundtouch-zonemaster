"""The merged configuration as something a person reads: dotted keys, and where each came from.

Only the shaping. Which of the two output modes a command prints, and the envelope it prints in,
belong to the command; what is here is the part that would otherwise be written twice.

The house database is not a config layer and does not become one here: a preference stored there
(``prefs``) is laid over the merged view afterwards, by :func:`overlay_preferences`, so the loader
and ``config-deploy`` never learn it exists.
"""

from __future__ import annotations

import json
from pathlib import PurePath
from typing import TYPE_CHECKING, Any

from ...domain.database_url import masked as masked_database_url
from ...domain.preferences import PreferenceName, plain_value, shown, stored
from .errors import ConfigInputError
from .loader import is_private_file
from .settings_map import config_path_of

if TYPE_CHECKING:
    from collections.abc import Collection, Iterator, Mapping, Sequence

    from ...domain.preferences import PreferenceRow, Stored

__all__ = [
    "DATABASE_LAYER",
    "IGNORED_ROW",
    "flatten",
    "line_beneath",
    "mask_database_preferences",
    "mask_database_settings",
    "mask_values_from_layer",
    "mask_values_from_private_files",
    "overlay_preferences",
    "shows_a_preference",
    "where",
]

DATABASE_LAYER = "database"
"""The origin a value has when a row in the house database, not a config layer, decides it (``prefs``)."""

IGNORED_ROW = "ignored_database_row"
"""The provenance entry for a stored row the preference rule refused, kept beside the origin that
stays in force: the row is shown, never dropped, so the person who wrote it finds out why."""

_PREFERENCES = frozenset(str(name) for name in PreferenceName)
"""The dotted keys a stored row can decide: the preference names, which are their config paths."""

DATABASE_URL_KEY = config_path_of("database")
"""The one dotted key ``config`` reports whose value can itself be a URL carrying a password."""

DATABASE_PASSWORD_KEY = config_path_of("database_password")
"""The dotted key whose whole value is a password: never shown, whatever it holds. Read from the
settings map, so the key masked here is the key the service reads."""

_DATABASE_SECTION = DATABASE_PASSWORD_KEY.partition(".")[0]
"""The section both keys above live in. Every other key under it is masked whole."""


def where(origin: Mapping[str, Any] | None) -> str:
    """A value's origin as one readable phrase; a value with none is one nothing claimed.

    A stored row is named by who set it and when, which is what a person reaching for
    ``prefs unset`` needs; the file value it overrides is :func:`line_beneath`'s.
    """
    if origin is None:
        return "unknown"
    layer = origin.get("layer", "unknown")
    if layer == DATABASE_LAYER:
        return f"{DATABASE_LAYER} ({origin.get('source')}, {origin.get('changed_at') or 'time not recorded'})"
    path = origin.get("path")
    return f"{layer}: {path}" if path else str(layer)


def line_beneath(key: str, origin: Mapping[str, Any] | None) -> str | None:
    """The line printed under a value, when the house database has something to say about it.

    Either the file value a stored row overrides - on its own line, so somebody who edited the file
    and saw nothing change reads why in the same view - or a stored row that was refused, with the
    text it holds and the reason, while the value above it stays the file's.
    """
    if origin is None:
        return None
    if origin.get("layer") == DATABASE_LAYER:
        overridden: Mapping[str, Any] = origin["overrides"]
        return f"#   overridden: {key} = {json.dumps(overridden['value'])}    # {where(overridden['origin'])}"
    ignored: Mapping[str, Any] | None = origin.get(IGNORED_ROW)
    if ignored is None:
        return None
    # Cut and escaped for the terminal only: the JSON view keeps the raw text whole.
    return f"#   ignored in the house database: {key} = {shown(str(ignored['text']))} ({ignored['why']})"


def shows_a_preference(values: Sequence[tuple[str, Any]]) -> bool:
    """Whether a view holds any key a stored row could decide - the only reason to open the database."""
    return any(key in _PREFERENCES for key, _ in values)


def overlay_preferences(
    values: Sequence[tuple[str, Any]],
    provenance: Mapping[str, Mapping[str, Any] | None],
    rows: tuple[PreferenceRow, ...],
) -> tuple[list[tuple[str, Any]], dict[str, Mapping[str, Any] | None]]:
    """The same pairs, with every preference a usable stored row decides shown as that row's value.

    A row wins over every layer while it is set (``domain/preferences.py``), so the view shows it
    as the value, with the layer :data:`DATABASE_LAYER`, and keeps the file value it replaces under
    ``overrides``. A row the rule refuses decides nothing: the file value and its origin stay, and
    the row is added beside them under :data:`IGNORED_ROW` with its raw text and the reason. A row
    under a name that is no preference at all has no key here to belong to, and ``prefs`` lists it.
    Only keys already in ``values`` are visited, which holds every preference because each of the
    five ships a default (``defaultconfig.d``); one without a default would need adding here.

    Judged by :func:`~soundtouch_zonemaster.domain.preferences.stored`, the one rule the running
    service applies too, so the view cannot show a row as deciding that the service ignores. The
    provenance mapping handed in is not changed; every origin written here is a new one.
    """
    usable, rejected = stored(rows)
    decided = {str(name): held for name, held in usable.items()}
    refused = {row.name: (row, why) for row, why in rejected if row.name in _PREFERENCES}
    origins: dict[str, Mapping[str, Any] | None] = dict(provenance)
    shown: list[tuple[str, Any]] = []
    for key, value in values:
        held = decided.get(key)
        if held is not None:
            origins[key] = _decided_by(held, overriding=value, origin=provenance.get(key))
            shown.append((key, plain_value(held.value)))
            continue
        if key in refused:
            row, why = refused[key]
            origins[key] = _with_ignored_row(provenance.get(key), row=row, why=why)
        shown.append((key, value))
    return shown, origins


def _decided_by(held: Stored, *, overriding: Any, origin: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "layer": DATABASE_LAYER,
        "path": None,
        "source": held.row.source,
        "changed_at": held.row.changed_at,
        "overrides": {"value": overriding, "origin": origin},
    }


def _with_ignored_row(origin: Mapping[str, Any] | None, *, row: PreferenceRow, why: str) -> dict[str, Any]:
    kept: dict[str, Any] = {} if origin is None else dict(origin)
    kept[IGNORED_ROW] = {"text": row.text, "source": row.source, "changed_at": row.changed_at, "why": why}
    return kept


def mask_database_preferences(
    values: Sequence[tuple[str, Any]],
    provenance: Mapping[str, Mapping[str, Any] | None],
    *,
    mask: str,
) -> tuple[list[tuple[str, Any]], dict[str, Mapping[str, Any] | None]]:
    """The view with everything the house database said replaced by ``mask``, for ``--redact``.

    What the database holds is per-machine in the way a ``.env`` is, so none of it is shown: a
    value a row decides, the file value it overrides (which can itself have come from a ``.env``),
    and a refused row's text together with the reason, because the reason quotes the value. New
    origins are built for the ones masked; the mapping handed in is not changed.
    """
    shown = mask_values_from_layer(values, provenance, layer=DATABASE_LAYER, mask=mask)
    return shown, {key: _masked_origin(origin, mask=mask) for key, origin in provenance.items()}


def _masked_origin(origin: Mapping[str, Any] | None, *, mask: str) -> Mapping[str, Any] | None:
    if origin is None:
        return None
    if origin.get("layer") == DATABASE_LAYER:
        overridden: Mapping[str, Any] = origin["overrides"]
        return {**origin, "overrides": {**overridden, "value": mask}}
    ignored: Mapping[str, Any] | None = origin.get(IGNORED_ROW)
    if ignored is None:
        return origin
    return {**origin, IGNORED_ROW: {**ignored, "text": mask, "why": mask}}


def mask_database_settings(values: Sequence[tuple[str, Any]], *, mask: str) -> list[tuple[str, Any]]:
    """The same pairs, with every value under the database section replaced by ``mask`` except
    :data:`DATABASE_URL_KEY`'s, which is masked wherever it carries a password.

    Unconditional, unlike :func:`mask_values_from_layer` and :func:`mask_values_from_private_files`:
    a database URL can carry a password from ANY layer - a config file, an environment variable, a
    dotenv, or ``--set`` on the command line - not only from a private file or a ``.env``, which
    are the two things ``--redact`` exists to catch. So a URL carrying a password is shown as its
    scheme alone before ``--redact`` is even asked about, in every output mode the ``config`` view
    has; one without a password is shown as typed. The password setting is replaced whatever its
    value and type, empty included, so no reading of it can put any of it on screen.

    Every OTHER key in that section is replaced too, and the section is matched without regard to
    case. The section holds two settings, so a third key there is almost certainly the password
    under a name nobody spelled right (``passwrod``, ``Password``), or the password setting read
    as a table, whose leaves carry names of their own. The exact key would miss every one of them.

    The url's own rule reads text, so it holds only while the url IS text. The environment layer
    reads a value opening with ``[`` as a JSON array (``--set`` and a TOML array do the same), and
    a URL carrying a password inside a list would otherwise be shown as it came; a url that is not
    text is not a URL, so it is masked whole. One that arrived as a table is walked into leaves
    under ``database.url.``, which the section rule already masks. The one exception is no value
    at all (``null`` or ``none`` from the environment, ``--set database.url=null``): it can carry
    no password, and showing it says what is wrong, since the service refuses it as a database
    given nowhere.
    """

    def hidden(key: str, value: Any) -> Any:
        if key == DATABASE_URL_KEY:
            if value is None:
                return None
            return masked_database_url(value) if isinstance(value, str) else mask
        section, dot, _ = key.partition(".")
        if dot and section.lower() == _DATABASE_SECTION:
            return mask
        return value

    return [(key, hidden(key, value)) for key, value in values]


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
