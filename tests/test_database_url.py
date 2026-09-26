"""``domain/database_url.py`` on its own: the one rule the three adapters read instead of a copy.

``tests/test_database_password_parity.py`` still drives the boundary and the store end to end and
checks they agree - that is what proves the WIRING, not just the rule, reaches this module. What
belongs here is the rule itself, including the shapes neither adapter test happens to exercise
(masking, rather than only refusing; a malformed port that a bare ``.port`` access would raise on).
"""

from __future__ import annotations

import pytest

from soundtouch_zonemaster.domain.database_url import PASSWORD_QUERY_KEYS, UNREADABLE, carries_a_password, masked


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


def test_masked_shows_only_the_scheme_of_a_url_with_a_userinfo_password() -> None:
    shown = masked("postgresql+psycopg://zm:s3cret@db.example/zm")
    assert "s3cret" not in shown
    assert shown == "postgresql+psycopg://***"


def test_masked_shows_only_the_scheme_of_a_url_with_a_password_query_key_in_any_case() -> None:
    shown = masked("postgresql+psycopg://zm@db.example/zm?SslPassword=TOPSECRET")
    assert "TOPSECRET" not in shown
    assert shown == "postgresql+psycopg://***"


def test_masked_leaves_a_non_password_query_value_alone() -> None:
    shown = masked("postgresql+psycopg://zm@db.example/zm?passfile=/etc/pgpass")
    assert shown == "postgresql+psycopg://zm@db.example/zm?passfile=/etc/pgpass"


def test_masked_does_not_raise_on_a_password_beside_a_port_that_is_not_a_number() -> None:
    """A port that is not a number is text like any other here: the mask never converts it, so a
    URL SQLAlchemy would refuse outright for its port is still masked rather than crashed on."""
    shown = masked("postgresql+psycopg://zm:s3cret@db.example:54x2/zm")
    assert shown == "postgresql+psycopg://***"


def test_masked_never_repeats_the_password_of_a_url_with_an_unbalanced_bracket() -> None:
    malformed = "postgresql+psycopg://zm:s3cret@[not-a-valid-host"
    assert "s3cret" not in masked(malformed)


def test_masked_shows_a_url_with_a_password_whose_scheme_it_cannot_read_as_a_placeholder() -> None:
    assert masked(" postgresql+psycopg://zm:s3cret@db.example/zm") == UNREADABLE


def test_masked_shows_a_password_free_url_as_typed_whatever_its_scheme() -> None:
    """Nothing in it is a password to any reader, so there is nothing to replace with a placeholder."""
    setting = " postgresql+psycopg://db.example/zm"
    assert not carries_a_password(setting)
    assert masked(setting) == setting


def test_masked_shows_a_fragment_of_a_password_free_url_as_typed() -> None:
    """SQLAlchemy reads a fragment as part of a query value or the database name, never as a password."""
    setting = "postgresql+psycopg://zm@db.example/zm?a=1#note"
    assert masked(setting) == setting


def test_masked_copies_every_other_value_as_it_was_typed() -> None:
    """Percent-encoding is kept, so a value reads back exactly as it was written."""
    setting = "postgresql+psycopg://zm@db.example/zm?options=-c%20x%3Dy&sslmode=require"
    assert masked(setting) == setting


@pytest.mark.parametrize(
    "setting",
    [
        pytest.param("postgresql+psycopg://zm@db.example/zm?password=", id="empty-value"),
        pytest.param("postgresql+psycopg://zm@db.example/zm?password", id="bare-key"),
    ],
)
def test_masked_shows_only_the_scheme_of_a_password_key_with_no_value(setting: str) -> None:
    """The rule refuses these (it fails closed), and a setting it refuses is shown as its scheme
    alone: there is no second rule for which refused settings may be shown in full."""
    assert carries_a_password(setting)
    assert masked(setting) == "postgresql+psycopg://***"


def test_masked_shows_only_the_scheme_of_a_password_holding_a_raw_at_sign() -> None:
    assert masked("postgresql+psycopg://zm:TOP@SECRET@db.example/zm") == "postgresql+psycopg://***"


def test_password_query_keys_are_lowercase_and_stable() -> None:
    assert frozenset({"password", "sslpassword"}) == PASSWORD_QUERY_KEYS


@pytest.mark.parametrize(
    ("setting", "secrets"),
    [
        pytest.param("postgresql+psycopg://zm@db.example:5432/zm?password=TOP@SECRET", ("TOP", "SECRET"), id="port"),
        pytest.param(
            "postgresql+psycopg://zm:pw1@db.example/zm?password=TOP@SECRET", ("pw1", "TOP", "SECRET"), id="userinfo"
        ),
        pytest.param(
            "postgresql+psycopg://zm@db.example:5432/zm?sslpassword=TOP@SECRET&sslmode=require",
            ("TOP", "SECRET"),
            id="more-after",
        ),
    ],
)
def test_masked_shows_nothing_after_the_scheme_of_a_url_carrying_a_password(
    setting: str, secrets: tuple[str, ...]
) -> None:
    """A raw ``@`` inside a query value, alone or beside a userinfo password: no part of the URL
    after its scheme is shown, so no reading of where the password ends can put a piece of it on
    screen. The secrets are the ones this test wrote, not any parser's reading of them."""
    shown = masked(setting)
    assert shown == "postgresql+psycopg://***"
    assert [secret for secret in secrets if secret in shown] == []
