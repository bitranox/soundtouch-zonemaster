"""The service's edge: what argv and the six config layers said, validated into one record.

The record the rest of the program reads is a frozen dataclass with no framework in it
(``application/options.py``). Getting there from a command line and a stack of files is a parsing
job - a window written ``"0.6"`` in a file is 0.6, a console list arrives as JSON from an
environment variable - so the parsing happens HERE, in a pydantic model that keeps the archive's
fields, coercions and validators exactly.

Keeping the VALIDATORS, and not only the field types, is what makes the refusals identical rather
than similar. A validator that raises something other than a ``ValueError`` aborts a pydantic run
instead of being collected into it, so a bad device id beside a missing address refuses the device
id and never mentions the address. That is what the archive did, and the golden options corpus
records it case by case.

Two checks that look like invariants are here rather than on the record, because they are
questions about the MACHINE rather than about the option set: whether the state file's directory
exists, and - for the other program - whether an address is one this house never touches.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError, field_validator, model_validator

from ...__init__conf__ import service_command
from ...application.options import (
    DEFAULT_BASE_URL,
    MPD_HOST,
    MPD_PORT,
    MPD_REWIND_S,
    REGISTRY_POLL_S,
    ChannelPolicy,
    ServiceOptions,
    default_device_id,
)
from ...application.outcome import ExitCode, OptionsError, device_id_or_refuse, preference_or_refuse, tcp_port_or_refuse
from ...domain.database_url import carries_a_password
from ...domain.dialling import WINDOW_DEFAULT_S
from ...domain.longpress import HOLD_THRESHOLD_DEFAULT_S
from ...domain.membership import UNREACHABLE_TIMEOUT_S
from ...domain.preferences import FADE_DEFAULT_S, HousePreferences, PreferenceName
from ...domain.secret import Secret
from ...domain.switch import POLL_S
from ..config.loader import ENV_PREFIX
from ..config.settings_map import config_path_of, env_name_of, service_settings, unknown_settings
from ..logging.narration import log

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from lib_layered_config import Config

    from ...domain.logfn import LogFn

__all__ = [
    "ChannelPolicyInput",
    "ServiceOptionsInput",
    "configured_settings",
    "database_password_of",
    "database_setting_of",
    "database_text_or_refuse",
    "layered_preferences",
    "merge_service_settings",
    "no_value_anywhere",
    "parse_service_options",
    "scoped_to_the_configured_database",
]

_PREFERENCE_FIELDS = ("dial_window_s", "hold_threshold_s", "mpd_rewind_s", "fade_s", "consoles_allowed")
"""The record fields that are preferences; each one's config path IS its preference name."""

_DATABASE_FIELD = "database"
"""The record field the database setting fills. Its dotted config path comes from the settings
map (:func:`config_path_of`), so no message here spells ``database.url`` itself."""

_CREDENTIAL_FIELD = "database_password"
"""The record field the password setting fills; its config path likewise comes from the map.
Named for what it holds rather than with the word itself, which a secret scanner reads as a
password literal assigned in code."""


class ChannelPolicyInput(BaseModel):
    """A channel policy as a config file delivers it, before it is the record's own.

    Its own model rather than a nested dict, so that a port written ``"x"`` is refused as
    ``channel_policy.port`` and not as the whole table.
    """

    model_config = ConfigDict(frozen=True)

    port: int = ChannelPolicy().port
    backoff_s: tuple[float, ...] = ChannelPolicy().backoff_s

    def record(self) -> ChannelPolicy:
        """The validated policy as the frozen record the program reads."""
        return ChannelPolicy(port=self.port, backoff_s=self.backoff_s)


