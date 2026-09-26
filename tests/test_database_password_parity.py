"""The boundary's password check and the store's own agree, case by case.

Two independent readers exist for the same rule: the boundary (``adapters/cli/boundary.py``) reads
a database setting with the stdlib so that ``adapters/cli`` need not import SQLAlchemy, and the
store (``adapters/files/house_db.py``) reads it with SQLAlchemy's own parser, which it already
depends on. A rule written twice can drift without either half's own tests noticing, so this feeds
both the same inputs and requires the same verdict - except on the one input where they may
legitimately differ (a URL too malformed for SQLAlchemy to parse at all), where both must still
refuse, each for its own reason.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from soundtouch_zonemaster.adapters.cli.boundary import parse_service_options
from soundtouch_zonemaster.adapters.files.house_db import database_url
from soundtouch_zonemaster.application.errors import StoreError
from soundtouch_zonemaster.application.outcome import ExitCode, OptionsError

if TYPE_CHECKING:
    from pathlib import Path

_NOTHING_TYPED: dict[str, Any] = {
    "device_id": None,
    "channel_file": None,
    "switch_file": None,
    "state_file": None,
    "registry_url": None,
    "allow_console": (),
    "unreachable_timeout_s": None,
    "dial_window_s": None,
    "mpd_host": None,
    "mpd_port": None,
    "mpd_rewind_s": None,
}


def _boundary_refuses(database: str) -> bool:
    """Whether ``parse_service_options`` refuses this database setting, for this reason alone.

    ``bind_ip`` is a fixed valid value and every other setting is left at its default, so the only
    thing that can make this refuse is the ``database`` field's own validator.
    """
    try:
        parse_service_options(bind_ip="127.0.0.1", database=database, configured={}, **_NOTHING_TYPED)
    except OptionsError as exc:
        assert exc.exit_code == ExitCode.REFUSED
        return True
    return False


def _store_refuses(database: str) -> bool:
    try:
        database_url(database)
    except StoreError:
        return True
    return False


@pytest.mark.parametrize(
    "database",
    [
        pytest.param("postgresql+psycopg://zm:s3cret@db.example/zm", id="userinfo-password"),
        pytest.param("postgresql+psycopg://zm:@db.example/zm", id="empty-password"),
        pytest.param("postgresql+psycopg://zm@db.example/zm?password=s3cret", id="query-key-lowercase"),
        pytest.param("postgresql+psycopg://zm@db.example/zm?PassWord=s3cret", id="query-key-mixed-case"),
        pytest.param("postgresql+psycopg://zm@db.example/zm?sslpassword=s3cret", id="sslpassword-lowercase"),
        pytest.param("postgresql+psycopg://zm@db.example/zm?SslPassword=s3cret", id="sslpassword-mixed-case"),
        pytest.param("postgresql+psycopg://zm@db.example/zm", id="no-password"),
        pytest.param("postgresql+psycopg://zm@db.example/zm?passfile=/etc/pgpass", id="passfile-is-not-a-password"),
    ],
)
def test_the_boundary_and_the_store_agree_on_urls(database: str) -> None:
    assert _boundary_refuses(database) is _store_refuses(database)


def test_the_boundary_and_the_store_agree_a_plain_path_carries_no_password(tmp_path: Path) -> None:
    """A plain path never has a userinfo or a query, so nothing here can be a password - checked
    with a directory that exists, so the boundary's own directory check does not also fire and
    make this look like disagreement over something the password rule never decides."""
    database = str(tmp_path / "zonemaster.sqlite")
    assert _boundary_refuses(database) is False
    assert _store_refuses(database) is False


def test_a_bad_port_carrying_a_password_is_refused_by_both_for_different_reasons() -> None:
    """Where the two legitimately differ. The boundary reads with ``urlsplit``, which still finds
    the userinfo on a URL this malformed, and refuses it as carrying a password; the store's
    SQLAlchemy parser refuses the same URL outright as unparsable and never gets far enough to see
    the password. Both refuse - the constraint this test pins is "both", not "for the same reason".
    """
    database = "postgresql+psycopg://zm:s3cret@db.example:54x2/zm"
    assert _boundary_refuses(database)
    assert _store_refuses(database)


def test_a_password_passed_as_sslpassword_is_refused_and_never_echoed() -> None:
    """The regression this parity test exists for: a ``?sslpassword=`` value is refused, exactly
    like ``?password=``, and the secret never surfaces in either reader's own refusal message."""
    database = "postgresql+psycopg://zm@db.example/zm?sslpassword=TOPSECRET"
    assert _boundary_refuses(database)
    with pytest.raises(OptionsError) as boundary_caught:
        parse_service_options(bind_ip="127.0.0.1", database=database, configured={}, **_NOTHING_TYPED)
    assert "TOPSECRET" not in str(boundary_caught.value)
    with pytest.raises(StoreError) as store_caught:
        database_url(database)
    assert "TOPSECRET" not in str(store_caught.value)
