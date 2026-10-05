"""The configuration vocabulary: the files that ship, the scope maps, and the one refusal type.

Three of these earn their keep and the rest are ordinary. A default written in two places drifts
silently - the program keeps working and the file that documents it starts lying - so every value
in ``defaultconfig.d`` is checked against the record it fills. The scope maps are the only
enumeration of the settings, so each is pinned in BOTH directions: a map checked one way can gain
an entry for a field nobody added, or miss a field somebody did. And the settings with no default
are checked to be exactly the ones the files leave commented out.

Adding a field to :class:`ServiceOptions` and forgetting the files turns all three red.

There are TWO maps now, one per program. The service's is the big one; the prototype's has a
single entry, and that entry is the reason the section exists at all - the addresses this house
never touches were a constant in the source until this rebuild, so the one refusal protecting a
real speaker could only be changed by editing the program. A one-entry map still earns a
both-ways pin: what it guards against is somebody adding a second setting to one side only.

The records differ in kind, and the helpers below say so rather than papering over it.
:class:`ServiceOptions` is a frozen dataclass with no framework in it, which is what the rebuild
converted it to; :class:`PrototypeSettings` is a pydantic model, because it is read straight off a
configuration file and nothing else validates it on the way.
"""

from __future__ import annotations

import dataclasses
import re
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast, get_type_hints

import pytest

from soundtouch_zonemaster import __init__conf__
from soundtouch_zonemaster.adapters.cli.prototype import PrototypeSettings
from soundtouch_zonemaster.adapters.config.errors import ConfigInputError
from soundtouch_zonemaster.adapters.config.loader import (
    ENV_PREFIX,
    clear_config_cache,
    default_config_path,
    get_config,
)
from soundtouch_zonemaster.adapters.config.overrides import apply_set_overrides, parse_set_override
from soundtouch_zonemaster.adapters.config.settings_map import (
    PROTOTYPE_SECTIONS,
    PROTOTYPE_SETTINGS,
    SECTIONS,
    SETTINGS,
    config_path_of,
    prototype_settings,
    service_settings,
    unknown_settings,
)
from soundtouch_zonemaster.application.options import ServiceOptions
from soundtouch_zonemaster.domain.secret import Secret

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence


def _scope_dir() -> Path:
    """The companion directory the library expands: ``defaultconfig.toml`` -> ``defaultconfig.d``."""
    return default_config_path().with_suffix(".d")


def _scope_files() -> list[Path]:
    """The shipped scope files, in the order the loader reads them.

    Only ``.toml``: the tracked ``*-rnhome.toml.example`` files sit in the same directory and the
    library never reads them, so neither does this. The directory itself is the conftest's copy of
    the tracked tree, so a checkout's private ``-rnhome`` overrides are not in it either.
    """
    return sorted(path for path in _scope_dir().iterdir() if path.suffix == ".toml")


def _file_for_section(section: str) -> Path:
    """The one shipped file that declares ``[section]``, header included.

    One section, one file (pinned by ``test_the_base_file_carries_no_settings_and_every_section_has_one_file``),
    so this is unambiguous even for a section every value in which is commented out: the header
    line ``[section]`` still parses as an empty table.
    """
    for path in _scope_files():
        if section in tomllib.loads(path.read_text(encoding="utf-8")):
            return path
    message = f"no shipped file declares [{section}]"
    raise AssertionError(message)


def _shipped() -> dict[str, Any]:
    """Every value the shipped files actually set, by dotted config path."""
    found: dict[str, Any] = {}
    for path in _scope_files():
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
        for section, table in parsed.items():
            assert isinstance(table, dict), f"{path.name}: [{section}] must be a table"
            found.update({f"{section}.{key}": value for key, value in cast("dict[str, Any]", table).items()})
    return found


def _fields(record: type, prefix: str = "") -> Iterator[tuple[str, dataclasses.Field[Any]]]:
    """Every leaf field of a frozen record, dotted, descending into nested records.

    The annotations are strings (``from __future__ import annotations``) and ``Path`` and
    ``Secret`` are imported for typing only, so they are resolved with the names supplied rather
    than read off the field - which is the difference between knowing ``channel_policy`` is a
    record and guessing it from whatever its default happens to be.
    """
    hints = get_type_hints(record, localns={"Path": Path, "Secret": Secret})
    for field in dataclasses.fields(record):
        annotation = hints[field.name]
        if dataclasses.is_dataclass(annotation):
            yield from _fields(cast("type", annotation), f"{prefix}{field.name}.")
        else:
            yield f"{prefix}{field.name}", field


