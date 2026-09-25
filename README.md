# soundtouch-zonemaster

<!-- Badges -->
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-blue.svg)](https://www.python.org/)
[![Code Style: Ruff](https://img.shields.io/badge/Code%20Style-Ruff-46A3FF?logo=ruff&labelColor=000)](https://docs.astral.sh/ruff/)
[![Types: pyright strict](https://img.shields.io/badge/types-pyright%20strict-2A6DB2.svg)](https://microsoft.github.io/pyright/)
[![security: bandit](https://img.shields.io/badge/security-bandit-yellow.svg)](https://github.com/PyCQA/bandit)
[![Open in Codespaces](https://img.shields.io/badge/Codespaces-Open-blue?logo=github&logoColor=white&style=flat-square)](https://codespaces.new/bitranox/soundtouch-zonemaster?quickstart=1)

**A software zone master for Bose SoundTouch speakers, and the measured protocol description behind it**

Bose shut down the SoundTouch cloud, and with it the app that grouped speakers into a zone. The
speakers themselves still work: a master feeds synchronised audio to its slaves over a protocol Bose
never documented. This repository is that protocol, written down from captures of real speakers,
plus a Python program that plays the master's part.

## Table of Contents

- [Why soundtouch-zonemaster?](#why-soundtouch-zonemaster)
- [Scope and boundaries](#scope-and-boundaries)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
  - [Running the master](#running-the-master)
  - [Running the tests](#running-the-tests)
  - [Key mappings](#key-mappings)
- [The recovered schemas](#the-recovered-schemas)
- [Repository layout](#repository-layout)
- [Development](#development)
- [Further Documentation](#further-documentation)
- [Acknowledgements](#acknowledgements)
- [License](#license)

## Why soundtouch-zonemaster?

A SoundTouch speaker will still play a stream you point it at, but grouping several of them was the
cloud app's job, and the zone protocol underneath it was never published. Without it, hardware that
works perfectly well plays one room at a time.

A Python process on an ordinary Linux box takes the place of the master speaker. It serves the
SoundTouch HTTP API on port 8090, answers the clock protocol on UDP 40005, drives the transport
channel on TCP 40002 and the data channel on TCP 40003, and streams an internet radio station to one
or more real speakers. A speaker joining a stream that is already playing is placed on the same
timeline as the others, so the room does not echo.

Every one of those claims was measured on real speakers, and the measurements are written up in a
protocol research report, `research/REPORT.md`. The report and the raw measurements are kept on the
development machine while development continues and are published in anonymized form when it is
finished; docstrings cite the report by section (`REPORT.md S3`) in the meantime. Nothing in it
comes from reading names in a binary: where a capture and a plausible reading of the firmware
disagreed, the capture won.

## Scope and boundaries

This is a working prototype, not a product.

| Proven                                                  | Untested                                          |
|---------------------------------------------------------|---------------------------------------------------|
| Two speakers playing one stream in sync                 | Three or more speakers at once                    |
| A late joiner placed on the zone timeline, no echo      | Bose's Lifestyle console as a slave               |
| MP3 (constant rate) and AAC in ADTS (variable) stations | A station whose server sends no initial burst     |
| Clock, transport, data and HTTP against real firmware   | What the speakers do when the master process dies |

Two programs are built on it. The prototype master is started from the command line and runs for a
given duration. The service holds a zone until it is stopped, and reads its settings from layered
configuration files.

## Installation

The master needs Python 3.12 or newer. It is not on PyPI; run it from a clone.

### Recommended: uv

```bash
# Install uv
pip install --upgrade uv

git clone https://github.com/bitranox/soundtouch-zonemaster.git
cd soundtouch-zonemaster

uv venv
uv pip install -e ".[dev]"
```

### Alternative: pip

```bash
python -m pip install --upgrade pip
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Nothing has to be installed on the speakers, and no firmware is modified. The master needs to be on
the same LAN as them.

## Configuration

The service reads its settings through
[`lib_layered_config`](https://github.com/bitranox/lib_layered_config). Six layers are merged in a
fixed order, and a command-line option overrides all of them:

```
defaults -> app -> host -> user -> dotenv -> env -> command line
```

The settings are split by scope, one section per file, so a change to one concern is a change to
one file:

```
config.toml                  the header: which layers exist and in what order
config.d/10-zone.toml        [zone]        what this master is on the network
config.d/20-files.toml       [files]       the channel list, the switch, the state
config.d/30-registry.toml    [registry]    where the speaker list comes from
config.d/40-membership.toml  [membership]  who may join, and when a box stops counting
config.d/50-dialling.toml    [dialling]    how a channel number is typed on the preset keys
config.d/60-switch.toml      [switch]      how quickly the on/off file is noticed
config.d/70-observer.toml    [observer]    each speaker's notification channel
config.d/80-prototype.toml   [prototype]   the prototype master, and the boxes it must never touch
config.d/90-mpd.toml         [mpd]         the Music Player Daemon beside the service
```

Write yourself a copy of that whole tree, then read back what the merged answer is and which file
produced each value:

```bash
soundtouch-zonemaster-service config-deploy --target user   # ~/.config/soundtouch-zonemaster/: the header and every scope file
soundtouch-zonemaster-service config                        # every value, with its source
soundtouch-zonemaster-service config --section registry     # one scope
soundtouch-zonemaster-service --json config --redact        # the same, parseable, secrets masked
```

`--json` and `--json-bare` belong to the group and go BEFORE the subcommand; `--section` and
`--redact` are `config`'s own and go after it. A scope that no layer has set - `zone` and `files`
on a machine nobody has configured yet - is reported as empty rather than refused; only a name
this program does not read at all is an error.

Each file documents its own settings: what the default is, what the setting does, when to change
it, and what changing it costs. It also names the environment variable that overrides each one.
The prefix is `SOUNDTOUCH_ZONEMASTER___`, and the segments of a key are joined by double underscores, so
`registry.poll_s` is set by `SOUNDTOUCH_ZONEMASTER___REGISTRY__POLL_S`.

You are not obliged to keep that layout. The loader merges every file key by key, so a deployed
`config.d/` file may be deleted, or a section moved into `config.toml`, without changing what the
service reads.

### Private settings in a checkout

The tracked files hold public defaults only. Values that belong to one house - its addresses, its
file paths, the boxes the prototype must never touch - go into an override file in the SAME
directory, named `9N-<scope>-rnhome.toml` (for example `91-zone-rnhome.toml` beside
`10-zone.toml`). Every `*.toml` in a `.d` directory is merged in sorted order, so the higher number
wins over the default; the `-rnhome` suffix is gitignored, so the file never leaves the machine.
Each one has a tracked `*-rnhome.toml.example` beside it that shows its shape. The research
scripts read `research/defaultconfig.toml` and `research/defaultconfig.d/` the same way.

The same convention holds the private name list, `tools/public_redactions-rnhome.txt`: every name
of the house, its machines and its device ids. A test in the gate fails if any tracked file holds
one of them, and skips where the list is absent (see `tools/public_redactions.example.txt`).

Four settings have no default at all, because they describe one deployment: the address to bind,
and the three files that hold the channel list, the switch and the state. Give them in a config
file, in the environment, or on the command line. A setting missing from every layer is refused at
startup and names itself. A fifth, the device id, falls back to the host's own MAC.

## Usage

### Running the master

You need a speaker on the same LAN and a station URL it can reach. `--bind-ip` is the address of the
machine running the master, `--slave` a speaker that should play (repeatable), and `--preset-from` a
speaker whose stored presets supply the station.

```bash
uv run python -m soundtouch_zonemaster \
    --bind-ip 192.168.1.10 \
    --slave 192.168.1.21 \
    --preset-from 192.168.1.21 \
    --preset 1
```

Add `--late-slave` with `--join-after` to have a second speaker join a stream that is already
running, which is the case the timeline work exists for:

```bash
uv run python -m soundtouch_zonemaster \
    --bind-ip 192.168.1.10 \
    --slave 192.168.1.21 \
    --late-slave 192.168.1.22 --join-after 30 \
    --preset-from 192.168.1.21 --preset 1 --duration 120
```

The `sync` lines it logs are the instrument: they report how far each speaker is from the zone, in
milliseconds, and they agreed with the ear on every run the ear judged.

Anything that makes a speaker play is audible in the room it stands in. Check the speakers are in
standby before a run, and note that the master refuses to target a Lifestyle console, whose input
switches when it is sent a power command.

### Running the tests

The tests need no hardware. Framing is proven against captured bytes, and a loopback end-to-end run
drives a fake slave through all four channels.

```bash
uv run python -m pytest -q tests/
```

### Key mappings

While the service holds the house, the remote's keys are its controls: the preset keys dial
channel numbers, next and previous step through channels (or through the files of an MPD channel),
and the thumbs manage the rotation and, held, take a room in or out of multiroom. A key's meaning
depends on the state of the box and the house, so the full mapping, state by state, is in
[`keymappings.md`](keymappings.md).

## The recovered schemas

`research/proto/` holds 330 `.proto` files recovered from a speaker's own firmware. protoc embeds
every compiled schema in the binary it generates as a serialized `FileDescriptorProto`, so these are
Bose's definitions, byte for byte, not a reconstruction. `research/extract_protos.py` is the recovery
tool, and it works on any binary that links libprotobuf. The handful the master needs are compiled
into `src/soundtouch_zonemaster/adapters/soundtouch/pb/`.

## Repository layout

```
research/               the spike that produced the protocol description
  REPORT.md             the protocol report (published in anonymized form when development finishes)
  extract_protos.py     recovers the .proto schemas protoc embedded in the firmware binaries
  generate_pb.py        regenerates adapters/soundtouch/pb (runtime modules and .pyi stubs) from proto/
  capture_zone.py       drives two real speakers through a zone while recording everything
  analyze_capture.py    decodes a capture frame by frame with the recovered schemas
  analyze_late_join.py  places a joining speaker's first byte, clock and buffer on the timeline
  capture_model.py      the vocabulary a capture is read in: endpoints, frames, payload types
  golden_check.py       re-runs the instruments and requires their recorded output byte for byte
  proto/                the 330 recovered schemas
src/soundtouch_zonemaster/    the master, in the layered package: clock (UDP 40005), transport (40002),
                              data (40003), HTTP (8090); domain, application, adapters, composition
  domain/               the pure rules as frozen dataclasses: wire enums, frames, timeline, membership,
                        dialling, channellist, presses, state - no I/O, no framework
  application/          ports and the option records; the service loop as a chain of eight classes,
                        one file each; the prototype's run loop
  adapters/             everything that speaks or serves: the zone protocol over the wire (ZoneMaster,
                        placement, channels, source, clock, ipc, pb), the file boundaries, the speaker
                        registry, the six-layer config, the rich-click CLI
  composition/          the one module naming both sides: build_production() wires one adapter per port
tests/                  framing against captured bytes; a loopback run with a fake slave; the golden
                        corpus that pins every converted boundary to the old code's bytes
tools/                  install_service.py (installs the service on the machine that runs it),
                        export_public.py (the anonymized export) and the private name list's example
```

## Development

The gate is `make test` ([bmk](https://github.com/bitranox/bmk)): ruff, pyright strict,
import-linter, bandit, pip-audit, and pytest with branch coverage. It must be green before a push,
and it rewrites the Makefile and raises dependency floors as it runs.

```bash
git clone https://github.com/bitranox/soundtouch-zonemaster.git
cd soundtouch-zonemaster

make test                      # the whole gate
make test-human                # the same, with the tool output shown
```

Two things worth knowing before changing protocol code:

- Protocol claims come from a capture or a live run, never from reading names in a binary. Where the
  two disagree, the capture wins and the protocol report is corrected.
- A response frame carries the sequence number of the request it answers; events and requests count
  on their own, per connection. Sharing one counter stalls a slave silently.

Never hand-edit `src/soundtouch_zonemaster/adapters/soundtouch/pb/`. Regenerate it:

```bash
uv run --with grpcio-tools python research/generate_pb.py
```

## Further Documentation

- Protocol report - the measured description of the zone protocol and how each claim was
  measured; published in anonymized form when development is finished
- [Key mappings](keymappings.md) - what every remote key does, in every state of the box and the
  house
- [Changelog](CHANGELOG.md) - what changed, and why
- [License](LICENSE) - MIT

## Acknowledgements

The speakers, and the people who kept using them after the service they were sold with was switched
off.

## License

This software is licensed under the [MIT License](LICENSE). The recovered schemas are Bose's,
included here as the interface description a second implementation needs.
