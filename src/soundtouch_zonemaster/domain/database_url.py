"""Whether a database setting carries a password, and how to show one without it.

A database setting is written into config files, ``--json`` envelopes and logs, so a password
inside it must never be echoed - libpq reads one from ``~/.pgpass`` (or the file ``PGPASSFILE``
names) instead. A password can arrive two ways: in a URL's own userinfo, or as one of
:data:`PASSWORD_QUERY_KEYS` - a query key some drivers (psycopg among them) pass straight through
as a connect argument.

This is the ONE place that knows either shape. ``adapters/cli/boundary.py`` (the option refusal),
``adapters/files/house_db.py`` (the store's own refusal, and the key set behind its own
SQLAlchemy-based redaction) and ``adapters/config/display.py`` (the ``config`` view, which has
only a raw setting string and no SQLAlchemy URL to parse) all read the rule from here instead of
keeping a copy of the key set or the check.

Pure and stdlib-only (``urllib.parse``), so it is reachable from the domain layer, which may not
import a framework. A plain path (no ``"://"``) is not a URL at all, and neither function here
treats one as carrying a password.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlsplit, urlunsplit

if TYPE_CHECKING:
    from urllib.parse import SplitResult

__all__ = ["PASSWORD_QUERY_KEYS", "carries_a_password", "masked"]

PASSWORD_QUERY_KEYS = frozenset({"password", "sslpassword"})
"""Query keys a driver passes straight through as a connect argument, case-insensitively."""


def carries_a_password(setting: str) -> bool:
    """Whether ``setting`` is a URL whose userinfo or query carries a password.

    A setting with no ``"://"`` is a plain path, never a URL, so it never carries one. A URL this
    function cannot even split is treated as NOT carrying a password by declining to look inside
    it - a caller that must be conservative about an unparsable URL (refusing it outright, say)
    decides that for itself; this function only answers what it can actually see.
    """
    if "://" not in setting:
        return False
    try:
        parsed = urlsplit(setting)
    except ValueError:
        return False
    has_password_key = any(key.lower() in PASSWORD_QUERY_KEYS for key, _ in parse_qsl(parsed.query))
    return parsed.password is not None or has_password_key


def masked(setting: str) -> str:
    """``setting``, safe to print: the userinfo password and any password-shaped query value replaced by ``***``.

    A setting with no ``"://"``, or a URL this function cannot even split, is returned UNCHANGED:
    neither is a URL it can find a password inside, and an unparsable string can itself be the
    thing carrying the secret, so it must not be reassembled and echoed here either.
    """
    if "://" not in setting:
        return setting
    try:
        parsed = urlsplit(setting)
    except ValueError:
        return setting
    netloc = parsed.netloc if parsed.password is None else _masked_netloc(parsed)
    query = "&".join(_masked_pair(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True))
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))


def _masked_netloc(parsed: SplitResult) -> str:
    """The netloc with its password replaced by ``***``. Only called when there is one.

    Read off ``netloc`` by TEXT rather than through ``.hostname``/``.port``: those coerce the port
    to an int and raise on one that is not (``db.example:54x2``), which is exactly the malformed
    shape a password check must still get through without crashing.
    """
    userinfo, _, hostport = parsed.netloc.rpartition("@")
    username, _, _password = userinfo.partition(":")
    return f"{username}:***@{hostport}"


def _masked_pair(key: str, value: str) -> str:
    """One query pair, its value replaced by ``***`` when the key names a password."""
    shown = "***" if key.lower() in PASSWORD_QUERY_KEYS else value
    return f"{key}={shown}"
