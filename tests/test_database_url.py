"""``domain/database_url.py`` on its own: the one rule the three adapters read instead of a copy.

``tests/test_database_password_parity.py`` still drives the boundary and the store end to end and
checks they agree - that is what proves the WIRING, not just the rule, reaches this module. What
belongs here is the rule itself, including the shapes neither adapter test happens to exercise
(masking, rather than only refusing; a malformed port that a bare ``.port`` access would raise on).
"""

from __future__ import annotations

import pytest

from soundtouch_zonemaster.domain.database_url import PASSWORD_QUERY_KEYS, carries_a_password, masked


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        pytest.param("postgresql+psycopg://zm:s3cret@db.example/zm", True, id="userinfo-password"),
        pytest.param("postgresql+psycopg://zm:@db.example/zm", True, id="empty-password"),
        pytest.param("postgresql+psycopg://zm@db.example/zm?password=s3cret", True, id="query-key-lowercase"),
        pytest.param("postgresql+psycopg://zm@db.example/zm?PassWord=s3cret", True, id="query-key-mixed-case"),
        pytest.param("postgresql+psycopg://zm@db.example/zm?sslpassword=s3cret", True, id="sslpassword-lowercase"),
        pytest.param("postgresql+psycopg://zm@db.example/zm?SslPassword=s3cret", True, id="sslpassword-mixed-case"),
        pytest.param("postgresql+psycopg://zm@db.example/zm", False, id="no-password"),
        pytest.param(
            "postgresql+psycopg://zm@db.example/zm?passfile=/etc/pgpass", False, id="passfile-is-not-a-password"
        ),
        pytest.param("/var/lib/zonemaster/zonemaster.sqlite", False, id="plain-path"),
        pytest.param("postgresql+psycopg://zm:s3cret@db.example:54x2/zm", True, id="bad-port-still-seen"),
    ],
)
def test_carries_a_password(setting: str, expected: bool) -> None:
    assert carries_a_password(setting) is expected


def test_a_plain_path_is_not_a_url_and_is_returned_unmasked() -> None:
    path = "/var/lib/zonemaster/zonemaster.sqlite"
    assert masked(path) == path


def test_masked_hides_a_userinfo_password_but_keeps_the_rest_readable() -> None:
    shown = masked("postgresql+psycopg://zm:s3cret@db.example/zm")
    assert "s3cret" not in shown
    assert shown == "postgresql+psycopg://zm:***@db.example/zm"


def test_masked_hides_a_password_shaped_query_value_case_insensitively() -> None:
    shown = masked("postgresql+psycopg://zm@db.example/zm?SslPassword=TOPSECRET")
    assert "TOPSECRET" not in shown
    assert shown == "postgresql+psycopg://zm@db.example/zm?SslPassword=***"


def test_masked_leaves_a_non_password_query_value_alone() -> None:
    shown = masked("postgresql+psycopg://zm@db.example/zm?passfile=/etc/pgpass")
    assert shown == "postgresql+psycopg://zm@db.example/zm?passfile=/etc/pgpass"


def test_masked_does_not_raise_on_a_password_beside_a_port_that_is_not_a_number() -> None:
    """The regression this test guards: an earlier draft read the port through ``urlsplit``'s own
    ``.port`` property, which coerces it to an int and raises on ``54x2`` - exactly a URL this
    function still has to mask rather than crash on."""
    shown = masked("postgresql+psycopg://zm:s3cret@db.example:54x2/zm")
    assert "s3cret" not in shown
    assert "54x2" in shown, "only the password is masked; the rest of the netloc is unchanged"


def test_masked_returns_a_url_unchanged_that_it_cannot_even_split() -> None:
    malformed = "postgresql+psycopg://zm:s3cret@[not-a-valid-host"
    assert masked(malformed) == malformed


def test_password_query_keys_are_lowercase_and_stable() -> None:
    assert frozenset({"password", "sslpassword"}) == PASSWORD_QUERY_KEYS
