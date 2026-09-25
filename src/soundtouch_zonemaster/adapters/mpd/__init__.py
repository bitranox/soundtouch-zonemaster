"""The Music Player Daemon beside the service, and the line protocol it answers on.

Its own adapter family rather than a module in another one, because it speaks to a different thing
from every neighbour: ``soundtouch`` talks to the speakers, ``aftertouch`` to the service next door,
``files`` to the disk, and this one to a daemon on a socket. The import-linter contract "The adapter
families do not reach into each other" is what holds that apart; nothing here may reach into a
sibling, and no sibling may reach in here.

MPD costs the master nothing on the SOURCE side - its ``httpd`` output is an HTTP MP3 stream and
that is exactly what ``adapters/soundtouch/source.py`` already pulls. Everything in this package is
the CONTROL side: choosing which stored playlist is behind that URL.

What is NOT here is the three ways this can go wrong. ``MpdError``, ``MpdRefusalError`` and
``NotInMpdError`` are the application's (``application/errors.py``) for the same reason
``RegistryError`` is: the zone service reacts to each, and it may not import an adapter to name an
outcome. What stays here is the wire that tells them apart.
"""

from __future__ import annotations

from .client import MpdControl, quote

__all__ = ["MpdControl", "quote"]
