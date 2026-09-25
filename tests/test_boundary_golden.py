"""The converted records, replayed against what the pydantic ones did.

The thirteen records that moved from pydantic to frozen dataclasses cannot be proved equivalent by
the tests that came with them: those tests were written against the rules, and the rules did not
change - the construction, coercion and refusal behaviour underneath them did. So the contract for
this milestone's share of them is a corpus generated from the OLD code at
``887934c7c05d80ea48180c72986584271873cf4c`` (``research``-side generator in the rebuild's baseline
directory), replayed here against the new code.

Eight corpora are here, each landing with the code it holds to account. Seeding and the dialler
came with M2, because their boundary is in ``domain``; the state file and the channel file with M3,
where the two file adapters and their pydantic boundary models are; the registry, the observer and
the HTTP face with M4, which is where the speakers are actually spoken to. Options came with M6,
and it is the only one that covers TWO boundaries plus the six configuration layers under one of
them, because that is what an option set is made of.

The file corpora are what says the boundary models are faithful, and they are the reason those
models exist: they pin the bytes written, the coercions a hand-edited file relies on (a volume
written ``"17"`` is 17, a window written ``true`` is 1.0), the ``N problem(s)`` COUNT in each
refusal, and which documents refuse rather than start empty.

The observer corpus is 475 real frames off four real speakers across two live runs, plus its edge
cases. They are replayed one per test rather than in a loop: a loop stops at the first
disagreement, and what this corpus is for is knowing WHICH frames a change moved.

**Where old and new differ, this file states the difference rather than hiding it.** Each delta is
named in the test that meets it, with what the old code did and why the new answer is the same
decision reached a different way. There are thirteen. Deltas 6 to 9 are the options corpus's and
are written out where that corpus is replayed; deltas 10 to 13 are a different species from all the
others and the comment at each one says so - every delta from 1 to 9 is the SAME behaviour reached
another way, while 10 to 13 are deliberate REPAIRS that supersede what the old code did. The
recorded case keeps the old answer, because that is what a corpus is for; the assertion beside it
is what the code does now, and why:

1. ``seed_from_presets`` returned a list and LOGGED; it now returns the list and what it would have
   said. The texts are compared, in order, against the lines the old code emitted.
2. ``Dialler.digit`` returned nothing and LOGGED an ignored digit; it now returns the line. The
   recorded log is compared against the returned outcome.
3. Constructing a ``Channel`` with a bad field raised pydantic's ``ValidationError``, which collects
   EVERY failing field; a dataclass ``__post_init__`` raises on the first check that fails. The
   seeding corpus holds one such case and it has one bad field, so the message is the same - the
   count is the part that cannot be, and the test says so.
4. The same for a ``Channel`` constructed directly, where the channel-file corpus DOES hold a case
   with two bad fields at once. The new code raises the first refusal in field order, which is the
   first of the two the old code collected. Loading a FILE is unaffected and the corpus proves it:
   the boundary model checks per field, so a document with four problems still says four.
5. A registry entry the response cannot be read as produces a pydantic message naming the MODEL,
   and the model is now ``SpeakerRecord`` where it was ``Speaker`` - the response's record of a
   speaker, now that the domain has a ``Speaker`` of its own. One word of one diagnostic; the
   field, the type, the input value and the documentation URL are byte-identical, and the test
   substitutes only the name so the rest stays under test.
10. A REPAIR, not an equivalence: a state file whose bytes are not UTF-8 raised ``UnicodeDecodeError``
   out of ``load_state``, whose own docstring says it never raises, and stopped the service at
   startup on a traceback. It now starts empty and names the path, which is what every other
   unreadable state file already did.
11. The same repair on the channel file, in that file's own direction: the bytes still REFUSE,
   because the list is the only copy of something a person built, but as a ``ChannelFileError``
   naming the path rather than a raw ``UnicodeDecodeError`` naming nothing.
12. A REPAIR of the same species: a state file that Windows saved as "UTF-8 with BOM" was read with
   the mark as part of the document, so a file a person can read and repair reported itself
   unusable and the channel it remembered was lost on the very next restart. The bytes are decoded
   as ``utf-8-sig`` now - the same codec plus the rule that a leading mark belongs to the encoding
   and not to the document - so the document loads and nothing is said about it.
13. The same repair on the channel file, in that file's own direction: it refused outright rather
   than starting empty, and it is the file most likely to have been hand-edited from a Windows box,
   because it is the only copy of something a person built.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import io
import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import BaseModel, ValidationError
from registry_double import FakeRegistry

from soundtouch_zonemaster.__init__conf__ import LAYEREDCONF_SLUG, service_command, shell_command
from soundtouch_zonemaster.adapters.aftertouch.registry import fetch_speakers
from soundtouch_zonemaster.adapters.cli.boundary import parse_service_options
from soundtouch_zonemaster.adapters.cli.envelope import OutputMode, report_failure
from soundtouch_zonemaster.adapters.cli.prototype import parse_options
from soundtouch_zonemaster.adapters.config.errors import ConfigInputError
from soundtouch_zonemaster.adapters.config.loader import ENV_PREFIX, clear_config_cache, get_config
from soundtouch_zonemaster.adapters.config.overrides import apply_set_overrides
from soundtouch_zonemaster.adapters.config.settings_map import service_settings, unknown_settings
from soundtouch_zonemaster.adapters.files.channel_file import ChannelFileError, load_channels, save_channels
from soundtouch_zonemaster.adapters.files.state_file import load_state, save_state
from soundtouch_zonemaster.adapters.soundtouch.http_api import HttpApi, Request, key_press, parse_request
from soundtouch_zonemaster.adapters.soundtouch.observer import parse_frame, parse_now_playing
from soundtouch_zonemaster.adapters.soundtouch.wire import encryption_type
from soundtouch_zonemaster.application.errors import RegistryError
from soundtouch_zonemaster.application.options import Options, ServiceOptions, default_device_id
from soundtouch_zonemaster.application.outcome import OptionsError
from soundtouch_zonemaster.application.ports import ZoneServicePorts
from soundtouch_zonemaster.application.zone_service import ZoneService
from soundtouch_zonemaster.domain.channellist import (
    Channel,
    ChannelList,
    ChannelNumberError,
    PresetStation,
    SeedingError,
    dialable,
    ladder_index,
    nth_number,
    seed_from_presets,
)
from soundtouch_zonemaster.domain.dialling import Dialler, DigitIgnored
from soundtouch_zonemaster.domain.enums import ChannelEnd, ChannelKind, FrameKind, SourceName
from soundtouch_zonemaster.domain.events import SpeakerEvent
from soundtouch_zonemaster.domain.speakers import ProtectedSpeaker, Speaker
from soundtouch_zonemaster.domain.state import Place, ZoneState
from soundtouch_zonemaster.domain.station import StationRequest

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Sequence

    from soundtouch_zonemaster.application.options import ChannelPolicy
    from soundtouch_zonemaster.application.ports import AddressOf, MpdControlPort
    from soundtouch_zonemaster.domain.logfn import LogFn
    from soundtouch_zonemaster.domain.mpd import MpdStatus
    from soundtouch_zonemaster.domain.station import Station

GOLDEN = Path(__file__).parent / "fixtures" / "golden"

SOURCE_HEAD = "887934c7c05d80ea48180c72986584271873cf4c"
"""The archive commit the corpora were generated from. Pinned so a refreshed fixture that was
taken from some other tree cannot quietly become the contract."""

CASE_COUNTS = {
    "seeding": 8,
    "dialler": 10,
    "state_file": 31,
    "channel_file": 56,
    "registry": 18,
    "observer": 479,
    "slavemsg": 46,
    "options": 58,
}
"""How many cases each corpus holds. A truncated fixture would otherwise pass every assertion
below by containing nothing to disagree with."""


def corpus(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))
    return loaded


def as_json_value(value: object) -> str:
    """What ``model_dump(mode="json")`` made of a field ``json`` itself cannot render.

    One type reaches this: a ``Path``, which the old side recorded as its string. Anything else is
    a field no corpus has seen, so raising is the honest answer rather than stringifying whatever
    turns up and comparing two descriptions of it.
    """
    if isinstance(value, Path):
        return str(value)
    message = f"no recorded JSON rendering for a field of type {type(value).__name__}"
    raise TypeError(message)


def canonical(value: object) -> object:
    """The shape the corpus records a value in, reproduced for a dataclass.

    The old side wrote ``{"type": <class>, "fields": model_dump(mode="json")}``. Going through
    ``json`` is what reproduces the ``mode="json"`` half: it renders a ``StrEnum`` member as its
    value and a tuple as a list, which is what the recorded fields hold.
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields: object = json.loads(json.dumps(dataclasses.asdict(value), default=as_json_value))
        return {"type": type(value).__name__, "fields": fields}
    if isinstance(value, BaseModel):
        # The records at a parsing boundary stayed pydantic, and the old side recorded them with
        # the same model_dump(mode="json") this calls - so these compare without a conversion.
        return {"type": type(value).__name__, "fields": value.model_dump(mode="json")}
    if isinstance(value, (tuple, list)):
        # A bare tuple or list carries no element type, so the members read as unknown; the
        # isinstance above is what makes the cast true.
        return [canonical(item) for item in cast("tuple[object, ...] | list[object]", value)]
    return value


