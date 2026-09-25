"""The machine-readable shape both commands print, and the one place that writes it.

Every CLI in this repository answers in the same two ways, because an LLM drives them and reads
JSON perfectly: ``--json`` for an indented envelope and ``--json-bare`` for one line, with the
narration moved to stderr so stdout carries the envelope and nothing else. The exit codes are the
contract and are format-independent (``application/outcome.py``).

The archive declared four envelopes - one per command - with identical fields. They are one
generic here, parametrised with the payload each command reports, which is what keeps the four
byte-identical rather than nearly so. **Always parametrise it**: ``Envelope[RunReport](...)``
serialises the payload, while the bare ``Envelope(...)`` is a field typed as the bound and is not
what any caller means.
"""

from __future__ import annotations

import sys
import traceback

from pydantic import BaseModel, ConfigDict

from . import safe_console

__all__ = ["Envelope", "ErrorEnvelope", "OutputMode", "report_crash", "report_failure", "write_envelope"]


class OutputMode(BaseModel):
    """Which of the two shapes this invocation answers in, decided once from the two flags.

    The pair travels together because it is one decision: ``--json-bare`` is ``--json`` without
    the indent, and a function given only one half cannot print either shape correctly.
    """

    model_config = ConfigDict(frozen=True)

    machine: bool
    """Whether stdout carries an envelope. It also routes the narration to stderr."""
    indent: int | None
    """What ``model_dump_json`` is given: two spaces, or nothing for the one-line form."""

    @classmethod
    def of(cls, *, as_json: bool, as_json_bare: bool) -> OutputMode:
        """The mode two click flags describe. Bare implies machine, which is why it is not an or."""
        return cls(machine=as_json or as_json_bare, indent=None if as_json_bare else 2)


class Envelope[PayloadT: BaseModel](BaseModel):
    """What a command reports when it ran; ``ok`` is false when the answer was no.

    ``skipped`` is part of the house envelope shape and is empty here in every case either command
    produces. It stays declared rather than dropped, because a caller written against one of these
    CLIs reads the same four keys from all of them.
    """

    ok: bool
    command: str
    data: PayloadT
    skipped: list[str] = []


def write_envelope(document: BaseModel, *, mode: OutputMode) -> None:
    """The envelope, on stdout, where nothing else has been written in this mode.

    Written through the encode-safe adapter because the payload is not all ASCII by
    construction: a channel file under a path with an umlaut in it, or an exception message
    carrying a station's name, reaches a cp1252 Windows console as a UnicodeEncodeError - AFTER
    the work succeeded, which is the part that misleads. A stream that can take the text gets
    it verbatim, so the bytes here are the archive's wherever this actually runs.
    """
    text = document.model_dump_json(indent=mode.indent)
    sys.stdout.write(safe_console.encode_safe(text, sys.stdout.encoding) + "\n")


class ErrorEnvelope(BaseModel):
    """The machine-readable result when a command was refused or could not start.

    Shared by both programs, because a caller parsing one should not have to learn a second shape
    for the same news.
    """

    ok: bool = False
    command: str
    error: str
    message: str


def report_failure(exc: Exception, *, command: str, mode: OutputMode) -> None:
    """Report a refusal or a crash in whichever shape the caller asked for.

    Written to the stream rather than printed: a refusal belongs on stderr in either shape, and in
    machine mode stdout carries the envelope and nothing else.
    """
    if mode.machine:
        write_envelope(ErrorEnvelope(command=command, error=type(exc).__name__, message=str(exc)), mode=mode)
    else:
        sys.stderr.write(f"{exc}\n")


def report_crash(exc: Exception, *, command: str, mode: OutputMode) -> None:
    """Report an exception NOBODY expected, keeping the one thing that can explain it afterwards.

    Separate from :func:`report_failure` because the two say opposite things. A refusal is an
    ANSWER - the input was wrong and the program said so - and a stack trace under it would be
    noise over a question already answered. A crash is the program failing, and the only reader is
    somebody arriving later at a journal.

    Measured in the flat 2026-09-20 at 22:46:25: the house service died of a ConnectionResetError
    and the entire record was its class and its message. It did not recur on the repeat, so that
    one line was all the evidence there would ever be, and it named no file, no line and no frame.
    A service systemd restarts on failure is exactly the one whose crash nobody is watching.

    The traceback goes to STDERR and never into the envelope: machine mode promises stdout carries
    the envelope and nothing else, and a caller parsing it must not have to strip a stack first.
    """
    traceback.print_exception(exc, file=sys.stderr)
    report_failure(exc, command=command, mode=mode)
