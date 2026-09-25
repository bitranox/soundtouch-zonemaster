"""The zone master's machine-readable mode, driven the way a caller drives it.

The promise is about a PROCESS: with ``--json`` or ``--json-bare`` the run's narration moves to
stderr so that stdout carries the envelope and nothing else, which is what lets a caller parse
stdout without filtering the log out of it. Asserting that inside this process would leave the
promise untested exactly where it breaks - anything at all printing on the way - so the command
runs here as a command, and its two streams are read apart.

Nothing is stubbed. The run in that subprocess reads a preset off a speaker, plays the station the
preset names, takes a slave on and dissolves the zone again; the speaker and the station are
servers this test starts on loopback. A speaker answers on port 8090 and so does the master, and
both ports are hardcoded because a real box has no other, so the two cannot share an address: the
master is bound to a second loopback address, and the file is skipped where the host has none.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path

import pytest
from speaker_double import FakeSpeaker

from soundtouch_zonemaster.entry import prototype_main as main

ROOT = Path(__file__).resolve().parents[1]
SPEAKER = "127.0.0.1"
"""The double: it owns port 8090 here."""
MASTER = "127.0.0.2"
"""The master's own address, so its own port 8090 does not collide with the double's."""
CONTENT_ITEM = (
    '<ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" location="{url}" '
    'sourceAccount="" isPresetable="true"><itemName>Preset One</itemName></ContentItem>'
)
RUN_TIMEOUT_S = 120.0
"""Generous on purpose: it is here to end a hung run, not to time a healthy one."""


def _binds(host: str) -> bool:
    """Whether this host hands out ``host`` to bind on.

    Linux routes the whole 127.0.0.0/8 to loopback; macOS routes only 127.0.0.1 until a second
    address is aliased onto lo0. Probed rather than assumed, so the skip reason below is true of
    the machine running it rather than of the machine it was written on.
    """
    try:
        with socket.socket() as probe:
            probe.bind((host, 0))
    except OSError:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _binds(MASTER), reason=f"this host does not hand out {MASTER}: no second loopback address"
)


async def _station_server(payload: bytes) -> tuple[asyncio.AbstractServer, str]:
    """A station that hands out ``payload`` and then holds the connection, as a live one does.

    Holding it is also how the handler ends: when the run is over, the master's socket closes and
    the read below returns nothing, so no task is left sleeping behind the test.
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\n\r\n" + payload)
        await writer.drain()
        await reader.read()
        writer.close()

    server = await asyncio.start_server(handle, SPEAKER, 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"http://{SPEAKER}:{port}/live"


async def _run_cli(*args: str) -> tuple[int, str, str]:
    """Run the real command as a command; returns its exit code and its two streams."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "soundtouch_zonemaster",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=RUN_TIMEOUT_S)
    assert proc.returncode is not None, "communicate() returned, so the process is finished"
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def test_a_machine_mode_run_puts_one_envelope_on_stdout_and_its_narration_on_stderr() -> None:
    """The whole promise of ``--json-bare``, on a run that really played to a really joined slave."""
    server, url = await _station_server(os.urandom(64_000))
    speaker = FakeSpeaker({1: CONTENT_ITEM.format(url=url)}, host=SPEAKER)
    await speaker.start()
    try:
        rc, out, err = await _run_cli(
            "--bind-ip",
            MASTER,
            "--slave",
            SPEAKER,
            "--preset-from",
            SPEAKER,
            "--preset",
            "1",
            "--duration",
            "0",
            "--json-bare",
        )
    finally:
        await speaker.stop()
        server.close()
        await server.wait_closed()

    assert speaker.paths() == ["/presets", "/info", "/setZone", "/setZone"], (
        "the run really happened: a preset was read, the slave was taken on, and the zone was undone"
    )
    assert "<member" not in speaker.bodies_for("/setZone")[-1], "the last zone the box got is the empty one"
    assert rc == 0
    assert out.count("\n") == 1, "--json-bare is one line, so a caller can read a stream of them"
    envelope = json.loads(out)
    assert envelope["ok"] is True
    assert envelope["command"] == "soundtouch-zonemaster"
    assert envelope["data"]["exit_code"] == 0
    assert envelope["data"]["bind_ip"] == MASTER
    assert envelope["data"]["slaves"] == [SPEAKER]
    assert "dissolving and shutting down" in err, "the narration is on the other stream, not gone"


@pytest.mark.usefixtures("a_house_that_protects_its_console")
def test_a_refusal_in_machine_mode_is_an_envelope_on_stdout_rather_than_prose(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other half of the promise: a caller parses one shape whether the answer is yes or no.

    In process because this refusal is decided before the first socket, and with ``--json`` so the
    indented envelope - the shape the bare flag turns off - is the one being read.
    """
    monkeypatch.setattr(
        "sys.argv",
        ["soundtouch-zonemaster", "--bind-ip", MASTER, "--preset-from", SPEAKER, "--slave", "192.168.0.30", "--json"],
    )
    assert main() == 1
    out = capsys.readouterr().out
    assert out.count("\n") > 1, "--json is the indented envelope"
    envelope = json.loads(out)
    assert envelope == {
        "ok": False,
        "command": "soundtouch-zonemaster",
        "error": "OptionsError",
        "message": "refused: Room5 (192.168.0.30) is the Lifestyle console",
    }
