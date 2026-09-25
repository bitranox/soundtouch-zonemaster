"""The two console scripts, each wired to the production adapters before argv is parsed.

This module sits above ``composition`` on purpose: it is the only place that names both a command
and the world that command runs against, and keeping it out of ``adapters`` is what lets the CLI
modules be read - and tested - against a run that is a stand-in.

Each entry point takes the run as an optional keyword, which is the seam the CLI tests substitute.
Given nothing it uses the real one, which is what an installed console script does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .__init__conf__ import service_command, shell_command
from .adapters.cli import main as cli_main
from .adapters.cli.prototype import cli as prototype_command
from .adapters.cli.service.root import cli as service_command_group
from .composition import hold_the_zone, run_prototype

if TYPE_CHECKING:
    from .application.ports import RunService, RunZone

__all__ = ["prototype_main", "service_main"]


def prototype_main(*, run_zone: RunZone | None = None) -> int:
    """Parse argv and hold a zone for a duration. ``run_zone`` is the seam a test substitutes."""
    return cli_main.run(prototype_command, services=run_zone or run_prototype, prog_name=shell_command)


def service_main(*, run_service: RunService | None = None) -> int:
    """Parse argv and hold the house. ``run_service`` is the seam a test substitutes."""
    return cli_main.run(service_command_group, services=run_service or hold_the_zone, prog_name=service_command)