def texts_of(logs: list[list[str]], *, kind: str) -> list[str]:
    """The recorded lines, in order, once it is established they are all of the one kind."""
    assert {entry[0] for entry in logs} <= {kind}, f"the corpus recorded a kind other than {kind!r}"
    return [entry[1] for entry in logs]


@pytest.mark.parametrize("name", sorted(CASE_COUNTS))
def test_the_corpus_is_the_one_this_contract_was_written_against(name: str) -> None:
    """Provenance, before anything is replayed against it."""
    meta = corpus(name)["meta"]
    assert meta["source_head"] == SOURCE_HEAD
    assert meta["corpus"] == name
    assert len(corpus(name)["cases"]) == CASE_COUNTS[name]


def presets_of(recorded: list[list[Any]]) -> dict[int, PresetStation | None]:
    """The corpus input as ``seed_from_presets`` takes it."""
    return {int(number): (PresetStation(**spec) if spec else None) for number, spec in recorded}


@pytest.mark.parametrize("case", corpus("seeding")["cases"], ids=lambda c: c["name"])
def test_seeding_answers_what_the_old_code_answered(case: dict[str, Any]) -> None:
    presets = presets_of(case["input"]["presets"])
    expect = case["expect"]
    said = texts_of(expect["logs"], kind="channels")

    if "value" in expect:
        report = seed_from_presets(presets)
        assert canonical(report.seeded) == expect["value"]
        # DELTA 1: these lines were LOGGED by the old code, in this order, as it walked the presets.
        assert list(report.said) == said
        return

    # DELTA 3: the old code raised pydantic's ValidationError here; the dataclass raises the same
    # refusal from __post_init__, wrapped so the lines said before it are not lost.
    recorded = expect["raises"]
    assert recorded["type"] == "ValidationError"
    assert recorded["error_count"] == 1, "the corpus case has one bad field, so the count can match"
    with pytest.raises(SeedingError) as caught:
        seed_from_presets(presets)
    assert str(caught.value) == recorded["errors"][0]["msg"].removeprefix("Value error, ")
    assert list(caught.value.said) == said


def dial_op(dialler: Dialler, op: list[Any]) -> object:
    """One recorded operation, driven exactly as the generator drove it."""
    name = op[0]
    if name == "digit":
        return dialler.digit(op[1], op[2], at=op[3])
    if name == "step":
        return dialler.step(op[1], op[2], at=op[3])
    if name == "forget_steps":
        return dialler.forget_steps(op[1])
    if name == "deadline":
        return dialler.deadline()
    if name == "due":
        return [list(pair) for pair in dialler.due(at=op[1])]
    return [list(pair) for pair in dialler.steps_due(at=op[1])]


@pytest.mark.parametrize("case", corpus("dialler")["cases"], ids=lambda c: c["name"])
def test_the_dialler_answers_what_the_old_code_answered(case: dict[str, Any]) -> None:
    """Every recorded script, op by op, including the floats the window is compared on.

    The returned values are compared exactly rather than approximately, deliberately: the window
    compares floats, so a digit at 0.3 under a 0.8 window is due at 1.1 while one at 0.4 is NOT due
    at 1.2. Rounding that comparison here would hide exactly the behaviour the corpus pins.
    """
    dialler = Dialler(window_s=case["input"]["window_s"])
    for step in case["expect"]["trace"]:
        returned = dial_op(dialler, step["op"])
        if step["op"][0] != "digit":
            assert returned == step["returned"], f"{step['op']} answered differently"
            assert step["logs"] == [], "only a digit ever narrated anything"
            continue
        # DELTA 2: the old digit() returned None and logged; the new one returns the line instead.
        assert step["returned"] is None, "the old digit() always returned None"
        if not step["logs"]:
            assert returned is None
            continue
        (said,) = texts_of(step["logs"], kind="dial")
        assert isinstance(returned, DigitIgnored), f"{step['op']} ignored a digit, so it hands the line back"
        assert returned.said == said


# ------------------------------------------------------------------------------------------------
# The state file and the channel file: the two pydantic boundary models, against the recorded bytes
# ------------------------------------------------------------------------------------------------


def logged(lines: list[tuple[str, str]]) -> Any:
    """The log callable the file modules are handed, collecting what they said."""

    def log(kind: str, text: str) -> None:
        lines.append((kind, text))

    return log


def placed(work: Path, recorded: dict[str, Any]) -> Path:
    """The recorded path under this run's scratch directory, with the recorded file put there.

    The corpus writes ``<WORK>`` for the scratch root, in the path AND in every log line it
    recorded, so one substitution makes both sides comparable without loosening an assertion.
    """
    path = Path(str(recorded["path"]).replace("<WORK>", str(work)))
    path.parent.mkdir(parents=True, exist_ok=True)
    spec = cast("dict[str, Any]", recorded["file"])
    kind = spec["kind"]
    if kind == "missing":
        return path
    if kind == "directory":
        path.mkdir()
        return path
    if kind == "text":
        path.write_text(cast("str", spec["text"]), encoding="utf-8")
        return path
    path.write_bytes(base64.b64decode(cast("str", spec["base64"])))
    return path


def said(work: Path, recorded: list[list[str]]) -> list[tuple[str, str]]:
    """The recorded log lines with the scratch root substituted in."""
    return [(kind, text.replace("<WORK>", str(work))) for kind, text in recorded]


def state_of(fields: dict[str, Any]) -> ZoneState:
    """A ``ZoneState`` from the corpus's recorded field values."""
    return ZoneState(
        channel=cast("str | None", fields["channel"]),
        members=tuple(cast("list[str]", fields["members"])),
        muted=dict(cast("dict[str, int]", fields["muted"])),
        out_of_multiroom=tuple(cast("list[str]", fields["out_of_multiroom"])),
        dial_window_s=cast("float | None", fields["dial_window_s"]),
    )


