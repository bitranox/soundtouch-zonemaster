"""A typed view of the rich_click decorators, and the context accessor.

``rich_click`` re-exports click's decorators without complete type information, so under pyright
strict every ``@click.option`` reports "Type of option is partially unknown" - one error per
decorator (measured 2026-09-06: four options, four errors; ``@click.command`` is unaffected).
``get_current_context`` is typed as returning ``Context | None``, so every ``ctx.exit`` is an
optional-member access.

The house rule is to DEFINE the missing type rather than switch the rule off for the file, so this
declares the shape actually used and casts the real objects to it once. Both casts are runtime-free:
the objects returned ARE rich_click's.

This is a deliberate copy of ``tools/_click.py`` and ``research/_click.py``; the research
scripts cannot import this package (they run where it is not installed), so the declaration
lives once per runnable area.

Beside the casts, one function: ``run_cli``, the library's runner with a handler that answers a
click USAGE error as the refusal envelope when ``--json`` or ``--json-bare`` is on argv. It is the
only logic here, it is identical in the three copies, and ``tests/test_click_facades.py`` holds
them to that.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Protocol, cast

import click
import lib_cli_exit_tools
import rich_click as _rich_click

if TYPE_CHECKING:
    from lib_cli_exit_tools.application.runner import ClickCommand
    from rich_click import Context

__all__ = ["MACHINE_FLAGS", "argument", "current_context", "option", "run_cli"]

_Decorator = Callable[[Callable[..., Any]], Callable[..., Any]]


class _OptionDecorator(Protocol):
    """``click.option`` as it is actually called here: flag names, then keyword attributes."""

    def __call__(self, *param_decls: str, **attrs: Any) -> _Decorator: ...


class _ArgumentDecorator(Protocol):
    """``click.argument`` as it is actually called here: one name, then keyword attributes."""

    def __call__(self, *param_decls: str, **attrs: Any) -> _Decorator: ...


class _ContextGetter(Protocol):
    """``click.get_current_context`` inside a running command, where a context always exists."""

    def __call__(self) -> Context: ...


# Reading the partially-typed member is the one unknown this facade exists to contain, so the
# suppression is one line wide and names its rule. Remove it when rich_click ships complete
# types for its decorator re-exports and the cast stops being needed at all.
option = cast("_OptionDecorator", _rich_click.option)  # pyright: ignore[reportUnknownMemberType]
argument = cast("_ArgumentDecorator", _rich_click.argument)  # pyright: ignore[reportUnknownMemberType]
current_context = cast("_ContextGetter", _rich_click.get_current_context)

MACHINE_FLAGS = ("--json", "--json-bare")
"""The two output flags every command here takes, in whatever position argv carries them."""


def run_cli(command: ClickCommand, *, argv: Sequence[str] | None, prog_name: str) -> int:
    """``lib_cli_exit_tools.run_cli``, with a click USAGE error answered in the shape the caller asked for.

    A usage error - a missing option, an unknown one, a bad value - is raised while click parses,
    before any command body has read ``--json``, so every command here used to answer it with prose
    on stderr and an empty stdout. This reads the flags off argv itself and, when one is there,
    prints the same refusal envelope as every other failure; the exit code stays click's 2. Anything
    else goes to the library's own handler unchanged, which keeps the signal exit codes.
    """
    given = list(sys.argv[1:] if argv is None else argv)
    machine = [flag for flag in MACHINE_FLAGS if flag in given]

    def handler(exc: BaseException) -> int:
        if machine and isinstance(exc, click.UsageError):
            envelope = {"ok": False, "command": prog_name, "error": type(exc).__name__, "message": exc.format_message()}
            bare = "--json-bare" in machine
            text = json.dumps(
                envelope, ensure_ascii=False, indent=None if bare else 2, separators=(",", ":") if bare else None
            )
            sys.stdout.write(text + "\n")
            return exc.exit_code
        return lib_cli_exit_tools.handle_cli_exception(exc, signal_specs=lib_cli_exit_tools.default_signal_specs())

    return lib_cli_exit_tools.run_cli(command, argv=argv, prog_name=prog_name, exception_handler=handler)
