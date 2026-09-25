"""Who the speakers are, reduced to what the service acts on.

The record only. WHERE it is read from - the replacement service next door, which discovers the
boxes by SSDP/UPnP and serves what it found at ``/api/setup/devices`` - is the registry adapter's
business, and so is every way that read can fail. This is what comes back.

One thing here is deliberate and was measured: a record keeps its ``product_code`` as a plain
string and compares against :class:`~soundtouch_zonemaster.domain.enums.ProductCode`, because the
enum is what one deployment answered on one day and a model nobody has seen must not take the
registry down.

The wire names (``ip_address``, ``mac_address``) are NOT here. They belong to the response, so the
aliases live on the adapter's boundary model, which maps them onto ``ip`` and ``mac``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .enums import ProductCode

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = ["ProtectedSpeaker", "Speaker", "first_protected"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Speaker:
    """One speaker as the registry reports it, reduced to what the service acts on.

    The response carries more - firmware versions, two serial numbers, a components list - and
    none of it decides anything here, so it is dropped rather than carried into a record that
    would then have to keep it true.
    """

    device_id: str
    name: str
    ip: str
    mac: str
    product_code: str
    account_id: str

    @property
    def is_console(self) -> bool:
        """Whether this is the Lifestyle console rather than a speaker.

        An API POWER flips the console's input, so it stays out of the zone unless it is named
        explicitly. An unrecognised product code is NOT a console: a model nobody has seen is
        treated as an ordinary device, which the membership policy can still refuse by name.
        """
        return self.product_code == ProductCode.LIFESTYLE


@dataclass(frozen=True, slots=True, kw_only=True)
class ProtectedSpeaker:
    """One address this house never sends anything to, and the reason a refusal quotes.

    The reason travels WITH the address because it is what the refusal says out loud: a person
    reading ``refused: Room5 (192.168.0.30) is the Lifestyle console`` learns why, and the
    alternative - a bare list of addresses plus a sentence somewhere in the program - is a list
    that grows an entry nobody can explain a year later.
    """

    ip: str
    name: str
    why: str


def first_protected(addresses: Iterable[str], protected: Sequence[ProtectedSpeaker]) -> ProtectedSpeaker | None:
    """The first of ``addresses`` that must never be touched, or ``None`` if none of them is.

    In the order the addresses were GIVEN rather than the order the list declares them, so the
    refusal names the first one a run would have reached.

    Matched on the exact address text. An entry with a stray space protects nothing, and that is
    said in the shipped file rather than papered over here: an address is what a speaker answers
    on, and quietly accepting a near-miss would make the list look like it protects shapes.
    """
    by_ip = {speaker.ip: speaker for speaker in protected}
    return next((by_ip[address] for address in addresses if address in by_ip), None)
