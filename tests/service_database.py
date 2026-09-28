"""A house database as the service leaves it after its first start.

Only the service creates the house database; every store verb refuses one that is not there. A
test of a store verb therefore starts from a database this creates, through the same opener the
service's composition root uses, rather than relying on the verb to make one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from soundtouch_zonemaster.composition import open_house_store

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["created_by_the_service"]


def created_by_the_service(path: Path) -> Path:
    """Create the SQLite house database at ``path``, brought to head and closed; return ``path``."""
    store = open_house_store(str(path), password=None, log=lambda _kind, _text: None)
    store.open(exclusive=True)
    store.close()
    return path
