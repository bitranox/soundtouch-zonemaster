"""How a command ends: the three exit codes, and the refusal that carries one.

The codes are the contract every command here is driven by, and they are format-independent: a
caller reading the JSON envelope and a caller reading the shell's status learn the same thing.
They are an ``IntEnum`` so that a comparison against a bare 0, 1 or 2 - which is what a shell and
half the tests do - still means what it says.

:func:`device_id_or_refuse` and :func:`tcp_port_or_refuse` are here rather than in ``domain``
because they raise the CLI's refusal with its exit code, which is an application answer rather
than a house rule. Each is called from TWO places - the record's own check and the pydantic model
at the boundary that builds it - so that a caller who typed the value and a caller who wrote it
into a config file are refused with the same sentence.
"""

from __future__ import annotations

import re
from enum import IntEnum

__all__ = ["ExitCode", "OptionsError", "device_id_or_refuse", "tcp_port_or_refuse"]


class ExitCode(IntEnum):
    """What the shell is told, whichever output shape was asked for."""

    OK = 0
    """Finished what was asked."""
    REFUSED = 1
    """It ran, and the answer is no.

    An option set this program refuses, and equally a run that had nothing to play to. Both are
    answers, not failures, and a caller has to be able to tell them from "could not run".
    """
    ERROR = 2
    """It could not run."""


_DEVICE_ID = re.compile(r"[0-9A-F]{12}")

_LOWEST_PORT, _HIGHEST_PORT = 1, 65535
"""What a TCP port can be. Zero is not one: it means "any free port" to a listener and nothing at
all to a caller trying to reach a daemon, so it is refused rather than silently connected to."""


class OptionsError(Exception):
    """A refusal the CLI prints on stderr and reports as its exit code.

    Deliberately not a ``ValueError``: pydantic wraps a ValueError raised inside a validator into
    a ``ValidationError``, which would flatten both refusals onto one exit code. Anything else
    propagates unchanged, so the code raised here is the code the shell sees (probe-verified
    against a control that a plain ValueError IS wrapped).
    """

    def __init__(self, message: str, *, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def device_id_or_refuse(value: str) -> str:
    """Twelve hex digits, or a refusal. Both programs take one, so both refuse the same way."""
    if not _DEVICE_ID.fullmatch(value):
        raise OptionsError("device id must be 12 hex digits", exit_code=ExitCode.ERROR)
    return value


def tcp_port_or_refuse(value: int, *, what: str) -> int:
    """A port a caller can actually dial, or a refusal naming which setting was wrong.

    ``what`` is the setting's name because there is more than one port in this program's options
    and a message that only said "port" would send an operator looking through all of them.
    """
    if not _LOWEST_PORT <= value <= _HIGHEST_PORT:
        message = f"refused: {what} must be a port between {_LOWEST_PORT} and {_HIGHEST_PORT}, not {value}"
        raise OptionsError(message, exit_code=ExitCode.REFUSED)
    return value
