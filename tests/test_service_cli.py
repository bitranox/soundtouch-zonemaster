"""The service's command line: what it refuses, and what the service is actually handed.

Everything here runs the real ``main`` with a real argv and touches no speaker: the checks under
test all sit ahead of the first socket, and the run itself is substituted through ``main``'s own
parameter rather than patched, so what is asserted is the whole way from argv into
:class:`ServiceOptions` - the click option and the callback signature included, which are the two
halves that drift apart.

The machine-readable mode is a promise about a PROCESS - the narration on stderr so that stdout
carries the envelope and nothing else - and the unit that will run this is driven by a systemd
file rather than by a person, so a caller has to be able to parse what it printed.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import lib_cli_exit_tools
import pytest
from nothing_typed import NOTHING_TYPED

from soundtouch_zonemaster.__init__conf__ import version
from soundtouch_zonemaster.adapters.cli.boundary import parse_service_options
from soundtouch_zonemaster.adapters.files.house_store import SqlHouseStore
from soundtouch_zonemaster.adapters.logging.narration import log
from soundtouch_zonemaster.application.outcome import ExitCode, OptionsError
from soundtouch_zonemaster.composition import hold_the_zone
from soundtouch_zonemaster.entry import service_main as main

if TYPE_CHECKING:
    from pathlib import Path

    from soundtouch_zonemaster.application.options import ServiceOptions
    from soundtouch_zonemaster.application.ports import RunService


def _argv(tmp_path: Path, *extra: str) -> list[str]:
    """An otherwise-valid argv, with ``extra`` appended."""
    return [
        "soundtouch-zonemaster-service",
        "--bind-ip",
        "203.0.113.190",
        "--database",
        str(tmp_path / "zonemaster.sqlite"),
        *extra,
    ]


async def _refuse_to_run(_options: ServiceOptions) -> int:
    """Stands in for the run, and fails loudly if a refusal let one start."""
    raise AssertionError("the service must not start when the options were refused")


_REAL_RUN_BOUND_S = 5.0
"""How long the real service may run before a test that expects a refusal gives up on it.

A bound the subject does not control: if the refusal these tests check for ever regressed, the
real ``hold_the_zone`` would bind the fixed ports (8090, the notification port, 40005) and hold
them until the process is killed, hanging ``make test`` rather than failing it. Named so the
timeout reads as what it is - the refusal did not fire - rather than as an unrelated hang.
"""


class _Bound:
    """Whether the bounded run in :func:`_hold_the_zone_bounded` had to time out.

    ``main`` funnels the run coroutine through ``lib_cli_exit_tools.run_cli``, whose single
    ``except BaseException`` handler turns ANY exception raised inside it - a ``pytest.fail``
    included - into an ordinary exit code. So a bound that reports itself by raising can never
    surface as a test failure; it has to record onto something the test still holds after
    ``main()`` returns.
    """

    def __init__(self) -> None:
        self.timed_out = False


_BOUND_FIRED_MESSAGE = f"the real service ran past its {_REAL_RUN_BOUND_S}s bound: the expected refusal did not fire"


def _hold_the_zone_bounded(bound: _Bound) -> RunService:
    """Build a bounded real run that records a timeout onto ``bound`` instead of raising past ``main``.

    Used only by tests that expect ``main`` to refuse the start before this is ever reached; a
    passing test never waits out the bound, because the refusal happens first and this coroutine
    is never awaited to completion. A caller must assert ``not bound.timed_out`` right after
    ``main()`` returns and before reading its return code or envelope - once the bound has fired,
    both are meaningless, because ``run_cli`` mapped the timeout to an ordinary exit code rather
    than letting it fail the test.
    """

    async def _run(options: ServiceOptions) -> int:
        try:
            return await asyncio.wait_for(hold_the_zone(options), timeout=_REAL_RUN_BOUND_S)
        except TimeoutError:
            bound.timed_out = True
            return ExitCode.ERROR

    return _run


def test_no_database_anywhere_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "--bind-ip", "203.0.113.190"])
    rc = main(run_service=_refuse_to_run)
    assert rc == 2
    assert "database" in capsys.readouterr().err


def test_a_database_in_a_directory_that_does_not_exist_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["soundtouch-zonemaster-service", "--bind-ip", "203.0.113.190", "--database", str(tmp_path / "nope" / "db")],
    )
    rc = main(run_service=_refuse_to_run)
    assert rc == 1, "a place the program cannot use is a refusal, not an error: ExitCode.REFUSED"
    assert "is not a directory to keep the house database in" in capsys.readouterr().err


def test_a_database_url_in_a_directory_that_does_not_exist_is_not_refused_at_the_boundary() -> None:
    """The boundary's own directory check names a PLAIN PATH only: a ``sqlite:////...`` URL naming
    a missing directory is not caught here at all, unlike the equivalent plain path above - it is
    the store's own ``open()`` that fails on it, later and differently."""
    database = parse_service_options(
        bind_ip="127.0.0.1",
        database="sqlite:////nonexistent-directory/zonemaster.sqlite",
        configured={},
        **NOTHING_TYPED,
    ).database
    assert database == "sqlite:////nonexistent-directory/zonemaster.sqlite"