def _comparable(value: object) -> object:
    """A tuple and a list of the same things are the same setting; TOML can only write one.

    A record inside one counts too: the prototype's never-touch entries are frozen records and a
    config file writes them as tables, so both sides are reduced to plain mappings first.
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _comparable(dataclasses.asdict(value))
    if isinstance(value, (list, tuple)):
        return [_comparable(item) for item in cast("Sequence[object]", value)]
    if isinstance(value, dict):
        return {key: _comparable(item) for key, item in cast("dict[str, object]", value).items()}
    return value


def _default_of(field: dataclasses.Field[Any]) -> object:
    """What the record falls back to for a field, whichever way it declares it."""
    if field.default_factory is not dataclasses.MISSING:
        return field.default_factory()
    return field.default


def _is_required(field: dataclasses.Field[Any]) -> bool:
    """Whether a field must be given: no default, and no factory to make one."""
    return field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING


def _has_nothing_to_show(field: dataclasses.Field[Any]) -> bool:
    """Whether a file can show this field's default at all: no default, or a default of ``None``.

    ``None`` is not a TOML value, so a field that falls back to it - the old files, now that the
    database is what a deployment must set, and the database password - is commented out exactly
    like a required one, even though a run may still leave it unset.
    """
    return _is_required(field) or _default_of(field) is None


def test_the_scope_map_covers_the_record_exactly_and_nothing_else() -> None:
    """The map is the only enumeration of the settings, so it is checked both ways. One way only
    lets it gain an entry for a field nobody added, or miss a field somebody did."""
    known = {name for name, _ in _fields(ServiceOptions)}
    targets = list(SETTINGS.values())

    assert set(targets) == known, "every field needs an entry, and every entry needs a field"
    assert len(targets) == len(set(targets)), "no field may be written from two places in a file"


def test_the_prototype_scope_map_covers_its_record_exactly_and_nothing_else() -> None:
    """The same pin on the other program's map. One entry today, and the point of checking both
    ways is the day somebody adds a second setting to the file or to the record alone."""
    known = set(PrototypeSettings.model_fields)
    targets = list(PROTOTYPE_SETTINGS.values())

    assert set(targets) == known, "every field needs an entry, and every entry needs a field"
    assert len(targets) == len(set(targets)), "no field may be written from two places in a file"


def test_every_value_in_the_shipped_files_is_the_records_own_default() -> None:
    """One source of truth. A default changed in the record and not in the files would leave them
    documenting a number the program no longer uses, and nothing else would notice."""
    fields = dict(_fields(ServiceOptions))
    shipped = _shipped()
    assert shipped, "the shipped files must set something"
    for path, written in shipped.items():
        if path in PROTOTYPE_SETTINGS:
            continue  # checked against its own record below
        assert path in SETTINGS, f"{path!r} is in a shipped file but not in a scope map"
        field = fields[SETTINGS[path]]
        assert _comparable(written) == _comparable(_default_of(field)), path


def test_every_value_the_prototype_file_ships_is_that_records_own_default() -> None:
    """The shipped ``never_touch`` is empty, in the file and in the record alike: which box a house
    must never reach is that house's own entry, in a layer or a private override of its own."""
    shipped = {path: value for path, value in _shipped().items() if path in PROTOTYPE_SETTINGS}
    assert shipped, "80-prototype.toml must set something"
    defaults = PrototypeSettings()
    for path, written in shipped.items():
        assert _comparable(written) == _comparable(getattr(defaults, PROTOTYPE_SETTINGS[path])), path


