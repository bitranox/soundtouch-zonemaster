"""The password rule, held against the parser that actually hands a database URL to the driver.

``domain/database_url.py`` reads a setting as text, because the domain may not import SQLAlchemy.
The store then hands the setting to SQLAlchemy's ``make_url``, and what that parser reads out of
it - its ``password`` and its query keys - is what psycopg receives. A rule that reads a URL
differently from that parser can pass a password the driver will use, so this file uses
SQLAlchemy as the ORACLE: for every row where ``make_url`` yields a password or a password-shaped
query key, the domain rule must say so, and the store and the boundary must both refuse.

The oracle compares query keys after stripping whitespace and lowercasing, which is wider than
SQLAlchemy itself: psycopg builds a libpq conninfo string from the keys, and libpq skips the
whitespace around a keyword, so a key that decodes to ``" password"`` still sets the password.

Every row also carries the fake secrets its text holds, and no display (``masked``, the name
every store message starts with) and no refusal message may contain any of them - including on
rows the oracle cannot parse at all.
The ``oracle`` column is asserted too, so a row meant to exercise a password SQLAlchemy reads
cannot silently stop exercising one.
"""

from __future__ import annotations

import hashlib
import re

import pytest
from nothing_typed import NOTHING_TYPED
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from soundtouch_zonemaster.adapters.cli.boundary import parse_service_options
from soundtouch_zonemaster.adapters.files.house_db import database_url
from soundtouch_zonemaster.adapters.files.house_store import SqlHouseStore
from soundtouch_zonemaster.application.errors import StoreError
from soundtouch_zonemaster.application.outcome import OptionsError
from soundtouch_zonemaster.domain.database_url import carries_a_password, masked

_ORACLE_KEYS = frozenset({"password", "sslpassword"})
"""Written out rather than imported, so the oracle does not shrink if the domain's set does."""

PG = "postgresql+psycopg://"

