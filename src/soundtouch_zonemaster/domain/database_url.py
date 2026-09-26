"""Whether a database setting carries a password, and how to show one without it.

A database setting is written into config files, ``--json`` envelopes and logs, so a password
inside it must never be echoed - libpq reads one from ``~/.pgpass`` (or the file ``PGPASSFILE``
names) instead. A password can arrive two ways: in a URL's own userinfo, or as one of
:data:`PASSWORD_QUERY_KEYS` - a query key some drivers (psycopg among them) pass straight through
as a connect argument.

This is the ONE place that knows either shape. ``adapters/cli/boundary.py`` (the option refusal),
``adapters/files/house_db.py`` and ``house_store.py`` (the store's own refusal, and every message
naming the database) and ``adapters/config/display.py`` (the ``config`` view) all read the rule
from here instead of keeping a copy of the key set, the check or the mask.

**The URL is read as TEXT, the way the parser that consumes it reads it.** The store hands the
setting to SQLAlchemy's ``make_url``, and what that parser takes out of it is what the driver
receives, so this rule must be at least as strict as that parser and must fail closed where the
two could disagree. ``urllib.parse.urlsplit`` is not used: it stops the authority at the first
``/``, ``?`` or ``#`` and the query at ``#``, while SQLAlchemy lets a password run to the next
``@`` whatever it contains and a query run to the end of the string, so a ``urlsplit`` reading
passes passwords SQLAlchemy reads. ``tests/test_database_url_differential.py`` holds the rule to
SQLAlchemy row by row.

Pure and stdlib-only, so it is reachable from the domain layer, which may not import a framework.
A plain path (no ``"://"``) is not a URL at all: the store opens it as a SQLite file and no reader
takes a password out of it, so neither function here treats one as carrying a password.
"""

from __future__ import annotations

import re
from urllib.parse import unquote_plus

__all__ = ["PASSWORD_QUERY_KEYS", "UNREADABLE", "carries_a_password", "is_a_password_key", "masked"]

PASSWORD_QUERY_KEYS = frozenset({"password", "sslpassword"})
"""Query keys a driver passes straight through as a connect argument.

Compared after percent-decoding, stripping whitespace and lowercasing: libpq skips the whitespace
around a keyword, so a key that decodes to ``" password"`` still sets the password.
"""

UNREADABLE = "<unreadable database URL>"
"""What :func:`masked` shows for a URL whose scheme it cannot read, in place of any of its text."""

_SCHEME = re.compile(r"[\w+]+")
"""A scheme SQLAlchemy accepts. Anything else before ``"://"`` means the whole string is unread."""

_KEY_START = re.compile(r"(?:^|[/?;\s])")
"""Where a key can start inside one ``&``-separated part, for :func:`masked`."""

_PIECE_DELIMITERS = re.compile(r"([/?&;\s])")
"""Where a query key can start. Wider than the ``&`` a query parser splits on, so no key it finds
can be hidden inside a piece this module reads as one."""


def carries_a_password(setting: str) -> bool:
    """Whether ``setting`` is a URL whose userinfo or query carries a password. Never raises.

    Read from the text after the first ``"://"`` (the whole setting when what stands before it is
    not a scheme SQLAlchemy accepts, which fails closed rather than skipping the text):

    * **userinfo**: SQLAlchemy reads a password exactly when the first ``:`` comes before any
      ``/`` and an ``@`` follows it anywhere later - the password may itself hold ``/``, ``?``
      or ``#``. That condition is checked as it stands.
    * **query**: everything after the first ``?``, fragment included, cut at every ``/``, ``?``,
      ``&``, ``;`` and whitespace; a piece whose key (up to its first ``=``) decodes to one of
      :data:`PASSWORD_QUERY_KEYS` counts, with or without a value.

    Both are no looser than SQLAlchemy: every key it reads out of a query starts right after a
    ``?`` or ``&`` at or past the first ``?``, and a key that decodes to a password key contains
    none of the delimiters above, so it is one of the pieces read here.
    """
    if "://" not in setting:
        return False
    text = _after_the_scheme(setting)
    if text is None:
        text = setting
    return _userinfo_password_at(text) is not None or _query_names_a_password(text)


