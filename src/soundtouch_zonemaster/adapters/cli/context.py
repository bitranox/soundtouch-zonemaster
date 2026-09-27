"""What one invocation parsed before any subcommand ran, and how a subcommand reaches it.

The service is a click GROUP, so the output shape and which configuration to read are decided by
the group and needed again by ``config`` and ``config-deploy``. Click's own channel for that is
``ctx.obj``, and :func:`shared_of` is the one read of it.

The prototype is a single command and has no subcommand to hand anything to, but it parses exactly
the same three things, so it builds one of these too rather than growing its own pair.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, ConfigDict

from ..config.loader import get_config
from ..config.overrides import apply_set_overrides
from ..logging.narration import log
from .boundary import (
    configured_database_text,
    configured_settings,
    database_password_of,
    database_setting_of,
    scoped_to_the_configured_database,
)
from .envelope import OutputMode

if TYPE_CHECKING:
    import rich_click as click

    from ...application.ports import OpenHouseStore
    from ...domain.logfn import LogFn
    from ...domain.secret import Secret
    from ..config.overrides import Merged

__all__ = [
    "DatabaseChoice",
    "Shared",
    "config_for",
    "database_for",
    "named_database",
    "remember_store_opener",
    "shared_of",
    "store_opener_of",
]

_STORE_OPENER = "open_store"
"""The ``ctx.meta`` key the store opener is kept under, since ``ctx.obj`` already carries Shared."""


class Shared(BaseModel):
    """What the group parsed and a subcommand needs: the output shape and which config to read."""

    model_config = ConfigDict(frozen=True)

    mode: OutputMode
    profile: str | None
    overrides: tuple[str, ...]
    database: str | None = None


def config_for(shared: Shared) -> Merged:
    """The merged configuration this invocation should read, ``--set`` applied on top."""
    return apply_set_overrides(get_config(profile=shared.profile), shared.overrides)


def shared_of(ctx: click.Context) -> Shared:
    """What the group parsed, for a subcommand. It is always there: the group always runs first."""
    parent = ctx.find_object(Shared)
    if parent is None:  # pragma: no cover - only reachable if a subcommand is invoked without the group
        return Shared(mode=OutputMode(machine=False, indent=2), profile=None, overrides=())
    return parent


def remember_store_opener(ctx: click.Context, opener: OpenHouseStore) -> None:
    """Keep the store opener where every subcommand's context can find it."""
    ctx.meta[_STORE_OPENER] = opener


def store_opener_of(ctx: click.Context) -> OpenHouseStore:
    """The store opener the group was handed. Always there: the group runs first."""
    return cast("OpenHouseStore", ctx.meta[_STORE_OPENER])


@dataclass(frozen=True)
class DatabaseChoice:
    """The database an invocation opens and the password it opens it with, always together, so a
    verb that has the one cannot open the store without having been handed the other."""

    setting: str
    password: Secret | None


def database_for(shared: Shared) -> DatabaseChoice:
    """The database this invocation means - typed, else configured, else refused by name (exit 2) -
    and the password the configuration layers give for it (``database.password``; there is no
    command-line option for it, since argv is visible to every user of the machine). That password
    goes only with the configured database: see
    :func:`~.boundary.scoped_to_the_configured_database`, which the service run shares.

    An EMPTY configured setting is handed on as it is, and the store refuses it by name: a verb
    that is about to write must not answer as if no database had been meant."""
    configured = _scoped_settings(shared, narrate=log)
    password = database_password_of(configured)
    if shared.database is not None:
        return DatabaseChoice(setting=shared.database, password=password)
    return DatabaseChoice(setting=database_setting_of(configured), password=password)


def named_database(shared: Shared, *, narrate: LogFn) -> DatabaseChoice | None:
    """The database this invocation names, or ``None`` when nothing names one: no ``--database``
    typed, and ``database.url`` unset or empty in every layer. For a reader (``config``) whose
    answer to "no database" is "nothing stored" rather than a refusal; an empty setting is one
    nobody filled in, so it names nothing here. The same choice and password rule as
    :func:`database_for`.

    Reading the layers for this narrates (a stray key, a password left out); ``narrate`` is where
    those lines go, so ``config``, whose STDOUT is the view itself, can keep them out of it."""
    configured = _scoped_settings(shared, narrate=narrate)
    setting = shared.database if shared.database is not None else configured_database_text(configured)
    if not setting:
        return None
    return DatabaseChoice(setting=setting, password=database_password_of(configured))


def _scoped_settings(shared: Shared, *, narrate: LogFn) -> dict[str, Any]:
    """The configured settings, less a password that belongs to another database than the typed one."""
    return scoped_to_the_configured_database(
        configured_settings(config_for(shared).config, narrate=narrate), typed=shared.database, narrate=narrate
    )