@pytest.mark.parametrize("flag", ["--channel-file", "--switch-file", "--state-file"])
def test_the_old_file_options_are_not_options_any_more(
    flag: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The three files from before the house database are gone with their one-time import. A unit
    still passing one must FAIL rather than start with it ignored, for the reason
    ``--seed-from`` gives below: a silently accepted option would hide that the unit was never
    updated."""
    monkeypatch.setattr("sys.argv", _argv(tmp_path, flag, str(tmp_path / "old")))

    assert main(run_service=_refuse_to_run) == 2
    assert flag.removeprefix("--") in capsys.readouterr().err


def test_a_device_id_that_is_not_twelve_hex_digits_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr("sys.argv", _argv(tmp_path, "--device-id", "nothex"))

    assert main(run_service=_refuse_to_run) == 2
    assert "12 hex digits" in capsys.readouterr().err


def test_naming_a_speaker_to_seed_from_is_not_an_option_any_more(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The list is seeded from the box that is switched on first (user, 2026-09-07), so there is
    nothing to name. An old unit still passing it must FAIL rather than start with it ignored -
    the two repositories cannot move at once, and a silently accepted option would hide that."""
    monkeypatch.setattr("sys.argv", _argv(tmp_path, "--seed-from", "Room1"))

    assert main(run_service=_refuse_to_run) == 2
    assert "seed-from" in capsys.readouterr().err


def test_what_was_typed_is_what_the_service_is_handed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[ServiceOptions] = []

    async def capture(options: ServiceOptions) -> int:
        seen.append(options)
        return 0

    monkeypatch.setattr(
        "sys.argv",
        _argv(
            tmp_path,
            "--allow-console",
            "AABBCC000012",
            "--unreachable-timeout-s",
            "60",
            "--registry-url",
            "http://127.0.0.1:9000",
        ),
    )

    assert main(run_service=capture) == 0
    options = seen[0]
    assert options.bind_ip == "203.0.113.190"
    assert options.consoles_allowed == ("AABBCC000012",)
    assert options.unreachable_timeout_s == 60
    assert options.registry_url == "http://127.0.0.1:9000"


def test_machine_mode_puts_one_envelope_on_stdout_and_the_narration_on_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A systemd unit is not a person: what it prints has to be parseable without filtering."""

    async def narrate(_options: ServiceOptions) -> int:
        log("zone", "a line a run would print")
        return 0

    monkeypatch.setattr("sys.argv", _argv(tmp_path, "--json-bare"))

    assert main(run_service=narrate) == 0
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert envelope["ok"] is True
    assert envelope["command"] == "soundtouch-zonemaster-service"
    assert captured.out.count("\n") == 1, "one line, so a caller can read it without a parser"
    assert "a line a run would print" in captured.err


def test_a_stop_signal_is_a_clean_end_and_not_a_crash(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A unit is STOPPED, and a stop must not read as a failure.

    lib_cli_exit_tools installs the signal handlers, and the one for SIGINT raises SigIntInterrupt
    - which is a RuntimeError, so a plain `except KeyboardInterrupt` never sees it and the generic
    handler reports exit 2. On a unit with Restart=on-failure and an OnFailure mail that makes
    every ordinary `systemctl stop` look like a crash and send one. Measured on the real unit,
    2026-09-07: the zone dissolved correctly and systemd still marked the service failed.
    """

    async def interrupted(_options: ServiceOptions) -> int:
        raise lib_cli_exit_tools.SigIntInterrupt("Aborted (SIGINT).")

    monkeypatch.setattr("sys.argv", _argv(tmp_path))

    assert main(run_service=interrupted) == 0


def _the_frame_that_failed() -> None:
    """A named frame, so a test can require the traceback to NAME where it broke."""
    msg = "Connection lost"
    raise ConnectionResetError(msg)


async def _crash(_options: ServiceOptions) -> int:
    """A run that dies of something nobody expected, from a frame with a findable name."""
    _the_frame_that_failed()
    return 0  # pragma: no cover - the line above always raises


def test_an_unexpected_crash_keeps_its_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A crash has to name where it broke, because the house is not watched while it runs.

    Measured in the flat 2026-09-20 at 22:46:25: the service died of a ConnectionResetError and
    the whole record of it was `error ConnectionResetError: Connection lost` plus the message
    again. It did not happen on the repeat, so the one occurrence was all the evidence there was
    and it named no file, no line and no frame. A service a systemd unit restarts is exactly the
    one whose crash nobody watches, so the traceback is the only thing that can carry the cause.
    """
    monkeypatch.setattr("sys.argv", _argv(tmp_path))

    assert main(run_service=_crash) == 2

    err = capsys.readouterr().err
    assert "ConnectionResetError: Connection lost" in err, "the one-line summary stays"
    assert "Traceback (most recent call last)" in err
    assert "_the_frame_that_failed" in err, "a traceback that does not name the frame is not one"


def test_a_crash_traceback_stays_out_of_the_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Machine mode promises stdout carries the envelope and nothing else, crash included."""
    monkeypatch.setattr("sys.argv", _argv(tmp_path, "--json-bare"))

    assert main(run_service=_crash) == 2

    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert envelope["ok"] is False
    assert envelope["error"] == "ConnectionResetError"
    assert captured.out.count("\n") == 1, "one line, so a caller can read it without a parser"
    assert "Traceback" not in captured.out, "a traceback is narration, and narration is stderr"
    assert "Traceback (most recent call last)" in captured.err


def test_a_refusal_carries_no_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The control, and the reason the two above mean anything.

    A refusal is an ANSWER - the options were wrong and the program said so - so it exits 2 with
    prose and no stack. A blanket traceback would satisfy both tests above and be wrong here, so
    this is what makes them a statement about the unexpected branch rather than about printing.
    """
    monkeypatch.setattr("sys.argv", _argv(tmp_path, "--device-id", "nothex"))

    assert main(run_service=_refuse_to_run) == 2

    err = capsys.readouterr().err
    assert "12 hex digits" in err
    assert "Traceback" not in err, "a refusal is an answer, not a crash"


def test_a_database_another_service_holds_refuses_the_start_as_busy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """One writer: a second service is told the database is held, and the answer is no (exit 1)."""
    holder = SqlHouseStore(str(tmp_path / "zonemaster.sqlite"), log=log)
    holder.open(exclusive=True)
    bound = _Bound()
    try:
        monkeypatch.setattr("sys.argv", _argv(tmp_path, "--json-bare"))
        rc = main(run_service=_hold_the_zone_bounded(bound))
    finally:
        holder.close()
    assert not bound.timed_out, _BOUND_FIRED_MESSAGE
    assert rc == 1

    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert envelope["error"] == "StoreBusyError"
    assert "zonemaster.sqlite.lock" in envelope["message"]
    assert "Traceback" not in captured.err


def test_version_answers_without_any_setting_and_starts_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The quick check of what a deploy landed, so it must work on a host nobody configured."""
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "--version"])

    assert main(run_service=_refuse_to_run) == 0
    assert capsys.readouterr().out == f"soundtouch-zonemaster-service {version}\n"


