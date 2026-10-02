"""A speaker's device id: the one place it is checked and folded to upper case.

A device id is a speaker's MAC, twelve hex digits, as the registry spells it (upper case). Every
surface that takes one - a console, an alarm's box, the master's own ``--device-id`` /
``zone.device_id`` - holds a person or a deployed config to the same rule and hands back the same
shape the registry reports, however it was spelled.
"""

from __future__ import annotations

import re

__all__ = [
    "DEVICE_ID",
    "normalized_device_id",
]

DEVICE_ID = re.compile(r"[0-9A-F]{12}")
"""A speaker's device id: its MAC, twelve hex digits, as the registry spells it (upper case)."""


def normalized_device_id(value: str) -> str | None:
    """Twelve hex digits of either case, folded to upper case; ``None`` if it is not one."""
    upper = value.upper()
    return upper if DEVICE_ID.fullmatch(upper) else None
