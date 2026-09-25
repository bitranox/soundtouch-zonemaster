"""``python -m soundtouch_zonemaster``: the prototype, which is the one that measures.

The service is a console script and nothing else, because it is a service: a module entry point
for it would be a second way to start the same unit, and the deployed one names the script.
"""

from __future__ import annotations

import sys

from .entry import prototype_main

if __name__ == "__main__":
    sys.exit(prototype_main())
