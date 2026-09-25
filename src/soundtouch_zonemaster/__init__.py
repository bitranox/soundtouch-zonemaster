"""A software zone master for Bose SoundTouch speakers.

The package is layered. ``domain`` holds the house's rules as pure records and functions,
``application`` the use cases and the ports they are handed their adapters through, ``adapters``
everything that touches a speaker, a file, the configuration or the command line, and
``composition`` wires the adapters to the ports. The package metadata lives in ``__init__conf__``.
"""

from __future__ import annotations

from .__init__conf__ import print_info

__all__ = ["print_info"]
