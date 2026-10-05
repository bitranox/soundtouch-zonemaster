"""What the switch MEANS, apart from where it is stored.

The one word that turns the service off and how often the service re-reads the switch are rules,
not I/O: the service reads them to decide, and a test states them without touching a database.
The switch lives in one row of the house database; reading that row, watching it for a change,
and every way that read can fail are the adapter's (``adapters/files/house_switch.py``), and the
prose explaining why a lost switch means ON lives there with the code that implements it. The
old switch file is read by the same word (``adapters/files/switch_file.py``), and only by the
installer's switch seed.
"""

from __future__ import annotations

__all__ = ["OFF", "POLL_S"]

OFF = "off"
"""The one word that turns the service off, read case-insensitively and stripped of spaces."""

POLL_S = 1.0
"""How often the service re-reads the switch row. A person flipping a switch waits a second without minding."""
