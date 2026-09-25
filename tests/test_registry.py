"""The speaker registry, against a server that answers the way AfterTouch answers.

The registry is the one thing M1 stands on: it is where the service learns that a speaker exists,
what it is called, where it is, and whether it is a speaker at all rather than the Lifestyle
console. The design records why it is read from the replacement service rather than discovered
again by us, and what was measured of its shape on 2026-09-06.

Nothing is monkeypatched. ``fetch_speakers`` takes a base URL, so a small server on loopback IS
the service next door, and the assertions are on the records that came out of real bytes. The
fixture beside this file carries the real SHAPE with invented values: the live response holds the
flat's addresses, MACs and serial numbers, and none of those has a redaction rule.

The two absence cases are here because they are the ones that will happen. The service is a
container that can be restarting while ours is not, and "it answered 404" and "nothing is
listening" are different sentences a person reading a log needs to be able to tell apart.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import pytest
from registry_double import DEVICES_FIXTURE as FIXTURE
from registry_double import FakeRegistry

from soundtouch_zonemaster.adapters.aftertouch.registry import fetch_speakers
from soundtouch_zonemaster.application.errors import RegistryError
from soundtouch_zonemaster.domain.enums import ProductCode

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator


@asynccontextmanager
async def _serving(body: str, *, status_line: str = "200 OK") -> AsyncGenerator[FakeRegistry, None]:
    """A context manager rather than a bare generator, so an assertion cannot end up outside it.

    Written the other way first, the two error tests asserted on a name bound inside the loop,
    which is a test that proves nothing if the body never runs.
    """
    service = FakeRegistry(body, status_line=status_line)
    await service.start()
    try:
        yield service
    finally:
        await service.stop()


@pytest.fixture
async def service() -> AsyncIterator[FakeRegistry]:
    async with _serving(FIXTURE.read_text(encoding="utf-8")) as one:
        yield one


async def test_the_device_list_becomes_records_with_the_fields_the_service_needs(service: FakeRegistry) -> None:
    speakers = await fetch_speakers(service.base_url)

    assert [s.device_id for s in speakers] == ["AABBCC000010", "AABBCC000011", "AABBCC000012"]
    first = speakers[0]
    assert first.name == "Bose Studio"
    assert first.ip == "192.0.2.10"
    assert first.mac == "AABBCC000010"
    assert first.account_id == "1000001"
    assert service.paths == ["/api/setup/devices"]


async def test_a_lifestyle_entry_is_a_console_and_a_soundtouch_one_is_not(service: FakeRegistry) -> None:
    by_id = {s.device_id: s for s in await fetch_speakers(service.base_url)}

    assert by_id["AABBCC000012"].product_code == ProductCode.LIFESTYLE
    assert by_id["AABBCC000012"].is_console
    assert by_id["AABBCC000010"].product_code == ProductCode.SOUNDTOUCH
    assert not by_id["AABBCC000010"].is_console


async def test_a_product_code_nobody_has_seen_is_carried_rather_than_refused() -> None:
    """A model absent from the enum must not take the registry down, and must not read as a console.

    The enum is what one deployment answered on one day. Every other fixed value set in this
    project keeps the wire string and compares against its members for exactly this reason.
    """
    entries = json.loads(FIXTURE.read_text(encoding="utf-8"))
    entries[0]["product_code"] = "SoundTouchPassport"

    async with _serving(json.dumps(entries)) as service:
        speakers = await fetch_speakers(service.base_url)

    assert speakers[0].product_code == "SoundTouchPassport"
    assert not speakers[0].is_console


async def test_an_entry_missing_a_required_field_is_skipped_and_the_others_survive() -> None:
    """One half-discovered device must not cost the house every speaker.

    Measured on the live registry 2026-09-19: a non-Bose device answered SSDP, AfterTouch
    wrote a placeholder entry for it carrying no ``mac_address``, and the all-or-nothing parse
    this replaces discarded the real speakers - so the zone could hold nobody for as long as
    that device stayed on the network. ANY UPnP device on the LAN can produce such an entry, which
    is why tolerating it is the rule rather than an indulgence.
    """
    entries = json.loads(FIXTURE.read_text(encoding="utf-8"))
    del entries[1]["ip_address"]
    lines: list[tuple[str, str]] = []

    async with _serving(json.dumps(entries)) as service:
        speakers = await fetch_speakers(service.base_url, log=lambda kind, text: lines.append((kind, text)))

    assert [s.device_id for s in speakers] == ["AABBCC000010", "AABBCC000012"]
    assert any("ip_address" in text and "AABBCC000011" in text for _, text in lines), lines


async def test_a_skipped_entry_needs_no_log_to_be_skipped() -> None:
    """``log`` is optional, so the tolerance does not depend on the caller having wired one."""
    entries = json.loads(FIXTURE.read_text(encoding="utf-8"))
    del entries[1]["ip_address"]

    async with _serving(json.dumps(entries)) as service:
        speakers = await fetch_speakers(service.base_url)

    assert [s.device_id for s in speakers] == ["AABBCC000010", "AABBCC000012"]


async def test_a_list_in_which_nothing_validates_is_refused_by_name() -> None:
    """The schema-change case stays loud, because skipping every entry is a silently empty house.

    This is the whole reason the rule is "skip what is unusable" and not "never refuse on
    content": if AfterTouch renames a field, every entry fails at once, and a service that
    reported that as "no speakers found" would leave a quiet flat with nothing in the log naming
    a cause.
    """
    entries = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for entry in entries:
        del entry["ip_address"]

    async with _serving(json.dumps(entries)) as service:
        with pytest.raises(RegistryError) as caught:
            await fetch_speakers(service.base_url)

    assert "ip_address" in str(caught.value)
    assert service.base_url in str(caught.value)


async def test_an_empty_list_is_not_a_refusal() -> None:
    """The control for the refusal above: no entries is not the same as no entry being usable."""
    async with _serving("[]") as service:
        assert await fetch_speakers(service.base_url) == ()


async def test_a_body_that_is_not_a_list_is_refused_by_name() -> None:
    async with _serving('{"error": "nope"}') as service:
        with pytest.raises(RegistryError) as caught:
            await fetch_speakers(service.base_url)

    assert "list" in str(caught.value)


async def test_a_404_says_the_registry_answered_and_refused() -> None:
    async with _serving("404 page not found", status_line="404 Not Found") as service:
        with pytest.raises(RegistryError) as caught:
            await fetch_speakers(service.base_url)

        assert "404" in str(caught.value)
        assert service.base_url in str(caught.value)


async def test_nothing_listening_says_so_rather_than_raising_a_transport_error() -> None:
    """A restarting container is the commonest case, and it must not surface as an httpx type."""
    service = FakeRegistry("[]")
    await service.start()
    base_url = service.base_url
    await service.stop()

    with pytest.raises(RegistryError) as caught:
        await fetch_speakers(base_url)

    assert base_url in str(caught.value)
