"""The domain layer reaches nothing: no I/O, no framework, not even behind TYPE_CHECKING.

This is the half of the purity rule that ``lint-imports`` cannot see. The project sets
``exclude_type_checking_imports = true``, which is right for the layers contract - an annotation is
not a dependency - but it means a ``if TYPE_CHECKING: from pydantic import BaseModel`` inside
``domain/`` would pass every contract while putting the framework back into the rules. This reads
the source instead, so a forbidden name is caught wherever it is written.

The checker is proved rather than trusted: :func:`test_the_checker_fires_on_a_planted_violation`
runs it over a module that is deliberately impure and requires it to name every offence. A checker
that silently matched nothing would otherwise report a clean domain for as long as nobody looked.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

DOMAIN = Path(__file__).resolve().parents[1] / "src" / "soundtouch_zonemaster" / "domain"

FORBIDDEN_ROOTS = frozenset(
    {
        # I/O and concurrency: a rule that waits, reads or listens is not a rule.
        "asyncio",
        "os",
        "pathlib",
        "socket",
        "subprocess",
        "threading",
        # Frameworks: these belong at an adapter boundary, which is where the parsing lives.
        "click",
        "google",
        "httpx",
        "lib_cli_exit_tools",
        "lib_layered_config",
        "pydantic",
        "rich_click",
        "websockets",
    }
)
"""Roots no module under ``domain/`` may name. Matched on the first dotted segment, so
``google.protobuf`` is caught by ``google`` and ``os.path`` by ``os``."""


def imported_roots(source: str) -> set[str]:
    """Every module root a source file imports, TYPE_CHECKING blocks included.

    Relative imports are skipped: they name a sibling inside ``domain/``, which is what the
    layers contract governs, and a dotted level has no root to judge here.
    """
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def offences(source: str) -> set[str]:
    """Which forbidden roots this source names."""
    return imported_roots(source) & FORBIDDEN_ROOTS


def domain_modules() -> list[Path]:
    """Every module the domain layer ships, the package marker included."""
    return sorted(DOMAIN.glob("*.py"))


def test_the_domain_layer_has_modules_to_check() -> None:
    """The control for every test below: a glob that found nothing would pass them all."""
    found = domain_modules()
    assert len(found) >= 10, f"expected the M2 domain modules, found {[p.name for p in found]}"
    assert (DOMAIN / "__init__.py") in found


@pytest.mark.parametrize("module", domain_modules(), ids=lambda p: p.name)
def test_a_domain_module_reaches_no_io_and_no_framework(module: Path) -> None:
    named = offences(module.read_text(encoding="utf-8"))
    assert not named, f"{module.name} imports {sorted(named)}, which the domain layer may not name"


def test_the_checker_fires_on_a_planted_violation() -> None:
    """The positive control. Every forbidden shape is planted, and all of them must be reported.

    Including the two the import graph cannot see: an import inside ``if TYPE_CHECKING`` and one
    inside a function body.
    """
    planted = """
from __future__ import annotations

import os
from typing import TYPE_CHECKING

import httpx
from pydantic import BaseModel
from google.protobuf import message

if TYPE_CHECKING:
    import asyncio

    from lib_layered_config import Config


def later() -> None:
    import socket
"""
    assert offences(planted) == {"asyncio", "google", "httpx", "lib_layered_config", "os", "pydantic", "socket"}


def test_the_checker_passes_a_module_that_only_imports_what_domain_may_use() -> None:
    """The negative control, so the test above is not passing because everything matches."""
    allowed = """
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from .enums import ChannelKind

if TYPE_CHECKING:
    from collections.abc import Mapping
"""
    assert offences(allowed) == set()
