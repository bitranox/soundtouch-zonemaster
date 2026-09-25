"""The zone service, split along its own methods into one file per concern.

The chain is ``ServiceState -> ChannelBook -> SpeakerBook -> VolumeGuard -> ZoneReconcile ->
Dialling -> KeyReading -> ZoneService``, each class extending the one before it, and a method may
call only methods of its own class or of an earlier one. That rule is what makes the order mean
something: read from ``state`` upwards and nothing is ever used before it is defined.

Only the top of the chain is exported. The seven below it are the shape of one class, not seven
things a caller picks between.
"""

from __future__ import annotations

from .service import ZoneService

__all__ = ["ZoneService"]