def test_the_files_leave_exactly_the_settings_with_no_default_commented_out() -> None:
    """Three settings describe one deployment and have no default at all - bind_ip, device_id and
    database - because a value here would be a guess about somebody else's machine. The old files and
    the database password have a default of ``None``, which is not a TOML value either, so they are
    shown commented the same way even though a run may leave them unset. Adding a field with nothing
    printable to show and forgetting the files fails here rather than at a startup weeks later."""
    fields = dict(_fields(ServiceOptions))
    nothing_to_show = {name for name, field in fields.items() if _has_nothing_to_show(field)}
    # The marker is the config file's own key, not the field's name: they agree everywhere except
    # `database`, whose field kept its name while its config key moved to `[database] url`. Each
    # name is searched only in the ONE file that owns its section - `registry` also has a `url`,
    # so searching the whole tree's text for "# url = " would match there too.
    shown: set[str] = set()
    for name in nothing_to_show:
        section, _, key = config_path_of(name).partition(".")
        text = _file_for_section(section).read_text(encoding="utf-8")
        if f"# {key} = " in text:
            shown.add(name)
    settable = {SETTINGS[path] for path in _shipped() if path in SETTINGS}

    assert settable == set(fields) - nothing_to_show
    assert shown == nothing_to_show, "every setting with nothing printable needs a commented example in a file"


def test_the_base_file_carries_no_settings_and_every_section_has_one_file() -> None:
    """The shape the sibling projects use, and the reason the split is worth anything: a change to
    one concern is a change to one file, and the base file is the page that explains the layers."""
    base = tomllib.loads(default_config_path().read_text(encoding="utf-8"))
    assert base == {}, "the base file is a header; the settings live one scope per file beside it"

    owners: dict[str, set[str]] = {}
    for path in _scope_files():
        for section in tomllib.loads(path.read_text(encoding="utf-8")):
            owners.setdefault(section, set()).add(path.name)
    for section, files in owners.items():
        assert len(files) == 1, f"[{section}] is split across {sorted(files)}; one section, one file"
    assert set(SECTIONS) | set(PROTOTYPE_SECTIONS) == {
        path.split(".")[0] for path in {**SETTINGS, **PROTOTYPE_SETTINGS}
    }


def test_every_shipped_file_names_the_environment_variable_for_each_of_its_settings() -> None:
    """The files are the reference an operator reads. A setting whose override cannot be found
    from the file it is documented in sends them to the source instead."""
    for path in _scope_files():
        text = path.read_text(encoding="utf-8")
        section = path.stem.partition("-")[2]
        for config_path in {**SETTINGS, **PROTOTYPE_SETTINGS}:
            if config_path.startswith(f"{section}."):
                variable = f"{ENV_PREFIX}{config_path.upper().replace('.', '__')}"
                assert variable in text, f"{path.name} does not name {variable}"


def test_the_env_prefix_is_the_one_the_base_file_documents() -> None:
    assert ENV_PREFIX == "SOUNDTOUCH_ZONEMASTER___"
    assert ENV_PREFIX in default_config_path().read_text(encoding="utf-8")


def test_the_three_identifiers_that_decide_where_the_config_lives_are_the_deployed_ones() -> None:
    """These three name the directories a running house's configuration sits in, and the hardware
    tests read this house's ``.env`` through the same slug. The template's ``rename.sh`` writes
    them, so they are a rebuild away from being something else; a changed one would send the
    service looking somewhere empty and start it on its defaults with nothing said."""
    assert __init__conf__.LAYEREDCONF_VENDOR == "bitranox"
    assert __init__conf__.LAYEREDCONF_APP == "soundtouch-zonemaster"
    assert __init__conf__.LAYEREDCONF_SLUG == "soundtouch-zonemaster"


@pytest.mark.parametrize(
    ("raw", "path", "value"),
    [
        ("zone.bind_ip=203.0.113.190", ("zone", "bind_ip"), "203.0.113.190"),
        ("dialling.window_s=1.9", ("dialling", "window_s"), 1.9),
        ("observer.port=1234", ("observer", "port"), 1234),
        ('membership.consoles_allowed=["AABBCC000012"]', ("membership", "consoles_allowed"), ["AABBCC000012"]),
        ("zone.bind_ip=", ("zone", "bind_ip"), ""),
        ("zone.note=a=b", ("zone", "note"), "a=b"),
    ],
)
def test_a_set_override_is_typed_where_the_library_types_it_and_text_where_it_does_not(
    raw: str, path: tuple[str, ...], value: object
) -> None:
    assert parse_set_override(raw) == (path, value)


