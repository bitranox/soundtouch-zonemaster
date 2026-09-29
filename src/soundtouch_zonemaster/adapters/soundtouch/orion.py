"""Where a RELATIVE Orion location points, found the way a speaker finds it.

AfterTouch writes a speaker preset's location as ``/station?data=...`` and leaves the rest to the
speaker, which prepends the ``baseUrl`` of the ``LOCAL_INTERNET_RADIO`` entry in its BMX service
registry (``GET <service>/bmx/registry/v1/services``, no authentication). The master fetches a
station itself, so it has to complete the location the same way before the first request; the
absolute spelling and the legacy ``/custom/v1/playback/<b64>`` form need nothing and are handed
back unchanged.

The stored channel keeps the location it was given. What a SPEAKER is sent is the completed one:
the absolute URL is exactly what a speaker would compute from its own registry, so handing it over
is right whether or not a given firmware resolves the relative form itself, and it does not rest
on a behaviour nobody has measured. One :class:`OrionBase` per service answers both the fetch and
every ``/select``, so the two can never be completed against different bases.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...application.options import DEFAULT_BASE_URL
from ...domain.enums import SourceName
from ...domain.station import is_relative_orion_location
from ..http_client import client_without_deadline

if TYPE_CHECKING:
    from ...domain.logfn import LogFn

__all__ = [
    "BMX_REGISTRY_PATH",
    "ORION_FALLBACK_PATH",
    "REGISTRY_MAX_BYTES",
    "REGISTRY_RETRY_S",
    "REGISTRY_TIMEOUT_S",
    "BmxRegistry",
    "OrionBase",
]

BMX_REGISTRY_PATH = "/bmx/registry/v1/services"
"""The service registry a speaker reads its source adapters from, under the service's base URL."""

ORION_FALLBACK_PATH = "/core02/svc-bmx-adapter-orion/prod/orion"
"""Where AfterTouch serves its Orion adapter under the service base, used when the registry cannot say.

It is the path AfterTouch's registry names for ``LOCAL_INTERNET_RADIO`` itself, so the fallback
reaches the same adapter whenever the registry is merely unreadable rather than configured
differently - and the log line says which of the two answered.
"""

REGISTRY_TIMEOUT_S = 2.0
"""How long the registry may take before the fallback is used.

Well inside the master's ten seconds for a station's first bytes (``FIRST_BYTES_TIMEOUT_S``),
because this wait is spent INSIDE that window: at the device list's eight seconds a registry that
accepted and never answered left two seconds for the station itself, on every start. The registry
is a neighbour on the loopback, so an answer that has not come in two seconds is not coming.
"""

REGISTRY_RETRY_S = 60.0
"""How long a registry that could not be read is left alone before it is asked again.

A stalled registry costs :data:`REGISTRY_TIMEOUT_S` once, rather than on every station start and
every reconnect of a dropped stream; the fallback answers in the meantime, and a minute later the
registry gets its next chance, so a neighbour that recovered is found again.
"""

REGISTRY_MAX_BYTES = 64 * 1024
"""The most of a registry document that is read, the bound station documents get (``source._peek``).

AfterTouch's registry is a few hundred bytes. A body past this is refused rather than buffered, so
a neighbour answering with something else entirely costs a bounded read.
"""


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
        """The base URL the registry names for ``name``, or None when it names none a master can fetch.

        A base carrying a query or a fragment is refused: a location is APPENDED to the base, so
        ``?`` or ``#`` in it would swallow the station path into the query or cut it off entirely,
        and the result would name something the adapter never served.
        """
        for service in self.bmx_services:
            if service.id.name == name and _is_a_base(service.base_url):
                return service.base_url
        return None


def _is_a_base(url: str) -> bool:
    """An http(s) URL that a path can be appended to: a host, and no query or fragment.

    The characters are tested rather than ``urlsplit``'s parts, because a bare trailing ``?`` or
    ``#`` leaves both parts empty and still turns everything appended after it into the query.
    """
    if not url.startswith(("http://", "https://")) or "?" in url or "#" in url:
        return False
    return bool(urlsplit(url).netloc)


class _UnreadableError(Exception):
    """The registry did not name a usable base; the message says why, for the fallback's log line."""


