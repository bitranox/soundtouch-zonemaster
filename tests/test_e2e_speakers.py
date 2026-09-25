"""End to end against the real speakers: this repository's parsers, on documents a speaker served.

Everything the master believes about a speaker's XML is a regex written from a capture. A capture
is a moment; the speakers are still there, and they are the only authority on what they emit. So
this reads five documents from each real speaker and runs the SHIPPED parsers over them - the same
functions the prototype uses to find a device id and a preset before it plays anything.

Two constraints shape how it runs, and both are structural rather than careful:

- The development host cannot reach the speakers; only a machine on their LAN can. So the probe is
  SHIPPED there and run, and hands back one JSON envelope. Nothing here drives the far end step by
  step over ssh, and nothing here parses prose.
- The probe issues GET only. It cannot make a sound, so it is safe on every speaker in the flat,
  the Lifestyle console included: a POST is what would flip that one's input. Anything AUDIBLE is
  gated behind ``e2e_audible`` in an untracked settings file and is not in this file.

Marked ``integration``: ``make test`` skips it, ``make testintegration`` runs it. The settings come
from ``tests/e2e_config.py``; with no private ``tests/e2e_defaults.d/90-e2e-rnhome.toml`` there is
no host and no speaker list, and every test here skips rather than guessing an address.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from e2e_config import E2ESettings, load_e2e_settings
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))
import capture_zone as cz

pytestmark = pytest.mark.integration

PROBE = Path(__file__).resolve().parent / "e2e" / "speaker_probe.py"
REMOTE_PROBE = "/tmp/soundtouch_speaker_probe.py"
SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "PreferredAuthentications=publickey", "-o", "ConnectTimeout=8")


class ProbeData(BaseModel):
    """Each speaker's documents, keyed by IP and then by path."""

    speakers: dict[str, dict[str, str]]


class ProbeEnvelope(BaseModel):
    """The probe's envelope, the same shape the tools in this repository print."""

    ok: bool
    command: str
    data: ProbeData
    skipped: list[str] = []


def _settings() -> E2ESettings:
    """The settings, or a skip naming what is missing rather than a failure that reads as a defect."""
    settings = load_e2e_settings()
    if not settings.reachable:
        pytest.skip(
            "no e2e host or speakers configured; copy tests/e2e_defaults.d/90-e2e-rnhome.toml.example"
            " to 90-e2e-rnhome.toml beside it to run these"
        )
    if not (settings.key and Path(settings.key).exists()):
        pytest.skip(f"no usable ssh key at {settings.key!r}")
    return settings


@pytest.fixture(scope="module")
def probed() -> ProbeEnvelope:
    """Ship the probe to the host on the speakers' LAN, run it there, and parse what it returns."""
    settings = _settings()
    scp, ssh = shutil.which("scp"), shutil.which("ssh")
    if scp is None or ssh is None:
        pytest.skip("ssh and scp are how the probe reaches the speakers' LAN")
    target = f"{settings.user}@{settings.host}"

    copied = subprocess.run(  # noqa: S603 - argv list, every element from settings or a literal
        [scp, "-i", settings.key, *SSH_OPTIONS, str(PROBE), f"{target}:{REMOTE_PROBE}"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if copied.returncode != 0:
        pytest.skip(f"cannot reach {target}: {copied.stderr.strip()[:200]}")

    done = subprocess.run(  # noqa: S603 - argv list, every element from settings or a literal
        [
            ssh,
            "-i",
            settings.key,
            *SSH_OPTIONS,
            target,
            "python3",
            REMOTE_PROBE,
            "--speakers",
            ",".join(settings.speakers),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    # Exit 1 means a speaker did not answer, which the envelope reports in `skipped`; the tests
    # below read that. Only a probe that produced no envelope at all is a reason to stop.
    if not done.stdout.strip():
        pytest.fail(f"the probe produced no envelope (rc={done.returncode}): {done.stderr.strip()[:300]}")
    return ProbeEnvelope.model_validate(json.loads(done.stdout))


def test_every_configured_speaker_answered(probed: ProbeEnvelope) -> None:
    """A speaker that did not answer is worth saying out loud: the rest of this file skipped it."""
    assert probed.data.speakers, "no speaker answered at all"
    assert probed.skipped == [], "a configured speaker did not answer"


def test_a_real_device_id_is_what_the_master_requires_of_one(probed: ProbeEnvelope) -> None:
    """The one assumption a run cannot recover from: the master addresses members by this string.

    ``capture_zone.device_id_in`` matches uppercase hex only, and the CLI refuses a ``--device-id``
    that is not exactly twelve hex digits. Both were written from one capture of one speaker; this
    holds them against every speaker in the flat.
    """
    for ip, documents in probed.data.speakers.items():
        device_id = cz.device_id_in(documents["/info"])
        assert re.fullmatch(r"[0-9A-F]{12}", device_id), f"{ip} reports a device id the master would refuse"


def test_a_real_now_playing_names_a_source_the_parser_recognises(probed: ProbeEnvelope) -> None:
    """``source_of`` falls back to a sentinel rather than raising, so a silent miss is possible."""
    for ip, documents in probed.data.speakers.items():
        source = cz.source_of(documents["/now_playing"])
        assert source != cz.UNKNOWN_SOURCE, f"{ip}: no source attribute found in a real /now_playing"
        assert re.fullmatch(r"[A-Z_]+", source), f"{ip}: {source!r} is not the shape the parser promises"


def test_a_real_volume_document_yields_a_number(probed: ProbeEnvelope) -> None:
    """``volume_in`` answers -1 when it finds nothing, which a real speaker must never produce."""
    for ip, documents in probed.data.speakers.items():
        volume = cz.volume_in(documents["/volume"])
        assert 0 <= volume <= 100, f"{ip}: volume {volume} means the document did not parse"


def test_a_real_preset_yields_the_content_item_the_master_would_play(probed: ProbeEnvelope) -> None:
    """The preset parser is what ``--preset-from`` uses to find something to play.

    A speaker with no preset 1 is not a defect, so the requirement is that at least one speaker in
    the flat has one and that it parses. A house where none does would make ``--preset-from``
    untestable here, and saying so is better than passing silently.
    """
    found: dict[str, str] = {}
    for ip, documents in probed.data.speakers.items():
        if '<preset id="1"' not in documents["/presets"]:
            continue
        item = cz.preset_item_in(documents["/presets"], 1)
        assert item.startswith("<ContentItem"), f"{ip}: preset 1 did not parse into a ContentItem"
        found[ip] = item
    assert found, "no speaker in the flat has a preset 1, so --preset-from has nothing to read"
