"""A secret held by the records: it can be handed on, and it cannot be shown by accident."""

from __future__ import annotations

import copy
import dataclasses
import pickle
from typing import TYPE_CHECKING

import pytest

from soundtouch_zonemaster.domain.secret import Secret

if TYPE_CHECKING:
    from collections.abc import Callable

FAKE = "TOPSECRET"


def test_the_value_comes_out_only_through_reveal() -> None:
    assert Secret(FAKE).reveal() == FAKE


def _f_string(secret: Secret) -> str:
    return f"{secret}"


def _f_string_repr(secret: Secret) -> str:
    return f"{secret!r}"


def _format_spec(secret: Secret) -> str:
    return format(secret, ">40")


def _percent(secret: Secret) -> str:
    return "%s %r" % (secret, secret)  # noqa: UP031 - the spelling logging uses is the one tested


@pytest.mark.parametrize(
    "shown",
    [
        pytest.param(repr, id="repr"),
        pytest.param(str, id="str"),
        pytest.param(_f_string, id="f-string"),
        pytest.param(_f_string_repr, id="f-string-repr"),
        pytest.param(_format_spec, id="format-spec"),
        pytest.param(_percent, id="percent"),
    ],
)
def test_no_way_of_printing_it_shows_the_value(shown: Callable[[Secret], str]) -> None:
    text = shown(Secret(FAKE))
    assert FAKE not in text
    assert "***" in text, "the control: it prints something, and that something is the mask"


def test_a_record_holding_one_shows_the_mask_in_its_own_repr() -> None:
    """The records print themselves in a traceback or a log line; the field inside must not."""

    @dataclasses.dataclass(frozen=True)
    class Holder:
        password: Secret

    assert FAKE not in repr(Holder(Secret(FAKE)))
    assert FAKE not in str(dataclasses.asdict(Holder(Secret(FAKE))))


def test_two_secrets_with_the_same_value_are_equal_and_hash_alike() -> None:
    assert Secret(FAKE) == Secret(FAKE)
    assert hash(Secret(FAKE)) == hash(Secret(FAKE))
    assert Secret(FAKE) != Secret("other")
    assert Secret(FAKE) != FAKE, "a secret is not the plain string it holds"


def test_it_survives_a_copy() -> None:
    assert copy.deepcopy(Secret(FAKE)).reveal() == FAKE


def test_it_refuses_to_be_pickled() -> None:
    """A pickle is bytes on disk or on a wire, and it would carry the value in the clear."""
    with pytest.raises(TypeError, match="pickle"):
        pickle.dumps(Secret(FAKE))


def test_an_empty_value_is_refused_because_no_password_is_none_rather_than_an_empty_one() -> None:
    """Empty means "no password" to the store, which then passes nothing to the driver. Two ways of
    saying that would leave a caller free to pick the one the store does not expect."""
    with pytest.raises(ValueError, match="empty"):
        Secret("")


def test_it_cannot_be_changed_after_it_is_made() -> None:
    secret = Secret(FAKE)
    attribute = "_value"
    with pytest.raises(AttributeError):
        setattr(secret, attribute, "other")
    assert secret.reveal() == FAKE
