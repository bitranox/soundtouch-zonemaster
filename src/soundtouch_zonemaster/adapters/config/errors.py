"""The one refusal this package raises, whatever went wrong underneath it.

Its own module rather than a name inside the loader, because every other module here raises it and
the loader is the one they all import; a shared exception living in one of the siblings would make
that sibling everybody's dependency for no other reason.
"""

from __future__ import annotations

__all__ = ["ConfigInputError"]


class ConfigInputError(Exception):
    """The only exception anything in this package raises, whatever went wrong.

    A malformed ``--set``, a section nobody asked about, a profile name that is really a path, a
    config file with a typo in it: to a caller they are one kind of news, "this run cannot be
    configured", and they all end as the same exit code. Collapsing them here is what lets each
    command catch ONE type and be complete, rather than growing a tuple that the next library
    release quietly outgrows.

    Named for the input rather than for configuration in general, because ``lib_layered_config``
    exports a ``ConfigError`` of its own and two same-named exceptions in one program is a trap.
    Not a ``ValueError``, for the same reason ``OptionsError`` is not: pydantic wraps a ValueError
    raised inside a validator, and this one has to reach the command unchanged.
    """
