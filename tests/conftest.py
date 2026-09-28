"""Cut every test off from this machine's configuration, and from a shared coverage database.

The first half matters more than it looks. The service reads its settings through
``lib_layered_config``, so a test that asserts "this setting is missing" is really asserting
something about the machine it runs on: a developer with
``~/.config/soundtouch-zonemaster/config.toml`` would see a different answer from CI, and the
failure would read as a defect in whatever was just changed. So every test gets the layers above
the defaults pointed at an empty directory of its own.

The defaults layer needs the same care for a different reason. It is ``defaultconfig.toml`` plus
every file in ``defaultconfig.d`` beside it, and a checkout keeps its private values there, in
gitignored ``9N-<scope>-rnhome.toml`` files. So the whole session reads a COPY of that tree holding
only what git tracks, through the loader's own seam (``loader.defaults_from``): a developer's
``bind_ip`` then cannot turn up in a test as if it were a default, and a local run sees exactly what
CI sees.

The second half is the template's, and it is about where this tree LIVES: coverage.py keeps its
trace data in a SQLite database, SQLite wants POSIX locking, and this checkout sits on a network
share or a filesystem without POSIX locking. Redirecting that database to local disk needs
``COVERAGE_FILE`` set in the process environment BEFORE pytest starts: pytest-cov's own plugin
builds its ``coverage.Coverage()`` in a ``tryfirst`` hook on ``pytest_load_initial_conftests``,
which runs before ANY conftest.py is even imported, so nothing a conftest does - a hook, or even
this module's own top level - can still redirect it (measured against pytest-cov 7.1.0 / coverage
7.16.2). The two ways this suite is actually run both already set it early enough:
``default_cicd_public.yml`` at the job-step level, and ``bmk``'s own stage runner in the
subprocess environment it launches pytest with. A bare ``pytest --cov`` on this checkout,
bypassing both, writes its database wherever ``pyproject.toml``'s ``[tool.coverage.run]`` defaults
it to - on the network share this paragraph exists to keep it off - so export ``COVERAGE_FILE``
yourself first if you run it that way.

What is deliberately NOT here: a ``sys.path`` block, because ``pythonpath = ["src"]`` in
``pyproject.toml`` does that, and ``.env`` loading, because a checkout's ``.env`` is exactly what the
first half exists to keep out. The hardware tests read their own settings through
``tests/e2e_config.py``.
"""

from __future__ import annotations

import os
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import hang_watchdog
import pytest
from lib_layered_config import read_config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from soundtouch_zonemaster.__init__conf__ import LAYEREDCONF_APP, LAYEREDCONF_SLUG, LAYEREDCONF_VENDOR
from soundtouch_zonemaster.adapters.config import loader
from soundtouch_zonemaster.adapters.files.house_schema import METADATA
from soundtouch_zonemaster.domain.database_url import masked
from soundtouch_zonemaster.domain.secret import Secret

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine

_LAYER_ROOTS = (
    # Linux: the app and host layers hang off /etc, the user layer off $XDG_CONFIG_HOME.
    ("LIB_LAYERED_CONFIG_ETC", "etc"),
    ("XDG_CONFIG_HOME", "xdg"),
    # macOS and Windows, so the suite is as isolated on a CI cell as it is here.
    ("LIB_LAYERED_CONFIG_MAC_APP_ROOT", "mac-app"),
    ("LIB_LAYERED_CONFIG_MAC_HOME_ROOT", "mac-home"),
    ("LIB_LAYERED_CONFIG_PROGRAMDATA", "programdata"),
    ("LIB_LAYERED_CONFIG_APPDATA", "appdata"),
    ("LIB_LAYERED_CONFIG_LOCALAPPDATA", "localappdata"),
)


def pytest_configure(config: pytest.Config) -> None:
    """Arm the hang watchdog. This hook cannot redirect pytest-cov's data file; see the module docstring.

    ``config`` is unread: pytest matches this hook by NAME and passes what the hook spec declares.
    An earlier version of this hook tried to set ``COVERAGE_FILE`` here when it was still unset,
    on the theory that a later reader would pick it up; measured against pytest-cov 7.1.0 /
    coverage 7.16.2, pytest-cov had already opened its ``Coverage()`` before this hook - or even
    this module's own top level - could run, so the assignment changed nothing for it (confirmed
    by moving it to import time and watching the data file still land at coverage's default
    location). It is gone rather than kept as a no-op.
    """
    # A hang in CI otherwise burns the job's six hours and reports nothing (OPEN-WORK rank 223).
    hang_watchdog.arm_in_ci(config)


def _untracked(_directory: str, names: list[str]) -> list[str]:
    """The names a checkout keeps out of git, so the copy below holds what CI's checkout holds."""
    return [name for name in names if loader.is_private_file(name)]