ROWS = [
    # (setting, fake secrets its text holds, does SQLAlchemy read a password out of it)
    pytest.param(f"{PG}zm@db.example/zm?a=1#&password=TOPSECRET", ("TOPSECRET",), True, id="hash-then-query-key"),
    pytest.param(f"{PG}zm:TOPSECRET@[bad/zm", ("TOPSECRET",), True, id="unbalanced-bracket-host"),
    pytest.param(f"{PG}zm:TOPSECRET@[::1/zm", ("TOPSECRET",), False, id="unbalanced-ipv6-sqlalchemy-refuses"),
    pytest.param(f"{PG}zm:TOPSECRET@db.example/zm", ("TOPSECRET",), True, id="userinfo"),
    pytest.param(f"{PG}zm:@db.example/zm", (), True, id="userinfo-empty-password"),
    pytest.param(f"{PG}zm:TOP@SECRET@db.example/zm", ("TOP", "SECRET"), True, id="raw-at-in-password"),
    pytest.param(f"{PG}zm:TOP/SECRET@db.example/zm", ("TOP", "SECRET"), True, id="slash-in-password"),
    pytest.param(f"{PG}zm:TOP?SECRET@db.example/zm", ("TOP", "SECRET"), True, id="question-mark-in-password"),
    pytest.param(f"{PG}zm:TOP#SECRET@db.example/zm", ("TOP", "SECRET"), True, id="hash-in-password"),
    pytest.param(f"{PG}zm:TOP%40SECRET@db.example/zm", ("TOP", "SECRET"), True, id="encoded-at-in-password"),
    pytest.param(f"{PG}zm?a:TOPSECRET@db.example/zm", ("TOPSECRET",), True, id="question-mark-in-username"),
    pytest.param(f"{PG}zm?x@db.example/zm?password=TOPSECRET", ("TOPSECRET",), True, id="second-question-mark"),
    pytest.param(f"{PG}zm:TOPSECRET@[::1]:5432/zm", ("TOPSECRET",), True, id="ipv6-host"),
    pytest.param(f"{PG}zm@[::1]:5432/zm", (), False, id="ipv6-host-no-password"),
    pytest.param(f"{PG}zm:TOPSECRET@db.example:54x2/zm", ("TOPSECRET",), False, id="bad-port"),
    pytest.param(f"{PG}zm@db.example/zm?password=TOPSECRET", ("TOPSECRET",), True, id="query-key"),
    pytest.param(f"{PG}zm@db.example/zm?PassWord=TOPSECRET", ("TOPSECRET",), True, id="query-key-mixed-case"),
    pytest.param(f"{PG}zm@db.example/zm?sslpassword=TOPSECRET", ("TOPSECRET",), True, id="sslpassword"),
    pytest.param(f"{PG}zm@db.example/zm?SSLPASSWORD=TOPSECRET", ("TOPSECRET",), True, id="sslpassword-upper"),
    pytest.param(f"{PG}zm@db.example/zm?pass%77ord=TOPSECRET", ("TOPSECRET",), True, id="percent-encoded-key"),
    pytest.param(f"{PG}zm@db.example/zm?%20password=TOPSECRET", ("TOPSECRET",), True, id="encoded-space-key"),
    pytest.param(f"{PG}zm@db.example/zm?+password=TOPSECRET", ("TOPSECRET",), True, id="plus-space-key"),
    pytest.param(f"{PG}zm@db.example/zm?password%20=TOPSECRET", ("TOPSECRET",), True, id="trailing-space-key"),
    pytest.param(f"{PG}zm@db.example/zm?password=pw1&password=TOPSECRET", ("pw1", "TOPSECRET"), True, id="repeat"),
    pytest.param(f"{PG}zm@db.example/zm?a=1&PASSWORD=TOPSECRET&b=2", ("TOPSECRET",), True, id="key-in-the-middle"),
    pytest.param(f"{PG}zm@db.example/zm?a=1;password=TOPSECRET", ("TOPSECRET",), False, id="semicolon"),
    pytest.param(f"{PG}zm@db.example/zm?password=pw1;TOPSECRET", ("TOPSECRET",), True, id="semicolon-in-value"),
    pytest.param(f"{PG}zm@db.example/zm?password=pw1/TOPSECRET", ("TOPSECRET",), True, id="slash-in-value"),
    pytest.param(f"{PG}zm@db.example/zm?pass%77ord=;TOPSECRET", ("TOPSECRET",), True, id="value-starts-with-semicolon"),
    pytest.param(f"{PG}zm@db.example/zm?sslpassword ==TOPSECRET", ("TOPSECRET",), True, id="space-before-equals"),
    pytest.param(f"{PG}zm@db.example/zm?a=1\npassword=TOPSECRET", ("TOPSECRET",), False, id="newline"),
    pytest.param(f"{PG}zm@db.example/zm?password=", (), False, id="empty-value"),
    pytest.param(f"{PG}zm@db.example/zm?password", (), False, id="bare-key"),
    pytest.param(f"{PG}zm@db.example/zm?a=1#TOPSECRET", ("TOPSECRET",), False, id="fragment"),
    pytest.param(f"{PG}zm@db.example/zm#TOPSECRET", ("TOPSECRET",), False, id="fragment-after-database"),
    pytest.param(f" {PG}zm:TOPSECRET@db.example/zm", ("TOPSECRET",), False, id="leading-space-scheme"),
    pytest.param("zm:TOPSECRET@x://db.example/zm", ("TOPSECRET",), False, id="unreadable-scheme"),
    pytest.param("sqlite:///srv/zm.sqlite?password=TOPSECRET", ("TOPSECRET",), True, id="sqlite-query-key"),
    pytest.param("sqlite:///srv/zm.sqlite?passfile=/etc/pgpass", (), False, id="sqlite-passfile"),
    pytest.param(f"{PG}zm@db.example/zm?passfile=/etc/pgpass", (), False, id="passfile"),
    pytest.param(f"{PG}zm@db.example/zm", (), False, id="no-password"),
    # A plain path is not a URL: the store opens it as a SQLite file, and no reader takes a
    # password out of it, so nothing in its text is a secret to anybody.
    pytest.param("zm:pw@db.example/zm", (), False, id="no-scheme-is-a-path"),
]


def _sqlalchemy_reads_a_password(setting: str) -> bool:
    """The oracle: what the parser that feeds the driver takes out of this setting."""
    if "://" not in setting:
        return False  # the store never parses a plain path as a URL
    try:
        url = make_url(setting)
    except (ArgumentError, ValueError):
        return False
    return url.password is not None or any(key.strip().lower() in _ORACLE_KEYS for key in url.query)


def _quiet(_kind: str, _text: str) -> None:
    return None


def _store_refusal(setting: str) -> str | None:
    try:
        database_url(setting)
    except StoreError as exc:
        return str(exc)
    return None


def _boundary_refusal(setting: str) -> str | None:
    try:
        parse_service_options(bind_ip="127.0.0.1", database=setting, configured={}, **NOTHING_TYPED)
    except OptionsError as exc:
        return str(exc)
    return None


