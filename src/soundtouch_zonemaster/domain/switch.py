"""What the switch MEANS, apart from the file it is written in.

The one word that turns the service off and how often the file is re-read are rules, not I/O: the
service reads them to decide, and a test states them without touching a disk. Reading the file,
watching it for a change, and every way that read can fail are the adapter's
(``adapters/files/switch_file.py``), and the prose explaining why a lost file means ON lives there
with the code that implements it.
"""

from __future__ import annotations

__all__ = ["OFF", "POLL_S"]

OFF = "off"
"""The one word that turns the service off, read case-insensitively and stripped of spaces."""

POLL_S = 1.0
"""How often the file is re-read. A person flipping a switch waits a second without minding."""