def channel_of(fields: dict[str, Any]) -> Channel:
    """A ``Channel`` from the corpus's recorded field values, defaults included.

    ``kind`` is built through the enum rather than handed over as the recorded string: a dataclass
    coerces nothing, so passing "radio" straight through would store a ``str`` where the field
    declares a ``ChannelKind`` - a difference the corpus cannot see, because both render as
    "radio" once they reach JSON.
    """
    return Channel(
        number=cast("str", fields["number"]),
        name=cast("str", fields["name"]),
        kind=ChannelKind(fields["kind"]),
        url=cast("str", fields["url"]),
        in_rotation=cast("bool", fields.get("in_rotation", True)),
    )


@pytest.mark.parametrize("case", corpus("state_file")["cases"], ids=lambda c: c["name"])
def test_the_state_file_behaves_as_the_old_one_did(case: dict[str, Any], tmp_path: Path) -> None:
    """Every recorded save and load, byte for byte and line for line.

    The bytes matter as much as the values: a file written by the deployed service is read by this
    code and the other way round, so a changed indent or a changed key order would be a migration
    nobody asked for.
    """
    given, expect = case["input"], case["expect"]
    lines: list[tuple[str, str]] = []

    if given["op"] == "save":
        path = tmp_path / "state.json"
        save_state(path, state_of(cast("dict[str, Any]", given["state"])))
        assert path.read_text(encoding="utf-8") == expect["text"]
        assert sorted(p.name for p in tmp_path.iterdir()) == expect["files_after"]
        return

    path = placed(tmp_path, cast("dict[str, Any]", given))
    if "raises" in expect:
        # DELTA 10, a REPAIR rather than an equivalence. The old code let UnicodeDecodeError out of
        # a function whose docstring says it never raises, and the bare gather that runs the
        # service took the whole thing down with it at startup. Bytes that are not UTF-8 are now
        # one of the ways this file can be unreadable, like the permission error beside it: start
        # empty, name the path. The recorded message is kept here so the case still proves WHICH
        # document is meant - it is the old answer, and this asserts it is no longer given.
        assert expect["raises"]["type"] == "UnicodeDecodeError"
        loaded = load_state(path, log=logged(lines))
        assert loaded == ZoneState()
        assert lines == [("state", f"{path}: could not be read (UnicodeDecodeError); starting empty")]
        return

    if case["name"] == "load: a UTF-8 byte order mark":
        # DELTA 12, a REPAIR rather than an equivalence, and the same species as 10 and 11. The old
        # code read the mark as part of the document, so a state file a person can read and repair
        # called itself unusable and the channel it remembered was lost on the very next restart.
        # The bytes are decoded as utf-8-sig now, which is the same codec plus the rule that a
        # leading mark belongs to the encoding. The recorded empty start is asserted first, so the
        # case still proves WHICH document is meant; what follows is that it is no longer given.
        assert expect["value"] == canonical(ZoneState())
        assert said(tmp_path, cast("list[list[str]]", expect["logs"])) == [
            ("state", f"{path}: unusable, starting empty (1 problem(s))")
        ]
        loaded = load_state(path, log=logged(lines))
        assert loaded == ZoneState(channel="1")
        assert lines == []
        return

    loaded = load_state(path, log=logged(lines))
    assert canonical(loaded) == expect["value"]
    assert lines == said(tmp_path, cast("list[list[str]]", expect["logs"]))


def replay_edit(given: dict[str, Any], expect: dict[str, Any], path: Path) -> None:
    """The recorded sequence of list edits, checked after every one of them."""
    have = ChannelList(channels=tuple(channel_of(f) for f in cast("list[dict[str, Any]]", given["base"])))
    assert canonical(have) == expect["start"]["list"]

    for step in cast("list[dict[str, Any]]", expect["trace"]):
        op = cast("list[Any]", step["op"])
        was = have
        if op[0] == "with_rotation":
            have = have.with_rotation(cast("str", op[1]), in_rotation=cast("bool", op[2]))
        elif op[0] == "with_channel":
            have = have.with_channel(channel_of(cast("dict[str, Any]", op[1])))
        else:
            have = have.without_number(cast("str", op[1]))
        # A change that changes nothing returns the SAME object, which is what lets the service
        # hold one list across a pass without copying it on every no-op.
        assert (have is was) == step["unchanged_object"]
        assert canonical(have) == step["list"]
        assert list(have.numbers_in_order()) == step["numbers_in_order"]
        assert list(have.rotation_numbers()) == step["rotation_numbers"]
        assert have.lowest_free_number() == step["lowest_free_number"]
        for current, steps, landed in cast("list[list[Any]]", step["steps"]):
            assert have.step(cast("str | None", current), cast("int", steps)) == landed

    save_channels(path, have)
    assert path.read_text(encoding="utf-8") == expect["text"]


def replay_construct(given: dict[str, Any], expect: dict[str, Any]) -> None:
    """Constructing a ``Channel``, including the one case with two bad fields at once."""
    fields = cast("dict[str, Any]", given["fields"])
    if "value" in expect:
        assert canonical(channel_of(fields)) == expect["value"]
        return

    recorded = cast("dict[str, Any]", expect["raises"])
    assert recorded["type"] == "ValidationError"
    errors = cast("list[dict[str, Any]]", recorded["errors"])
    with pytest.raises(ChannelNumberError) as caught:
        channel_of(fields)
    # DELTA 4: pydantic collected EVERY failing field; __post_init__ raises on the first. The
    # errors are recorded in field order and so are the checks, so the message is the first of
    # them - and when there is only one, old and new say exactly the same thing.
    assert str(caught.value) == errors[0]["msg"].removeprefix("Value error, ")
    assert recorded["error_count"] == len(errors)


@pytest.mark.parametrize("case", corpus("channel_file")["cases"], ids=lambda c: c["name"])
def test_the_channel_file_behaves_as_the_old_one_did(case: dict[str, Any], tmp_path: Path) -> None:
    """Every recorded construction, ladder question, save, edit and load.

    The load cases are the ones the boundary model exists for. A person edits this file by hand, so
    a document can be wrong in several ways at once, and ``unusable (N problem(s))`` is what tells
    them how many - which is why the rules are checked per field rather than by building a record
    and letting it raise.
    """
    given, expect = case["input"], case["expect"]
    op = given["op"]
    lines: list[tuple[str, str]] = []

    if op == "construct":
        replay_construct(cast("dict[str, Any]", given), cast("dict[str, Any]", expect))
        return

    if op in {"nth_number", "ladder_index", "dialable"}:
        call = {"nth_number": nth_number, "ladder_index": ladder_index, "dialable": dialable}[op]
        argument: Any = given["argument"]
        if "raises" in expect:
            with pytest.raises(ChannelNumberError) as caught:
                call(argument)
            assert expect["raises"]["type"] == "ChannelNumberError"
            assert str(caught.value) == expect["raises"]["message"]
            return
        assert call(argument) == expect["value"]
        return

    if op == "save":
        path = tmp_path / "channels.json"
        have = ChannelList(channels=tuple(channel_of(f) for f in cast("list[dict[str, Any]]", given["channels"])))
        save_channels(path, have)
        assert path.read_text(encoding="utf-8") == expect["text"]
        assert sorted(p.name for p in tmp_path.iterdir()) == expect["files_after"]
        return

    if op == "edit":
        replay_edit(cast("dict[str, Any]", given), cast("dict[str, Any]", expect), tmp_path / "channels.json")
        return

    path = placed(tmp_path, cast("dict[str, Any]", given))
    if case["name"] == "load: a UTF-8 byte order mark":
        # DELTA 13, the same REPAIR as delta 12 in this file's own direction. Refusing is what this
        # file does with a document it cannot read, and that was never the problem: the mark was
        # read as part of the document, so a list that is in fact perfectly good was refused - and
        # this is the file most likely to have been hand-edited from a Windows box, because it is
        # the only copy of something a person built. The recorded refusal is asserted first, so the
        # case still proves WHICH document is meant; what follows is that it is no longer given.
        recorded = cast("dict[str, Any]", expect["raises"])
        assert recorded["message"].replace("<WORK>", str(tmp_path)) == f"{path}: unusable (1 problem(s))"
        loaded = load_channels(path, log=logged(lines))
        assert loaded == ChannelList()
        assert lines == [("channels", f"{path}: 0 channel(s)")]
    elif "raises" in expect:
        recorded = cast("dict[str, Any]", expect["raises"])
        with pytest.raises(ChannelFileError) as caught:
            load_channels(path, log=logged(lines))
        if recorded["type"] == "UnicodeDecodeError":
            # DELTA 11, the same REPAIR as delta 10 and the same species: deliberate, not an
            # equivalence. Refusing was always right here - the list is the only copy of something
            # a person built - but the old code refused as a raw UnicodeDecodeError, so the one
            # person who has to repair it got a traceback naming no file. It is now the module's
            # own refusal, worded exactly as the other unreadable-file cases are.
            assert str(caught.value) == f"{path}: could not be read (UnicodeDecodeError)"
        else:
            assert str(caught.value) == recorded["message"].replace("<WORK>", str(tmp_path))
        assert lines == said(tmp_path, cast("list[list[str]]", expect["logs"]))
    else:
        loaded = load_channels(path, log=logged(lines))
        assert canonical(loaded) == expect["value"]
        assert lines == said(tmp_path, cast("list[list[str]]", expect["logs"]))


