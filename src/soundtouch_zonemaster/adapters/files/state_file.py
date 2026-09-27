"""Reading and writing the state file, in a file a person can read and repair.

**A state file is read after whatever ended the last run**, which includes a power cut in the
middle of a write. So a document this cannot make sense of starts the service EMPTY and says which
file it was, rather than raising: everything in it can be asked again, so an empty start costs one
reconcile. The channel list answers the same question the other way and says why
(``channel_file``). Saving goes through ``atomicfile``, which is what makes a half-written document
unobservable in the first place.

:class:`StateDocument` is the boundary, and it is a pydantic model on purpose. It carries the same
field names, types and defaults the record did when the record itself was a pydantic model, so a
file written before the rebuild loads the same way, the coercions a hand-edited file relies on are
unchanged (a volume written ``"17"`` or ``17.0`` is still 17, a window written ``true`` is still
1.0), and the ``unusable, starting empty (N problem(s))`` count in the log still counts what it
counted. ``tests/test_boundary_golden.py`` replays the recorded old behaviour against it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, ValidationError

from ...domain.preferences import PreferenceName
from ...domain.state import Place, ZoneState
from .atomicfile import write_atomic

if TYPE_CHECKING:
    from pathlib import Path

    from ...domain.logfn import LogFn

__all__ = [
    "LegacyState",
    "PlaceDocument",
    "StateDocument",
    "StateFileError",
    "load_state",
    "load_state_strict",
    "save_state",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class LegacyState:
    """What an old state file holds: the state, and the two numbers a calibration wrote beside it.

    The file kept them together; the house database keeps the numbers as preferences
    (``house_preferences``), so they part here, at the one reader of the old format."""

    state: ZoneState = field(default_factory=ZoneState)
    dial_window_s: float | None = None
    hold_threshold_s: float | None = None

    def calibration(self) -> tuple[tuple[PreferenceName, float], ...]:
        """The calibrated numbers the file carries, as preferences, skipping the ones it does not."""
        held = ((PreferenceName.WINDOW, self.dial_window_s), (PreferenceName.HOLD, self.hold_threshold_s))
        return tuple((name, value) for name, value in held if value is not None)


class PlaceDocument(BaseModel):
    """Where one channel was left, as it is written and parsed.

    A file written before the track was remembered holds a bare NUMBER here rather than this, and
    such a file still loads: the number is the offset and the track is the first one, which is what
    that version would have played anyway. The other way round is not needed - a file written by
    this version is read by this version or a later one.
    """

    model_config = ConfigDict(frozen=True)

    track: int
    seconds: float
    file: str | None = None


class StateDocument(BaseModel):
    """The state file as it is written and parsed, mapped to and from :class:`LegacyState`.

    Every field mirrors the record or one of the two calibrated numbers beside it. The model is the
    one place that may coerce, so the record itself never has to accept a string where it declares
    a number.
    """

    model_config = ConfigDict(frozen=True)

    channel: str | None = None
    members: tuple[str, ...] = ()
    muted: dict[str, int] = {}
    out_of_multiroom: tuple[str, ...] = ()
    positions: dict[str, PlaceDocument | float] = {}
    """The document arm first, so that only a bare number reaches the second one.

    Pydantic tries a union's arms in order and a permissive arm swallows the rest; here neither
    can stand in for the other - a mapping is not a float and a float is not a mapping - so the
    order is for the reader rather than for the parser."""
    dial_window_s: float | None = None
    hold_threshold_s: float | None = None
    owed_volume: dict[str, int] = {}

    @classmethod
    def of(cls, legacy: LegacyState) -> StateDocument:
        """The document for a record and its calibration, which is what gets written."""
        state = legacy.state
        return cls(
            channel=state.channel,
            members=state.members,
            muted=state.muted,
            out_of_multiroom=state.out_of_multiroom,
            positions={
                number: PlaceDocument(track=p.track, seconds=p.seconds, file=p.file)
                for number, p in state.positions.items()
            },
            dial_window_s=legacy.dial_window_s,
            hold_threshold_s=legacy.hold_threshold_s,
            owed_volume=state.owed_volume,
        )

    def to_legacy(self) -> LegacyState:
        """The record and the calibration for a parsed document, which is what the import is handed."""
        return LegacyState(
            state=ZoneState(
                channel=self.channel,
                members=self.members,
                muted=dict(self.muted),
                out_of_multiroom=self.out_of_multiroom,
                positions={number: _place(held) for number, held in self.positions.items()},
                owed_volume=dict(self.owed_volume),
            ),
            dial_window_s=self.dial_window_s,
            hold_threshold_s=self.hold_threshold_s,
        )


def _place(held: PlaceDocument | float) -> Place:
    """One channel's place, from either shape a state file can hold it in."""
    if isinstance(held, PlaceDocument):
        return Place(track=held.track, seconds=held.seconds, file=held.file)
    return Place(track=0, seconds=held)


