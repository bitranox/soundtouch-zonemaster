"""Where a RELATIVE Orion location points, found the way a speaker finds it.

AfterTouch writes a speaker preset's location as ``/station?data=...`` and leaves the rest to the
speaker, which prepends the ``baseUrl`` of the ``LOCAL_INTERNET_RADIO`` entry in its BMX service
registry (``GET <service>/bmx/registry/v1/services``, no authentication). The master fetches a
station itself, so it has to complete the location the same way before the first request; the
absolute spelling and the legacy ``/custom/v1/playback/<b64>`` form need nothing and are handed
back unchanged.

Only the FETCH uses the completed URL. The station keeps the location it was given, because that
is what a speaker is sent in a ``/select`` and what it resolves against its own registry.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...application.options import DEFAULT_BASE_URL
from ...domain.enums import SourceName
from ...domain.station import is_relative_orion_location

if TYPE_CHECKING:
    from ...domain.logfn import LogFn

__all__ = ["BMX_REGISTRY_PATH", "ORION_FALLBACK_PATH", "REGISTRY_TIMEOUT_S", "BmxRegistry", "OrionBase"]

BMX_REGISTRY_PATH = "/bmx/registry/v1/services"
"""The service registry a speaker reads its source adapters from, under the service's base URL."""

ORION_FALLBACK_PATH = "/core02/svc-bmx-adapter-orion/prod/orion"
"""Where AfterTouch serves its Orion adapter under the service base, used when the registry cannot say.

It is the path AfterTouch's registry names for ``LOCAL_INTERNET_RADIO`` itself, so the fallback
reaches the same adapter whenever the registry is merely unreadable rather than configured
differently - and the log line says which of the two answered.
"""

REGISTRY_TIMEOUT_S = 8.0
"""How long the registry may take before the fallback is used; the same budget the device list has."""


class _ServiceId(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str = ""


class _BmxService(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    id: _ServiceId = _ServiceId()
    base_url: str = Field(default="", alias="baseUrl")


class BmxRegistry(BaseModel):
    """The registry document, reduced to the one thing read from it: each service's base URL.

    Everything else it carries (``askAgainAfter``, assets, streaming types) is ignored, because a
    field nobody reads is a field that can be wrong without anything behaving differently.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    bmx_services: tuple[_BmxService, ...]

    def base_of(self, name: SourceName) -> str | None:
        """The base URL the registry names for ``name``, or None when it names none a master can fetch."""
        for service in self.bmx_services:
            if service.id.name == name and service.base_url.startswith(("http://", "https://")):
                return service.base_url
        return None


class _UnreadableError(Exception):
    """The registry did not name a usable base; the message says why, for the fallback's log line."""


@dataclass
class OrionBase:
    """Completes a relative Orion location against the service's registry, read once per holder.

    One per master: every station a run plays after the first reuses the base the registry named.
    A registry that could not be read is NOT remembered, so the next station asks it again rather
    than staying on the fallback for the rest of the run.
    """

    service_url: str = DEFAULT_BASE_URL
    """The replacement service's base URL (``[registry] url``), which serves the registry too."""
    _found: str | None = field(default=None, init=False, repr=False)

    async def absolute(self, client: httpx.AsyncClient, location: str, log: LogFn) -> str:
        """``location`` as a URL the master can fetch; anything but the relative form comes back as it is."""
        if not is_relative_orion_location(location):
            return location
        base = await self._base(client, log)
        url = f"{base.rstrip('/')}{location}"
        log("source", f"relative location -> {url}")
        return url

    async def _base(self, client: httpx.AsyncClient, log: LogFn) -> str:
        if self._found is not None:
            return self._found
        registry = f"{self.service_url.rstrip('/')}{BMX_REGISTRY_PATH}"
        try:
            found = await _read_base(client, registry)
        except _UnreadableError as exc:
            fallback = f"{self.service_url.rstrip('/')}{ORION_FALLBACK_PATH}"
            log("source", f"bmx registry {registry}: {exc}; resolving against {fallback}")
            return fallback
        log("source", f"bmx registry names {SourceName.LOCAL_INTERNET_RADIO} at {found}")
        self._found = found
        return found


async def _read_base(client: httpx.AsyncClient, registry: str) -> str:
    """The LOCAL_INTERNET_RADIO base the registry names, or :class:`_UnreadableError` saying why not."""
    try:
        # The deadline is asyncio's, never httpx's (adapters/http_client.py says why).
        async with asyncio.timeout(REGISTRY_TIMEOUT_S):
            response = await client.get(registry)
        response.raise_for_status()
        document = BmxRegistry.model_validate_json(response.content)
    except httpx.HTTPStatusError as exc:
        raise _UnreadableError(f"answered {exc.response.status_code}") from exc
    except (httpx.HTTPError, TimeoutError) as exc:
        raise _UnreadableError(f"could not be reached ({exc.__class__.__name__})") from exc
    except ValidationError as exc:
        raise _UnreadableError("is not a service registry") from exc
    base = document.base_of(SourceName.LOCAL_INTERNET_RADIO)
    if base is None:
        raise _UnreadableError(f"names no http base for {SourceName.LOCAL_INTERNET_RADIO}")
    return base
