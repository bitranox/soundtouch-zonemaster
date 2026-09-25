# Module Reference: Architecture & File Index

## Status

Complete (v0.2.0+, the template rebuild)

---

## Related Files

### Domain Layer
- `src/soundtouch_zonemaster/domain/enums.py`  -  Every fixed wire value the master sends or matches, as a StrEnum
- `src/soundtouch_zonemaster/domain/events.py`  -  One record for both ways a speaker speaks: a frame, or a key it forwarded
- `src/soundtouch_zonemaster/domain/speakers.py`  -  Speaker, ProtectedSpeaker, first_protected (the never_touch check)
- `src/soundtouch_zonemaster/domain/station.py`  -  The station request read from a ContentItem
- `src/soundtouch_zonemaster/domain/logfn.py`  -  The LogFn protocol the narration adapter implements
- `src/soundtouch_zonemaster/domain/xmlfmt.py`  -  Escaping for the served XML by POSITION, and the one way to read a value back
- `src/soundtouch_zonemaster/domain/zonexml.py`  -  Every XML document a speaker sees, built in one place
- `src/soundtouch_zonemaster/domain/frames.py`  -  MP3/ADTS frame starts in the ring, numbered (FrameIndex)
- `src/soundtouch_zonemaster/domain/timeline.py`  -  Where the zone is in a stream: a straight line in frames from t0
- `src/soundtouch_zonemaster/domain/presses.py`  -  Which selections a person made, and which are our own station change echoed
- `src/soundtouch_zonemaster/domain/longpress.py`  -  How long each key was held: a thumb held is the rotation, tapped twice is multiroom
- `src/soundtouch_zonemaster/domain/membership.py`  -  Who belongs in the zone, decided from what the speakers said
- `src/soundtouch_zonemaster/domain/channellist.py`  -  The house's channels: the dialable numbers, the ladder, the list rule
- `src/soundtouch_zonemaster/domain/dialling.py`  -  One digit buffer per speaker, and the one wait time above all of them
- `src/soundtouch_zonemaster/domain/calibration.py`  -  The gesture that starts a calibration, and the window it measures
- `src/soundtouch_zonemaster/domain/state.py`  -  ZoneState: the state file's record, what survives a restart
- `src/soundtouch_zonemaster/domain/switch.py`  -  The switch rule: off only when the file says so

### Application Layer
- `src/soundtouch_zonemaster/application/outcome.py`  -  ExitCode (OK/REFUSED/ERROR), OptionsError, device_id_or_refuse
- `src/soundtouch_zonemaster/application/errors.py`  -  PortsBusyError, RegistryError
- `src/soundtouch_zonemaster/application/options.py`  -  Options, ServiceOptions, ChannelPolicy; every default lives on a field
- `src/soundtouch_zonemaster/application/ports.py`  -  Nineteen Protocols for adapter functions, bundled as ZoneServicePorts and PrototypePorts
- `src/soundtouch_zonemaster/application/prototype.py`  -  The prototype's run: options in, the run loop it drives
- `src/soundtouch_zonemaster/application/zone_service/`  -  The service loop as a chain of eight classes, one file each:
  - `constants.py`  -  The constants more than one class in the chain reads
  - `state.py`  -  ServiceState (the pass state)
  - `channels.py`  -  ChannelBook (sources, the ring, the fetch loops)
  - `speakers.py`  -  SpeakerBook (records, observers, the speaker transport book)
  - `volume.py`  -  VolumeGuard (the fade-in guard around a join)
  - `zone.py`  -  ZoneReconcile (take in, let go, put back what the zone left behind)
  - `dialling.py`  -  Dialling (digit buffers, the wait ladder, the calibrated window)
  - `keys.py`  -  KeyReading (presses, long presses, calibrations)
  - `service.py`  -  ZoneService (the pass itself: poll, observe, reconcile, guard the switch)

### Adapters Layer
- `src/soundtouch_zonemaster/adapters/files/`  -  The file boundaries, pydantic models parse every hand-edited document:
  - `atomicfile.py`  -  The one temp-file-plus-fsync-and-rename writer state and channel files share
  - `state_file.py`  -  The state file (read/write ZoneState)
  - `channel_file.py`  -  The channel file, refuses to start empty rather than overwrite the list
  - `switch_file.py`  -  The switch file, watched rather than read once