@pytest.mark.parametrize(("setting", "secrets", "oracle"), ROWS)
def test_the_rule_is_no_looser_than_sqlalchemy(setting: str, secrets: tuple[str, ...], oracle: bool) -> None:
    del secrets
    assert _sqlalchemy_reads_a_password(setting) is oracle, "the row no longer exercises what its id says"
    if not oracle:
        return
    assert carries_a_password(setting), "SQLAlchemy reads a password here and the domain rule does not"
    assert _store_refusal(setting) is not None, "SQLAlchemy reads a password here and the store accepts it"
    assert _boundary_refusal(setting) is not None, "SQLAlchemy reads a password here and the boundary accepts it"


@pytest.mark.parametrize(("setting", "secrets", "oracle"), ROWS)
def test_no_display_or_refusal_repeats_a_secret(setting: str, secrets: tuple[str, ...], oracle: bool) -> None:
    del oracle
    shown = {
        "masked": masked(setting),
        "store refusal": _store_refusal(setting) or "",
        "boundary refusal": _boundary_refusal(setting) or "",
        "store name": SqlHouseStore(setting, log=_quiet).where,
    }
    for where, text in shown.items():
        leaked = [secret for secret in secrets if secret in text]
        assert not leaked, f"{where} repeats {leaked}"


@pytest.mark.parametrize(
    "setting",
    [
        pytest.param(f"{PG}zm:TOPSECRET@[::1/zm", id="unbalanced-ipv6"),
        pytest.param(f"{PG}zm:TOPSECRET@db.example:54x2/zm", id="bad-port"),
        pytest.param(f"{PG}zm@db.example/zm?a=1;password=TOPSECRET", id="semicolon"),
        pytest.param(f"{PG}zm@db.example/zm?a=1\npassword=TOPSECRET", id="newline"),
        pytest.param(f"{PG}zm@db.example/zm?password=", id="empty-value"),
        pytest.param(f" {PG}zm:TOPSECRET@db.example/zm", id="leading-space-scheme"),
        pytest.param("zm:TOPSECRET@x://db.example/zm", id="unreadable-scheme"),
    ],
)
def test_the_rule_fails_closed_where_sqlalchemy_reads_no_password(setting: str) -> None:
    """Stricter than the oracle on purpose: each of these holds a password-shaped piece that
    SQLAlchemy happens not to read (it refuses the URL, or reads the piece as part of another
    value). The rule reads text and refuses them anyway rather than betting on which parser a
    future version of the store hands them to."""
    assert carries_a_password(setting)


_FUZZ_ATOMS = (
    "zm", "db", ":", "@", "/", "?", "#", "&", ";", "=", "[", "]", " ", "+", "%20", "%40", "%3A", "\n", "\t",
    "password", "PassWord", "sslpassword", "pass%77ord", "SECRET", "5432", "::1", ".", "-", "%", "x",
)  # fmt: skip


def test_a_seeded_fuzz_finds_no_url_the_rule_or_the_mask_gets_wrong() -> None:
    """The table above is what somebody thought of; this is what they did not. Each generated URL
    numbers its secrets (``S0Z``, ``S1Z``, ...), so a marker SQLAlchemy reads as part of a password
    and that still appears in ``masked`` is a leak rather than the same text shown elsewhere.
    Each URL is drawn from a hash of its own index, so a failure reproduces."""
    problems: list[str] = []
    for index in range(5000):
        setting = _generated(index)
        read = _sqlalchemy_read(setting)
        if read is None:
            continue
        if read and not carries_a_password(setting):
            problems.append(f"looser: {setting!r}")
        shown = masked(setting)
        problems.extend(f"leak: {setting!r}" for marker in re.findall(r"S\d+Z", " ".join(read)) if marker in shown)
    assert problems == []


def _generated(index: int) -> str:
    """URL number ``index``: its first hash byte picks a length of 1 to 12 atoms, the rest pick them."""
    digest = hashlib.blake2b(index.to_bytes(4, "big"), digest_size=13).digest()
    atoms = [_FUZZ_ATOMS[byte % len(_FUZZ_ATOMS)] for byte in digest[1 : 2 + digest[0] % 12]]
    return PG + "".join(f"S{n}Z" if atom == "SECRET" else atom for n, atom in enumerate(atoms))


def _sqlalchemy_read(setting: str) -> list[str] | None:
    """Every password SQLAlchemy reads out of ``setting`` (userinfo and query), or ``None`` if it cannot parse it."""
    try:
        url = make_url(setting)
    except (ArgumentError, ValueError):
        return None
    read = [] if url.password is None else [url.password]
    for key, value in url.query.items():
        if key.strip().lower() in _ORACLE_KEYS:
            read.extend([value] if isinstance(value, str) else value)
    return read
