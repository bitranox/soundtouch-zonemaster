"""The refusals the application raises and an adapter is expected to recognise.

They live here rather than beside the adapter that raises them because the ZONE SERVICE is what
reacts to each: a registry that could not be read is a pass that answers nothing, a busy listening
port is a pass worth running again in a second, and a playlist MPD does not have is somebody's
typo in the channel file. A caller that had to import the HTTP adapter to catch the first, the
SoundTouch adapter to catch the second or the MPD adapter to catch the third would be importing
downwards to name an outcome.

The MPD three are a hierarchy rather than one type with a code on it, for the reason
:class:`PortsBusyError` exists at all: the caller has to tell them apart, and the NUMBER that
tells them apart is MPD's wire and belongs in the adapter that reads the wire. So the service
names the outcome and never the code.
"""

from __future__ import annotations

__all__ = ["MpdError", "MpdRefusalError", "NotInMpdError", "PortsBusyError", "RegistryError"]


class RegistryError(RuntimeError):
    """The device list could not be read, in any of the ways that can happen."""


class PortsBusyError(RuntimeError):
    """A port the protocol fixes was still held after ``BIND_ATTEMPTS`` tries.

    Its own type rather than the ``OSError`` underneath, because the caller has to tell it from
    every other reason a listener will not bind: this one is worth coming back to on the next pass,
    and an address that does not exist is not.
    """


class MpdError(RuntimeError):
    """MPD could not be reached, or said something this program does not understand."""


class MpdRefusalError(MpdError):
    """MPD understood the command and declined it, which is a different thing from being down.

    The two need different reactions and only MPD's own code tells them apart: a connection that
    will not open is a daemon that needs restarting, while a declined command is something about
    what was asked. A caller that cannot distinguish them can only report the wrong one.
    """

    def __init__(self, code: int, command: str, message: str) -> None:
        super().__init__(f"mpd refused {command or 'the command'}: {message} (code {code})")
        self.code = code
        self.command = command
        self.message = message


class NotInMpdError(MpdRefusalError):
    """The channel names something MPD does not have: a stored playlist, a directory, or a file.

    Its own type because it is the one refusal that is a HOUSE mistake rather than a fault: a name
    mistyped in the channel file, fixed by editing that file, or a file taken away between MPD
    listing a directory and the queue being built from it. Everything else MPD declines is
    about this program, and the two must not be reported the same way.
    """