- `src/soundtouch_zonemaster/adapters/aftertouch/registry.py`  -  Who the speakers are, read from AfterTouch's own device list
- `src/soundtouch_zonemaster/adapters/soundtouch/`  -  The zone protocol as a master speaks it:
  - `zone_master.py`  -  ZoneMaster: the zone itself - lifecycle, station, slaves, transport book
  - `placement.py`  -  Where each slave sits: its PLAY time, first byte, the four books
  - `connections.py`  -  The transport and data connections a slave opens, their frame loops
  - `http_api.py`  -  The speaker HTTP API served on 8090; the /slaveMsg face a test drives
  - `source.py`  -  Station URL resolution, the fetch loop, the bounded ring buffer
  - `observer.py`  -  One WebSocket per speaker: what a box says while NOT in the zone
  - `clock.py`  -  The UDP 40005 sync server (BOSE901); one record per client, evicted on silence
  - `ipc.py`  -  The length-prefixed IPC envelope both TCP channels speak (MAX_FRAME_BYTES)
  - `reports.py`  -  The trackData a slave sends with every state report
  - `speaker_http.py`  -  The httpx verbs one speaker answers (GET/POST on its own 8090)
  - `wire.py`  -  The one place application's names meet the wire's values (encryption_type)
  - `pb/`  -  protoc output for the schemas it speaks (never hand-edit; regenerate from `research/proto`)
- `src/soundtouch_zonemaster/adapters/config/`  -  The six-layer configuration:
  - `loader.py`  -  defaults -> app -> host -> user -> dotenv -> env, command line above all
  - `settings_map.py`  -  The map from config paths to the field each fills (the only enumeration)
  - `overrides.py`  -  `--set SECTION.KEY=VALUE`, one value, one run
  - `display.py`  -  The `config` view: every value, and the file it came from
  - `deploy.py`  -  config-deploy: the files this machine needs, written where the layers read them
  - `errors.py`  -  ConfigInputError: everything the adapter raises, one type
  - `defaultconfig.toml`  -  The header that explains the layers; carries no settings itself
  - `defaultconfig.d/10..80.toml`  -  One file per scope, where the settings live
- `src/soundtouch_zonemaster/adapters/logging/narration.py`  -  LogRouting and log: the log callable every part is handed
- `src/soundtouch_zonemaster/adapters/cli/`  -  The boundary both commands share, and each command's own module:
  - `typed_click.py`  -  The typed rich-click facade
  - `envelope.py`  -  The JSON envelope both commands print: {ok, command, data}, its refusal shape
  - `boundary.py`  -  parse_service_options: argv + config layers -> one validated ServiceOptions
  - `context.py`  -  The run context handed to, not built by, a command body
  - `main.py`  -  The shared main wiring (run_zone=/run_service= injected by entry)
  - `safe_console.py`  -  The subprocess-safe console face (FORCE_COLOR, COLUMNS, exit-code mapping)
  - `prototype.py`  -  The prototype command: click options, [prototype] never_touch refusal
  - `service/root.py`  -  The group entry; invoke_without_command so an argv of options alone holds the zone
  - `service/config_cmd.py`  -  `config`: every value, and the file it came from
  - `service/deploy_cmd.py`  -  `config-deploy`: writes the files and prints which

### Composition Layer
- `src/soundtouch_zonemaster/composition/__init__.py`  -  build_production() wires one adapter per port; hold_the_zone and run_prototype

### Entry Points
- `src/soundtouch_zonemaster/entry.py`  -  prototype_main and service_main; the only modules linking a command to its world
- `src/soundtouch_zonemaster/__main__.py`  -  Module entry: delegates to entry.prototype_main
- `src/soundtouch_zonemaster/__init__conf__.py`  -  Package metadata constants, incl. the two LAYEREDCONF literals
- `src/soundtouch_zonemaster/py.typed`  -  PEP 561 marker

