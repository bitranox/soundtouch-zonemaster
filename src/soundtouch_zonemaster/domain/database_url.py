"""Whether a database setting carries a password, and how to show one without it.

A database setting is written into config files, ``--json`` envelopes and logs, so a password
inside it must never be echoed - the password is its own setting, ``database.password``, or libpq
reads one from ``~/.pgpass`` (or the file ``PGPASSFILE`` names). A password can arrive in a URL two
ways: in a URL's own userinfo, or as one of
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

**Showing a setting is all or nothing.** :func:`masked` never cuts a password out and keeps the
rest of the URL readable: where a password ends depends on how a parser reads the text, so a cut
cannot be proved to remove all of it. A setting without a password is shown as typed; one with a
password is shown as its scheme alone.
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
"""What :func:`masked` shows for a URL carrying a password whose scheme it cannot read."""

_SCHEME = re.compile(r"[\w+]+")
"""A scheme SQLAlchemy accepts. Anything else before ``"://"`` means the whole string is unread."""

_SHOWN_SCHEME = re.compile(r"[A-Za-z0-9+.-]+")
"""A scheme :func:`masked` may repeat: ASCII letters, digits, ``+``, ``-`` and ``.`` only."""

_PIECE_DELIMITERS = re.compile(r"[?&;\s]")
"""Where a query key can start. Wider than the ``&`` a query parser splits on, so no key it finds
can be hidden inside a piece this module reads as one. Not ``/``: a key never starts after one, and
a path such as ``passfile=/run/secrets/password`` would otherwise read as a bare password key."""


def carries_a_password(setting: str) -> bool:
    """Whether ``setting`` is a URL whose userinfo or query carries a password. Never raises.

    Read from the text after the first ``"://"`` (the whole setting when what stands before it is
    not a scheme SQLAlchemy accepts, which fails closed rather than skipping the text):

    * **userinfo**: SQLAlchemy reads a password exactly when the first ``:`` comes before any
      ``/`` and an ``@`` follows it anywhere later - the password may itself hold ``/``, ``?``
      or ``#``. That condition is checked as it stands. It also means a raw ``@`` in a query value
      after a ``:port`` is read as userinfo by SQLAlchemy itself, so such a URL is refused;
      percent-encode it as ``%40``.
    * **query**: everything after the first ``?``, fragment included, cut at every ``?``, ``&``,
      ``;`` and whitespace; a piece whose key (up to its first ``=``) decodes to one of
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
    return _userinfo_carries_a_password(text) or _query_names_a_password(text)


def masked(setting: str) -> str:
    """``setting``, safe to print. Never raises and never repeats a password it could hold.

    * When :func:`carries_a_password` finds none, the setting is returned exactly as typed. That
      rule is no looser than the parser the store hands the setting to, so there is nothing in it
      to hide. A plain path (no ``"://"``) is always this case.
    * When it finds one, nothing after the scheme is shown: the result is ``<scheme>://***``, or
      :data:`UNREADABLE` when the scheme is anything but ASCII letters, digits, ``+``, ``-`` and
      ``.``. Not one character of the text after ``"://"`` is copied, so no reading of where the
      password ends can put a piece of it on screen.
    """
    if not carries_a_password(setting):
        return setting
    scheme = setting.partition("://")[0]
    return f"{scheme}://***" if _SHOWN_SCHEME.fullmatch(scheme) else UNREADABLE


def _after_the_scheme(setting: str) -> str | None:
    """The text after the first ``"://"``, or ``None`` when what stands before it is no scheme."""
    scheme, _, rest = setting.partition("://")
    return rest if _SCHEME.fullmatch(scheme) else None


def _userinfo_carries_a_password(text: str) -> bool:
    """Whether SQLAlchemy would read a userinfo password out of ``text`` (the part after ``"://"``).

    Its userinfo pattern is a username of anything but ``:`` and ``/``, then ``:``, then a
    password of anything but ``@``, then ``@``: a password exists exactly when the first ``:``
    precedes the first ``/`` and some ``@`` follows it.
    """
    colon = text.find(":")
    if colon < 0:
        return False
    slash = text.find("/")
    if 0 <= slash < colon:
        return False
    return "@" in text[colon + 1 :]


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
