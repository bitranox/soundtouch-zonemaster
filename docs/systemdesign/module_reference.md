# Module Reference: Architecture & File Index

## Status

Complete (v0.2.0+, the template rebuild)

---

## Related Files

### Domain Layer
- `src/soundtouch_zonemaster/domain/enums.py`  -  Every fixed wire value the master sends or matches, as a StrEnum
- `src/soundtouch_zonemaster/domain/events.py`  -  One record for both ways a speaker speaks: a frame, or a key it forwarded
- `src/soundtouch_zonemaster/domain/speakers.py`  -  Speaker, ProtectedSpeaker, first_protected (the never_touch check)
- `src/soundtouch_zonemaster/domain/station.py`  -  A station to play, and the one way to read one out of a ContentItem (and what a relative Orion location is)
- `src/soundtouch_zonemaster/domain/logfn.py`  -  The LogFn protocol the narration adapter implements
- `src/soundtouch_zonemaster/domain/xmlfmt.py`  -  Escaping for the served XML by POSITION (writing only; reading is `adapters/soundtouch/xmlread.py`)
- `src/soundtouch_zonemaster/domain/zonexml.py`  -  Every XML document a speaker sees, built in one place
- `src/soundtouch_zonemaster/domain/frames.py`  -  MP3/ADTS frame starts in the ring, numbered (FrameIndex)
- `src/soundtouch_zonemaster/domain/timeline.py`  -  Where the zone is in a stream: a straight line in frames from t0
- `src/soundtouch_zonemaster/domain/presses.py`  -  Which selections a person made, and which are our own station change echoed
- `src/soundtouch_zonemaster/domain/longpress.py`  -  How long each key was held: a thumb held is the rotation, tapped twice is multiroom
- `src/soundtouch_zonemaster/domain/housevolume.py`  -  A thumb tapped once then volume: which report steps the house, and what a box is owed
- `src/soundtouch_zonemaster/domain/membership.py`  -  Who belongs in the zone, decided from what the speakers said
- `src/soundtouch_zonemaster/domain/channellist.py`  -  The house's channels: the dialable numbers, the ladder, the list rule
- `src/soundtouch_zonemaster/domain/dialling.py`  -  One digit buffer per speaker, and the one wait time above all of them
- `src/soundtouch_zonemaster/domain/calibration.py`  -  The gesture that starts a calibration, and the window it measures
- `src/soundtouch_zonemaster/domain/state.py`  -  ZoneState: the house's state, what survives a restart
- `src/soundtouch_zonemaster/domain/switch.py`  -  The switch rule: off only when the switch row says so
- `src/soundtouch_zonemaster/domain/mpd.py`  -  MpdStatus: what MPD said about itself, in words both sides of the port may name
- `src/soundtouch_zonemaster/domain/playorder.py`  -  The order a directory channel plays in, and where a held next/previous lands
- `src/soundtouch_zonemaster/domain/preferences.py`  -  The five house preferences a person may change while the service runs: one rule for the value and the stored row alike
- `src/soundtouch_zonemaster/domain/database_url.py`  -  Whether a database setting carries a password, and how to show one without it
- `src/soundtouch_zonemaster/domain/secret.py`  -  Secret: a password the records carry, shown as `***` everywhere and read only through `reveal()`

