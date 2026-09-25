"""Reading the device list from the replacement service on the loopback.

The wire names (``ip_address``, ``mac_address``) live HERE, on the boundary model, and nowhere
else: they belong to the response rather than to the speaker, and mapping them onto ``ip`` and
``mac`` is this module's job. Everything else the response carries is dropped by
``extra="ignore"``, which is deliberate - a field nobody reads is a field that can be wrong for
years without anything behaving differently.

Every way the read can fail becomes one :class:`RegistryError` with a message naming the URL. One
error for all of them because the caller's useful reaction is the same in every case, while the
person reading the log still needs to tell "the registry answered 500" from "the body is not JSON".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...application.errors import RegistryError
from ...application.options import DEFAULT_BASE_URL
from ...domain.speakers import Speaker

if TYPE_CHECKING:
    from ...domain.logfn import LogFn

__all__ = ["DEVICES_PATH", "SpeakerRecord", "fetch_speakers"]


DEVICES_PATH = "/api/setup/devices"
"""The device list. Guessing this path costs an afternoon: every plausible spelling of it answers
404, and the service's own setup page is what names the real one."""

TIMEOUT_S = 8.0


class SpeakerRecord(BaseModel):
    """One device entry as the registry serves it, mapped onto :class:`Speaker`.

    The aliases and the coercions are the whole reason this is a pydantic model and the record it
    produces is not: a response is untrusted input, and a frozen dataclass coerces nothing.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    device_id: str
    name: str
    ip: str = Field(alias="ip_address")
    mac: str = Field(alias="mac_address")
    product_code: str
    account_id: str

    def to_speaker(self) -> Speaker:
        """The domain record for this entry."""
        return Speaker(
            device_id=self.device_id,
            name=self.name,
            ip=self.ip,
            mac=self.mac,
            product_code=self.product_code,
            account_id=self.account_id,
        )


def _why_unusable(entry: object, exc: ValidationError) -> str:
    """One short line naming WHICH entry was dropped and which field decided it.

    The whole pydantic report is several lines carrying a URL and the offending input repr, which
    is unreadable once per poll in a service log. What a person needs is the identity of the box
    that vanished and the field that did it, so the entry's own ``device_id`` is quoted when it
    has one and the errors are reduced to their field names.
    """
    who = f"an entry of type {type(entry).__name__}"
    if isinstance(entry, dict):
        # A JSON object always has string keys, which is what makes this cast true; the isinstance
        # above is all that is checkable at runtime and leaves the element type unknown otherwise.
        fields_of = cast("dict[str, object]", entry)
        # Whichever of the three the entry still has. The field that failed is often the
        # identifying one, so falling through to the address and then the name is what keeps the
        # line able to say WHICH box vanished rather than only that one did.
        named = next((fields_of[key] for key in ("device_id", "ip_address", "name") if fields_of.get(key)), None)
        who = str(named) if named is not None else "an entry with no id, address or name"
    # An error whose ``loc`` is empty is about the entry itself rather than a field of it (a
    # string where an object belongs), and its type is the only thing that says so.
    fields = sorted({".".join(str(part) for part in error["loc"]) or error["type"] for error in exc.errors()})
    return f"{who}: {', '.join(fields)}"


async def fetch_speakers(base_url: str = DEFAULT_BASE_URL, *, log: LogFn | None = None) -> tuple[Speaker, ...]:
    """Read the device list, keeping every entry that is usable.

    An entry that does not validate is SKIPPED rather than taken as a reason to discard the read,
    because the list is a discovery result and its elements are independent: AfterTouch writes a
    placeholder for anything that answers SSDP before it can read its details, so any UPnP device
    on the LAN puts an entry with no ``mac_address`` in it. Measured in the flat 2026-09-19, one
    such placeholder took all six real speakers out of the service's view, and the zone could hold
    nobody while that device stayed on the network.

    A list in which NOTHING validates still raises :class:`RegistryError`. That is the case where
    skipping would be wrong: a renamed field breaks every entry at once, and reporting it as an
    empty device list would leave a silent flat with no cause anywhere in the log.

    ``log`` is optional and names each skipped entry. Skipping does not depend on it - a dropped
    speaker is a fact about the read, not about whether the caller wired a log.
    """
    url = f"{base_url.rstrip('/')}{DEVICES_PATH}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload: object = response.json()
    except httpx.HTTPStatusError as exc:
        raise RegistryError(f"{url}: the registry answered {exc.response.status_code}") from exc
    except httpx.RequestError as exc:
        raise RegistryError(f"{url}: could not be reached ({exc.__class__.__name__})") from exc
    except ValueError as exc:
        raise RegistryError(f"{url}: the body is not JSON") from exc

    if not isinstance(payload, list):
        raise RegistryError(f"{url}: expected a list of devices, got {type(payload).__name__}")

    entries = cast("list[object]", payload)
    """``response.json()`` is untyped, so the element type is stated once here rather than left
    unknown at the call below; the isinstance above is what makes the cast true."""

    speakers: list[Speaker] = []
    unusable: list[str] = []
    first_failure: ValidationError | None = None
    for entry in entries:
        try:
            speakers.append(SpeakerRecord.model_validate(entry).to_speaker())
        except ValidationError as exc:
            unusable.append(_why_unusable(entry, exc))
            first_failure = first_failure or exc

    if unusable and not speakers:
        # Chained on the FIRST failure rather than left bare: the summary line names every field,
        # and the traceback is then still able to show one of them in full.
        raise RegistryError(f"{url}: no device entry is usable: {'; '.join(unusable)}") from first_failure
    if log is not None:
        for note in unusable:
            log("registry", f"{url}: skipping a device entry: {note}")
    return tuple(speakers)
