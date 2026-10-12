"""Digit-only .env values reach every text setting as the text the operator wrote.

lib_layered_config's dotenv and environment layers turn an unquoted value into an int or float
whenever that number reads back as the same text (``123456789012`` stays ``123456789012``). A
pydantic ``str`` field is strict about type, so the records that validate the settings must turn
such a number back into its digits; otherwise an ordinary ``.env`` line such as
``ZONE__DEVICE_ID=123456789012`` (twelve hex digits that happen to contain no letter) stops the
service at startup.

These tests load a real ``.env`` through the real loader, so a change in the library's coercion
shows up here rather than on a host. The database password and URL are not covered: they refuse a
number on purpose (``tests/test_database_password.py``), and one case below keeps it that way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from nothing_typed import NOTHING_TYPED

from soundtouch_zonemaster.adapters.cli.boundary import configured_settings, parse_service_options
from soundtouch_zonemaster.adapters.config.loader import clear_config_cache, get_config
from soundtouch_zonemaster.application.outcome import OptionsError

if TYPE_CHECKING:
    from pathlib import Path

    from lib_layered_config import Config

_REQUIRED = "ZONE__BIND_IP=10.0.0.1\nZONE__DEVICE_ID=689E19123456\nDATABASE__URL=house.sqlite\n"
"""The settings a service cannot start without, so a case adds only the line it is about."""


def _load_dotenv(directory: Path, lines: str) -> Config:
    dotenv = directory / ".env"
    dotenv.write_text(lines, encoding="utf-8")
    clear_config_cache()
    return get_config(dotenv_path=str(dotenv))


@pytest.mark.os_agnostic
def test_a_digit_only_device_id_is_read_as_its_digits(tmp_path: Path) -> None:
    config = _load_dotenv(tmp_path, _REQUIRED.replace("689E19123456", "123456789012"))
    # Premise: the library hands the value over as a number. Without it the test proves nothing.
    assert isinstance(config.get("zone.device_id"), int)

    options = parse_service_options(
        bind_ip=None, database=None, configured=configured_settings(config), **NOTHING_TYPED
    )

    assert options.device_id == "123456789012"


@pytest.mark.os_agnostic
def test_a_digit_only_mpd_host_is_read_as_its_digits(tmp_path: Path) -> None:
    config = _load_dotenv(tmp_path, _REQUIRED + "MPD__HOST=127\n")
    assert isinstance(config.get("mpd.host"), int)

    options = parse_service_options(
        bind_ip=None, database=None, configured=configured_settings(config), **NOTHING_TYPED
    )

    assert options.mpd_host == "127"


@pytest.mark.os_agnostic
def test_a_numeric_setting_still_reads_as_a_number(tmp_path: Path) -> None:
    config = _load_dotenv(tmp_path, _REQUIRED + "MPD__PORT=6601\n")

    options = parse_service_options(
        bind_ip=None, database=None, configured=configured_settings(config), **NOTHING_TYPED
    )

    assert options.mpd_port == 6601


@pytest.mark.os_agnostic
def test_a_digit_only_database_password_is_still_refused(tmp_path: Path) -> None:
    config = _load_dotenv(tmp_path, _REQUIRED + "DATABASE__PASSWORD=8675309\n")

    with pytest.raises(OptionsError, match=r"database\.password"):
        parse_service_options(bind_ip=None, database=None, configured=configured_settings(config), **NOTHING_TYPED)


@pytest.mark.os_agnostic
def test_a_device_id_that_is_not_twelve_hex_digits_is_still_refused(tmp_path: Path) -> None:
    config = _load_dotenv(tmp_path, _REQUIRED.replace("689E19123456", "12345"))

    with pytest.raises(OptionsError, match="12 hex digits"):
        parse_service_options(bind_ip=None, database=None, configured=configured_settings(config), **NOTHING_TYPED)


@pytest.mark.os_agnostic
def test_a_digit_only_registry_url_is_still_refused(tmp_path: Path) -> None:
    config = _load_dotenv(tmp_path, _REQUIRED + "REGISTRY__URL=8090\n")
    assert isinstance(config.get("registry.url"), int)

    with pytest.raises(OptionsError, match="registry_url"):
        parse_service_options(bind_ip=None, database=None, configured=configured_settings(config), **NOTHING_TYPED)