# ------------------------------------------------------------------------------------------------
# The three adapter corpora: the registry read, the observer's frame parsing, and the HTTP face
# ------------------------------------------------------------------------------------------------


FIXTURES = Path(__file__).parent / "fixtures"

PLACEHOLDERS = {
    "<INPUT_FRAME>": "frame",
    "<ARRIVAL_TIME>": "received_at",
}
"""What the corpus writes instead of a value it could not record: the frame it was handed (which
for a capture fixture would have doubled the file), and the moment the event was built. Each names
the field it stands for, so a placeholder in the WRONG field is still a failure."""


def with_actual(recorded: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    """The recorded fields with each placeholder replaced by what this run produced."""
    filled = dict(recorded)
    for token, field in PLACEHOLDERS.items():
        if filled.get(field) == token:
            filled[field] = actual[field]
    return filled


def frames_of(fixture: str) -> list[dict[str, Any]]:
    """The recorded frames a capture fixture holds, in the order they arrived.

    Each entry carries its own ``speaker`` and ``received_at`` beside the frame, which is why a
    corpus case naming a fixture and an index needs to supply none of the three. The file is a
    document with a ``note`` beside its ``frames``, not a bare list: the note says which live run
    they came from, which is the half a reader needs and a list cannot carry.
    """
    loaded: dict[str, Any] = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))
    return cast("list[dict[str, Any]]", loaded["frames"])


@pytest.mark.parametrize("case", corpus("observer")["cases"], ids=lambda c: c["name"])
def test_the_observer_parses_what_the_old_code_parsed(case: dict[str, Any]) -> None:
    """Every frame of two live runs, plus the edge cases, plus parse_now_playing.

    475 of these are real frames off real speakers. They are replayed one per test rather than in
    a loop on purpose: a loop reports the first disagreement and stops, and what this corpus is for
    is knowing WHICH frames a change moved.
    """
    given, expect = case["input"], case["expect"]

    if given["op"] == "parse_now_playing":
        parsed = parse_now_playing(
            cast("str", given["speaker"]),
            cast("str", given["device_id"]),
            cast("str", given["document"]),
            cast("float", given["received_at"]),
        )
    else:
        # A case either quotes the frame inline with its own speaker and moment, or names a
        # fixture entry that already carries all three.
        recorded: dict[str, Any] = dict(given)
        if "fixture" in given:
            recorded = {**frames_of(cast("str", given["fixture"]))[cast("int", given["index"])], **given}
        parsed = parse_frame(
            cast("str", recorded["speaker"]),
            cast("str", recorded["frame"]),
            cast("float", recorded["received_at"]),
        )

    recorded = cast("dict[str, Any]", expect["value"])
    got = cast("dict[str, Any]", canonical(parsed))
    fields = with_actual(cast("dict[str, Any]", recorded["fields"]), cast("dict[str, Any]", got["fields"]))
    assert got == {"type": recorded["type"], "fields": fields}


class StandInMaster:
    """What the HTTP face needs from a master, with the values the corpus was recorded against.

    The three documents are fixed strings rather than a real master's output: what this corpus
    pins is the FACE - which path returns which document, which body causes a select, what the
    log says - and a real master would make every case depend on the zone it happened to hold.
    """

    device_id = "5EB0CE000001"

    def __init__(self) -> None:
        self.selected: list[tuple[str, str]] = []
        self.left: list[str] = []

    def info_xml(self) -> str:
        return "<info/>"

    def now_playing_xml(self) -> str:
        return "<nowPlaying/>"

    def zone_xml(self) -> str:
        return '<?xml version="1.0" encoding="UTF-8" ?><zone master="5EB0CE000001" />'

    async def select(self, content_item_xml: str, *, origin: str) -> None:
        self.selected.append((content_item_xml, origin))

    async def slave_left(self, ip: str) -> None:
        self.left.append(ip)


async def drive_the_face(given: dict[str, Any], lines: list[tuple[str, str]]) -> dict[str, Any]:
    """Replay one recorded sequence of requests through a real ``HttpApi``."""
    master = StandInMaster()
    events: asyncio.Queue[Any] | None = asyncio.Queue() if given["listening"] else None
    api = HttpApi(master, logged(lines), events=events)

    answers = [
        await api.handle(
            Request(
                method=r["method"],
                path=cast("str", r["path"]),
                body=cast("str", r["body"]),
                peer=cast("str", r["peer"]),
            )
        )
        for r in cast("list[dict[str, Any]]", given["requests"])
    ]

    # A forwarded key is put on the queue by a background task, so the queue is read only after
    # every request has been handled and the loop has been given a turn to run those tasks.
    await asyncio.sleep(0)
    seen: list[object] = []
    while events is not None and not events.empty():
        seen.append(events.get_nowait())
    return {"answers": answers, "events": seen, "selected": master.selected, "left": master.left}


@pytest.mark.parametrize("case", corpus("slavemsg")["cases"], ids=lambda c: c["name"])
async def test_the_http_face_answers_what_the_old_code_answered(case: dict[str, Any]) -> None:
    """Key parsing, request parsing, and the whole face driven request by request."""
    given, expect = case["input"], case["expect"]
    lines: list[tuple[str, str]] = []

    if given["op"] == "key_press":
        pressed = key_press(cast("str", given["body"]))
        assert canonical(pressed) == expect["value"]
        assert lines == []
        return

    if given["op"] == "parse_request":
        raw = base64.b64decode(cast("str", given["raw_base64"]))
        if "raises" in expect:
            with pytest.raises(Exception) as caught:
                parse_request(raw, cast("str", given["peer"]))
            assert type(caught.value).__name__ == expect["raises"]["type"]
            return
        assert canonical(parse_request(raw, cast("str", given["peer"]))) == expect["value"]
        return

    got = await drive_the_face(cast("dict[str, Any]", given), lines)
    assert got["answers"] == expect["answers"]
    assert [list(pair) for pair in cast("list[Any]", got["selected"])] == expect["selected"]
    assert list(cast("list[Any]", got["left"])) == expect["left"]
    seen = [cast("dict[str, Any]", canonical(event)) for event in cast("list[Any]", got["events"])]
    wanted = [
        {"type": r["type"], "fields": with_actual(r["fields"], s["fields"])}
        for r, s in zip(expect["events"], seen, strict=True)
    ]
    assert seen == wanted
    assert [list(pair) for pair in lines] == expect["logs"]


