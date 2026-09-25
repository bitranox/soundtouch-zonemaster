"""``config-deploy``: the defaults that ship in the wheel, written into a layer as files to edit."""

from __future__ import annotations

import sys

import rich_click as click
from pydantic import BaseModel

from ....__init__conf__ import service_command
from ....application.outcome import ExitCode
from ...config.deploy import DEPLOY_TARGETS, deploy_defaults
from ..context import shared_of
from ..envelope import Envelope, report_failure, write_envelope
from ..typed_click import option

__all__ = ["DeployReport", "cli_config_deploy"]


class DeployReport(BaseModel):
    """Which files a deploy wrote, and which it left alone because they were already there."""

    source: str
    targets: list[str]
    written: list[str]
    profile: str | None


@click.command("config-deploy", context_settings={"help_option_names": ["-h", "--help"]})
@option(
    "--target",
    "targets",
    type=click.Choice(DEPLOY_TARGETS, case_sensitive=False),
    multiple=True,
    required=True,
    help="which layer to write the file into (repeatable)",
)
@option("--force", is_flag=True, help="overwrite a config file that is already there")
@click.pass_context
def cli_config_deploy(ctx: click.Context, *, targets: tuple[str, ...], force: bool) -> None:
    """Write the defaults that ship in the wheel into a layer, as a file to edit.

    Nothing is overwritten without ``--force``, so running this on a machine that already has a
    configured house changes nothing and says so.
    """
    shared = shared_of(ctx)
    try:
        source, written = deploy_defaults(profile=shared.profile, targets=targets, force=force)
    except PermissionError as exc:
        # Separated from the branch below only because it has something to add: the app and host
        # layers live under /etc and a deploy there needs root, which is the single likeliest
        # reason this command fails and is not obvious from the library's own message.
        #
        # Re-raised as its OWN class rather than the OSError base, because `error` in the envelope
        # is what a caller branches on: a permission problem it can fix by choosing another target
        # must not arrive looking like every other OSError this command can produce.
        hint = PermissionError(f"{exc}. Writing the app or host layer needs root; --target user does not.")
        report_failure(hint, command=f"{service_command} config-deploy", mode=shared.mode)
        ctx.exit(ExitCode.ERROR)
    except Exception as exc:  # noqa: BLE001 - CLI edge
        report_failure(exc, command=f"{service_command} config-deploy", mode=shared.mode)
        ctx.exit(ExitCode.ERROR)
    if shared.mode.machine:
        report = DeployReport(
            source=str(source), targets=[target.lower() for target in targets], written=written, profile=shared.profile
        )
        write_envelope(
            Envelope[DeployReport](ok=True, command=f"{service_command} config-deploy", data=report),
            mode=shared.mode,
        )
        return
    if not written:
        sys.stdout.write("nothing written: every target file was already there (--force overwrites)\n")
        return
    for path in written:
        sys.stdout.write(f"wrote {path}\n")