SPELLINGS = [
    "null",
    "None",
    "NULL",
    "true",
    "False",
    "10",
    "-7",
    "0",
    "007",
    "0640",
    "1.50",
    "3.5",
    "1e3",
    "+5",
    "1_000",
    "nan",
    "inf",
    '["a", 1]',
    '{"a": 1}',
    "[not json",
    "plain text",
    "a=b",
    '"8675309"',
    "'true'",
    '"null"',
]
"""Every shape the two library layers read differently from plain JSON, plus the quoted forms that
are the way to keep a value as text."""


def _through_the_library(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, key: str, spelling: str) -> object:
    """What the library's own dotenv layer makes of ``SECTION__KEY=<spelling>``: the rule ``--set``
    is held to. Read through the loader, so it is the library's code and not a copy of it."""
    section, name = key.split(".")
    dotenv = tmp_path / "rule.env"
    dotenv.write_text(f"{section.upper()}__{name.upper()}={spelling}\n", encoding="utf-8")
    monkeypatch.delenv(f"{ENV_PREFIX}{section.upper()}__{name.upper()}", raising=False)
    clear_config_cache()
    try:
        return get_config(dotenv_path=str(dotenv))[section][name]
    finally:
        clear_config_cache()


def _through_the_environment(monkeypatch: pytest.MonkeyPatch, key: str, spelling: str) -> object:
    section, name = key.split(".")
    monkeypatch.setenv(f"{ENV_PREFIX}{section.upper()}__{name.upper()}", spelling)
    clear_config_cache()
    try:
        return get_config()[section][name]
    finally:
        clear_config_cache()
        monkeypatch.delenv(f"{ENV_PREFIX}{section.upper()}__{name.upper()}")


@pytest.mark.usefixtures("isolated_config_layers")
@pytest.mark.parametrize("key", ["zone.note", "database.password"], ids=["plain-key", "sensitive-key"])
@pytest.mark.parametrize("spelling", SPELLINGS)
def test_a_set_override_reads_every_spelling_as_the_library_s_own_layers_do(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, key: str, spelling: str
) -> None:
    """One rule for the two top layers (OPEN-WORK 231): ``--set`` and a ``.env`` line read the same
    text as the same value, on an ordinary key and on a sensitive one, where ``null`` stays text."""
    expected = _through_the_library(monkeypatch, tmp_path, key, spelling)
    got = parse_set_override(f"{key}={spelling}")[1]
    assert (got, type(got)) == (expected, type(expected))


@pytest.mark.usefixtures("isolated_config_layers")
@pytest.mark.parametrize("spelling", [s for s in SPELLINGS if s[:1] not in "\"'"])
def test_the_environment_and_a_dotenv_agree_on_every_unquoted_spelling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spelling: str
) -> None:
    """The control that makes the rule above one rule: the library's environment layer reads every
    unquoted spelling exactly as its dotenv layer does, so following one follows both."""
    for key in ("zone.note", "database.password"):
        assert _through_the_environment(monkeypatch, key, spelling) == _through_the_library(
            monkeypatch, tmp_path, key, spelling
        )


@pytest.mark.parametrize("raw", ["nonsense", "zone=1", ".key=1", "zone.=1", "=1"])
def test_a_set_override_that_is_not_a_section_and_a_key_is_refused(raw: str) -> None:
    with pytest.raises(ConfigInputError):
        parse_set_override(raw)


def test_two_set_overrides_that_disagree_about_a_table_are_refused() -> None:
    """Applying half of what was asked for is worse than refusing all of it, and a value under a
    value is a typo rather than an intention."""
    config = get_config()
    with pytest.raises(ConfigInputError, match="already given a value"):
        apply_set_overrides(config, ("zone.bind_ip=x", "zone.bind_ip.port=1"))


def test_nothing_set_returns_the_same_configuration_and_claims_nothing() -> None:
    config = get_config()
    merged = apply_set_overrides(config, ())

    assert merged.config is config
    assert merged.overridden == frozenset()


def test_a_set_override_says_which_keys_it_wrote() -> None:
    """``Config.with_overrides`` keeps each key's original provenance, so the only record that a
    value came from the command line is the one kept here."""
    merged = apply_set_overrides(get_config(), ("dialling.window_s=1.9", "observer.port=1"))

    assert merged.overridden == {"dialling.window_s", "observer.port"}
    assert merged.config["dialling"]["window_s"] == 1.9