async def run_the_registry(given: dict[str, Any]) -> tuple[object, str]:
    """``fetch_speakers`` against a loopback double, as the corpus recorded it.

    Three shapes of failure are recorded and each needs the double to behave differently: a body
    it refuses, nothing listening at all, and a socket that accepts and never answers. The last is
    NOT the same as the second and the messages say so (ReadTimeout against ConnectError).
    """
    body = base64.b64decode(cast("str", given["body_base64"])).decode("utf-8")

    if not given["answers"]:
        # Accept the connection and say nothing, which is what a wedged registry does.
        held: list[asyncio.StreamWriter] = []

        async def say_nothing(_r: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            held.append(writer)
            await asyncio.Event().wait()

        silent = await asyncio.start_server(say_nothing, "127.0.0.1", 0)
        port = silent.sockets[0].getsockname()[1]
        try:
            base = f"http://127.0.0.1:{port}{given['base_url_suffix']}"
            return await fetch_speakers(base), str(port)
        finally:
            # The held connections are dropped BEFORE wait_closed, which waits for every client
            # transport to be gone: a handler still sitting in its await would hold it for ever,
            # and the run would report nothing at all rather than a failure.
            silent.close()
            for writer in held:
                writer.close()
            await silent.wait_closed()

    fake = FakeRegistry(body, status_line=cast("str", given["status_line"]))
    await fake.start()
    port = str(fake.port)
    if not given["listening"]:
        await fake.stop()
    try:
        return await fetch_speakers(f"{fake.base_url}{given['base_url_suffix']}"), port
    finally:
        if given["listening"]:
            await fake.stop()


@pytest.mark.parametrize("case", corpus("registry")["cases"], ids=lambda c: c["name"])
async def test_the_registry_answers_what_the_old_code_answered(case: dict[str, Any]) -> None:
    """Every recorded body and every recorded way the read can fail.

    This is the corpus that proves SpeakerRecord: the wire aliases, ``extra="ignore"`` and every
    coercion moved to a pydantic model at the boundary when ``Speaker`` became a frozen dataclass,
    and a mapping that quietly dropped one of them would still produce speakers here.
    """
    given, expect = case["input"], case["expect"]

    if "raises" in expect:
        recorded = cast("dict[str, Any]", expect["raises"])
        with pytest.raises(RegistryError) as caught:
            await run_the_registry(cast("dict[str, Any]", given))
        port = re.search(r":(\d+)/", str(caught.value))
        assert port, f"the message names no port: {caught.value}"
        wanted = recorded["message"].replace("<PORT>", port.group(1))
        # DELTA 5: pydantic puts the MODEL NAME in its message, and the boundary model is called
        # SpeakerRecord where the old one was called Speaker - the response's record of a speaker,
        # now that the domain has a Speaker of its own. Everything else in the message - the field,
        # the type, the input value, the documentation URL - is byte-identical, which is what the
        # substitution below leaves under test.
        # pydantic names the model in two places, and a message can carry both.
        for names_the_model in (
            "validation error for Speaker\n",
            "validation errors for Speaker\n",
            "instance of Speaker [",
        ):
            wanted = wanted.replace(names_the_model, names_the_model.replace("Speaker", "SpeakerRecord"))
        assert str(caught.value) == wanted
        return

    speakers, _port = await run_the_registry(cast("dict[str, Any]", given))
    # is_console sits beside the fields rather than inside them: it was a property on the old
    # pydantic model and is a property on the dataclass, so model_dump never carried it. It is
    # the one piece of domain behaviour this corpus can see, and it is compared.
    got = [
        {**cast("dict[str, Any]", canonical(one)), "is_console": one.is_console}
        for one in cast("tuple[Speaker, ...]", speakers)
    ]
    assert got == expect["value"]


# ------------------------------------------------------------------------------------------------
# DELTAS 1 and 2, closed at the other end: the SERVICE is what says these lines now
# ------------------------------------------------------------------------------------------------
#
# The two deltas above are half a contract each. The corpus proves that ``seed_from_presets`` and
# ``Dialler.digit`` hand back the text they used to write; nothing there proves anybody writes it.
# So the same recorded lines are replayed once more, through a whole ``ZoneService`` driven the way
# production drives it - the kind included, because a line under the wrong kind is a line a person
# filtering the log will not see.
#
# The service is wired through its ports and through nothing else: no monkeypatch, no private
# method called from here, no socket bound. That is the first thing in this suite to exercise
# ``ZoneServicePorts`` as a substitution seam, which is what the seam is for.


class PresetNotSetError(RuntimeError):
    """What the preset port raises for a key nobody has set.

    A real speaker answers a request for an empty key with something that does not parse, and
    ``_presets_of`` treats every failure alike: it says so and records the key as empty. The type
    and the message are named here because the line the service writes quotes both.
    """


class FakeSwitch:
    """On, and never anything else. Nothing in this section is about the switch."""

    def is_on(self) -> bool:
        return True

    async def watch(self) -> AsyncGenerator[bool, None]:
        yield True
        await asyncio.Event().wait()


class FakeWatch:
    """An observer that connects to nothing. The frames here are put on the queue by hand."""

    async def run(self) -> None:
        await asyncio.Event().wait()


class _NoMpd:
    """The MPD this corpus never reaches, and says so loudly if it ever does.

    Every channel in the golden fixtures is RADIO, so a call here would mean the fixtures had
    changed under the contract they exist to hold still. A stand-in that quietly did nothing
    would let that pass."""

    async def play_entry(self, entry: str, *, place: Place | None = None, end: ChannelEnd) -> None:
        message = f"the golden corpus has no MPD channel, and something asked for {entry!r}"
        raise AssertionError(message)

    async def play_at(self, position: int) -> None:
        message = f"the golden corpus has no MPD channel, and something stepped to entry {position}"
        raise AssertionError(message)

    async def play_files(self, files: Sequence[str], *, place: Place | None = None, end: ChannelEnd) -> None:
        message = f"the golden corpus has no MPD channel, and something queued {len(files)} file(s)"
        raise AssertionError(message)

    async def files_under(self, directory: str) -> tuple[str, ...]:
        message = f"the golden corpus has no MPD channel, and something listed {directory!r}"
        raise AssertionError(message)

    async def queue_files(self) -> tuple[str, ...]:
        message = "the golden corpus has no MPD channel, and something read the queue"
        raise AssertionError(message)

    async def status(self) -> MpdStatus:
        message = "the golden corpus has no MPD channel, and something asked mpd what it was doing"
        raise AssertionError(message)

    async def close(self) -> None:
        """Closing what was never opened is the one call here that may legitimately happen."""


class FakeMaster:
    """A zone that binds no port and reaches no speaker, so a run costs milliseconds.

    It answers every call the pass makes and remembers nothing that is asserted on: what these
    tests read is the LOG, and a master that bound the protocol's four real ports would make this
    file collide with the loopback suite for a reason unrelated to what it checks.
    """

    def __init__(self) -> None:
        self.station: Station | None = None
        self.slaves: dict[str, object] = {}

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def play(self, request: StationRequest) -> Station | None:
        return None

    async def stop_station(self) -> None: ...

    async def add_slave(self, ip: str) -> None: ...

    async def slave_left(self, ip: str) -> None: ...

    async def release(self, ip: str) -> None: ...

    async def dissolve(self) -> None: ...

    def slaves_left_on_an_old_stream(self) -> list[str]:
        return []

    async def put_back_on_the_station(self, peer: str) -> bool:
        return False


SPEAKER_IP = "192.0.2.10"
"""TEST-NET-1: an address nothing here connects to, so a wiring mistake fails rather than reaches."""

MASTER_ID = "5EB0CE000001"


def a_speaker(device_id: str) -> Speaker:
    """One ordinary box - not a console, so the membership rule does not leave it out."""
    return Speaker(
        device_id=device_id,
        name="Studio",
        ip=SPEAKER_IP,
        mac="AABBCC000010",
        product_code="SoundTouch 20",
        account_id="",
    )


def playing_its_own_radio(speaker: Speaker) -> SpeakerEvent:
    """What the start-up probe hears back: awake, on its own station, so not a member and not asleep."""
    return SpeakerEvent(
        received_at=time.time(),
        speaker=speaker.ip,
        device_id=speaker.device_id,
        kind="nowPlayingUpdated",
        source=SourceName.LOCAL_INTERNET_RADIO,
        stream_owner=speaker.device_id,
        frame="",
    )


def wired_service(
    tmp_path: Path, *, speaker: Speaker, presets: dict[int, PresetStation | None]
) -> tuple[ZoneService, list[tuple[str, str]]]:
    """A whole service over fake ports, and the list its narration lands in."""
    lines: list[tuple[str, str]] = []

    def log(kind: str, text: str) -> None:
        lines.append((kind, text))

    def load_state(path: Path, *, log: LogFn) -> ZoneState:
        return ZoneState()

    def save_state(path: Path, state: ZoneState) -> None: ...

    def load_channels(path: Path, *, log: LogFn) -> ChannelList:
        return ChannelList()

    def save_channels(path: Path, channels: ChannelList) -> None: ...

    def open_switch(path: Path, *, log: LogFn, poll_s: float = 1.0) -> FakeSwitch:
        return FakeSwitch()

    async def fetch_speakers(base_url: str = "", *, log: LogFn | None = None) -> tuple[Speaker, ...]:
        return (speaker,)

    def watch_speaker(
        device_id: str,
        /,
        *,
        address_of: AddressOf,
        events: asyncio.Queue[SpeakerEvent],
        log: LogFn,
        policy: ChannelPolicy,
    ) -> FakeWatch:
        return FakeWatch()

    def open_zone_master(
        *,
        bind_ip: str,
        device_id: str,
        log: LogFn,
        events: asyncio.Queue[SpeakerEvent],
        slave_heard: Callable[[str], None],
        ignore_selects: bool,
    ) -> FakeMaster:
        return FakeMaster()

    async def read_volume(ip: str) -> int:
        return 0

    async def set_volume(ip: str, level: int) -> None: ...

    async def select_station(ip: str, *, url: str, name: str) -> None: ...

    async def ask_now_playing(ip: str, device_id: str) -> SpeakerEvent | None:
        return playing_its_own_radio(speaker)

    async def read_preset(ip: str, number: int) -> StationRequest:
        station = presets.get(number)
        if station is None:
            raise PresetNotSetError("no preset there")
        return StationRequest(playback_url=station.url, name=station.name, content_item_xml="")

    def open_mpd(host: str, port: int, log: LogFn) -> MpdControlPort:
        """The corpus has no MPD channel, so reaching this is the test lying about what it ran."""
        return _NoMpd()

    options = ServiceOptions(
        bind_ip="127.0.0.1",
        device_id=MASTER_ID,
        switch_file=tmp_path / "switch",
        state_file=tmp_path / "state.json",
        channel_file=tmp_path / "channels.json",
    )
    ports = ZoneServicePorts(
        load_state=load_state,
        save_state=save_state,
        load_channels=load_channels,
        save_channels=save_channels,
        open_switch=open_switch,
        fetch_speakers=fetch_speakers,
        watch_speaker=watch_speaker,
        open_zone_master=open_zone_master,
        read_volume=read_volume,
        set_volume=set_volume,
        select_station=select_station,
        ask_now_playing=ask_now_playing,
        read_preset=read_preset,
        open_mpd=open_mpd,
    )
    return ZoneService(options, log=log, ports=ports), lines


async def until(condition: Callable[[], bool], what: str, *, timeout: float = 5.0) -> None:
    """Wait for something the SERVICE does, bounded by the wall clock and by nothing it controls.

    The bound has to be independent of the mechanism under test: a loop whose only exit is the
    thing it is waiting for hangs the suite instead of failing it, and names no cause when it does.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)
    message = f"timed out after {timeout}s waiting for {what}"
    raise AssertionError(message)


def lines_of_kind(lines: list[tuple[str, str]], kind: str) -> list[str]:
    return [text for said_kind, text in lines if said_kind == kind]


def probe_lines(presets: dict[int, PresetStation | None]) -> list[str]:
    """What ``_presets_of`` says about each key that is not set, before the seeding says anything.

    The old code wrote exactly these too, from the same place; they are here because the corpus
    records what ``seed_from_presets`` said and not what its caller said around it.
    """
    return [
        f"Studio preset {number}: PresetNotSetError: no preset there"
        for number in sorted(presets)
        if presets[number] is None
    ]


def nothing_to_seed(case: dict[str, Any]) -> list[str]:
    """The caller's own line when a box turned out to have nothing on its keys.

    It is the service's, not the rule's, so the corpus does not record it - but it lands in the
    same stream directly after the recorded lines, and leaving it out of the comparison would make
    this an assertion that cannot see a line arriving in the wrong place.
    """
    if case["expect"]["value"]["fields"]["channels"]:
        return []
    return ["Studio has no presets; the next box switched on gets the chance"]


ALL_SIX = frozenset(range(1, 7))
"""The keys a box has. A corpus case naming any other set describes a call the service cannot make:
``_presets_of`` always asks for all six, so those cases stay proved at the rule's own level above."""

SERVICE_SEEDING = [
    case for case in corpus("seeding")["cases"] if frozenset(int(n) for n, _ in case["input"]["presets"]) == ALL_SIX
]
SERVICE_SEEDING_CASES = 3
"""How many of the eight seeding cases a real box could produce. Pinned for the same reason
CASE_COUNTS is: a selection that quietly matched nothing would pass every assertion below."""


def test_the_service_replays_as_many_seeding_cases_as_a_box_can_produce() -> None:
    assert len(SERVICE_SEEDING) == SERVICE_SEEDING_CASES


@pytest.mark.parametrize("case", SERVICE_SEEDING, ids=lambda c: c["name"])
async def test_the_service_says_the_seeding_lines_the_rule_used_to_say(case: dict[str, Any], tmp_path: Path) -> None:
    """DELTA 1, the other half: in order, under the kind ``channels``, out of a whole service."""
    presets = presets_of(case["input"]["presets"])
    speaker = a_speaker("AABBCC000010")
    service, lines = wired_service(tmp_path, speaker=speaker, presets=presets)

    task = asyncio.create_task(service.run())
    try:
        # The probe line is the marker, and it is unique to the end of the start-up: it is written
        # AFTER the seeding and under a kind nothing else here uses, so it cannot fire early.
        await until(lambda: bool(lines_of_kind(lines, "probe")), "the start-up to finish")
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert lines_of_kind(lines, "channels") == [
        *probe_lines(presets),
        *texts_of(case["expect"]["logs"], kind="channels"),
        *nothing_to_seed(case),
    ]


async def test_the_service_says_what_the_seeding_said_before_it_refused(tmp_path: Path) -> None:
    """The refusal path of DELTA 1: the lines said on the way to a raise are not lost.

    The corpus case this reads is one a box cannot produce - it names three keys - so its presets
    are padded with the empty keys 4, 5 and 6. The padding cannot change what is said, because the
    walk raises at key 2 and never reaches them; that the recorded lines still match is the check.
    """
    (case,) = [c for c in corpus("seeding")["cases"] if "raises" in c["expect"]]
    presets = presets_of(case["input"]["presets"])
    presets.update(dict.fromkeys(ALL_SIX - presets.keys()))
    speaker = a_speaker("AABBCC000010")
    service, lines = wired_service(tmp_path, speaker=speaker, presets=presets)

    with pytest.raises(ChannelNumberError) as caught:
        await service.run()

    recorded = case["expect"]["raises"]
    assert str(caught.value) == recorded["errors"][0]["msg"].removeprefix("Value error, ")
    assert lines_of_kind(lines, "channels") == [
        *probe_lines(presets),
        *texts_of(case["expect"]["logs"], kind="channels"),
    ]


def ignored_digits() -> list[tuple[str, str, str]]:
    """Every recorded ignored digit a PRESS could carry: (device id, digit, the line it produced).

    A digit reaches the service as ``str(preset_id)`` off a selection frame, so only the decimal
    ones are reachable this way. The corpus's empty string, ``a`` and ``'`` are not a press any box
    can send, and they stay proved against the rule itself above.
    """
    found: list[tuple[str, str, str]] = []
    for case in corpus("dialler")["cases"]:
        for step in case["expect"]["trace"]:
            op = step["op"]
            if op[0] == "digit" and step["logs"] and str(op[2]).isdigit():
                (line,) = texts_of(step["logs"], kind="dial")
                found.append((str(op[1]), str(op[2]), line))
    return found


SERVICE_DIALLER = ignored_digits()
SERVICE_DIALLER_CASES = 3
"""How many ignored digits a preset press could carry, of the corpus's six. Pinned for the same
reason as the seeding count above."""


def test_the_service_replays_as_many_ignored_digits_as_a_press_can_carry() -> None:
    assert len(SERVICE_DIALLER) == SERVICE_DIALLER_CASES


@pytest.mark.parametrize(("device_id", "digit", "line"), SERVICE_DIALLER, ids=str)
async def test_the_service_says_the_ignored_digit_the_dialler_used_to_say(
    device_id: str, digit: str, line: str, tmp_path: Path
) -> None:
    """DELTA 2, the other half: one press, through the event stream, under the kind ``dial``.

    The pair is what makes it a press - a selection alone is the master's own station change coming
    back - so both frames go on the queue, and the box is awake, which is what sends the press to
    the dialler rather than reading it as a wake.
    """
    speaker = a_speaker(device_id)
    service, lines = wired_service(tmp_path, speaker=speaker, presets=dict.fromkeys(ALL_SIX))

    task = asyncio.create_task(service.run())
    try:
        await until(lambda: bool(lines_of_kind(lines, "probe")), "the start-up to finish")
        now = time.time()
        service.events.put_nowait(
            SpeakerEvent(
                received_at=now,
                speaker=speaker.ip,
                device_id=device_id,
                kind="nowPlayingUpdated",
                preset_id=int(digit),
                frame="",
            )
        )
        service.events.put_nowait(
            SpeakerEvent(
                received_at=now,
                speaker=speaker.ip,
                device_id=device_id,
                kind=FrameKind.USER_ACTIVITY_UPDATE,
                frame="",
            )
        )
        await until(lambda: bool(lines_of_kind(lines, "dial")), "the digit to be read")
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert lines_of_kind(lines, "dial") == [line]


# ------------------------------------------------------------------------------------------------
# The options corpus: both option boundaries, and the six layers underneath one of them
# ------------------------------------------------------------------------------------------------
#
# Four deltas, each stated rather than hidden, and each the consequence of a move the rebuild made:
#
# 6. ``parse_options`` no longer HOLDS the never-touch refusal: the address of the Room5
#    console was a constant in the archive and is ``[prototype] never_touch`` now, so the refusal
#    is a fact about the flat rather than about the option set. The wheel ships the list EMPTY
#    and the house names its console in its own host layer, so it is replayed with the value the
#    archive's constant held, spelled out below; ``tests/test_never_touch.py`` covers what the
#    setting does in each layer.
# 7. ``Options.encryption_type()`` became ``adapters.soundtouch.wire.encryption_type``. The
#    application may not import the generated protobuf at all, so the option set carries the
#    encryption's NAME and the wire value is looked up at the edge that sends it. Same number.
# 8. A raw ``ValidationError`` names the MODEL, and the model is ``OptionsInput`` where it was
#    ``Options`` - the option set as argv delivered it, now that ``Options`` is the record it
#    produces. One word of one diagnostic; the field, the type, the input value and the
#    documentation URL are byte-identical, and the test substitutes only the name.
# 9. ``report_failure`` takes an :class:`OutputMode` instead of a ``machine``/``indent`` pair. The
#    three recorded shapes are replayed through it, so the bytes stay the contract.


ARCHIVE_NEVER_TOUCH = (ProtectedSpeaker(ip="192.168.0.30", name="Room5", why="the Lifestyle console"),)
"""What the archive's constant held: the corpus was recorded against it, so this is what makes the
replay comparable. The house's host layer carries the same entry today."""

WORK_IN_CORPUS = "<WORK>/options"
"""The scratch directory the corpus was generated in, as its own placeholder renders it: the
generator worked inside ``<WORK>/options`` and the scrubber replaced only the root."""


def here(value: Any, work: Path, *, token: str = WORK_IN_CORPUS) -> Any:
    """A recorded value with the corpus's scratch root replaced by this run's."""
    if isinstance(value, str):
        return value.replace(token, str(work))
    if isinstance(value, list):
        return [here(item, work, token=token) for item in cast("list[Any]", value)]
    if isinstance(value, dict):
        return {key: here(item, work, token=token) for key, item in cast("dict[str, Any]", value).items()}
    return value


def without_host_mac(recorded: dict[str, Any]) -> dict[str, Any]:
    """The recorded fields with ``<HOST_MAC>`` replaced by what this host answers.

    One case gives no device id anywhere and falls back to this machine's MAC, which is a
    different number on the machine replaying it.
    """
    if recorded.get("device_id") != "<HOST_MAC>":
        return recorded
    return {**recorded, "device_id": default_device_id()}


def refusal_matches(caught: Exception, recorded: dict[str, Any], work: Path) -> None:
    """One recorded refusal against the one this run produced, message and exit code alike."""
    assert type(caught).__name__ == recorded["type"]
    assert str(caught) == here(recorded["message"], work)
    if "exit_code" in recorded:
        assert getattr(caught, "exit_code", None) == recorded["exit_code"]


def envelopes_match(caught: Exception, recorded: dict[str, Any], command: str, work: Path) -> None:
    """The three shapes a refusal is reported in, byte for byte against what the archive wrote."""
    for label, mode in (
        ("json_bare", OutputMode(machine=True, indent=None)),
        ("json", OutputMode(machine=True, indent=2)),
        ("human", OutputMode(machine=False, indent=2)),
    ):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            report_failure(caught, command=command, mode=mode)
        assert out.getvalue() == here(recorded[label]["stdout"], work), label
        assert err.getvalue() == here(recorded[label]["stderr"], work), label


NOTHING_TYPED: dict[str, Any] = {
    "bind_ip": None,
    "device_id": None,
    "channel_file": None,
    "switch_file": None,
    "state_file": None,
    "registry_url": None,
    "allow_console": [],
    "unreachable_timeout_s": None,
    "dial_window_s": None,
}
"""An argv in which nothing was typed, which is what the layered cases hand the boundary."""


def with_cli_values(given: dict[str, Any], *, configured: dict[str, Any]) -> ServiceOptions:
    """The boundary called with the recorded CLI values, each named with the type it must have.

    Spelled out rather than splatted: everything the corpus holds is ``Any``, and a ``**`` of it
    would hand nine parameters a type the checker cannot see through - so a recorded value of the
    wrong SHAPE would reach the boundary looking exactly like a correct one.
    """
    return parse_service_options(
        bind_ip=cast("str | None", given["bind_ip"]),
        device_id=cast("str | None", given["device_id"]),
        channel_file=cast("str | None", given["channel_file"]),
        switch_file=cast("str | None", given["switch_file"]),
        state_file=cast("str | None", given["state_file"]),
        registry_url=cast("str | None", given["registry_url"]),
        allow_console=tuple(cast("list[str]", given["allow_console"])),
        unreachable_timeout_s=cast("float | None", given["unreachable_timeout_s"]),
        dial_window_s=cast("float | None", given["dial_window_s"]),
        # Not in the corpus and never will be: the archive had no MPD settings, so "not typed" is
        # what the recorded run really passed. Every case here must answer what it answered then,
        # which is what makes a setting added later provably free of the refusals it records.
        mpd_host=None,
        mpd_port=None,
        mpd_rewind_s=None,
        configured=configured,
    )


def rooted_where_its_relative_paths_exist(given: dict[str, Any], work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replay a case that names RELATIVE paths inside this run's scratch directory, dirs made.

    One recorded case does, and it is there to prove a relative path is KEPT as typed rather than
    resolved - an assertion about the strings, which the working directory cannot move. What the
    working directory does decide is whether those directories EXIST, and the boundary now refuses
    a directory it cannot write the file in (OPEN-WORK rank 107, where a channel file in a
    directory that is not there was accepted at startup and crashed hours later on the first save).
    So the case is given a place where they do exist. Preparing the case's SURROUNDINGS keeps the
    assertion it was written for; changing its expectation to a refusal would have thrown that away
    to test something the case is not about.
    """
    relative = [Path(cast("str", value)) for key, value in given.items() if key.endswith("_file") and value]
    if not any(not path.is_absolute() for path in relative):
        return
    monkeypatch.chdir(work)
    for path in relative:
        if not path.is_absolute():
            path.parent.mkdir(parents=True, exist_ok=True)


def replay_service_options(case: dict[str, Any], work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One ``parse_service_options`` case: what argv and the files between them said."""
    given = here(cast("dict[str, Any]", case["input"]["given"]), work)
    configured = here(cast("dict[str, Any]", case["input"]["configured"]), work)
    expect = cast("dict[str, Any]", case["expect"])
    rooted_where_its_relative_paths_exist(given, work, monkeypatch)

    def call() -> ServiceOptions:
        return with_cli_values(given, configured=configured)

    if "raises" in expect:
        with pytest.raises(OptionsError) as caught:
            call()
        refusal_matches(caught.value, cast("dict[str, Any]", expect["raises"]), work)
        if "envelopes" in expect:
            envelopes_match(caught.value, cast("dict[str, Any]", expect["envelopes"]), service_command, work)
        return

    recorded = cast("dict[str, Any]", expect["value"])
    produced = cast("dict[str, Any]", canonical(call()))
    assert produced["type"] == recorded["type"]
    assert produced["fields"] == here(without_host_mac(cast("dict[str, Any]", recorded["fields"])), work)


def replay_prototype_options(case: dict[str, Any], work: Path) -> None:
    """One ``parse_options`` case, replayed with the never-touch list the wheel ships (delta 6)."""
    given = here(cast("dict[str, Any]", case["input"]["given"]), work)
    expect = cast("dict[str, Any]", case["expect"])

    def call() -> Options:
        return parse_options(**given, never_touch=ARCHIVE_NEVER_TOUCH)

    if "raises" in expect:
        recorded = cast("dict[str, Any]", expect["raises"])
        expected_type = ValidationError if recorded["type"] == "ValidationError" else OptionsError
        with pytest.raises(expected_type) as caught:
            call()
        if recorded["type"] == "ValidationError":
            # Delta 8: the model is named for what it is at this edge. Only the name is substituted,
            # so the field, the type, the input value and the URL stay under test.
            assert str(caught.value) == recorded["message"].replace("for Options", "for OptionsInput")
            return
        refusal_matches(caught.value, recorded, work)
        if "envelopes" in expect:
            envelopes_match(caught.value, cast("dict[str, Any]", expect["envelopes"]), shell_command, work)
        return

    options = call()
    assert canonical(options) == expect["value"]
    # Delta 7: the wire value is the SoundTouch adapter's lookup now, and the number is the same.
    assert int(encryption_type(options.encryption)) == expect["encryption_type"]


def replay_layers(case: dict[str, Any], work: Path, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One layered-configuration case: a file, some variables, some ``--set``, then the record.

    Three tokens, not one, and each is a different thing. The input's ``<WORK>`` is the scratch
    directory itself, because the generator wrote the file with it; the recorded output's is the
    root ABOVE it, because the scrubber replaced only the root; and a path INSIDE the layer tree
    is somewhere else again, since this suite's layers are the conftest's isolated root rather
    than a directory the corpus made. Reading them the same way would make every path case pass by
    comparing two strings that were both wrong, so the longest is substituted first.
    """
    given = cast("dict[str, Any]", case["input"])
    expect = here(cast("dict[str, Any]", case["expect"]), root, token=f"{WORK_IN_CORPUS}/layers")

    if given.get("user_toml") is not None:
        path = root / "xdg" / LAYEREDCONF_SLUG / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(here(given["user_toml"], work, token="<WORK>"), encoding="utf-8")
    for key, value in cast("dict[str, str]", given.get("env", {})).items():
        monkeypatch.setenv(f"{ENV_PREFIX}{key}", here(value, work, token="<WORK>"))
    clear_config_cache()

    if "raises" in expect:
        with pytest.raises(ConfigInputError) as caught:
            merged = apply_set_overrides(
                get_config(profile=given.get("profile")), list(cast("list[str]", given.get("sets", [])))
            )
            service_settings(merged.config)
        refusal_matches(caught.value, cast("dict[str, Any]", expect["raises"]), work)
        if "envelopes" in expect:
            envelopes_match(caught.value, cast("dict[str, Any]", expect["envelopes"]), service_command, work)
        return

    merged = apply_set_overrides(
        get_config(profile=given.get("profile")), list(cast("list[str]", given.get("sets", [])))
    )
    configured = service_settings(merged.config)
    assert configured == here(cast("dict[str, Any]", expect["configured"]), work)
    assert unknown_settings(merged.config) == expect["unknown"]
    assert sorted(merged.overridden) == expect["overridden"]

    parsed = cast("dict[str, Any]", expect["parse"])
    if "raises" in parsed:
        with pytest.raises(OptionsError) as caught:
            with_cli_values(NOTHING_TYPED, configured=configured)
        refusal_matches(caught.value, cast("dict[str, Any]", parsed["raises"]), work)
        return
    produced = cast("dict[str, Any]", canonical(with_cli_values(NOTHING_TYPED, configured=configured)))
    recorded = cast("dict[str, Any]", parsed["value"])
    assert produced["type"] == recorded["type"]
    assert produced["fields"] == here(without_host_mac(cast("dict[str, Any]", recorded["fields"])), work)


@pytest.mark.parametrize("case", corpus("options")["cases"], ids=lambda c: c["name"])
def test_the_option_boundaries_answer_what_the_archive_answered(
    case: dict[str, Any], tmp_path: Path, isolated_config_layers: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every recorded case of both boundaries and the six layers, replayed against the rebuild."""
    op = case["input"]["op"]
    if op == "parse_service_options":
        replay_service_options(case, tmp_path, monkeypatch)
        return
    if op == "parse_options":
        replay_prototype_options(case, tmp_path)
        return
    replay_layers(case, tmp_path, isolated_config_layers, monkeypatch)
