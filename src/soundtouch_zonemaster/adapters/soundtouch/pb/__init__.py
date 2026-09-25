"""The package around the protoc output, and the only hand-written file in this directory.

Everything beside it is generated: research/generate_pb.py runs protoc over research/proto and
writes ``*_pb2.py`` and ``*_pb2.pyi``. protoc writes no package ``__init__.py``, so this one and
the two empty ones below it are maintained by hand and survive every regeneration - which is why
the linter and the type checker are pointed at the generated names rather than at this directory.

The generated modules import each other by their proto path (``SoundTouchInterface.Zone_pb2``),
so this directory goes on sys.path once, here, before anything imports them.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import AudioServerMsgAudioData_pb2 as audio_data  # noqa: E402
import AudioServerMsgDefinitions_pb2 as audio  # noqa: E402
import IPCMessageEnvelopeBase_pb2 as envelope  # noqa: E402

__all__ = ["audio", "audio_data", "envelope"]