### Application Layer
- `src/soundtouch_zonemaster/application/outcome.py`  -  ExitCode (OK/REFUSED/ERROR), OptionsError, device_id_or_refuse (checks and folds to upper case), tcp_port_or_refuse, preference_or_refuse
- `src/soundtouch_zonemaster/application/errors.py`  -  PortsBusyError, RegistryError, MpdError (MpdRefusalError, NotInMpdError), StoreError (StoreBusyError, StoreMissingError)
- `src/soundtouch_zonemaster/application/options.py`  -  Options, ServiceOptions, ChannelPolicy, LegacyFiles; every default lives on a field, and both records hold a device id in upper case however they were built
- `src/soundtouch_zonemaster/application/ports.py`  -  The Protocols the service and the prototype reach the world through: HouseStore (the state, the channel list, the switch and the house preferences in one database - a SQLite file, or PostgreSQL) and OpenHouseStore (its opener); ServiceStore, the same store as the SERVICE calls it (every call awaited, writes queued in call order when called) and StoreOffTheLoop, which turns one into the other; LocationResolver and OpenLocationResolver (the relative Orion location, completed for the master's fetch and for a speaker apart); the speaker, MPD and master ports. Bundled as ZoneServicePorts (whose `off_the_loop` and `open_locations` fields carry the two newer openers), PrototypePorts and ServiceCommands
- `src/soundtouch_zonemaster/application/prototype.py`  -  The prototype's run: options in, the run loop it drives
- `src/soundtouch_zonemaster/application/zone_service/`  -  The service loop as a chain of nine classes, one file each:
  - `constants.py`  -  The constants more than one class in the chain reads
  - `state.py`  -  ServiceState (the pass state)
  - `channels.py`  -  ChannelBook (sources, the ring, the fetch loops)
  - `speakers.py`  -  SpeakerBook (records, observers, the speaker transport book)
  - `volume.py`  -  VolumeGuard (the fade-in guard around a join)
  - `zone.py`  -  ZoneReconcile (take in, let go, put back what the zone left behind)
  - `preferences.py`  -  PreferenceBook (the stored preferences laid over the options, and what the log says of them)
  - `dialling.py`  -  Dialling (digit buffers, the wait ladder, the calibrated window)
  - `keys.py`  -  KeyReading (presses, long presses, calibrations)
  - `service.py`  -  ZoneService (the pass itself: poll, observe, reconcile, guard the switch)

### Adapters Layer
- `src/soundtouch_zonemaster/adapters/files/`  -  The file and database boundaries the service reaches its state through:
  - `atomicfile.py`  -  The one temp-file-plus-fsync-and-rename writer state and channel files share
  - `state_file.py`  -  The state file, from before the database (read/write ZoneState)
  - `channel_file.py`  -  The channel file, from before the database, refuses to start empty rather than overwrite the list
  - `switch_file.py`  -  The switch file, from before the database, watched rather than read once
  - `house_db.py`  -  Where the database is (a path or a URL), its engine, the writer lock per backend (`flock` on SQLite, a session advisory lock on PostgreSQL), the schema brought to Alembic's head while that lock is held
  - `house_schema.py`  -  The tables, as one SQLAlchemy `MetaData` the migrations are held to
  - `migrations/`  -  Alembic: `env.py` and `versions/` (written by hand; `tests/test_house_db.py` holds them to `house_schema.py`; `0002_house_preferences.py` moves the calibrated window and hold out of the `zone` row and into the `preference` table)
  - `house_state.py`  -  The state, as rows: one table per collection field of ZoneState, replaced whole on every write
  - `house_switch.py`  -  The switch, as one row; off only when the row says so; every write locks it before reading it (`hold_the_switch`); DbSwitch is the service's watch
  - `house_channels.py`  -  The channel list, as rows ordered by `position` (never `rowid`), checked by the same rules the channel file is
  - `house_preferences.py`  -  The preferences, as rows: one per preference somebody set, an UPSERT never a delete-then-insert
  - `legacy_import.py`  -  The one-time import of the three old files into an empty part of the database
  - `house_store.py`  -  SqlHouseStore: the state, the channel list, the switch and the house preferences, in one database; `set_preferences` writes several preferences in one transaction (a calibration is stored whole or not at all)
  - `store_worker.py`  -  StoreWorker, the ServiceStore the service calls: the house store off the event loop on one daemon thread of its own, every call run in the order asked; writes queued when called and kept when their awaiter is cancelled; a close that waits at most `STOP_BOUND_S` (10 s) and says what it left behind
- `src/soundtouch_zonemaster/adapters/http_client.py`  -  Every httpx client, built with no timeout of its own: each call's deadline is asyncio's, because an anyio deadline can swallow a stop
- `src/soundtouch_zonemaster/adapters/aftertouch/registry.py`  -  Who the speakers are, read from AfterTouch's own device list
- `src/soundtouch_zonemaster/adapters/mpd/client.py`  -  MPD's line protocol on 6600: connect, quote, command, refusal (the control side only)
- `src/soundtouch_zonemaster/adapters/soundtouch/`  -  The zone protocol as a master speaks it:
  - `zone_master.py`  -  ZoneMaster: the zone itself - lifecycle, station, slaves, transport book
  - `placement.py`  -  Where each slave sits: its PLAY time, first byte, the four books
  - `connections.py`  -  The transport and data connections a slave opens, their frame loops
  - `http_api.py`  -  The speaker HTTP API served on 8090; the /slaveMsg face a test drives
  - `source.py`  -  Station URL resolution, the fetch loop, the bounded ring buffer
  - `orion.py`  -  OrionBase, the LocationResolver: completes a relative Orion location (`/station?data=...`) from the service's BMX registry, one resolver per service run, read in the background as the service starts and shared by the master's fetch (which waits for the registry, else takes the fallback path) and every document a speaker is sent (absolute only against a base the registry already named, never waiting, never the fallback; before that the relative form goes out, which speakers are proven to resolve in a STORED preset and not yet measured in a `/select`)
  - `observer.py`  -  One WebSocket per speaker: what a box says while NOT in the zone
  - `clock.py`  -  The UDP 40005 sync server (BOSE901); one record per client, evicted on silence
  - `ipc.py`  -  The length-prefixed IPC envelope both TCP channels speak (MAX_FRAME_BYTES)
  - `reports.py`  -  The trackData a slave sends with every state report
  - `speaker_http.py`  -  The httpx verbs one speaker answers (GET/POST on its own 8090)
  - `wire.py`  -  The one place application's names meet the wire's values (encryption_type)
  - `xmlread.py`  -  The ONE way a speaker document is parsed: at most 64 KiB, no DOCTYPE, depth 32, `None` for anything else
  - `xmlmodels.py`  -  The documents it reads, as pydantic-xml models (ContentItem, keyData, ...)
  - `pb/`  -  protoc output for the schemas it speaks (never hand-edit; regenerate from `research/proto`)
- `src/soundtouch_zonemaster/adapters/config/`  -  The six-layer configuration:
  - `loader.py`  -  defaults -> app -> host -> user -> dotenv -> env, command line above all
  - `settings_map.py`  -  The map from config paths to the field each fills (the only enumeration)
  - `overrides.py`  -  `--set SECTION.KEY=VALUE`, one value, one run
  - `display.py`  -  The `config` view: every value, and the file it came from
  - `deploy.py`  -  config-deploy: the files this machine needs, written where the layers read them
  - `errors.py`  -  ConfigInputError: everything the adapter raises, one type
  - `defaultconfig.toml`  -  The header that explains the layers; carries no settings itself
  - `defaultconfig.d/10..90.toml`  -  One file per scope, where the settings live
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
  - `service/store_cmd.py`  -  `switch` and `channels export|import`: the house database from the command line
  - `service/prefs_cmd.py`  -  `prefs`, `prefs set` and `prefs unset`: the house preferences from the command line

### Composition Layer
- `src/soundtouch_zonemaster/composition/__init__.py`  -  build_production() wires one adapter per port; hold_the_zone and run_prototype

### Entry Points
- `src/soundtouch_zonemaster/entry.py`  -  prototype_main and service_main; the only modules linking a command to its world
- `src/soundtouch_zonemaster/__main__.py`  -  Module entry: delegates to entry.prototype_main
- `src/soundtouch_zonemaster/__init__conf__.py`  -  Package metadata constants, incl. the two LAYEREDCONF literals
- `src/soundtouch_zonemaster/py.typed`  -  PEP 561 marker

### Configuration Defaults
- `adapters/config/defaultconfig.d/10-zone.toml`  -  Zone behaviour (take-in waits, member book-keeping)
- `adapters/config/defaultconfig.d/20-files.toml`  -  The three file paths (state, channel, switch) the house database imports once
- `adapters/config/defaultconfig.d/25-database.toml`  -  The house database (`database.url`), SQLite or PostgreSQL, and its password (`database.password`)
- `adapters/config/defaultconfig.d/30-registry.toml`  -  AfterTouch registry URL
- `adapters/config/defaultconfig.d/40-membership.toml`  -  Membership windows (wakes, stand-down)
- `adapters/config/defaultconfig.d/50-dialling.toml`  -  Dialling (digit timeout; two of the five house preferences are documented here)
- `adapters/config/defaultconfig.d/55-volume.toml`  -  The fade-in a joining box climbs back up in (a house preference)
- `adapters/config/defaultconfig.d/60-switch.toml`  -  Switch poll interval
- `adapters/config/defaultconfig.d/70-observer.toml`  -  Observer reconnect backoff
- `adapters/config/defaultconfig.d/80-prototype.toml`  -  `[prototype] never_touch`, shipped empty (a house names its own boxes in its host layer)
- `adapters/config/defaultconfig.d/90-mpd.toml`  -  Where MPD answers, and how far back an MPD channel starts (the rewind, a house preference)

### Tools (shipped to the service's machine and run there, beside `_click.py`)
- `tools/deploy_service.py`  -  One deploy: backup, switch off and wait (bounded) for the zone to empty, stop, install through `install_service.py`, the one-distribution venv check, start and watch the unit stay up, then put the switch back - only while the switch row still holds the deploy's own switch-off (`set-switch on --if-changed-at`), so a `switch off` somebody ran in between stands, and one run during the backup (which leaves the deploy's switch-off nothing to change) leaves nothing to put back; the envelope's `switch_restored`/`switch_note` say which. A failure before the stop puts the switch back the same way
- `tools/install_service.py`  -  The venv, the wheel, and a new house database seeded with the switch OFF
- `tools/service_venv.py`  -  Run by the service venv's python: `seed-switch`, `show`, `set-switch on|off [--if-changed-at STAMP]` (answers the row's `changed_at`; the conditional form is one UPDATE; on PostgreSQL the switch is locked before it is read, so `changed` is never a person's write taken for the deploy's), `backup`, `distributions`. Runs against the package being REPLACED too (v0.5.2 on the house's machine), standing in for what that package lacks

### Tests
- `tests/test_boundary_golden.py`  -  The golden corpus: one test per replayed case over eight fixture files
- `tests/test_config.py`  -  Pins SETTINGS both ways against dataclasses.fields, the shipped files, the corpus counts
- `tests/test_domain_is_pure.py`  -  The AST guard: domain imports no I/O, no framework
- `tests/test_never_touch.py`  -  The [prototype] never_touch refusal, end to end through the CLI
- `tests/test_service_cli.py`  -  The service command group, envelopes, DEPLOYED_ARGV
- `tests/test_zonemaster_cli.py`  -  The prototype command
- `tests/test_ports.py`  -  Protocol conformance of build_production's wiring
- `tests/test_service_loopback.py`  -  The loopback e2e with a fake slave
- `tests/test_store_worker.py`  -  The store off the loop: one thread, the order asked, bounded close
- `tests/test_deploy_service.py`  -  A deploy against a fake systemd and a fake house that keep state; the real helper once
- `tests/test_service_venv.py`, `tests/test_service_venv_old_package.py`  -  The helper against a real SQLite house, under this package and under v0.5.2's
- `tests/hang_watchdog.py`  -  pytest plugin that dumps tasks, servers and sockets INSIDE a hang and kills the run; armed by itself when `CI=true`, opt-in locally with `-p hang_watchdog`
- `tests/registry_double.py`, `tests/speaker_double.py`  -  Real-ish fakes the loopback drives over real sockets

---

## Architecture

### Layer Assignments

| Directory/Module            | Layer       | Responsibility                                                            |
|-----------------------------|-------------|---------------------------------------------------------------------------|
| `domain/`                   | Domain      | Pure rules as frozen dataclasses; no I/O, no framework                    |
| `application/`              | Application | Ports, option records, both run loops                                     |
| `application/zone_service/` | Application | The service loop's class chain                                            |
| `adapters/files/`           | Adapters    | The house database, plus the three legacy file boundaries it imports once |
| `adapters/aftertouch/`      | Adapters    | Speaker registry                                                          |
| `adapters/soundtouch/`      | Adapters    | The zone protocol over the wire, incl. generated pb                       |
| `adapters/config/`          | Adapters    | Six-layer configuration                                                   |
| `adapters/logging/`         | Adapters    | The narration adapter                                                     |
| `adapters/cli/`             | Adapters    | rich-click commands, envelopes, safe console                              |
| `composition/`              | Composition | build_production(): one adapter per port                                  |

### Import Enforcement

Layer boundaries enforced via `import-linter` contracts in `pyproject.toml`:
- **Domain is pure**: no adapter dependencies, and no framework (pydantic stays in adapters; the pure records are frozen dataclasses)
- **Clean Architecture layers**: composition -> adapters -> application -> domain
- **House policies independent**: membership, channellist, dialling, presses, calibration import no one another
- **Generated protobuf stays inside `adapters.soundtouch`**: `pb` and `google` reach no other module
- **The database library stays inside the house store**: `sqlalchemy` and `alembic` reach no other module directly (`allow_indirect_imports`, because composition and the CLI reach the store)
- **The two programs' chains are independent**: the service's zone_service and the prototype's application chain, and the two command modules
- **Adapter families independent**: soundtouch, aftertouch, files, config, logging, cli import no one another

Run `lint-imports` to verify; the gate must say `11 kept, 0 broken`.

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

| Option                                  | Description                                                                                                                                                                              |
|-----------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `--bind-ip IP`                          | Address on the speakers' LAN                                                                                                                                                             |
| `--database PATH or URL`                | The house database: state, channel list and switch, one database (a plain path is SQLite; `postgresql+psycopg://user@host/db` is PostgreSQL; password in `database.password`, not a URL) |
| `--channel-file PATH`                   | The channel list as a file, from before the database: imported once into an empty database, then renamed `<name>.imported`                                                               |
| `--switch-file PATH`                    | The switch as a file, from before the database: imported once, then not read                                                                                                             |
| `--state-file PATH`                     | The state as a file, from before the database: imported once, then set aside                                                                                                             |
| `--station-url ...` / `--seed-from ...` | REMOVED; seeding comes from presets of the first box switched on                                                                                                                         |
| `--profile NAME`                        | Configuration profile                                                                                                                                                                    |
| `--set SECTION.KEY=VAL`                 | Override one setting for this run (repeatable)                                                                                                                                           |

**Every setting may live in a configuration file**, read through `lib_layered_config` in the
precedence `defaults -> app -> host -> user -> dotenv -> env`, with the command line above all
six. A setting given in NO layer is refused by name with exit 2. The PostgreSQL password is the
setting `database.password` (`SOUNDTOUCH_ZONEMASTER___DATABASE__PASSWORD`), which has no command-line
option; `--set database.password=...` is the per-run override. The store hands it to the driver as
a connect argument; when it is empty, libpq's own `~/.pgpass`, `PGPASSFILE` or `PGPASSWORD` apply.
A password given for a SQLite database is refused. The configured password goes only with the
configured database: a typed `--database` other than exactly `database.url` is opened without it,
with one line saying so (`boundary.scoped_to_the_configured_database`, shared by the run and the
store verbs). A database that cannot be opened
refuses the start; an old file that exists but cannot be parsed refuses the start too, naming the
file, at the one-time import.

**config**: every value, and the file it came from (`--section`, `--json`, `--redact`).
`database.password` is always shown masked, with or without `--redact`, and so is every other key
under `[database]` but `url` (a stray key there is most likely the password misspelled). When the
view includes a preference (any of `dialling`, `mpd`, `volume`, `membership`) and a database is
named, `config` opens it (without the writer lock, migrating the schema to head if it is behind)
and shows a stored preference over the file value it overrides, printed beneath it; a stored row
nothing can use is shown as ignored, with its raw text and why. A database that does not exist
reads as "no preference is stored", nothing is created; one that cannot be opened or read costs
one line, unchanged exit code, and under `--redact` that line does not name the database.
Note: the running service's own `--json` envelope does not carry the preferences either; `config`
and `prefs` are where they are reported.

**config-deploy**: `--target [app|host|user]`, `--force`; writes `defaultconfig.toml` plus the
`defaultconfig.d/` files where the layers read them, and prints which.

**switch** `[on|off]`: read or set the switch. Opens the database without the writer lock, so it
works while the service runs.

**channels export** `--output FILE`: write the channel list as the JSON document a person reads
and repairs. Opens without the writer lock.

**channels import** `FILE`: replace the channel list with a file's. Opens WITH the writer lock, so
it is refused (exit 1) while the service holds it.

**prefs**: every one of the five house preferences (`dialling.window_s`,
`dialling.hold_threshold_s`, `mpd.rewind_s`, `volume.fade_s`, `membership.consoles_allowed`), its
value, and whether a config layer or a stored row is deciding it. Opens the database without the
writer lock, like `switch`. "configuration" means the config layers plus any `--set`: `prefs` cannot
see the flags a service was started with (`--allow-console`, `--dial-window-s`, `--mpd-rewind-s`),
so for a service started with one of those, the value it runs on is that flag's unless a stored row
decides it.

**prefs set** `NAME VALUE`: parse `VALUE` as JSON, check it by the same rule a config file's value
is, and write it to the house database as a row that beats every config layer; a running service
reads it within about a second. A console added to `membership.consoles_allowed` makes the service
read the speaker registry at once and ask the console what it is playing: one already playing the
house's stream is taken in straight away, one asleep the next time it wakes, and one playing
something of its own stays out, as any box on its own station does, until it goes to standby and
is switched on again, or somebody dials a channel on it. A refused value (out of bounds, not JSON, the wrong shape)
writes nothing and opens nothing; exit 1 for a refused value, exit 2 for an unknown name or
unparseable JSON.

**prefs unset** `NAME`: remove the stored row, so the config layers decide the preference again.

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

**Last Updated:** 2026-09-29 (the store worker, the Orion resolver, the deploy tools)
