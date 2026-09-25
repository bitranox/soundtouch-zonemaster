"""``config``: the merged configuration, and which layer and file each value came from."""

from __future__ import annotations

import json
import sys
from typing import Any

import rich_click as click
from lib_layered_config import REDACTED_PLACEHOLDER, Layer
from pydantic import BaseModel

from ....__init__conf__ import service_command
from ....application.outcome import ExitCode
from ...config.display import flatten, mask_values_from_layer, mask_values_from_private_files, where
from ...config.errors import ConfigInputError
from ...config.settings_map import CONFIGURABLE_NAMES
from ..context import config_for, shared_of
from ..envelope import Envelope, report_failure, write_envelope
from ..typed_click import option

__all__ = ["ConfigReport", "cli_config"]


class ConfigReport(BaseModel):
    """The merged configuration and, per dotted key, the layer and file that produced it."""

    profile: str | None
    config: dict[str, Any]
    provenance: dict[str, Any]


_SET_BY_HAND: dict[str, Any] = {"layer": "--set", "path": None}
"""The origin of a value a ``--set`` wrote, since the layers below no longer decide it."""


@click.command("config", context_settings={"help_option_names": ["-h", "--help"]})
@option("--section", "only", default=None, help="show one section instead of all of them")
@option("--redact", is_flag=True, help="mask secret-looking keys AND everything from a .env or a private -rnhome file")
@click.pass_context
def cli_config(ctx: click.Context, *, only: str | None, redact: bool) -> None:
    """Show the merged configuration, and which layer and file each value came from.

    This is the view of the FILES, which is not quite the view the service runs on: an option
    typed on the command line beats every layer shown here, and the service's own ``--json``
    envelope is what reports the values it actually used.

    Reading this from inside a checkout also shows the checkout's private ``-rnhome`` override
    files and any gitignored ``.env`` (walking up from the working directory for one is the
    library's default). ``--redact`` masks everything from either, and is what to reach for before
    pasting the output anywhere.
    """
    shared = shared_of(ctx)
    try:
        merged = config_for(shared)
        values = flatten(merged.config.as_dict(redact=redact), prefix=only, known_names=CONFIGURABLE_NAMES)
    except ConfigInputError as exc:
        report_failure(exc, command=f"{service_command} config", mode=shared.mode)
        ctx.exit(ExitCode.ERROR)
    provenance = {key: _SET_BY_HAND if key in merged.overridden else merged.config.origin(key) for key, _ in values}
    if redact:
        # The library masks by key NAME, which leaves a harmless-looking one like `e2e_host` in
        # clear. A `.env` holds what is true of ONE machine, so with --redact none of it is shown.
        values = mask_values_from_layer(values, provenance, layer=Layer.DOTENV.value, mask=REDACTED_PLACEHOLDER)
        # A private override file is per-machine in the same way, but it reports the DEFAULTS layer.
        values = mask_values_from_private_files(values, provenance, mask=REDACTED_PLACEHOLDER)
    if shared.mode.machine:
        report = ConfigReport(
            profile=shared.profile,
            config=dict(values),
            provenance={key: origin for key, origin in provenance.items() if origin is not None},
        )
        write_envelope(
            Envelope[ConfigReport](ok=True, command=f"{service_command} config", data=report), mode=shared.mode
        )
        return
    if only is not None and not values:
        # Printing nothing here would be the silence the refusal was written to avoid, and this is
        # the case an operator meets most: the scopes that describe one machine are empty until
        # somebody configures that machine.
        sys.stdout.write(f"{only}: nothing in any layer sets it. It is a name this program reads.\n")
        return
    for key, value in values:
        sys.stdout.write(f"{key} = {json.dumps(value)}    # {where(provenance.get(key))}\n")