def masked(setting: str) -> str:
    """``setting``, safe to print. Never raises and never repeats a password it could hold.

    * A plain path (no ``"://"``) is returned unchanged.
    * A URL whose scheme SQLAlchemy would not accept is shown as :data:`UNREADABLE`, none of its
      text copied: there is no reading of it to trust.
    * When :func:`carries_a_password` finds a userinfo password, everything from the first ``:``
      to the LAST ``@`` becomes ``:***@``. The last rather than the first: a raw ``@`` inside a
      password would otherwise show the rest of the password as the host.
    * A fragment (from the first ``#`` on) is shown as ``#***``, whatever it holds.
    * Outside the masked password and fragment (the username included), the text is cut at
      every ``&``, the one separator a query parser splits pairs on, so each part is at least one
      whole pair's value. Where a key that names a password starts inside a part - at its start,
      or after a ``/``, ``?``, ``;`` or whitespace - everything after that key's ``=`` up to the
      next ``&`` becomes ``***``, which is the whole value a parser would read however it cuts
      the text before it. A key with no value stays as it is, since there is nothing to hide.

    Everything else is copied as it was typed, percent-encoding included, so a value such as
    ``options=-c%20x%3Dy`` reads back exactly as written.
    """
    if "://" not in setting:
        return setting
    rest = _after_the_scheme(setting)
    if rest is None:
        return UNREADABLE
    scheme = setting[: len(setting) - len(rest) - len("://")]
    colon = _userinfo_password_at(rest)
    if colon is None:
        userinfo, after = "", rest
    else:
        userinfo, after = f"{_masked_pieces(rest[:colon])}:***@", rest[rest.rindex("@") + 1 :]
    before_fragment, hash_sign, _fragment = after.partition("#")
    return f"{scheme}://{userinfo}{_masked_pieces(before_fragment)}{'#***' if hash_sign else ''}"


def _after_the_scheme(setting: str) -> str | None:
    """The text after the first ``"://"``, or ``None`` when what stands before it is no scheme."""
    scheme, _, rest = setting.partition("://")
    return rest if _SCHEME.fullmatch(scheme) else None


def _userinfo_password_at(text: str) -> int | None:
    """Where the userinfo's ``:`` is when SQLAlchemy would read a password there, else ``None``.

    Its userinfo pattern is a username of anything but ``:`` and ``/``, then ``:``, then a
    password of anything but ``@``, then ``@``: a password exists exactly when the first ``:``
    precedes the first ``/`` and some ``@`` follows it.
    """
    colon = text.find(":")
    if colon < 0:
        return None
    slash = text.find("/")
    if 0 <= slash < colon:
        return None
    return colon if "@" in text[colon + 1 :] else None


def _query_names_a_password(text: str) -> bool:
    _, question_mark, query = text.partition("?")
    if not question_mark:
        return False
    return any(is_a_password_key(piece.partition("=")[0]) for piece in _PIECE_DELIMITERS.split(query))


def is_a_password_key(key: str) -> bool:
    """Whether a query key names a password, whether it arrives raw or already decoded.

    Decoded the way a query parser decodes it (``+`` and percent-escapes), then stripped and
    lowercased. A key a parser has already decoded is decoded once more here, which can only
    widen what counts, never narrow it.
    """
    return unquote_plus(key).strip().lower() in PASSWORD_QUERY_KEYS


def _masked_pieces(text: str) -> str:
    """``text`` with every password-keyed value replaced up to the next ``&``."""
    return "&".join(_masked_part(part) for part in text.split("&"))


def _masked_part(part: str) -> str:
    """One ``&``-separated part, cut from the first password key that starts in it and has a value."""
    for match in _KEY_START.finditer(part):
        start = match.end()
        equals = part.find("=", start)
        if equals < 0:
            return part
        if is_a_password_key(part[start:equals]) and part[equals + 1 :].strip():
            return f"{part[: equals + 1]}***"
    return part