### Configuration Defaults
- `adapters/config/defaultconfig.d/10-zone.toml`  -  Zone behaviour (take-in waits, member book-keeping)
- `adapters/config/defaultconfig.d/20-files.toml`  -  The three file paths (state, channel, switch)
- `adapters/config/defaultconfig.d/30-registry.toml`  -  AfterTouch registry URL
- `adapters/config/defaultconfig.d/40-membership.toml`  -  Membership windows (wakes, stand-down)
- `adapters/config/defaultconfig.d/50-dialling.toml`  -  Dialling (digit timeout; the seventh state-file level is documented here)
- `adapters/config/defaultconfig.d/60-switch.toml`  -  Switch poll interval
- `adapters/config/defaultconfig.d/70-observer.toml`  -  Observer reconnect backoff
- `adapters/config/defaultconfig.d/80-prototype.toml`  -  `[prototype] never_touch`, shipped empty (a house names its own boxes in its host layer)

### Tests
- `tests/test_boundary_golden.py`  -  The golden corpus: one test per replayed case over eight fixture files
- `tests/test_config.py`  -  Pins SETTINGS both ways against dataclasses.fields, the shipped files, the corpus counts
- `tests/test_domain_is_pure.py`  -  The AST guard: domain imports no I/O, no framework
- `tests/test_never_touch.py`  -  The [prototype] never_touch refusal, end to end through the CLI
- `tests/test_service_cli.py`  -  The service command group, envelopes, DEPLOYED_ARGV
- `tests/test_zonemaster_cli.py`  -  The prototype command
- `tests/test_ports.py`  -  Protocol conformance of build_production's wiring
- `tests/test_service_loopback.py`  -  The loopback e2e with a fake slave
- `tests/hang_watchdog.py`  -  Opt-in pytest plugin: dumps tasks, servers and sockets INSIDE a hang
- `tests/registry_double.py`, `tests/speaker_double.py`  -  Real-ish fakes the loopback drives over real sockets

---

## Architecture

### Layer Assignments

| Directory/Module            | Layer       | Responsibility                                         |
|-----------------------------|-------------|--------------------------------------------------------|
| `domain/`                   | Domain      | Pure rules as frozen dataclasses; no I/O, no framework |
| `application/`              | Application | Ports, option records, both run loops                  |
| `application/zone_service/` | Application | The service loop's class chain                         |
| `adapters/files/`           | Adapters    | State, channel, switch file boundaries                 |
| `adapters/aftertouch/`      | Adapters    | Speaker registry                                       |
| `adapters/soundtouch/`      | Adapters    | The zone protocol over the wire, incl. generated pb    |
| `adapters/config/`          | Adapters    | Six-layer configuration                                |
| `adapters/logging/`         | Adapters    | The narration adapter                                  |
| `adapters/cli/`             | Adapters    | rich-click commands, envelopes, safe console           |
| `composition/`              | Composition | build_production(): one adapter per port               |

### Import Enforcement

Layer boundaries enforced via `import-linter` contracts in `pyproject.toml`:
- **Domain is pure**: no adapter dependencies, and no framework (pydantic stays in adapters; the pure records are frozen dataclasses)
- **Clean Architecture layers**: composition -> adapters -> application -> domain
- **House policies independent**: membership, channellist, dialling, presses, calibration import no one another
- **Generated protobuf stays inside `adapters.soundtouch`**: `pb` and `google` reach no other module
- **The two programs' chains are independent**: the service's zone_service and the prototype's application chain, and the two command modules
- **Adapter families independent**: soundtouch, aftertouch, files, config, logging, cli import no one another

Run `lint-imports` to verify; the gate must say `10 kept, 0 broken`.

---

## Equivalence Contract

The converted types (each an old pydantic model, now a frozen dataclass) are held to the old
code's behaviour by the **golden boundary corpus** at `tests/fixtures/golden/`, generated from the
pre-rebuild code before the rebuild and replayed one case per test by
`tests/test_boundary_golden.py`:

| Corpus       | Fixture             | Holds                                                          |
|--------------|---------------------|----------------------------------------------------------------|
| state file   | `state_file.json`   | bytes written for representative ZoneStates; load refusals     |
| channel file | `channel_file.json` | bytes for seeded/edited lists; bad numbers, non-http URLs      |
| seeding      | `seeding.json`      | preset maps with gaps -> list + ordered log lines              |
| dialler      | `dialler.json`      | digit sequences incl. undialable -> outcomes + log lines       |
| registry     | `registry.json`     | aftertouch JSON and malformed variants -> speakers or error    |
| observer     | `observer.json`     | parse_frame over every captured frame, live-run and nowplaying |
| slaveMsg     | `slavemsg.json`     | forwarded key presses / http_api request cases                 |
| options      | `options.json`      | argv + config/env coercion, multi-invalid refusals, bytes      |

