"""How a command is run: the signal handling both programs need, and what a callback is handed.

``lib_cli_exit_tools.run_cli`` is kept rather than the template's own ``main`` (reached through
``typed_click.run_cli``, which also answers a usage error as an envelope), and the reason is
the dissolve: it installs handlers for SIGINT **and** SIGTERM that raise into the event loop, so
the ``finally`` that takes a real zone down actually runs. A stop that skipped it would leave
speakers in a zone whose master has gone, which needs a person to undo by hand.

What it does not support is click's ``obj=``, and a click callback receives only CLI parameters -
so the run each command performs, which is the seam its tests substitute, has nowhere to arrive.
:class:`_WithServices` is the one-line adapter for that: it is the command as far as ``run_cli``
can tell, and it forwards ``obj=`` on the way through. The archive used a module-level class
attribute instead, mutated by ``main`` and restored in a ``finally``; this holds no state between
runs at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..logging.narration import LogRouting
from .typed_click import run_cli

if TYPE_CHECKING:
    from collections.abc import Sequence

    import rich_click as click

__all__ = ["run"]


class _WithServices:
    """A click command, plus what a run of it needs, forwarded as ``ctx.obj``.

    Duck-typed rather than a ``click.Command`` subclass because ``run_cli`` asks for exactly one
    thing - a ``main`` it can call - and a subclass would have to carry a command's whole surface
    to add one keyword to one call.
    """

    def __init__(self, command: click.Command, services: object) -> None:
        self._command = command
        self._services = services

    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,  # noqa: FBT001, FBT002 - mirrors click's own positional main() signature
        **extra: object,
    ) -> object:
        """Click's own ``main``, with the services attached to the context it builds."""
        return self._command.main(
            args=args,
            prog_name=prog_name,
            complete_var=complete_var,
            standalone_mode=standalone_mode,
            obj=self._services,
            **extra,
        )


def run(command: click.Command, *, services: object, prog_name: str) -> int:
    """Parse argv and run ``command``, with ``services`` reachable from its callback as ``ctx.obj``.

    ``services`` is untyped here on purpose: this function does not read it, it only carries it,
    and the callback that does read it casts ``ctx.obj`` to the run it expects. A type variable
    that appears once in a signature says nothing, which is what pyright reports it as.

    The narration routing is reset afterwards because it is process-wide state a command sets from
    its flags: a test that runs one command in machine mode and the next in prose would otherwise
    find the second one still writing to stderr.
    """
    try:
        return run_cli(_WithServices(command, services), argv=None, prog_name=prog_name)
    finally:
        LogRouting.to_stderr = False
