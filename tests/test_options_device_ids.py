"""A device id held by an option record is upper case however the record was built.

Both CLI boundaries fold a device id to upper case, but the records they build are also built
directly - by the composition root's tests, by a caller that is not a CLI - and membership compares
the master's own id with the ``stream_owner`` a speaker reports EXACTLY. A record that kept a
lower-case id would therefore read every box playing the house's stream as playing somebody
else's, and take none of them in. So the fold lives in the records themselves, through the one
helper every other surface uses (``domain/preferences.normalized_device_id``), and these build the
records the way a non-CLI caller would.
"""

from __future__ import annotations

import pytest

from soundtouch_zonemaster.application.options import Options, ServiceOptions
from soundtouch_zonemaster.application.outcome import OptionsError
from soundtouch_zonemaster.domain.enums import Encryption, JoinMode

LOWER = "aabbcc0000a1"
UPPER = "AABBCC0000A1"


def _prototype_options(device_id: str) -> Options:
    return Options(
        bind_ip="127.0.0.1",
        device_id=device_id,
        slaves=(),
        preset_from="127.0.0.1",
        preset=1,
        preset2=0,
        switch_after=0.0,
        duration=1.0,
        encryption=Encryption.NONE,
        late_slaves=(),
        join_after=0.0,
        join_mode=JoinMode.SCHEDULE,
        ignore_selects=False,
        registry_url="http://127.0.0.1:8000",
    )


def test_a_service_record_built_with_a_lower_case_device_id_holds_it_upper_case() -> None:
    options = ServiceOptions(bind_ip="127.0.0.1", device_id=LOWER, database="house.sqlite")

    assert options.device_id == UPPER


def test_a_service_record_built_with_lower_case_consoles_holds_them_upper_case() -> None:
    """The consoles are compared with the ids the registry reports, which are upper case, too."""
    options = ServiceOptions(
        bind_ip="127.0.0.1", device_id=UPPER, database="house.sqlite", consoles_allowed=("aabbcc0000a5",)
    )

    assert options.consoles_allowed == ("AABBCC0000A5",)


def test_a_prototype_record_built_with_a_lower_case_device_id_holds_it_upper_case() -> None:
    assert _prototype_options(LOWER).device_id == UPPER


@pytest.mark.parametrize("bad", ["", "AABBCC0000A", "AABBCC0000AG", "not-an-id"])
def test_a_record_still_refuses_what_is_not_a_device_id(bad: str) -> None:
    """The control: folding is not accepting. Twelve hex digits or nothing, in either record."""
    with pytest.raises(OptionsError, match="12 hex digits"):
        ServiceOptions(bind_ip="127.0.0.1", device_id=bad, database="house.sqlite")
    with pytest.raises(OptionsError, match="12 hex digits"):
        _prototype_options(bad)
