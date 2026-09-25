"""Reading and writing the house's channel list, in a file a person can read and repair.

This is the one place the channel file behaves deliberately unlike the state file, which starts
empty when it cannot be read. The two are not the same kind of file. The state can be re-derived
by asking the speakers, so an empty start costs one reconcile. The channel list is the only copy
of something a person built, and starting empty would let the very next save overwrite it with
nothing.

A service that will not start is a service somebody fixes. One that silently empties the house's
channels is one nobody notices until a room goes quiet.

**The two rules are checked per FIELD, by the domain's own functions.** A person hand-edits this
file, so a document can be wrong in several ways at once, and the count in
``unusable (N problem(s))`` is what tells them how many. A model validator building a
:class:`Channel` and letting its ``__post_init__`` raise would collapse a channel's two problems
into one; two field validators calling :func:`a_number_a_key_can_press` and
:func:`a_url_the_master_can_fetch` keep the count and keep the rule in one place.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from ...domain.channellist import (
    Channel,
    ChannelList,
    a_number_a_key_can_press,
    a_url_the_master_can_fetch,
)

# ChannelKind and ChannelEnd are imported at RUNTIME, not under TYPE_CHECKING: pydantic resolves a model's
# annotations when the model is built, so a type it cannot see leaves ChannelDocument undefined
# and every construction raises PydanticUserError rather than anything to do with the document.
from ...domain.enums import ChannelEnd, ChannelKind
from .atomicfile import write_atomic

if TYPE_CHECKING:
    from pathlib import Path

    from ...domain.logfn import LogFn

__all__ = ["ChannelDocument", "ChannelFileError", "ChannelListDocument", "load_channels", "save_channels"]


class ChannelDocument(BaseModel):
    """One channel as the file holds it, mapped to and from :class:`Channel`."""

    model_config = ConfigDict(frozen=True)

    number: str
    name: str
    kind: ChannelKind
    url: str
    mpd_entry: str = ""
    mpd_directory: str = ""
    in_rotation: bool = True
    end: ChannelEnd = ChannelEnd.WRAP

    @field_validator("number")
    @classmethod
    def _a_number_a_key_can_press(cls, value: str) -> str:
        return a_number_a_key_can_press(value)

    @field_validator("url")
    @classmethod
    def _something_the_master_can_fetch(cls, value: str) -> str:
        return a_url_the_master_can_fetch(value)

    # The one rule here that is NOT per field, and it cannot be: it is about kind, playlist,
    # directory and end TOGETHER. It is the record's own rule, reached by building the record, so
    # it is written once and the two cannot drift. It does not cost the problem count the module
    # docstring protects - a model validator in "after" mode runs only once every field validator
    # has passed, so a channel already wrong in two field ways still reports 2, and this can only
    # ever add a problem to a channel that had none.
    @model_validator(mode="after")
    def _the_source_fits_the_kind(self) -> ChannelDocument:
        self.to_channel()
        return self

    # Both directions go field by field WITHOUT naming the fields. Written out by hand, a field
    # one of them forgot was dropped silently: the channel came back with the default, and
    # nothing raised anywhere. Now a field the RECORD lacks raises on the first load; one the
    # DOCUMENT lacks is still ignored on the way in, which is what the round-trip tests are for.
    @classmethod
    def of(cls, channel: Channel) -> ChannelDocument:
        return cls(**asdict(channel))

    def to_channel(self) -> Channel:
        return Channel(**self.model_dump())


class ChannelListDocument(BaseModel):
    """The channel file itself: the ``channels`` array and nothing else."""

    model_config = ConfigDict(frozen=True)

    channels: tuple[ChannelDocument, ...] = ()

    @classmethod
    def of(cls, channels: ChannelList) -> ChannelListDocument:
        return cls(channels=tuple(ChannelDocument.of(one) for one in channels.channels))

    def to_channel_list(self) -> ChannelList:
        return ChannelList(channels=tuple(one.to_channel() for one in self.channels))


class ChannelFileError(RuntimeError):
    """The channel file exists and could not be read, in any of the ways that can happen.

    One error for all of them, for the reason :class:`~soundtouch_zonemaster.application.errors.RegistryError`
    gives:
    the caller's useful reaction is the same in every case, while the person reading the log still
    needs to tell "it is not JSON" from "it names a number no key can press".
    """


def load_channels(path: Path, *, log: LogFn) -> ChannelList:
    """Read the channel list, or REFUSE. A missing file is an empty list, not a refusal.

    This is the one place the channel file behaves deliberately unlike the state file, which
    starts empty when it cannot be read. The two are not the same kind of file. The state can be
    re-derived by asking the speakers, so an empty start costs one reconcile. The channel list is
    the only copy of something a person built, and starting empty would let the very next save
    overwrite it with nothing.

    A service that will not start is a service somebody fixes. One that silently empties the
    house's channels is one nobody notices until a room goes quiet.

    The bytes are decoded as ``utf-8-sig`` rather than ``utf-8``, which is the same codec plus one
    rule: a leading byte order mark belongs to the encoding and not to the document. Windows writes
    that mark when it saves "UTF-8 with BOM", and this is the file most likely to have been edited
    by hand from there, because it is the only copy of something a person built - so without the
    rule three invisible bytes refuse a list that is in fact perfectly good.
    """
    try:
        written = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        log("channels", f"{path}: no channel list yet")
        return ChannelList()
    except (OSError, UnicodeDecodeError) as exc:
        # Bytes that are not UTF-8 are one of the ways this file can be unreadable, and the
        # refusal was already right - what was missing is the PATH. A raw UnicodeDecodeError
        # leaves the one person who has to repair the list with a traceback that never says
        # which file it was.
        raise ChannelFileError(f"{path}: could not be read ({type(exc).__name__})") from exc
    try:
        parsed = ChannelListDocument.model_validate_json(written)
    except ValidationError as exc:
        raise ChannelFileError(f"{path}: unusable ({exc.error_count()} problem(s))") from exc
    channels = parsed.to_channel_list()
    log("channels", f"{path}: {len(channels.channels)} channel(s)")
    return channels


def save_channels(path: Path, channels: ChannelList) -> None:
    """Write the channel list where a person can read and repair it.

    Indented and newline-terminated on purpose: this is a file somebody opens in an editor, unlike
    anything else the service writes.
    """
    write_atomic(path, ChannelListDocument.of(channels).model_dump_json(indent=2) + "\n")