@pytest.fixture(scope="session", autouse=True)
def tracked_defaults_only(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Read the defaults layer from a copy of the tracked files, for the whole session.

    Yields the copied base file. Its companion directory keeps the name the library looks for, so a
    provenance path still ends in ``defaultconfig.d/<file>``.
    """
    shipped = loader.PACKAGED_DEFAULTS
    copy = tmp_path_factory.mktemp("tracked-defaults") / shipped.name
    shutil.copy2(shipped, copy)
    shutil.copytree(shipped.with_suffix(".d"), copy.with_suffix(".d"), ignore=_untracked)
    with loader.defaults_from(copy):
        yield copy


@pytest.fixture(autouse=True)
def isolated_config_layers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point every config layer at an empty tree, and clear what a previous test merged.

    Yields the root, so a test that wants a file in a layer can write one: the app layer is
    ``<root>/etc/xdg/soundtouch-zonemaster/config.toml`` and the user layer is
    ``<root>/xdg/soundtouch-zonemaster/config.toml``.

    The dotenv layer is deliberately NOT redirected. Walking up from the working directory for a
    ``.env`` is the library's default, and a ``.env`` in the checkout is a layer the hardware tests
    still read; the tests below therefore assert on named keys rather than on the whole merged
    mapping.
    """
    root = tmp_path / "config-layers"
    for variable, leaf in _LAYER_ROOTS:
        monkeypatch.setenv(variable, str(root / leaf))
    for name in [name for name in os.environ if name.startswith(loader.ENV_PREFIX)]:
        monkeypatch.delenv(name, raising=False)
    loader.clear_config_cache()
    yield root
    loader.clear_config_cache()


_THE_HOUSE_S_HOST_LAYER = """[prototype]
never_touch = [{ ip = "192.168.0.30", name = "Room5", why = "the Lifestyle console" }]
"""


@pytest.fixture
def a_house_that_protects_its_console(isolated_config_layers: Path) -> Path:
    """Put the house's own entry where the deployed host keeps it: the host layer of THIS machine.

    The wheel ships ``never_touch`` empty, so a test about the refusal has to supply the house the
    way a house does - as a file in its host layer - rather than lean on a default that no longer
    names anybody's console. Returns the file it wrote.
    """
    written = isolated_config_layers / "etc" / "soundtouch-zonemaster" / "hosts" / f"{socket.gethostname()}.toml"
    written.parent.mkdir(parents=True)
    written.write_text(_THE_HOUSE_S_HOST_LAYER, encoding="utf-8")
    loader.clear_config_cache()
    return written


POSTGRES_URL_ENV = "ZONEMASTER_TEST_POSTGRES_URL"
"""A PostgreSQL URL (no password; the store refuses one in the URL itself) to run every store
test against as well. Checked first, so it still overrides the checkout's own ``.env`` below when
both are set. CI sets neither, so it runs the store tests on SQLite alone; a local run, ``make
test`` included, also runs them against PostgreSQL whenever the environment or the checkout's
``.env`` names one. The URL MUST name a THROWAWAY database: every store test drops the house
tables in it, both before and after. That database is shared by every checkout that names it, so
two machines or two sessions must not run the store tests against it at the same time."""

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _postgres_dotenv() -> dict[str, str]:
    """The ``ZONEMASTER_TEST_POSTGRES_URL`` and ``_PASSWORD`` keys from the checkout's own
    gitignored ``.env``, read the same way ``tests/e2e_config.py`` reads its own house-specific
    settings: through ``lib_layered_config`` with an explicit ``dotenv_path``, rather than a new
    parser. This module's docstring says ``.env`` loading is deliberately absent from the six
    layers every other test runs against - that still holds; this is the one place that reads it,
    for the one setting that names a THROWAWAY database rather than anything a test's own
    assertions depend on. A missing ``.env``, or a missing key inside it, reads back as ``""``.
    """
    config = read_config(
        vendor=LAYEREDCONF_VENDOR, app=LAYEREDCONF_APP, slug=LAYEREDCONF_SLUG, dotenv_path=_REPO_ROOT / ".env"
    )
    return {
        "url": str(config.get("zonemaster_test_postgres_url", default="")),
        "password": str(config.get("zonemaster_test_postgres_password", default="")),
    }


def _resolve_postgres() -> tuple[str | None, Secret | None]:
    """The PostgreSQL URL to run the store tests against (``None`` for SQLite alone), and its
    password when this suite knows it.

    ``os.environ[POSTGRES_URL_ENV]`` wins when set; the checkout's own ``.env`` is read only when
    it is not, and then only once, so CI - which sets neither - runs SQLite only. The ``.env``'s
    password belongs to the ``.env``'s URL alone: a URL from the environment may name another
    server, so it is neither handed to libpq nor returned for one.
    """
    from_env = os.environ.get(POSTGRES_URL_ENV)
    if from_env:
        return from_env, None
    dotenv = _postgres_dotenv()
    url = dotenv["url"] or None
    if url is None:
        return None, None
    _export_pgpassword(dotenv["password"])
    return url, Secret(dotenv["password"]) if dotenv["password"] else None


def _export_pgpassword(password: str) -> None:
    """Hand libpq the PostgreSQL arm's password as ``PGPASSWORD``, never in a URL.

    Only when ``PGPASSWORD`` is not already set in this process's environment - a developer who
    already exports it, or who relies on ``~/.pgpass``, is left alone - and at collection time, so
    every subprocess the store tests spawn inherits it too. The value is never printed, logged, or
    put into an assertion message anywhere in this module.
    """
    if password and not os.environ.get("PGPASSWORD"):
        os.environ["PGPASSWORD"] = password


POSTGRES_URL, _POSTGRES_PASSWORD = _resolve_postgres()
"""Resolved once at collection time. ``None`` means the store tests run on SQLite alone."""


def _backends() -> list[str]:
    return ["sqlite", "postgresql"] if POSTGRES_URL else ["sqlite"]


_POSTGRES_CONNECT_TIMEOUT_S = 5
"""How long one connection attempt to the test server may take before libpq gives up."""


def _postgres_engine(url: str, *, password: Secret | None = None) -> Engine:
    """An engine for the test server that gives up after a few seconds on an unreachable host
    instead of waiting out the TCP connect timeout once per test. ``password`` is passed as a
    connect argument when given, so a test that has taken ``PGPASSWORD`` away can still clean up."""
    connect_args: dict[str, object] = {"connect_timeout": _POSTGRES_CONNECT_TIMEOUT_S}
    if password is not None:
        connect_args["password"] = password.reveal()
    return create_engine(url, connect_args=connect_args)


def _empty_postgres(url: str, *, password: Secret | None = None) -> None:
    """Drop every house table and Alembic's own, so each test starts from a database never used.

    Destructive on purpose: the caller-supplied URL must name a throwaway database, because this
    runs before AND after every test that uses it.
    """
    engine = _postgres_engine(url, password=password)
    with engine.begin() as connection:
        METADATA.drop_all(connection)
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    engine.dispose()


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Refuse the whole run ONCE when the configured PostgreSQL server cannot be reached.

    Runs last, so after ``-k`` and ``-m`` have deselected what they will: a run that selects no
    PostgreSQL case never connects. An unreachable configured server is an error rather than a
    skip - the configuration says those cases should run - and one clear message beats a setup
    error per test. The message names the database through the domain's mask and the failure by
    its type only.
    """
    if POSTGRES_URL is None or not any(_uses_postgres(item) for item in items):
        return
    engine = _postgres_engine(POSTGRES_URL)
    try:
        with engine.connect():
            pass
    except SQLAlchemyError as exc:
        message = (
            f"{masked(POSTGRES_URL)}: the PostgreSQL server the store tests are configured to use "
            f"({POSTGRES_URL_ENV}) cannot be reached ({type(exc).__name__}); start it, fix the URL, "
            "or deselect those cases with -k 'not postgresql'"
        )
        raise pytest.UsageError(message) from None
    finally:
        engine.dispose()


def _uses_postgres(item: pytest.Item) -> bool:
    if "postgres_login" in getattr(item, "fixturenames", ()):
        return True
    callspec = getattr(item, "callspec", None)
    return callspec is not None and callspec.params.get("house_database") == "postgresql"


@pytest.fixture(params=_backends())
def house_database(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[str]:
    """The house database a store test runs against: a SQLite file always, PostgreSQL when configured."""
    if request.param == "sqlite":
        yield str(tmp_path / "house.sqlite")
        return
    assert POSTGRES_URL is not None, "this fixture only parametrizes postgresql when POSTGRES_URL is set"
    _empty_postgres(POSTGRES_URL)
    yield POSTGRES_URL
    _empty_postgres(POSTGRES_URL)


@dataclass(frozen=True)
class PostgresLogin:
    """The PostgreSQL arm's URL and its password, for a test that hands the password over itself."""

    url: str
    password: Secret


@pytest.fixture
def postgres_login() -> Iterator[PostgresLogin]:
    """The PostgreSQL arm's database, emptied before and after, with the password its ``.env`` gives.

    Skipped like every other PostgreSQL case when the arm is not configured, and also when the URL
    came from the environment rather than the ``.env``: this suite knows a password only for the
    ``.env``'s own server. The password is passed to the cleanup explicitly, so a test that removes
    ``PGPASSWORD`` from its environment does not take the cleanup's login with it.
    """
    if POSTGRES_URL is None:
        pytest.skip(f"no PostgreSQL arm configured ({POSTGRES_URL_ENV})")
    if _POSTGRES_PASSWORD is None:
        pytest.skip("the PostgreSQL arm's password is known only when its URL comes from the checkout's .env")
    _empty_postgres(POSTGRES_URL, password=_POSTGRES_PASSWORD)
    yield PostgresLogin(url=POSTGRES_URL, password=_POSTGRES_PASSWORD)
    _empty_postgres(POSTGRES_URL, password=_POSTGRES_PASSWORD)