def test_the_scope_sections_are_read_into_the_records_flat_field_names() -> None:
    """The whole point of the map: the files are organised by what a person is changing, and the
    record stays the flat thing every caller already reads."""
    settings = service_settings(get_config())

    assert settings["registry_url"] == "http://127.0.0.1:8000"
    assert settings["dial_window_s"] == 0.8
    assert settings["channel_policy"] == {"port": 8080, "backoff_s": [1.0, 2.0, 5.0, 10.0]}
    assert "bind_ip" not in settings, "a commented-out example must not arrive as a value"


def test_one_program_s_section_is_not_read_by_the_other() -> None:
    """The two maps are what keep them apart: each reader looks up only its own paths, so a
    section belonging to the other program yields nothing rather than an unexpected key.

    ``[registry] url`` is the one setting both read, on purpose: it names the service whose BMX
    registry completes a relative Orion location, and the two programs must complete it against
    the same service. Everything else in ``[registry]`` is the service's alone."""
    config = get_config()

    assert "never_touch" not in service_settings(config)
    assert set(prototype_settings(config)) == {"never_touch", "registry_url"}
    assert "registry_poll_s" not in prototype_settings(config)


def test_a_stray_key_in_one_of_our_sections_is_reported_and_one_elsewhere_is_not(
    isolated_config_layers: Path,
) -> None:
    """A ``.env`` key and another program's section in the same file are not misspellings of
    anything here, so looking at every top-level table would cry wolf on both."""
    written = isolated_config_layers / "xdg" / "soundtouch-zonemaster" / "config.toml"
    written.parent.mkdir(parents=True)
    written.write_text("[dialling]\nwindow = 1.4\n[something_else]\nwindow = 1.4\n", encoding="utf-8")

    assert unknown_settings(get_config()) == ["dialling.window"]


def test_a_config_file_that_will_not_parse_is_one_refusal_type_naming_the_file(
    isolated_config_layers: Path,
) -> None:
    """Every way the library can fail arrives as the one exception each command catches. The
    library's message is kept because it names the file and the line, which is the whole answer."""
    broken = isolated_config_layers / "xdg" / "soundtouch-zonemaster" / "config.toml"
    broken.parent.mkdir(parents=True)
    broken.write_text("[zone]\nbind_ip = \n", encoding="utf-8")

    with pytest.raises(ConfigInputError, match=str(broken)):
        get_config()


def test_a_dotenv_that_is_not_text_is_the_same_refusal_naming_the_file_and_not_its_content(
    isolated_config_layers: Path,
) -> None:
    """The ``.env`` is a layer like the files, so bytes in it that are not UTF-8 are refused the same way.

    This is what the floor of ``lib_layered_config>=6.0.0`` is for: releases before it raised a
    bare ``UnicodeDecodeError`` here, which no command catches, so one stray byte in a ``.env``
    ended a start on a traceback that named no file. The line may be the one holding the database
    password, so the refusal says WHERE and never what it read there.
    """
    dotenv = isolated_config_layers / ".env"
    dotenv.parent.mkdir(parents=True)
    dotenv.write_bytes(b"SOUNDTOUCH_ZONEMASTER___ZONE__BIND_IP=192.0.2.1\nX=\xff\xfeNOT-A-SECRET\n")

    with pytest.raises(ConfigInputError, match=re.escape(str(dotenv))) as caught:
        get_config(dotenv_path=str(dotenv))
    assert "NOT-A-SECRET" not in str(caught.value)


def test_a_section_written_as_something_other_than_a_table_reads_as_nothing_said(
    isolated_config_layers: Path,
) -> None:
    """A typo in a file must not stop a house working: the service falls back to its own defaults
    and the option list, which is what it did before there were any files at all."""
    written = isolated_config_layers / "xdg" / "soundtouch-zonemaster" / "config.toml"
    written.parent.mkdir(parents=True)
    written.write_text('zone = "not a table"\n', encoding="utf-8")

    assert "bind_ip" not in service_settings(get_config())
    assert unknown_settings(get_config()) == []