class ServiceOptionsInput(BaseModel):
    """One service as argv and the config layers delivered it, before it is the record.

    The archive's ``ServiceOptions`` model, field for field and validator for validator. What moved
    is where it sits: the record it produces is a frozen dataclass now, and this is the boundary that
    produces it.
    """

    model_config = ConfigDict(frozen=True)

    bind_ip: str
    device_id: str
    database: str
    """The house database: a URL, or a plain path meaning a SQLite file (``adapters/files/house_db.py``)."""
    database_password: SecretStr | None = None
    """The PostgreSQL password. pydantic's ``SecretStr`` while it is parsed here, so a validation
    error or a repr of this model cannot show it; the record receives the domain's ``Secret``."""
    switch_file: Path | None = None
    """The switch as a file, from before the database: imported once, then not read."""
    state_file: Path | None = None
    """The state as a file, from before the database: imported once, then set aside."""
    channel_file: Path | None = None
    """The channel list as a file, from before the database: imported once, then set aside."""
    registry_url: str = DEFAULT_BASE_URL
    consoles_allowed: tuple[str, ...] = ()
    """Device ids of consoles that may be taken into the zone anyway.

    A Lifestyle console is otherwise left out and not even watched: an API POWER flips its input,
    so a service that reached it would change what somebody is doing in the room it stands in.
    """
    dial_window_s: float = WINDOW_DEFAULT_S
    """How long a speaker's digits are collected into one number before it is read.

    ONE setting for the house, not one per speaker: the design considered per-speaker and declined
    it. The bounds are measured (``domain/dialling.py``). A calibration at a speaker measures it on
    the person who presses, and what it measured outranks this value (``domain/calibration.py``).
    """
    hold_threshold_s: float = HOLD_THRESHOLD_DEFAULT_S
    """How long a key must stay down to be held rather than tapped."""
    registry_poll_s: float = REGISTRY_POLL_S
    switch_poll_s: float = POLL_S
    unreachable_timeout_s: float = UNREACHABLE_TIMEOUT_S
    channel_policy: ChannelPolicyInput = ChannelPolicyInput()
    """Where a speaker's notification channel is and how patiently a lost one is retried."""
    mpd_host: str = MPD_HOST
    """Where MPD answers, for the channels whose sound is a stored playlist it holds."""
    mpd_port: int = MPD_PORT
    mpd_rewind_s: float = MPD_REWIND_S
    """How far back an MPD channel starts when the house comes back to it."""
    fade_s: float = FADE_DEFAULT_S
    """How long a joining box's volume climbs back."""

    @field_validator("device_id")
    @classmethod
    def _twelve_hex_digits(cls, value: str) -> str:
        return device_id_or_refuse(value)

    @field_validator(_DATABASE_FIELD, mode="before")
    @classmethod
    def _database_as_text(cls, value: object) -> str | None:
        return database_text_or_refuse(value)

    @field_validator(_DATABASE_FIELD)
    @classmethod
    def _no_password_in_a_database_url(cls, value: str) -> str:
        """A URL is echoed by envelopes, `config` and logs; the password is the ``database.password``
        setting instead (or libpq's own ``~/.pgpass``). The store refuses it too, for the verbs that
        reach it without this record.

        The rule - which shapes carry a password, and how to tell - lives in
        ``domain/database_url.py`` rather than here, so this layer (which may not import
        ``adapters/files`` and so cannot share SQLAlchemy's own parser) and the store agree on it
        without keeping two copies of it. That rule reads the text no more loosely than
        SQLAlchemy does, which is what lets this layer refuse without the parser.
        """
        if carries_a_password(value):
            message = (
                "refused: the database URL carries a password; "
                f"give it as {config_path_of(_CREDENTIAL_FIELD)} (or keep it in ~/.pgpass) instead"
            )
            raise OptionsError(message, exit_code=ExitCode.REFUSED)
        return value

    @field_validator(_CREDENTIAL_FIELD, mode="before")
    @classmethod
    def _password_as_text(cls, value: object) -> str | None:
        """The text ``SecretStr`` accepts. From the command line the value has already passed
        :func:`configured_settings`; this keeps the same refusal, in the same words, for a caller
        that hands :func:`parse_service_options` a mapping of its own, which pydantic would
        otherwise refuse by the record field's name in its own words."""
        return password_text_or_refuse(value)

    @field_validator("dial_window_s")
    @classmethod
    def _inside_the_measured_bounds(cls, value: float) -> float:
        """Below the floor a two-digit number starts splitting into two; above the ceiling the
        wait stops reading as a wait and starts reading as a fault."""
        return preference_or_refuse(PreferenceName.WINDOW, value)

    @field_validator("mpd_port")
    @classmethod
    def _a_port_a_caller_can_dial(cls, value: int) -> int:
        """Zero means "any free port" to a listener and nothing at all to a caller."""
        return tcp_port_or_refuse(value, what="the mpd port")

    @field_validator("mpd_rewind_s")
    @classmethod
    def _an_overlap_rather_than_a_jump_forward(cls, value: float) -> float:
        """A negative overlap would start LATER than the house stopped, skipping what it did not
        hear, which is the one thing this setting must not be able to do. Zero is allowed and
        means no overlap at all, which is what music rather than speech wants."""
        return preference_or_refuse(PreferenceName.REWIND, value)

    @field_validator("hold_threshold_s")
    @classmethod
    def _a_hold_a_person_can_make(cls, value: float) -> float:
        """Below the floor an ordinary slow tap is read as a hold; above the ceiling a held key
        acts so late that the person has let go and pressed again."""
        return preference_or_refuse(PreferenceName.HOLD, value)

    @field_validator("fade_s")
    @classmethod
    def _a_fade_a_room_does_not_wait_out(cls, value: float) -> float:
        """Above the ceiling a joining room is quiet long enough to read as a fault."""
        return preference_or_refuse(PreferenceName.FADE, value)

    @field_validator("consoles_allowed")
    @classmethod
    def _consoles_named_by_device_id(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """A typo here would allow nothing and say nothing, or allow a box nobody meant."""
        return preference_or_refuse(PreferenceName.CONSOLES, value)

    @model_validator(mode="after")
    def _somewhere_for_every_file_it_names(self) -> ServiceOptionsInput:
        """Refused now rather than hours later, when the first speaker joins and nothing can save.

        The database's directory, and the directory of every old file that is still named, because
        the import renames an old file where it lies. The state file is checked first so that an
        argv with two bad directories refuses with the same message it always did.
        """
        for path, what in (
            (self.state_file, "write the state file in"),
            (self.channel_file, "write the channel file in"),
            (self.switch_file, "read the switch file from"),
            (_database_file(self.database), "keep the house database in"),
        ):
            if path is not None and not path.parent.is_dir():
                message = f"refused: {path.parent} is not a directory to {what}"
                raise OptionsError(message, exit_code=ExitCode.REFUSED)
        return self

    def record(self) -> ServiceOptions:
        """The validated settings as the frozen record the service runs on.

        Every check above has already passed, so the record's own ``__post_init__`` - which
        repeats the device id and the window for a caller that builds one directly - cannot
        refuse what reaches it here.
        """
        return ServiceOptions(
            bind_ip=self.bind_ip,
            device_id=self.device_id,
            database=self.database,
            database_password=_secret_of(self.database_password),
            switch_file=self.switch_file,
            state_file=self.state_file,
            channel_file=self.channel_file,
            registry_url=self.registry_url,
            consoles_allowed=self.consoles_allowed,
            dial_window_s=self.dial_window_s,
            hold_threshold_s=self.hold_threshold_s,
            registry_poll_s=self.registry_poll_s,
            switch_poll_s=self.switch_poll_s,
            unreachable_timeout_s=self.unreachable_timeout_s,
            channel_policy=self.channel_policy.record(),
            mpd_host=self.mpd_host,
            mpd_port=self.mpd_port,
            mpd_rewind_s=self.mpd_rewind_s,
            fade_s=self.fade_s,
        )


def database_text_or_refuse(value: object) -> str | None:
    """A database setting as text, ``None`` passed through. Raises :class:`OptionsError` (exit 2).

    The environment layer reads a value opening with ``[`` or ``{`` as JSON and digits as a
    number, and ``--set`` reads JSON too, so ``database.url`` can arrive as a list, a table or a
    number. None of them is a URL or a path, and turning one back into text would name a file
    nobody meant (a number) or hand the store the printed form of a list, password and all. It is
    refused instead, naming the setting and the type it arrived as, never the value. ``None`` is
    passed through, because no value is not a type error but no database: both callers answer it
    with :func:`no_value_anywhere`, the store verbs through :func:`database_setting_of` and the run
    by dropping a ``None`` before the record is validated (:func:`parse_service_options`).
    """
    if value is None or isinstance(value, str):
        return value
    message = (
        f"refused: {config_path_of(_DATABASE_FIELD)} arrived as {type(value).__name__}, not as text; "
        "quote it in a config file, or use --set with a JSON string"
    )
    raise OptionsError(message, exit_code=ExitCode.ERROR)


def password_text_or_refuse(value: object) -> str | None:
    """A password setting as text, ``None`` when there is none. Raises :class:`OptionsError` (exit 2).

    Empty is none: the store then hands the driver no password, and libpq's own ``~/.pgpass``,
    ``PGPASSFILE`` and ``PGPASSWORD`` apply. A value that is not text is refused rather than turned
    back into text: the environment layer reads ``0123`` as the number 123 and ``true`` as a
    boolean, so converting it would hand the driver a password that differs from the one written.
    The refusal names the setting and the type it arrived as, never the value. It exits 2, like
    the password that arrived as no value: a password setting that cannot be used means the
    program could not run, not that it ran and the answer was no.
    """
    if value is None or value == "":
        return None
    if isinstance(value, SecretStr):
        return value.get_secret_value() or None
    if isinstance(value, str):
        return value
    message = (
        f"refused: {config_path_of(_CREDENTIAL_FIELD)} arrived as {type(value).__name__}, not as text; "
        "quote it in a config file, or use --set with a JSON string"
    )
    raise OptionsError(message, exit_code=ExitCode.ERROR)


def _refuse_a_malformed_password(configured: Mapping[str, Any]) -> None:
    """Refuse (exit 2) a password setting that is present but is not a password: no value, or not text.

    The environment layer reads ``null`` and ``none``, in any case, as no value, and ``--set``
    reads the JSON ``null`` the same way. Passed on, that would mean "no password" while somebody
    plainly wrote one, and ``config`` would still list the setting as coming from the environment.
    A setting that is absent, or empty text, stays "no password": only the mapping can tell
    absent from present-but-nothing, which is why this reads the mapping rather than the value.
    Anything else that is not text is refused by :func:`password_text_or_refuse`, the one rule for
    what a usable password is. Both refusals name the setting, never what was written.

    This runs on the configuration as it was read, before any typed ``--database`` decides whether
    the password is used (:func:`scoped_to_the_configured_database`), so a malformed password is
    refused whatever database the command opens.
    """
    if _CREDENTIAL_FIELD not in configured:
        return
    if configured[_CREDENTIAL_FIELD] is None:
        message = (
            f"refused: {config_path_of(_CREDENTIAL_FIELD)} arrived as no value, which is not the same as no password; "
            "unset the variable (or drop the --set) for no password, or give the password in a config file"
        )
        raise OptionsError(message, exit_code=ExitCode.ERROR)
    password_text_or_refuse(configured[_CREDENTIAL_FIELD])


def _secret_of(value: SecretStr | None) -> Secret | None:
    """The parsed password as the domain's secret; an empty one is none."""
    if value is None:
        return None
    text = value.get_secret_value()
    return Secret(text) if text else None


def scoped_to_the_configured_database(
    configured: Mapping[str, Any], *, typed: str | None, narrate: LogFn = log
) -> dict[str, Any]:
    """The settings, less the configured password when a typed database is not the configured one.

    The password in the configuration belongs to the database in the configuration. It goes along
    when nothing was typed, or when what was typed is exactly the configured ``database.url``; for
    any other typed database it is left out, so a SQLite file typed for one command is not refused
    for having been handed a password, and a URL naming another server is not sent this one's.
    libpq's own ``~/.pgpass``, ``PGPASSFILE`` and ``PGPASSWORD`` still apply to it. One line says
    so, naming the setting and neither the password nor the typed database.

    Only a well-formed password is ever left out here. One that arrived as no value or as anything
    but text is refused before this runs (:func:`configured_settings`), so it refuses even beside a
    typed database that would not use it.

    The service run and the store verbs both call this, so the two cannot come to disagree about
    which database a configured password is for. ``narrate`` is where that line goes: ``config``
    passes one that says nothing, because it prints every setting already and its STDOUT is the view.
    """
    scoped = dict(configured)
    if typed is None or typed == configured.get(_DATABASE_FIELD) or scoped.get(_CREDENTIAL_FIELD) in (None, ""):
        return scoped
    del scoped[_CREDENTIAL_FIELD]
    narrate(
        "config",
        f"the configured {config_path_of(_CREDENTIAL_FIELD)} was not used: the typed --database is not the "
        f"configured {config_path_of(_DATABASE_FIELD)}",
    )
    return scoped


def database_password_of(configured: Mapping[str, Any]) -> Secret | None:
    """The password the configuration layers give, for the store verbs that open the database
    without building the whole option record: text as the secret, empty as none. The same rule as
    the record's own field; on the mapping :func:`configured_settings` returns it can no longer
    refuse, because that has already refused a password that is not text."""
    text = password_text_or_refuse(configured.get(_CREDENTIAL_FIELD))
    return Secret(text) if text else None


def database_setting_of(configured: Mapping[str, Any]) -> str:
    """The database the configuration layers give, for the store verbs that open it without
    building the whole option record: text, or refused (exit 2) as not text or as given nowhere,
    by the same two rules the record's own field follows."""
    setting = database_text_or_refuse(configured.get(_DATABASE_FIELD))
    if setting is None:
        raise no_value_anywhere([_DATABASE_FIELD])
    return setting


def _database_file(database: str) -> Path | None:
    """The file a database setting names when it is a plain path; a URL is checked by the store at open."""
    return None if "://" in database else Path(database)


def merge_service_settings(*, configured: Mapping[str, Any], given: Mapping[str, Any]) -> dict[str, Any]:
    """The configuration layers first, then whatever was actually typed on top.

    A CLI value counts as typed when it is neither ``None`` nor an empty tuple: a repeatable option
    nobody used arrives as ``()``, and letting that overwrite a configured list would mean the
    command line could only ever add consoles and never leave the configured ones alone.
    """
    merged: dict[str, Any] = dict(configured)
    merged.update({key: value for key, value in given.items() if value is not None and value != ()})
    merged.setdefault("device_id", default_device_id())
    return merged


def parse_service_options(  # noqa: PLR0913 - one keyword per field; collapsing them is the untyped dict this avoids
    *,
    bind_ip: str | None,
    device_id: str | None,
    channel_file: str | None,
    switch_file: str | None,
    state_file: str | None,
    database: str | None,
    registry_url: str | None,
    allow_console: Sequence[str],
    unreachable_timeout_s: float | None,
    dial_window_s: float | None,
    mpd_host: str | None,
    mpd_port: int | None,
    mpd_rewind_s: float | None,
    configured: Mapping[str, Any],
) -> ServiceOptions:
    """Validate what the CLI and the config files between them said.

    Raises :class:`OptionsError` on a refusal, including the one refusal click used to make
    itself: a setting that has no default and was given nowhere.
    """
    given = {
        "bind_ip": bind_ip,
        "device_id": device_id,
        "channel_file": channel_file,
        "switch_file": switch_file,
        "state_file": state_file,
        "database": database,
        "consoles_allowed": tuple(allow_console),
        "registry_url": registry_url,
        "unreachable_timeout_s": unreachable_timeout_s,
        "dial_window_s": dial_window_s,
        "mpd_host": mpd_host,
        "mpd_port": mpd_port,
        "mpd_rewind_s": mpd_rewind_s,
    }
    merged = merge_service_settings(
        configured=scoped_to_the_configured_database(configured, typed=database), given=given
    )
    if _DATABASE_FIELD in merged and merged[_DATABASE_FIELD] is None:
        # The environment layer reads `null` and `none` as no value, and `--set` the JSON null.
        # Validated as it came, pydantic would refuse it as a record field that is not a string;
        # dropped, it is a database given nowhere, refused in the one sentence the store verbs give.
        del merged[_DATABASE_FIELD]
    try:
        return ServiceOptionsInput.model_validate(merged).record()
    except ValidationError as exc:
        raise _refusal(exc) from exc


def _refusal(exc: ValidationError) -> OptionsError:
    """A pydantic complaint turned into the sentence an operator can act on; a missing setting is
    :func:`no_value_anywhere`'s."""
    missing = sorted({str(error["loc"][0]) for error in exc.errors() if error["type"] == "missing" and error["loc"]})
    if missing:
        return no_value_anywhere(missing)
    complaints = "; ".join(f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors())
    return OptionsError(f"refused: {complaints}", exit_code=ExitCode.ERROR)


def no_value_anywhere(missing: Sequence[str]) -> OptionsError:
    """The refusal (exit 2) for settings that have no default and were given nowhere.

    Named with the places each can be put, because the whole point of the configuration layers is
    that the command line is no longer the only one, and an error that only mentions the flag would
    send a reader back to the unit file. The run and the store verbs both refuse a missing database
    with this, so the two say it in the same words.
    """
    names = ", ".join(missing)
    flags = " ".join(f"--{name.replace('_', '-')}" for name in missing)
    where = ", ".join(sorted(config_path_of(name) for name in missing))
    message = (
        f"refused: no value anywhere for {names}. "
        f"Give it on the command line ({flags}), or in a config file as {where} - "
        f"run `{service_command} config-deploy --target user` to write one - "
        f"or set {ENV_PREFIX}{env_name_of(missing[0])}."
    )
    return OptionsError(message, exit_code=ExitCode.ERROR)


def configured_settings(config: Config, *, narrate: LogFn = log) -> dict[str, Any]:
    """What the config files said, with a line for anything in our sections that is not a setting.

    The record ignores an unknown key either way, which is the right behaviour for a house - a
    stray key must not stop the speakers working - but a misspelled setting that does nothing and
    says nothing is the kind of thing somebody debugs for an hour. ``config`` is the exception: it
    prints the stray key itself, as a key, so it passes a ``narrate`` that says nothing.

    Both the service run and the store verbs read the configuration through here, which is what
    makes it the one place a malformed password - no value, or not text - is refused for both,
    whatever database the command then opens.
    """
    for stray in unknown_settings(config):
        section, _, key = stray.partition(".")
        narrate("config", f"ignored: [{section}] has no setting called {key!r}")
    found = service_settings(config)
    _refuse_a_malformed_password(found)
    return found


def layered_preferences(configured: Mapping[str, Any]) -> HousePreferences:
    """The five preferences as the config layers give them, each checked by the preference rule.

    What ``prefs`` lays the stored rows over. It reads only these five fields, so a host that has
    no bind address configured can still list and change its preferences. A field no layer sets
    takes the record's own default, read off the class so there is no second copy of it.
    """
    defaults = {field.name: field.default for field in dataclasses.fields(ServiceOptions)}
    values: dict[str, Any] = {}
    for field_name in _PREFERENCE_FIELDS:
        name = PreferenceName(config_path_of(field_name))
        values[field_name] = preference_or_refuse(name, configured.get(field_name, defaults[field_name]))
    return HousePreferences(
        window_s=values["dial_window_s"],
        hold_threshold_s=values["hold_threshold_s"],
        rewind_s=values["mpd_rewind_s"],
        fade_s=values["fade_s"],
        consoles_allowed=values["consoles_allowed"],
    )