def load_state(path: Path, *, log: LogFn) -> LegacyState:
    """Read the state, or start empty and say why. Never raises.

    Nothing in ``src/`` calls this: the service reads its state from the house database, and an old
    file is read once, strictly, by the import (:func:`load_state_strict`). It is kept because the
    golden corpus replays the old format through it, which is what holds the document both readers
    share to the one the deployed service wrote.

    The bytes are decoded as ``utf-8-sig`` rather than ``utf-8``, which is the same codec plus one
    rule: a leading byte order mark belongs to the encoding and not to the document. Windows writes
    that mark when it saves "UTF-8 with BOM", and this file is edited over SMB from there, so
    without the rule three invisible bytes make a file a person can read and repair report itself
    unusable - and the remembered channel is then lost on the very next restart, which is the one
    thing this file is kept for.

    A file whose BYTES are not UTF-8 at all is one of the ways it can be unreadable, not a
    different kind of problem: it starts empty and names the path, exactly as a permission error
    does. The atomic write makes those bytes unlikely rather than impossible - a disk error, or one
    hand in an editor set to another encoding - and everything in this file can be asked again, so
    the cost of being wrong here is one reconcile against the cost of a service that will not
    start.
    """
    try:
        written = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return LegacyState()
    except (OSError, UnicodeDecodeError) as exc:
        log("state", f"{path}: could not be read ({type(exc).__name__}); starting empty")
        return LegacyState()
    try:
        return StateDocument.model_validate_json(written).to_legacy()
    except ValidationError as exc:
        log("state", f"{path}: unusable, starting empty ({exc.error_count()} problem(s))")
        return LegacyState()


def save_state(path: Path, legacy: LegacyState) -> None:
    """Write the state so that no reader can ever see half of it."""
    write_atomic(path, StateDocument.of(legacy).model_dump_json(indent=2) + "\n")


class StateFileError(RuntimeError):
    """The state file exists and could not be read, in any of the ways that can happen.

    ``load_state`` never raises this, and nothing in the program calls it: it is kept for the golden
    corpus, which replays the old format through it. The one-time import into the house
    database is a different reader with a different cost of being wrong: writing an empty state
    there looks exactly like a real, if empty, file, and the file that caused it is then renamed
    and never looked at again. So the import reads through :func:`load_state_strict` instead,
    which refuses rather than guesses. It shares :class:`StateDocument` with ``load_state``, so a
    file either one accepts is read the same way by both, and only the reaction to a bad one
    differs.
    """


def load_state_strict(path: Path) -> LegacyState:
    """Read the state, or REFUSE naming the file. The caller checks the file exists first.

    Bytes are decoded as ``utf-8-sig``, the same rule ``load_state`` applies, so a file either
    reader accepts loads identically under both.
    """
    try:
        written = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise StateFileError(f"{path}: could not be read ({type(exc).__name__})") from exc
    try:
        return StateDocument.model_validate_json(written).to_legacy()
    except ValidationError as exc:
        raise StateFileError(f"{path}: unusable ({exc.error_count()} problem(s))") from exc
