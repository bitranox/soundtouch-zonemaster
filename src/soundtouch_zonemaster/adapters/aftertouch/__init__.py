"""The AfterTouch adapter: who the speakers are, read from the replacement service next door.

One module, because that service answers one question this program needs. Everything else it knows
about a box - firmware versions, serial numbers, a components list - is dropped at this boundary
rather than carried into a record that would then have to keep it true.
"""

from __future__ import annotations

__all__: list[str] = []
