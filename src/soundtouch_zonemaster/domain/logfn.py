"""The shape of the callable every part of the program is handed to say what it just did.

One shape for the whole program: the first argument is the line's kind (``transport``, ``data``,
``sync``, ...), the second the text. A test supplies a list append, and the ``sync`` lines it
collects are the instrument the placement work is judged by.

Only the TYPE lives here. The real callable, and the decision about which stream a line goes to,
are a property of the RUN rather than of the line, so they belong to the program's edge and sit in
``adapters.logging.narration``. Nothing in ``domain`` narrates anything: the two rules that used to
(the dialler ignoring a digit, and seeding a channel list) return what they would have said and the
caller writes it.
"""

from __future__ import annotations

from collections.abc import Callable

__all__ = ["ERROR_KIND", "LogFn"]

LogFn = Callable[[str, str], None]

ERROR_KIND = "error"
"""The one kind that goes to stderr whatever the routing says.

The fatal line goes where a refusal already goes, because they are the same kind of news: sending
one to each stream meant a caller redirecting stdout got the failure mixed into it and could not
filter it back out by stream.
"""
