"""A secret the records carry: handed on from the boundary to the one adapter that uses it, never shown.

The options record is a frozen dataclass and prints itself whole - in a traceback, a log line, a
test failure - so a password held there as a plain ``str`` would be printed with it. This type
prints ``***`` in every spelling (``repr``, ``str``, any format spec) and gives its value up through
:meth:`Secret.reveal` alone, so reading it is a call a reviewer can find by name.

It lives in the domain because every layer that touches it may import the domain and nothing
else is shared by all of them: the boundary builds it, the application's record and port carry it,
the store adapter reveals it. It is stdlib-only for the same reason. The boundary may hold the
value as pydantic's ``SecretStr`` while it parses, but that type is a framework's and may not
cross into the application.

Deliberately NOT a dataclass: ``dataclasses.asdict`` recurses into a dataclass field and would
render the value as a plain dict entry. A plain class is copied whole by it instead.
"""

from __future__ import annotations

from typing import NoReturn

__all__ = ["MASK", "Secret"]

MASK = "***"
"""What every printed form of a :class:`Secret` shows instead of its value."""


class Secret:
    """A non-empty secret string that no printed form of it shows."""

    __slots__ = ("_value",)

    _value: str

    def __init__(self, value: str) -> None:
        """Refuse an empty value: no secret at all is ``None``, so there is one way of saying so."""
        if not value:
            message = "an empty secret is no secret; pass None for none"
            raise ValueError(message)
        object.__setattr__(self, "_value", value)

    def reveal(self) -> str:
        """The value itself, for the one place that hands it on (a driver's connect argument)."""
        return self._value

    def __setattr__(self, name: str, value: object) -> NoReturn:
        message = f"a Secret cannot be changed after it is made (tried to set {name!r})"
        raise AttributeError(message)

    def __repr__(self) -> str:
        return f"Secret({MASK!r})"

    def __str__(self) -> str:
        return MASK

    def __format__(self, spec: str) -> str:
        return format(MASK, spec)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash((Secret, self._value))

    def __reduce__(self) -> NoReturn:
        message = "a Secret cannot be pickled: the pickle would carry its value in the clear"
        raise TypeError(message)

    def __deepcopy__(self, _memo: dict[int, object]) -> Secret:
        return self

    def __copy__(self) -> Secret:
        return self