@dataclass
class OrionBase:
    """Completes a relative Orion location against the service's registry, read once per holder.

    One per service: every station the master plays and every ``/select`` the service sends ask
    the same one, so the fetch and the speaker are never handed locations completed against two
    different bases, and the registry is read once rather than once per station. The lookup is
    serialised, so two stations starting at once ask it once between them rather than once each.

    Two things end a remembered answer. A fetch against a base the registry named that fails
    (:meth:`forget`) drops it, so an Orion adapter that moved is read again at the next station
    rather than never. And a registry that could not be read is remembered only for
    ``retry_after_s``: the fallback answers until then, so a stalled registry costs its timeout
    once instead of on every start, and is asked again afterwards.
    """

    service_url: str = DEFAULT_BASE_URL
    """The replacement service's base URL (``[registry] url``), which serves the registry too."""
    log: LogFn = field(kw_only=True, repr=False)
    """Where the registry's answer, and the fallback when there is none, is said."""
    timeout_s: float = field(default=REGISTRY_TIMEOUT_S, kw_only=True)
    """How long one registry read may take; a field so a test can stall it without waiting two seconds."""
    retry_after_s: float = field(default=REGISTRY_RETRY_S, kw_only=True)
    """How long a failed read is remembered before the registry is asked again."""
    _found: str | None = field(default=None, init=False, repr=False)
    _fallback_until: float = field(default=float("-inf"), init=False, repr=False)
    _lookup: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    @property
    def fallback(self) -> str:
        """AfterTouch's own adapter path under the service base, used when the registry cannot say."""
        return f"{self.service_url.rstrip('/')}{ORION_FALLBACK_PATH}"

    async def absolute(self, location: str) -> str:
        """``location`` as a URL that names the station completely; anything but the relative form as it is."""
        if not is_relative_orion_location(location):
            return location
        base = await self._base()
        url = f"{base.rstrip('/')}{location}"
        self.log("source", f"relative location -> {url}")
        return url

    def forget(self, url: str) -> None:
        """A fetch of ``url`` failed: if it was completed against the base the registry named, drop it.

        The next relative location asks the registry again, which is how an Orion adapter that
        moved is found. A url completed against the fallback, or never completed at all, leaves
        everything as it is: there is nothing remembered to be wrong about.
        """
        if self._found is None or not url.startswith(f"{self._found.rstrip('/')}/"):
            return
        self.log("source", f"a fetch against {self._found} failed; the bmx registry is asked again next time")
        self._found = None

    async def _base(self) -> str:
        async with self._lookup:
            if self._found is not None:
                return self._found
            if time.monotonic() < self._fallback_until:
                return self.fallback
            registry = f"{self.service_url.rstrip('/')}{BMX_REGISTRY_PATH}"
            try:
                found = await _read_base(registry, timeout_s=self.timeout_s)
            except _UnreadableError as exc:
                self._fallback_until = time.monotonic() + self.retry_after_s
                self.log(
                    "source",
                    f"bmx registry {registry}: {exc}; resolving against {self.fallback}"
                    f" (asked again in {self.retry_after_s:.0f} s)",
                )
                return self.fallback
            self.log("source", f"bmx registry names {SourceName.LOCAL_INTERNET_RADIO} at {found}")
            self._found = found
            return found


async def _read_base(registry: str, *, timeout_s: float) -> str:
    """The LOCAL_INTERNET_RADIO base the registry names, or :class:`_UnreadableError` saying why not.

    Read as a stream, at most :data:`REGISTRY_MAX_BYTES`, and never through a redirect: the
    registry is the service's own, at the address it was configured with, and an answer sending
    the master somewhere else is not a registry answering.
    """
    _require_an_address(registry)
    try:
        # The deadline is asyncio's, never httpx's (adapters/http_client.py says why).
        async with asyncio.timeout(timeout_s), client_without_deadline() as client:
            body = await _bounded_body(client, registry)
        document = BmxRegistry.model_validate_json(body)
    except httpx.HTTPStatusError as exc:
        raise _UnreadableError(f"answered {exc.response.status_code}") from exc
    except (httpx.HTTPError, TimeoutError) as exc:
        raise _UnreadableError(f"could not be reached ({exc.__class__.__name__})") from exc
    except ValidationError as exc:
        raise _UnreadableError("is not a service registry") from exc
    base = document.base_of(SourceName.LOCAL_INTERNET_RADIO)
    if base is None:
        raise _UnreadableError(f"names no usable http base for {SourceName.LOCAL_INTERNET_RADIO}")
    return base


def _require_an_address(url: str) -> None:
    """:class:`_UnreadableError` when no request can be sent to ``url`` at all.

    A mistyped ``[registry] url`` fails in ways that are not ``httpx.HTTPError``: a control
    character or a host that is no name raises ``httpx.InvalidURL`` while the request is built,
    and a port past 65535 is only noticed by the socket, as an ``OverflowError`` inside anyio's
    exception group. Either would escape to whoever asked for a location, which for a station
    start is the one step that must not raise, so both are checked here, before anything is sent,
    and answered the way an unreadable registry is.
    """
    try:
        httpx.URL(url)
        _ = urlsplit(url).port
    except (httpx.InvalidURL, ValueError) as exc:
        raise _UnreadableError(f"is not an address a request can be sent to ({exc})") from exc


async def _bounded_body(client: httpx.AsyncClient, url: str) -> bytes:
    """The whole body, or :class:`_UnreadableError` once it passes :data:`REGISTRY_MAX_BYTES`."""
    async with client.stream("GET", url, follow_redirects=False) as response:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes(8192):
            body += chunk
            if len(body) > REGISTRY_MAX_BYTES:
                message = f"answered with more than {REGISTRY_MAX_BYTES} bytes"
                raise _UnreadableError(message)
        return bytes(body)
