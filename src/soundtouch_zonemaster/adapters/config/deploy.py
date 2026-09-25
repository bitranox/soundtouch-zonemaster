"""Writing the defaults that ship inside the wheel into a layer, as files somebody can edit.

The library call and the identity it needs are here rather than in the command, so that
``lib_layered_config`` and the vendor/app/slug stay inside this package: what the command does is
parse, call this, and report. The lines are the archive's own, moved rather than rewritten.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lib_layered_config import deploy_config

from ...__init__conf__ import LAYEREDCONF_APP, LAYEREDCONF_SLUG, LAYEREDCONF_VENDOR
from .loader import default_config_path

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

__all__ = ["DEPLOY_TARGETS", "deploy_defaults"]


DEPLOY_TARGETS = ("app", "host", "user")
"""The three layers a file can be written to. ``dotenv`` and ``env`` are not files we own, and
``defaults`` is the one inside the wheel."""


_WROTE = frozenset({"created", "overwritten"})
"""Which deploy outcomes mean a file now holds our defaults. Everything else - a target that was
already there and was left alone - is reported as nothing written, which is the honest answer and
is what tells a reader that ``--force`` is the flag they wanted."""


def deploy_defaults(*, profile: str | None, targets: Sequence[str], force: bool) -> tuple[Path, list[str]]:
    """The defaults file that was copied, and every destination that now holds it.

    Raises whatever the library raises, ``PermissionError`` above all: the app and host layers live
    under ``/etc`` and writing one needs root. The command is what turns that into a sentence,
    because it is the half that knows which output shape was asked for.
    """
    source = default_config_path()
    results = deploy_config(
        source=source,
        vendor=LAYEREDCONF_VENDOR,
        app=LAYEREDCONF_APP,
        slug=LAYEREDCONF_SLUG,
        profile=profile,
        targets=[target.lower() for target in targets],
        force=force,
    )
    written = [
        str(entry.destination)
        for result in results
        for entry in (result, *result.dot_d_results)
        if entry.action.value in _WROTE
    ]
    return source, written