`tests/test_config.py` pins the corpus counts so a truncated fixture cannot pass by holding
nothing. The second kind of equivalence is `research/golden_check.py`: it re-runs the three
analysis instruments and requires their recorded output byte for byte.

---

## Exit Codes

| Code | Name    | Usage                                         |
|------|---------|-----------------------------------------------|
| 0    | OK      | Done; the answer to a question is yes         |
| 1    | REFUSED | It ran, and the answer is no                  |
| 2    | ERROR   | It could not run (bad option, bad file, ... ) |

Format-independent: `--json`/`--json-bare` change the bytes, not the code. A click USAGE error
(a missing or bad option) exits 2 as well, and with either flag on argv it is the refusal envelope
on stdout; without one it is click's prose on stderr.

---

## CLI Commands

### soundtouch-zonemaster (the prototype)

One measurement run: start the zone, join the slaves, hold, dissolve on exit.

| Option                  | Description                                    |
|-------------------------|------------------------------------------------|
| `--bind-ip IP`          | Address on the speakers' LAN to bind           |
| `--slave IP`            | A slave address (repeatable)                   |
| `--late-slave IP`       | A slave joining late                           |
| `--preset-from IP`      | The speaker whose presets the run may reuse    |
| `--preset N`            | Preset number to play                          |
| `--station-url URL`     | Play this URL instead of a preset              |
| `--profile NAME`        | Configuration profile                          |
| `--set SECTION.KEY=VAL` | Override one setting for this run (repeatable) |

**Refusal:** naming one of `[prototype] never_touch` as a slave ends the run before a packet is
sent (`first_protected`), message and exit code byte-equal to the pre-setting constant. The list
ships empty; a house names its own boxes in its host layer
(`/etc/soundtouch-zonemaster/hosts/<hostname>.toml`).

### soundtouch-zonemaster-service

`invoke_without_command=True`: an argv naming only options holds the zone until SIGINT.

| Option                                  | Description                                                      |
|-----------------------------------------|------------------------------------------------------------------|
| `--bind-ip IP`                          | Address on the speakers' LAN                                     |
| `--channel-file PATH`                   | The house's channel list                                         |
| `--switch-file PATH`                    | The watched switch                                               |
| `--state-file PATH`                     | What survives a restart                                          |
| `--station-url ...` / `--seed-from ...` | REMOVED; seeding comes from presets of the first box switched on |
| `--profile NAME`                        | Configuration profile                                            |
| `--set SECTION.KEY=VAL`                 | Override one setting for this run (repeatable)                   |

**Every setting may live in a configuration file**, read through `lib_layered_config` in the
precedence `defaults -> app -> host -> user -> dotenv -> env`, with the command line above all
six. A setting given in NO layer is refused by name with exit 2.

**config**: every value, and the file it came from (`--section`, `--json`, `--redact`).
Note: a calibrated `dial_window_s` is written to the state file and beats every config layer;
`config` does not report that, the run's `--json` envelope does.

**config-deploy**: `--target [app|host|user]`, `--force`; writes `defaultconfig.toml` plus the
`defaultconfig.d/` files where the layers read them, and prints which.

---

## Testing Infrastructure

### Which lane runs where

| Lane                       | Runs via               | Note                                              |
|----------------------------|------------------------|---------------------------------------------------|
| unit + local_only          | `make test`            | every declared Python version via `make test-all` |
| integration (hardware e2e) | `make testintegration` | private e2e settings; GET-only probes are silent  |
| everything CI runs         | `-m "not local_only"`  | integration gates the release                     |

Never run two suites at once: the tests bind fixed ports 40002/40003/40005/8090.

### Real-ish fakes at real seams

| Module                     | Substitutes                          | Talks over                      |
|----------------------------|--------------------------------------|---------------------------------|
| `tests/registry_double.py` | AfterTouch's device list             | loopback HTTP                   |
| `tests/speaker_double.py`  | a real slave (frames, keys, reports) | sockets: 40002/40003/40005/8090 |

---

**Last Updated:** 2026-09-12 (the template rebuild)