def test_version_in_machine_mode_is_the_same_envelope_as_everything_else(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["soundtouch-zonemaster-service", "--json-bare", "--version"])

    assert main(run_service=_refuse_to_run) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope == {
        "ok": True,
        "command": "soundtouch-zonemaster-service",
        "data": {"version": version},
        "skipped": [],
    }


@pytest.mark.parametrize(
    "database",
    [
        pytest.param("postgresql+psycopg://zm:s3cret@db.example/zm", id="userinfo"),
        pytest.param("postgresql+psycopg://zm@db.example/zm?password=s3cret", id="query-key-lowercase"),
        pytest.param("postgresql+psycopg://zm@db.example/zm?PassWord=s3cret", id="query-key-mixed-case"),
        pytest.param("postgresql+psycopg://zm:s3cret@db.example:54x2/zm", id="bad-port-with-userinfo"),
    ],
)
def test_a_database_url_carrying_a_password_is_refused_without_repeating_it(database: str) -> None:
    """The URL is echoed by envelopes, ``config`` and logs; the password belongs in the
    ``database.password`` setting or in ``~/.pgpass`` instead, which is what the refusal must point
    a reader to without ever printing the secret itself - in the userinfo, in a ``password`` query
    key whatever its case, and even on a URL SQLAlchemy would refuse outright for its bad port: the
    domain's rule reads the text, so it still finds the userinfo on that one."""
    with pytest.raises(OptionsError) as caught:
        parse_service_options(
            bind_ip="127.0.0.1",
            device_id=None,
            database=database,
            registry_url=None,
            allow_console=(),
            unreachable_timeout_s=None,
            dial_window_s=None,
            mpd_host=None,
            mpd_port=None,
            mpd_rewind_s=None,
            configured={},
        )
    assert caught.value.exit_code == ExitCode.REFUSED
    assert "s3cret" not in str(caught.value)
    assert "database.password" in str(caught.value)
    assert "pgpass" in str(caught.value)
