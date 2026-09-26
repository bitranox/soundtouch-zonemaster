"""Every ``parse_service_options`` keyword left untyped, for tests that type only one or two.

A test about the ``database`` or ``bind_ip`` field passes this as ``**NOTHING_TYPED`` rather than
spelling out every other keyword argument again, so the only thing that can make the call refuse
is the field under test.
"""

from __future__ import annotations

from typing import Any

__all__ = ["NOTHING_TYPED"]

NOTHING_TYPED: dict[str, Any] = {
    "device_id": None,
    "channel_file": None,
    "switch_file": None,
    "state_file": None,
    "registry_url": None,
    "allow_console": (),
    "unreachable_timeout_s": None,
    "dial_window_s": None,
    "mpd_host": None,
    "mpd_port": None,
    "mpd_rewind_s": None,
}
