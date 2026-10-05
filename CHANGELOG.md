# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `examples/systemd/soundtouch-zonemaster.service`, a sample unit for the service, and
  `examples/systemd/mpd.service.d/no-io-uring.conf`, an MPD drop-in. MPD 0.24's pending
  io_uring wait is counted as I/O wait, so a machine running it shows close to 100% I/O
  pressure while the disk is idle; the drop-in makes MPD use ordinary reads. The README's new
  section "Running the service under systemd" explains both.

### Changed

- Requires lib_layered_config 7.0.1 or later. From 7.0 the environment layer keeps `null` and
  `none` (in any case) as text for a sensitive key, so `database.password` spelled that way in
  the environment or a `.env` is now that password rather than a refused start.
- `--set SECTION.KEY=VALUE` reads its value by the same rule as the environment and a `.env`:
  a JSON array or object, `true`/`false` in any case, `null`/`none` as no value (text on a
  secret's key), a number only where it reads back as the same text (`1.50` and `0640` stay
  text), and a quoted value as the text inside the quotes. It used to read the value as JSON,
  so the two top layers could read one spelling two ways. One difference stays on purpose:
  `--set database.password=null` is no value rather than a password spelled `null`, and is
  refused by name, as is a `null` password in a JSON or YAML file in a `config.d` directory.

### Removed

- The one-time import of the three files the service kept before the house database (the
  channel list, the switch and the state), and with it the service's `--channel-file`,
  `--switch-file` and `--state-file` options, their `[files]` settings (`files.channel_file`,
  `files.switch_file`, `files.state_file` and their `SOUNDTOUCH_ZONEMASTER___FILES__*`
  variables) and the shipped `20-files.toml`. The service's `--json` envelope no longer carries
  `channel_file`, `switch_file` or `state_file`. **A deployed unit must stop passing the three
  options BEFORE this version is installed**: an `ExecStart` that still names one is refused as
  an unknown option (exit 2), so the service does not start. A `[files]` section left in a config
  file is not read and does no harm. A house that still keeps its list, switch and state in
  those files must start a 0.6 release once first, which imports them; `channels export|import`
  remains the way to move a channel list in and out as a file.

### Fixed

- A speaker that connects to the master in the very moment the master stops is now handed to
  its handler and answered, instead of being left on an open connection nobody reads until the
  garbage collector happens to close it. The master stops accepting first and closes its
  listeners two loop turns later, which works around CPython issue 109564 (an asyncio server
  closed between accepting a connection and building its transport drops the socket
  silently). The collection used to raise `TypeError` from asyncio's `Server._wakeup`.
- The service closes its MPD control connection when the house stands down, whether the switch
  went off or the service is ending. It used to close it only after a failure, so every stop
  left the socket open at MPD until a garbage collection reached it; the next channel opens a
  fresh one as it always did after an idle timeout.
- A connection to MPD that is given up while waiting for MPD's greeting (the service's deadline
  on every MPD exchange, or a channel start cancelled at stop) is closed instead of dropped open.
- Numbers dialled in quick succession end on the last of them, in MPD and on the zone alike. A
  channel start talks to MPD before the zone takes its station, so a number dialled while MPD
  was still answering the one before could reach the zone first: the earlier press then played
  while the house recorded the later one, and two starts sharing MPD's one control connection
  could fail or load their playlists in the wrong order behind the one `httpd` stream. Starts
  now talk to MPD one at a time, and a start that a later one has overtaken gives up without
  loading MPD or switching the zone.
- The service's first HTTP request no longer holds up the event loop, which also keeps the zone's
  clock and frames. Importing httpx's transport and anyio's asyncio backend and building an SSL
  context happened on the loop the first time a client was built or used, a stall of tens of
  milliseconds at the registry warm-up of every start; they now happen when the program starts.
  Every client shares that one SSL context rather than building its own for each speaker call.
- A speaker switched off while the service starts is no longer taken into the zone. The start
  asks every speaker what it is playing, and a speaker still on the service's stream from a run
  that ended without dissolving the zone used to be taken in on that answer even when it had
  switched itself off before the answer arrived: the frame saying so was read only after the
  start. Frames are now read while the start asks, so an answer the speaker has contradicted
  since is dropped, at start as in every later round.
- A speaker switched on while it is being asked what it is playing is taken into the zone on that
  one switch-on. Its answer, STANDBY, is dropped because the speaker named a source since, and
  the speaker then read as simply playing its own radio and stayed out until it was switched on
  a second time. A STANDBY answer followed by a frame naming internet radio now counts as the
  wake it is, at start and when the registry lists a speaker for the first time.

## [0.6.1] 2026-10-01 18:27:58

### Changed

- A stop now tells every speaker the zone is over at once rather than one after another, so the
  slowest box decides how long the dissolve takes. Told in turn, each box that was off cost a whole
  8 s speaker timeout, and with MPD hung as well six such boxes ran a stop past the unit's 60 s
  `TimeoutStopSec`: the kill landed inside the dissolve and the boxes not told yet stayed bound to
  a master that had gone. The worst stop is now about 31 s whatever the number of boxes.
- A required setting (`database.url`, `zone.bind_ip`) that a layer wrote as `null` - the
  environment's `null`/`none`, a `.env` file, or `--set ...=null` - is refused naming where the null
  was written, and says that removing it lets a lower layer's value apply. It used to be refused as
  a value given nowhere, which was false whenever a config file held a real value the null hid. A
  typed `--database` or `--bind-ip` still wins over such a null, and `config` still shows it as
  `null`.
- With the house switched off, nothing a person presses is booked for later: the house comes back
  on exactly as it was switched off. A box still plays its own preset meanwhile, as any SoundTouch
  speaker does. Before, a number dialled while the house was off became the channel every room
  started on at the next switch-on, and a box taken out of multiroom was let back in by being
  switched on, so an update, a test or a press made an hour earlier decided what the whole house
  played; the log also said "joining the zone" while no master was running. A press, or a number
  begun just before the switch went off, is now logged as ignored.
- Example addresses in `--help`, the shipped `10-zone.toml` comment and the research scripts now
  use the documentation range 203.0.113.x (RFC 5737) instead of a real private subnet.
- Requires `lib_layered_config>=6.1.1`, which warns when a secret setting such as
  `database.password` is set to an unquoted `null` in the environment and caps a `.env` file at
  10 MiB.

## [0.6.0] 2026-10-01 12:31:14

### Added

- A channel url, and a speaker preset the channel list is seeded from, may be a RELATIVE Orion
  location, `/station?data=...`, the form AfterTouch 0.138.0 writes presets in. The master
  completes it the way a speaker does: with the `LOCAL_INTERNET_RADIO` base URL from the service's
  BMX registry (`<[registry] url>/bmx/registry/v1/services`), read once per run. A registry that
  cannot be read within 2 s, answers an error or a redirect, sends more than 64 KiB, names a
  base with a query or fragment in it, or sits at an address no request can be sent to (a
  mistyped `[registry] url`), falls back to
  `<[registry] url>/core02/svc-bmx-adapter-orion/prod/orion`, says so in the log, and is left
  alone for a minute rather than asked on every station start; the first station start or speaker
  document after that minute asks it again (there is no timer). A station fetch that fails against
  the base the registry named makes the next one read the registry again, so an Orion adapter that
  moved is found. The channel list keeps the location exactly as given. What a speaker is sent - a
  `/select`, or the item the zone shows its slaves - carries the absolute location only once the
  registry has named its base; before that, and for as long as the registry cannot be read, it
  carries the location as stored, for the speaker to complete through its own registry, and never
  the fallback. Speakers are proven to resolve a stored
  relative preset; a `/select` carrying a relative location has not been measured on a real
  speaker yet. Building what a speaker is sent never waits for the registry: it starts a
  background read instead, so a later document can be absolute. The service starts that read
  itself as it starts, without waiting for it, so the first station a box is switched on to is
  normally absolute already. The prototype reads `[registry] url` too.
  The absolute form (`http://.../orion/station?data=...`) and the legacy
  `/custom/v1/playback/...` form still work unchanged; every other bare path is still refused.
- `tools/deploy_service.py`: one tested deploy, shipped to the service host and run there. It backs up
  the house database (the sqlite3 backup API, safe in WAL mode), switches the house off and waits,
  bounded, for the zone to empty, stops the unit, installs through `tools/install_service.py`, refuses a
  venv that still holds a second distribution or a superseded console script (checked before the house
  is touched), starts the unit and hands the house back only after the unit stayed active for a settle
  window with no restart, then restores the switch - but only while the switch row still holds the
  deploy's own switch-off, so a `switch off` (or `on`) somebody ran in the minutes between is left as
  they set it and the envelope's `switch_note` says so; the same holds when a failure before the
  stop puts the switch back. A `switch off` run while the database is being copied, before the
  deploy's own switch-off, leaves that switch-off nothing to change, and the switch stays off to the
  end; a deploy interrupted after that says who switched it off, not how to switch it on. `--dry-run` reads without changing anything, on PostgreSQL too; every step and command is
  bounded; `--json` / `--json-bare` as every CLI here.
- The installer (`tools/install_service.py`, with `tools/service_venv.py` beside it) seeds the switch into
  the configured house database (`database.url`, else `<state-dir>/zonemaster.sqlite`) in the same
  transaction that creates the schema: a database it creates starts OFF (or with an old `zone.switch`
  file's word), an existing database's switch is never changed, and an install interrupted half-way can
  no longer leave a database whose first start reads ON. It warns when no config layer names
  `database.url` and it seeded its own default path.

### Changed

- Requires `lib_layered_config` 6.0.0. A config file that will not parse is refused in the library's
  new words, `<path> is not valid TOML (line N, column M)`, without the parser's text; a `.env` that
  is not UTF-8 is now the same refusal naming the file, where it was a traceback. A number in an
  environment variable stays text unless it reads back the same (`0640`, `1.50`), and an UNQUOTED
  JSON array or object in `.env` is now a list or table, as in the environment: quote a
  `database.password` there that looks like one.
- `config-deploy` writes the modes it always did (the user layer 700 for directories and 600 for
  files, the app and host layers 755 and 644), but a layer it does not write, or the environment,
  may now configure them in `[lib_layered_config.default_permissions]` (`user_file = "0o640"` and
  so on, an octal string; a bare integer is refused). So it reads the other layers first, and
  refuses with exit 2, writing nothing, while one of their files cannot be parsed or sets a mode it
  cannot use. That refusal names the file and why, and ends with what to do here (correct it, or
  set the modes in that section); the library's own hint names parameters `config-deploy` has no
  option for. A file whose content is unchanged is still skipped and keeps its mode.
- `--device-id` / `zone.device_id` accept either case and fold to upper case now, the same rule
  `membership.consoles_allowed` already applies to a console's id: a lower-case id is no longer
  refused.
- A higher config layer's explicit `null` over `bind_ip` - `--set zone.bind_ip=null` or the
  equivalent environment variable - is refused naming the setting, rather than with pydantic's own
  words about the record field. Previously only `database.url` was refused this way. An explicit
  `null` over `device_id` is refused too, in a sentence of its own: it says that removing the null
  falls back to this host's MAC address, which is what a device id given nowhere does, rather than
  asking for a value that was never needed.
- The service's house database calls no longer run on the event loop that times the zone: every
  one - the state saves, the switch and preference polls, the channel list, open and close - runs
  on one thread of its own, in the order it was asked for, and a stop closes the database only
  after every write asked for before it. A state save that fails where a key press is read is
  logged and the service goes on, rather than ending the run.
- On PostgreSQL a schema migration is no longer cut off by the store's 5 s statement timeout; the
  timeout is lifted for the migration's own transaction only.

### Fixed

- On PostgreSQL a switch write (the `switch` verb, the service's own, and the deploy's
  `set-switch`) locks the switch before it reads it, so a `switch off` a person commits between that
  read and the write is no longer reported as the writer's change. A deploy's switch-off took such
  a `switch off` for its own and turned the house back on at the end; through the 0.5.2 package
  being replaced it failed on a duplicate key instead.
- `prefs`, `prefs set` and `prefs unset` read a preference the environment gives as text the way
  the service run does. Under `lib_layered_config` 6.0.0 a value such as
  `SOUNDTOUCH_ZONEMASTER___DIALLING__WINDOW_S=0.80` (the variable `50-dialling.toml` names) arrives
  as text, which the service always read as 0.8 but all three verbs refused with exit 1; the same
  held for the hold threshold, the MPD rewind and the fade. Text that is no number is still refused.
- A stop (a deploy's SIGINT, or `switch off`) that lands just as a calibration ends keeps both
  numbers it measured. It used to keep the dialling window and lose the hold threshold, because the
  hold was asked of the database only once the window's write had answered.
- A calibration is stored whole or not at all: its dialling window and hold threshold go to the
  house database in one transaction, so a refusal of either stores neither. It used to write them
  one at a time, and a refused hold left the window behind for the service to run on.
- A calibration the house database refuses is said once in the log, and the service goes on with
  the dialling window and hold threshold it already had. It used to end the whole service. One
  stored but not read back afterwards is said as stored, and the service takes it in at its next
  preference read.
- A console added to `membership.consoles_allowed` while the service runs is watched at once rather
  than from the next speaker-registry read (`registry.poll_s`, 30 s by default): the service reads
  the registry straight away and asks the console what it is playing. One already playing the
  house's stream is taken in at once, and one asleep is taken in on its next wake.
- A speaker the registry lists for the first time while the service runs is asked what it is
  playing, as every speaker is at start, so it is taken in on its first wake rather than its second.
- A speaker switched off while its answer to that question was still on the way is no longer taken
  into the zone and switched back on: an answer is dropped when the speaker has reported what it is
  playing in a notification of its own since it was asked. Each answer is also taken the moment it
  arrives, so one speaker slow to answer no longer holds back the others asked with it.
- When the channel list is empty and is seeded from a speaker's presets, the speakers asked
  together stand in the registry's order, ahead of any speaker a person switches on while their
  answers are still on the way. Which speaker seeds the list no longer depends on which radio
  answered fastest.
- `channels export` now reads the house database once and writes the file atomically, so the
  reported channel count can no longer disagree with the exported text (it used to read the store
  twice in two separate transactions).
- The switch row is now an upsert (`INSERT ... ON CONFLICT DO UPDATE`) instead of delete-then-
  insert, on both backends. `switch` opens the store without the writer lock, so two invocations
  really can write the row at the same instant; on PostgreSQL under READ COMMITTED that could end
  in a unique-constraint violation instead of the switch simply landing on whichever value won.
- The deploy helper's read of the house database (`service_venv.py show`, and the check before
  the switch is seeded) opens a SQLite file read-only and changes nothing in it: it used to switch
  a DELETE-mode file to WAL and grow an empty file to a page. A database driver it cannot load is
  now refused as a store error naming the database, as opening the store already was.
- `config` now masks the database's own location in anything the house store narrates on stderr
  under `--redact`, not only the values it prints. Nothing narrates on that path today, but a
  future line (a migration, an import) can no longer defeat `--redact` by naming it anyway.
- A PostgreSQL server whose host vanished without closing the connection (a network black hole)
  is noticed within seconds: every connection now sets TCP keepalives and `tcp_user_timeout`,
  each unless `database.url` gives that parameter itself.
  Before, a call on such a connection waited in the kernel for about fifteen minutes, because
  `statement_timeout` is enforced by the server and its cancel never arrives.
- A stop no longer waits on a house database that stopped answering, so the zone is always
  dissolved and the process always exits. The stand-down waits at most 3 s to write down where MPD
  had got to before it dissolves the zone (after MPD's own answers, each bounded at 5 s), a fade
  cut short by the stop asks for its save without waiting for it, and closing the database gives
  up after 10 s and says so. Before, one hung call held the dissolve, the close and then the
  process exit until systemd killed it, leaving the speakers bound to a master that had gone. A
  write given up on this way stays queued and lands only if the database answers again before the
  process exits; otherwise it is lost with the process, and the close's error line says how many
  writes that is. A failed state save is also logged once rather than twice.
- A fade whose final save the house database refuses is now said once in the house log. The fade
  runs on a task nobody awaits, so the failure used to surface only as asyncio's own "Task
  exception was never retrieved", outside the house log, whenever the task was collected.
- A house database that fails to open or migrate is refused with its own error again; a lock release
  that failed while closing it could replace that error with an unrelated one.

## [0.5.2] 2026-09-28 21:03:10

### Fixed

- A stop that arrived while `switch off` was still dissolving the zone - which is the order a
  deploy uses - left the boxes not yet told in a zone whose master had gone, and kept the master's
  four ports bound until the process ended. The dissolve is one call per box, so the stop only had
  to land while one box was answering. The stand-down now holds the master until it is down, and
  the service's own last stand-down finishes an interrupted one: every box is told, and the ports
  are closed.

### Known issues

- The rare stop that waits for ever on a station fetch, listed under 0.5.1, is still open: it has
  not recurred since, in CI or locally, and its cause is not yet known.

## [0.5.1] 2026-09-28 15:08:20

### Changed

- `switch`, `channels export`, `channels import`, `prefs`, `prefs set` and `prefs unset` no longer
  create the house database when the one they are given is not there. They refuse with exit 2 and
  `StoreMissingError`, naming the path: only the service creates the database. Before, a mistyped
  `--database` got a new, empty database and `switch off (changed)` while the service went on
  reading the real one, and a verb run as root before the first start left files the service could
  not open. A fresh install is unaffected: the installer's switch file is imported on the first
  start, which is what creates the database.

- A call to a speaker or to the speaker registry now has ONE deadline for the whole request (8 s,
  as before) rather than 8 s per phase, so a box that accepts a connection and answers slowly fails
  within 8 s instead of up to three times that. A registry that never answers is reported as
  `could not be reached (TimeoutError)` rather than `(ReadTimeout)`.

### Fixed

- One cause of a stop that could hang for ever. When the service was stopped on the same turn of its
  event loop as a station's read timeout fired, the timeout (httpx's, run by anyio) took the stop
  for its own and swallowed it: the fetch reconnected every 30 seconds and the stop, and with it the
  dissolve of the zone, waited on it indefinitely. It needs only a loop that is late at the moment
  of a stop, which a loaded host or a slow pass provides. Every HTTP deadline is now enforced by
  asyncio instead, which lets a stop through, and no httpx client carries a timeout of its own.

### Known issues

- A stop can still, rarely, wait for ever on a station fetch. It was seen once in CI after the fix
  above (one of fifteen test runs) and has not been reproduced locally, so its cause is not yet
  known. A service with no station playing is not affected.

## [0.5.0] 2026-09-28 01:25:05

### Added

- New commands: `soundtouch-zonemaster-service prefs`, `prefs set NAME VALUE`, `prefs unset NAME`
  for the five house preferences (`dialling.window_s`, `dialling.hold_threshold_s`, `mpd.rewind_s`,
  `volume.fade_s`, `membership.consoles_allowed`). `prefs` lists every preference, its value, and
  whether a config layer or a stored row is deciding it; `set` writes a row that beats every config
  layer until `unset` removes it. A running service reads a stored value within about a second.
  The window and the hold apply once nobody is dialling or holding a key, the fade from the next
  join. A console removed from `membership.consoles_allowed` is let go at the next pass; one added
  is only watched from the next speaker-registry read (`registry.poll_s`, 30 s by default) and is
  taken in the next time it wakes, so one that is already awake stays out until then.
  A new setting, `volume.fade_s` (default 0.8 s, bounds 0.0 to 5.0 s inclusive): how long a joining
  box's volume takes to climb back to its own level.
- `config` now shows a preference set in the house database over the file value it replaces: the
  file value is printed beneath it, on its own line. A stored row nothing can use (a name this
  version does not know, a value out of bounds) is shown as ignored, with its raw text and why.

### Changed

- The calibrated dialling window and hold threshold no longer live on the `zone` row: they are two
  of the five rows in a new `preference` table, migrated at the first start after the upgrade
  (source `calibration`, the time not recorded, as it never was before). A config file's value for
  any of the five preferences no longer decides while a stored row is set; `prefs unset` gives the
  decision back to the config layers.
- A console device id is now checked (`membership.consoles_allowed`, both from a config layer and
  from `prefs set`), rather than accepted as written: an id of twelve hex digits in any case is
  accepted and stored in upper case, and anything else is refused.

### Note

- `config` and `prefs` open the house database only when the view includes at least one
  preference, and, like `switch`, migrate its schema to head if it is behind, briefly taking the
  writer lock to do so. `config` never creates a database: a SQLite file that does not exist reads
  as "no preference is stored", nothing is written. A database `config` cannot open or read costs
  one line in its output and does not change its exit code; under `--redact` that line does not
  name the database's location. The JSON envelope of `config` gains a `database_note` field (null
  when there is nothing to say).

## [0.4.10] 2026-09-27 10:33:50

### Changed

- A `database.password` that arrives as anything but text (a number, a boolean, a list, a table) is
  now refused (exit 2) even beside a typed `--database` that would not use it, as a password that
  arrives as no value already was. It used to be left out there with the "not used" line.

## [0.4.9] 2026-09-27 09:54:21

### Added

- New commands: `soundtouch-zonemaster-service switch [on|off]`, `channels export --output FILE`,
  `channels import FILE` (refused while the service runs).
- A `database.password` setting (`SOUNDTOUCH_ZONEMASTER___DATABASE__PASSWORD`) for PostgreSQL,
  read through every configuration layer and handed to the driver as a connect argument. It has no
  command-line option, `config` always shows it masked, and no envelope or log line carries it.
  Left empty, the PostgreSQL client's own `~/.pgpass`, `PGPASSFILE` or `PGPASSWORD` apply.
- PostgreSQL needs the package installed with its `postgres` extra.

### Changed

- The service keeps its state, the house's channel list and the switch in one database
  (`database.url`, `--database`): a SQLite file by default, or a PostgreSQL URL for a machine that
  already runs one. On the first start after the upgrade it imports `zone-state.json`,
  `channels.json` and `zone.switch` once each and renames them to `*.imported`.
- A database that cannot be read now refuses the start. The state file used to start empty instead.
- An old `zone-state.json` or `channels.json` that exists but cannot be parsed also refuses the
  start, naming the file; nothing is imported and nothing is renamed.
- A SQLite older than 3.37 refuses the start: the tables are `STRICT`.
- A database URL carrying a password is refused; the password belongs in `database.password`.
- The configured password goes only with the configured database. A `--database` typed on the
  command line gets it only when it equals `database.url`; any other typed database is opened
  without it, and one log line says so.
- A `database.url` or `database.password` that arrives as no value (`null`, `none`) or as anything
  but text is refused (exit 2), naming the setting and never the value. A null url is reported as a
  database given nowhere, on every command. The one exception: a password that is not text, beside
  a typed `--database` that is not the configured one, is not used and so not refused.
- `config` masks every key under `[database]` except `url`, so a misspelled password key is not
  printed; a `url` that is not text is masked whole, and a null one is shown as null.

## [0.4.8] 2026-09-25 19:53:39

### Changed

- One public repository instead of a private working copy plus a redacted export. A machine's own
  values live in gitignored `9N-<scope>-rnhome.toml` override files beside the public default they
  override - the package's `defaultconfig.d/`, the research scripts' new `research/defaultconfig.d/`
  and the hardware tests' `tests/e2e_defaults.d/` - with a tracked `.example` beside each. The
  wheel and sdist exclude every `*-rnhome.*` file, and a test builds the wheel to prove it.
- `config --redact` masks every value a private `-rnhome` file set, not only what came from a
  `.env`: such a file reports the defaults layer, so masking by layer alone printed it in full.
- The hardware tests read typed settings from `tests/e2e_defaults.d/90-e2e-rnhome.toml`; a `.env`
  still layers above it.
- The research scripts read the capture host's `never_touch`, the ssh user, the golden-check
  inputs and the MPD A/B paths from their own layered settings instead of constants.
- Both commands answer `--version`.
- The private name list is `tools/public_redactions-rnhome.txt`, gitignored like every other
  `*-rnhome.*` file, with `tools/public_redactions.example.txt` tracked to show its format. Its
  rules map the rooms to `Room1`..`Room6`, the machines to `service-host` and `capture-host`, and
  the speakers' device ids to `AABBCC0000xx`, the same names the tracked tree uses.
- `tools/export_public.py` reads that list, refuses by name when it is absent, takes the list as a
  parameter of `build`, and also drops every path the source lists in its `.git/info/exclude`.

### Added

- CI on every push (the default_cicd_public workflows, Linux only), a Quickstart notebook the CI
  executes, and publishing to PyPI on a version tag.
- A name guard in the gate: `tests/test_no_private_names.py` fails, naming the file and the literal,
  when any tracked file holds a literal from the private name list in any case. It skips with its
  reason where the list is absent, as in CI and a fresh clone.

## [0.4.7] 2026-09-25 16:22:49

### Changed

- The thumbs have new gestures, so that one stray touch can no longer change the rotation for the
  whole house. HOLDING a thumb now puts the playing channel into the rotation (up) or takes it out
  (down); a single tap used to. A DOUBLE tap switches the box out of the group (down) or brings a
  box still attached back in (up); a hold used to, and at a box already in a double thumbs up is
  logged and does nothing else. A SINGLE tap of either thumb hands the box the house volume: the
  volume keys at that box then step every box in the zone, as a held thumbs up used to. The
  journal says `thumbed up once` or `thumbed down twice`.

## [0.4.6] 2026-09-25 13:52:03

### Added

- Channels that play a DIRECTORY: `mpd_directory` in the channel file names a path under MPD's
  music directory, and the channel plays every file under it, subdirectories included - on every
  level the files first, then the subdirectories, each to its end before the next, with names
  compared naturally (`Kapitel 2` before `Kapitel 10`), case-blind and without the machine's
  locale. An MPD channel names a playlist or a directory, never both. The place such a channel was
  left is remembered by the file's name, so a file added to the directory later does not move the
  house into another chapter.
- A held next or previous on an MPD channel moves by DIRECTORY: held next goes to the first file of
  the next directory, held previous to the start of the directory playing and from there to the
  one before. It works on a stored playlist too. On a radio channel a hold is still one step.

### Changed

- The channel file now writes `"mpd_directory": ""` for every channel that plays none.
- The journal names what a step moved by: `stepped +1 file inside 12` or `stepped +1 directory
  inside 12` (was `stepped +1 inside 12`).

## [0.4.5] 2026-09-24 23:26:09

### Added

- `end` on an MPD channel in the channel file: `wrap` (the default, and what a channel without
  the field gets) plays on from the first file at the end of the playlist; `stop` falls silent
  there and forgets the remembered place, so a book starts from the top the next time it is
  dialled. `"end": "stop"` on a radio channel is refused when the file is read.

### Fixed

- Next on the last file of an MPD channel no longer leaves the house silent: next and previous
  wrap at both ends on every MPD channel, a `stop` one included. The service now names the entry
  MPD is to play instead of sending MPD's own `next`, which stops at the end of the queue.

### Changed

- The channel file now writes `"end": "wrap"` for every channel, radio ones included.

## [0.4.4] 2026-09-24 21:54:39

### Added

- House volume. Hold thumbs up at a box in the zone, let go, then use volume up or down there: for
  as long as you keep going (4 s to start, 3 s after each change) every other box moves by the same
  step, each from its own level, so a quieter room stays quieter. The remote cannot send two keys
  at once, which is why the thumb comes first. A box that is off or on another input is not
  written; it is owed the step in the state file (`owed_volume`) and takes it when it next joins,
  by fading up to its own level plus what it missed. A box out of multiroom takes nothing.

### Changed

- A held thumbs up at a box already in the zone now hands it the house volume; it used to log
  "already in multiroom" and do nothing. At a box still attached but out of multiroom it still
  brings the box back in.

## [0.4.3] 2026-09-24 19:51:09

### Fixed

- A slow ordinary tap is no longer read as a hold. The dialling window (the pause BETWEEN two
  keys) was also the hold threshold (how long ONE key is down), so a house calibrated to a 0.6 s
  window read a 685 ms tap of the thumb as a hold and switched the box out of multiroom instead of
  moving the rotation. The two are now separate numbers.
- A held next or previous key steps once, not twice. With the threshold longer than the window it
  acted as a tap when the window closed and again as a hold at the threshold; a pending step now
  waits until its key is decided.

### Added

- `dialling.hold_threshold_s`: how long a key must be down to count as a hold. Default 1.0 s,
  refused outside 1.0 to 2.0 s (exit 1, like the window). The calibration gesture now measures it
  from the same presses as the window and writes both to the state file; a state file from before
  this carries a window and no threshold, and gets the default.

## [0.4.2] 2026-09-24 16:17:13

### Fixed

- A speaker that hangs up in the middle of a station change no longer ends the service. The
  change is sent in steps with a one-second pause before the final PLAY, and a box that closed its
  connection inside that pause made the PLAY fail with an error nothing caught: the whole service
  exited and was restarted, and the house had no zone for about ten seconds. Such a box is now
  logged as gone and the rest of the zone carries on.

## [0.4.1] 2026-09-24 15:20:52

### Fixed

- A box taken out of multiroom with a held thumbs down now really leaves the zone. It was only
  dropped from the master's books and sent nothing, so it stayed a slave, forwarded every station
  it was given and every preset pressed at it back to the master, and went on playing the group's
  stream. The master now sends it `/removeZoneSlave`, the message a SoundTouch master sends a
  leaving slave, and then hands it the channel the house was playing. A box that leaves the zone
  by itself (standby, AUX, Bluetooth) is still sent nothing.
- The station the service sends a box outside the zone no longer comes back as a preset press. The
  box answers it by naming the preset slot that holds the station, which read as a person pressing
  it: the service dialled the same channel again, about every 0.65 s, until another digit broke
  the loop. Every such `/select` now owes one selection and one touch, claimed as the service's own.

### Changed

- The way back into multiroom is switching the box on (the power key, or a preset key while it is
  in standby). A released box reports its keys only as anonymous touches, so a held thumbs up from
  it cannot reach the master; it still brings back a box the release did not reach.
- `keymappings.md` documents what every remote key does, state by state.

## [0.4.0] 2026-09-22 23:54:56

### Security

- The speaker HTTP face (port 8090, unauthenticated by the protocol) no longer lets one crafted
  request stall the zone. Three of its patterns, and two of the notification reader's, were
  quadratic in input any device on the LAN controls: a `POST /slaveMsg` of a repeated `<keyData`
  cost 0.9 s at 64 KB, and the face accepts a MiB, which held the event loop - and every slave -
  for minutes.
- Every speaker document is now read by ONE parser with three bounds - at most 64 KiB (the largest
  frame on record is 2,831 characters), no `<!DOCTYPE` at all, and no deeper than 32 elements -
  instead of by twelve patterns spread over six modules. A document it will not read is answered
  as unreadable rather than raising, and a frame is still kept whole. This closes the whole
  entity-attack family by construction rather than by the parser's own limits, and it stops a
  deeply nested selection reaching the recursion in serialising it back to the speakers.

### Changed

- `[prototype] never_touch` ships empty. Which box must be left alone is a fact about one flat, so
  the wheel names none; a house puts its own entry in a layer of its own, and the host layer
  (`/etc/soundtouch-zonemaster/hosts/<hostname>.toml`) is the one meant for it. A house that relied
  on the shipped value must add that file BEFORE installing this version, or its console is no
  longer refused. The refusal message and exit code are unchanged.
- A click usage error (a missing, unknown or malformed option) is the JSON refusal envelope on
  stdout when `--json` or `--json-bare` is on the command line, in every command, the research
  scripts and the tools included. It still exits 2; without either flag it is still click's prose
  on stderr.

## [0.3.11] 2026-09-22 15:39:51

### Fixed

- The service's own volume fade no longer reads as a person touching the box. A real box answers
  every volume write with a `userActivityUpdate`, the one frame that tells a press from our own
  station change coming back, and a join writes nine volumes; on 2026-09-21 one of those touches
  confirmed a selection nobody made, and the house dialled it. Each write now owes exactly one
  touch, from the moment it is sent until 0.5 s after it returns, and that one is dropped. A person
  pressing while the box climbs back to its volume is still heard, which a plain time window over
  the fade did not manage.

### Added

- `soundtouch-zonemaster-service --version`, and with `--json` or `--json-bare` the same envelope
  as every other answer. It needs no setting, so it works on a host nobody configured.
- A key pressed while a calibration runs is now said by box and key instead of doing nothing in
  silence.

### Changed

- A box waiting for a number, and an unusable entry in the device list, are said when they change
  rather than on every pass or poll: measured eleven identical lines in 0.9 s for the first and
  about 120 an hour for the second.

## [0.3.10] 2026-09-21 07:06:53

### Fixed

- Every MPD channel change stopped killing MPD. The resume sent `play <track>` then
  `seekcur <seconds>` as one command list, and that sequence SEGFAULTS MPD 0.24.6: the seek
  arrives before the decoder has opened the song, so there is no duration to seek within. It is
  upstream's MusicPlayerDaemon/MPD issue 276, open since 0.20.18. Every MPD channel in this house
  carries a saved position, so every channel change did it - MPD died four times between
  2026-09-20 16:16 and 2026-09-21 06:17, each within 10 to 31 ms of a dial, and with `Restart=no`
  on the unit it stayed dead for 1 h 36 m and 2 h 20 m at a time. Several symptoms chased over
  those two nights had a dead MPD underneath them. The resume is now a single
  `seek <songpos> <time>`, which names the entry and the offset together so nothing races the
  decoder.
- Measured rather than reasoned, on a container host: an interleaved A/B, two rounds per
  arm, `seekcur` after a `play` died on SIGSEGV every time with and without a listener on the
  httpd output, while the same change without a seek survived every time. Three replacements
  survived; `seek` was chosen because it is one command and needs no wait. It was then checked for
  doing the job rather than merely surviving - `seek 2 300.000` leaves MPD playing entry 2 at
  302.9 s.

## [0.3.9] 2026-09-21 05:49:01

### Fixed

- A four-digit number is no longer split by a thumb moving from one key to another. The dialling
  window is armed at the RELEASE of the previous key rather than at its press (user, 2026-09-21),
  because a window armed at the press charges a person for however long they hold the key. Measured
  in the flat that morning: the move from the `1` key to the `3` key took 0.645 and 0.623 s in the
  two four-digit numbers that came out as `111` and a stray `3`, against a 0.6 s window - and only
  about 0.3 s of it was idle time. Nothing was lost on the way; the box reported every press on both
  of its channels, fifteen groups out of fifteen.
- A press is read as a whole key action - two touches with a hold between them, beside the
  selection - rather than as a lone touch, and it claims both in one step. On the two runs of real
  human presses the rule is now correct at every forward window from 0.2 s to a minute and every
  backward window from 40 ms to five seconds, where before each had a measured edge: there is no
  longer a loose touch for a late echo to adopt.
- The pairing no longer waits out its whole window when a box has said more than it was asked. It
  releases as soon as every selection is answered and nothing loose can still matter, which is what
  cost one dial 1.967 s of silence in the flat.

### Changed

- The calibration measures the IDLE time between two keys - from one coming up to the next going
  down - because that is what the window now governs. A window built from press-to-press gaps is
  longer than the hand it was measured on by however long that hand holds a key, which is 0.23 to
  0.447 s in this house. The window stored from an earlier calibration is a press-to-press number
  and stays in force; it is safe, being too generous rather than too tight, so re-run the
  calibration at a box to get the number this rule wants.

### Corrected

- The 0.3.7 and 0.3.8 notes say a slave answers a burst in BLOCKS, with the confirmations arriving
  in their own group more than a second behind. That was read from a recording in which the presses
  were DRIVEN through a box's `/key` endpoint, and such a press reports no `userActivityUpdate` at
  all once the box is a slave: the block of touches in it is the service's own volume fade. A real
  burst pressed by hand at a slave carries a touch pair around each selection, measured over six
  presets pressed in a row in the second live run. The fix those notes describe is real and was
  confirmed in the house; the mechanism attributed to the speaker was ours.

## [0.3.8] 2026-09-21 04:27:38

### Fixed

- A four-digit number dialled at a box that is IN the zone reaches the house. The user's report was
  "1111 does not work in the first place". A box that is a slave does not answer a burst press by
  press: it sends the four selections in a row and the four confirmations afterwards in their own
  block, the first of them 1.06 s behind the selection it belongs to. The rule held ONE selection
  and ONE touch per box, so each new selection displaced the one before it and four presses became
  two digits - a number that is no channel, so the house stood still. Both sides are now collected
  and paired only when nothing further can change the answer, each touch with the nearest selection
  in time, what is left over discarded. Measured on eight presses driven through a box's own `/key`
  endpoint, four outside the zone and four as a slave; the recording is in the repository and the
  two earlier live runs replay byte for byte unchanged.
- The dialler is driven by the moment a press became KNOWN rather than the moment the key went
  down, which the deferral made necessary: a burst learned 1.8 s behind itself would otherwise
  complete as four separate one-digit numbers. The calibration still measures the key going down,
  because what it measures is how fast a hand presses.

## [0.3.7] 2026-09-21 02:45:21

### Fixed

- A channel is the same channel as another when it carries the same NUMBER, not when it carries the
  same URL. MPD has one `httpd` output, so every channel whose sound comes from a stored playlist
  names the same address, and the guard that refuses "you dialled what is already playing" compared
  those addresses. The house could therefore not move from one audiobook to another at all: the
  number was written into the state file, the log said `already playing it`, and nothing happened.
  Measured in the flat 2026-09-21, eleven dials in a row - every move between two MPD channels
  refused, every move that had a radio channel at one end fine, and the only way from one book to
  the next was to dial a radio channel in between. The zone now records which channel the running
  station was started from and answers the question with that.

### Added

- One log line per dialled DIGIT, not only per completed number. On the same night the house read
  the four-digit 1111 out of a burst for which the master held three forwarded presses, and the
  number was the only line either half wrote, so nothing said whether a press had been counted
  twice or a forward dropped.

## [0.3.6] 2026-09-20 23:26:33

### Fixed

- A press no longer waits for another press. A key held in the flat on 2026-09-20 was due at its
  0.6 s window and acted on 4.67 s later, because a competing gesture had put the service inside a
  station start, and a station start waits up to ten seconds for somebody else's server. In a room
  that is: hold a key, nothing happens, and five seconds later the house jumps. The zone master
  already solved this - several presses may be inside its `play` at once, the newest wins and the
  older ones give up - and the service was re-imposing the wait it exists to avoid, in two places
  at once: the pass lock was held across the start, and the dialling loop waited for the start it
  had dispatched, so no other deadline could be seen meanwhile. Now the newest press begins
  fetching at once and the older one stands down. How long a channel takes to arrive is unchanged.

## [0.3.5] 2026-09-20 23:09:28

### Fixed

- A crash now keeps its traceback. The service died in the flat on 2026-09-20 at 22:46:25 and the
  whole record of it was `ConnectionResetError: Connection lost`, naming no file, no line and no
  frame; it did not happen again on an identical repeat, so that one line was all the evidence
  there would ever be. A service that systemd restarts on failure is exactly the one whose crash
  nobody is watching. A refusal is unchanged and still answers with prose and no stack, because
  there the input was wrong and the program said so. The traceback goes to stderr and never into
  the envelope, so machine mode still puts one parseable line on stdout and nothing else.

## [0.3.4] 2026-09-20 22:28:39

### Changed

- One rule now tells a tap, a run of taps and a hold apart, and it governs every key. A key
  released inside the dialling window is a TAP and joins what the window is collecting; a key
  still down when the window passes is a HOLD, acted on THEN rather than when the finger comes
  up, because a box announces nothing at all while a key is down. The window is the only number
  either half uses: it is calibrated on the hand that presses, so a slower hand gets a longer
  window and a longer hold from the same measurement. Measured from the service's own journal,
  eighteen taps ran 285 to 448 ms against this house's calibrated 600 ms, a margin of 152 ms,
  and both the corpus and the margin are pinned in the tests.
- A held thumb takes its box into or out of multiroom at the window rather than at the release.
  A held next or previous still does what a tap does; its own meaning comes with the directory
  channels.
- A preset key keeps the count - `1` then `1` inside the window is still channel 11 - and cannot
  have a duration: it arrives as a selection frame with no release, so a long `1` is a short `1`.

### Fixed

- A calibration begins on the fourth press of its own gesture, and the guard that stops keys
  acting during one was swallowing that press's RELEASE, leaving a key the service believed was
  still down; it fired half a second later and moved the house. A release is now always read.

## [0.3.3] 2026-09-20 16:09:20

### Fixed

- Coming back to an MPD channel goes on in the file the house left, not the first one. The state
  file remembered how far into a channel the house got and not WHICH file it was in, so the offset
  was applied to the first entry of the playlist: heard in the flat on 2026-09-20, the house left
  the fifth file 1.5 s in and came back playing the first file 1.5 s in. A remembered place is now
  a track and an offset together, and a state file written by an earlier version still loads - its
  bare number means the first track, which is what that version would have played.

### Added

- A channel comes back a little BEFORE where it stopped, so that somebody returning to an
  audiobook hears their way back in rather than landing mid-sentence. The overlap is
  `[mpd] rewind_s`, twenty seconds by default, and an offset shorter than the overlap starts that
  same file again rather than stepping into the one before it, which is a different recording. It
  is applied when a place is read and never when one is written, so the overlap can be changed, or
  set to zero for music, without anything already recorded meaning something else.

## [0.3.2] 2026-09-20 15:28:48

### Fixed

- A box switched on while the house is silent is taken into the zone even when what it resumed is
  not one of its six presets. A speaker reports such a selection as preset 0, and a box switched
  on resumes whatever it had last, which after a zone is dissolved is the zone's own stream. The
  press went to the dialler, which cannot use that digit, and neither of the two branches that
  mark a wake was reached - so the strongest evidence there is, that somebody is standing at the
  box, was thrown away and membership was left to a source report that never came. Measured in
  the flat on 2026-09-20 at 08:10, where the box stayed out of the zone and silent. A press at a
  box that is ASLEEP now marks a wake whatever it selected; a press at an AWAKE box still does
  not, because the same preset 0 is what a box reports when somebody switches it to Bluetooth.

## [0.3.1] 2026-09-20 07:51:44

### Fixed

- A file step no longer stops working after MPD closes an idle connection. MPD closes a control
  connection that has been quiet for `connection_timeout`, 60 s by default, and this service
  speaks to MPD only when somebody presses something, so nearly every step begins on a connection
  that has been quiet for far longer. Two faults sat on that one path and the first hid the
  second: the client wrote into the closed socket instead of noticing, and then the service read
  the thrown-away connection as "MPD has never been asked for anything" and answered every later
  step with a sentence that was not true, doing nothing, until somebody dialled a channel. Found
  in the flat on 2026-09-20, where three presses of next in a row changed nothing and the tracks
  that did change were MPD's own queue running on. The client now asks whether the connection is
  still MPD's BEFORE it writes, which is what makes it safe: `next` is not idempotent, so a repair
  attempted after a failure would have to choose between losing the press and stepping twice. A
  connection MPD closed now costs nothing at all, and a daemon that is genuinely gone is still
  said by name once and still costs no zone.

## [0.3.0] 2026-09-20 06:56:02

### Added

- A channel can name a stored MPD playlist. `kind = "mpd"` plus `mpd_entry` in the channel file
  makes the sound come from the Music Player Daemon beside the service instead of a URL somebody
  else serves: the service asks MPD to load that playlist and play it, and only then points the
  zone at the stream, because MPD's `httpd` port does not listen until its output first opens.
  Both ways a channel starts - a box being taken into the zone, and a number dialled in a room -
  go through the same step, and a channel of any other kind asks MPD for nothing. A playlist MPD
  does not have, and an MPD that cannot be reached, are each said by name once and cost that one
  channel rather than the zone.
- An MPD channel comes back where it was left. How far into it the house got is written into the
  state file when the channel is left and when the service stands down, and the channel resumes
  there on the next dial or the next start - MPD keeps no position per stored playlist, so an
  audiobook nobody writes down begins again every time. A stopped MPD reports no position at all
  and that is not recorded as the beginning: a channel with no entry has never been left, which is
  a different thing from one left at the start. A state file written before this version still
  loads, with no positions.
- Next and previous page the FILES of an MPD channel. The key stays in the world it was pressed
  in: on a radio channel it is still the next channel, and on a channel that is a playlist it is
  the next file, which is what somebody listening to an audiobook means by it. Nothing about the
  zone changes for a file step - no station, no document, no speaker is sent anything - so it
  costs none of the rooms the 3.5 to 4 s of silence a channel change does.
- Two settings for it, in the six layers like every other one: `[mpd] host` and `[mpd] port`,
  shipped as `config.d/90-mpd.toml` and overridable with `--mpd-host` and `--mpd-port` or
  `SOUNDTOUCH_ZONEMASTER___MPD__HOST` and `..._MPD__PORT`. They default to `127.0.0.1:6600`, so a
  house that keeps its music beside the service needs to say nothing, and a port outside 1 to
  65535 is refused at startup rather than carried around as something nothing can dial.

### Changed

- Rebuilt on the `bitranox_template_py_cli` layers. The package is now `domain / application /
  adapters / composition`: pure rules as frozen dataclasses under `domain/`, ports and the service
  loop under `application/`, everything that speaks or serves under `adapters/`, and one composition
  root wiring an adapter per port. The domain no longer imports pydantic, the option records are
  frozen dataclasses, and the 1464-line service loop is a chain of eight classes, one file each.
  The old pydantic types at the boundaries were converted, and the equivalence contract for them is
  the golden corpus at `tests/fixtures/golden/` (seven hundred cases replayed from the old code's
  recorded refusals, coercions and file bytes; `research/golden_check.py` holds the three analysis
  scripts to their recorded output byte for byte as before). History before the rebuild (208
  commits) lives in an archive sibling of the repository; every sha dated before 2026-09-11
  resolves only there.
- The Lifestyle-console refusal is a setting. A `[prototype] never_touch` list in the configuration
  (shipped default: the Lifestyle console at 203.0.113.30) names the boxes an API POWER would
  disturb; the refusal message and exit code are unchanged. The prototype now reads the same six
  configuration layers as the service and takes `--profile` and `--set` like it does.
- The project is called `soundtouch-zonemaster`, everywhere: the distribution, the importable
  package `soundtouch_zonemaster`, both console scripts (`soundtouch-zonemaster` and
  `soundtouch-zonemaster-service`), the environment-variable prefix, the layered-config app and
  slug, the checkout directory and the public repository. It was `bose-zonemaster`; GitHub
  redirects the old name, so an existing clone or link keeps working.

  The name is now the same on every side, which it was not before: the directory said
  `Bose-Zonemaster` while the repository said `bose-zonemaster`, and reconciling the two was
  an open item. Two things did NOT move with it. The redaction rules in
  `tools/public_redactions.txt` still name every path and spelling this checkout has ever sat
  at, because the export rewrites the history as well as the working tree and the history holds
  those exact bytes. And the historical prose in `OPEN-WORK.md` reads as though the new name was
  always in use, because the rename ran over it: the publication on 2026-09-10 was under
  `bose-zonemaster`, and this entry is the anchor for that.

### Security

- A data request can no longer stop the master. `byte_count` is an `int32` from any device that
  can reach TCP 40003, and a negative one made the served size 0. A zero-length read never
  satisfies the serving loop's `while not chunk`, and with the bytes already in the ring that
  loop has no suspension point, so it spun inside the event loop: the clock server, every other
  slave's data channel and the HTTP API stopped with it, recoverable only by killing the process.
  No join was needed to reach it. `channels.chunk_size` now bounds the size at both ends.
- `ipc.encode_frame` enforces `MAX_FRAME_BYTES` on the way OUT. The ceiling was applied to every
  frame read and to none written, so a request naming two billion bytes was answered with the
  whole ring - eight times the limit this master refuses from anyone else, from a request of
  about thirty bytes.

- The XML the master serves is escaped per position: values between tags through `xmlfmt.text`,
  values inside quotes through `xmlfmt.attr`. An escaping helper had existed since the first
  commit with no caller, while a station name reached `<track>`, `<stationName>` and the
  `/notification` the master POSTs to every speaker in the zone unescaped. `status_xml`, the
  reply to every unhandled path, echoed the request path the same way. The `ContentItem` fragment
  a slave sends is still echoed as markup, which is what a real master does.
- `ipc.MAX_FRAME_BYTES` bounds a declared frame. The length prefix is an unsigned 32-bit count and
  the splitter held every byte sent against that promise, on a port that takes connections from
  anyone who can reach the LAN.
- `http_api.MAX_BODY_BYTES` bounds a request body. The read timeout bounded how long a caller
  could take, never how much it could send.
- `clock.CLIENT_TTL_US` evicts a client that stopped asking the time. The clock server kept one
  record per source address it had ever answered, and a UDP source address is a claim rather than
  an identity.
- The public export's detector searches case-insensitively while its rules stay literal, so a name
  written in a case no rule covers is reported instead of shipped. It had been reporting "clean"
  over `ROOM5_IP`, `test_room5_is_refused` and a `room3-room1` capture path.
- The public export no longer ships the developer's home directory, the account that made a commit,
  or a working ledger nobody classified. Three holes in one boundary, each of which let the run
  print "none of the redacted literals anywhere": the rules file declared absolute paths a redacted
  category while carrying rules only for the tree under `/media`, so every path written under the
  home directory shipped; a commit's author and committer live in the commit object, which no text
  rule reads, so the scaffold commit published a machine account's name and the container in its
  address; and the test that refuses an unclassified document read the top level only, so a ledger
  one directory down was created, tracked and shipped without ever being classified. The export now
  collapses every identity onto the repository owner and refuses if one survives, and it reads the
  whole tracked tree when it asks what is classified.
- `config --redact` masks every value that came out of a `.env`, not only the keys whose names look
  like secrets. The library's name list masks `e2e_key` and printed `e2e_host`, `e2e_user` and
  `e2e_speakers` in full, each with the `.env` path beside it, which is the output the documentation
  sends an operator to before pasting anything anywhere. A name list only knows the names somebody
  thought of; where a value came from is a property of the value.
- The export reports which redaction rules fired nowhere in the source. Its detector counted the
  rules file itself, so every rule was guaranteed one hit from its own definition: a rule whose text
  had been retyped - different case, no backticks - reported success while the sentence it was
  written for shipped, and the guard that refuses a source the detector finds nothing in could never
  fire.

### Added

- Every service setting can live in a configuration file, read through `lib_layered_config` in the
  precedence `defaults -> app -> host -> user -> dotenv -> env`, with the command line above all
  six. The deployed unit is unaffected: it names four settings on the command line and those four
  are still what the service uses, whatever any file says. That is the point of keeping the options
  rather than moving the house into a file on the same day, and `tests/test_service_config.py`
  asserts it against the exact argv read off `systemctl cat` on 2026-09-11. Three settings gain a
  home they never had, because they had no option either: `registry_poll_s`, `switch_poll_s` and
  the channel policy.
- `soundtouch-zonemaster-service config` prints every merged value with the layer and file that produced
  it, and `config-deploy --target user|host|app` writes the defaults that ship in the wheel as
  files to edit - the base file and the whole `config.d/` directory beside it, eight in all, each
  reported. Both honour `--json` / `--json-bare` and the repo's exit codes. The service CLI
  became a click group to hold them, invoked without a subcommand so an argv of options alone
  still holds the zone. `--set SECTION.KEY=VALUE` overrides one value for one run; it is reported
  as coming from the command line rather than from the file it replaced, because
  `Config.with_overrides` keeps the original provenance and the command exists to answer exactly
  that question.
- The settings are split by scope into `src/soundtouch_zonemaster/defaultconfig.d/`, one section per
  file, with `defaultconfig.toml` reduced to the header that explains the layers. That is the shape
  both sibling projects use, measured rather than assumed: `bitranox_template_py_cli` and another
  sibling each have an empty base file and one section per `.d` file. The scopes are this program's own
  vocabulary, so `30-registry.toml` holds `[registry]` and is the thing `registry.py` does, and
  each file documents its settings with the default, the effect, when to change it and what that
  costs. The loader merges every file key by key and provenance stays per key, so `config` names
  the exact scope file a value came from, and a deployed `config.d/` file may be deleted or a
  section moved into `config.toml` without changing what the service reads.
- `config.SETTINGS` maps each config path to the record field it fills, because `ServiceOptions` is
  flat and renaming twelve fields to match a file layout would be the tail wagging the dog. It is
  the only enumeration of the settings, so `tests/test_config.py` pins it in BOTH directions: a map
  checked one way can gain an entry for a field nobody added, or miss a field somebody did. The
  same file fails if a shipped value and the field default disagree, if a new field with no default
  reaches the record without a commented example, or if a file stops naming the environment
  variable for one of its own settings. The vendor, app and slug now live in one place and
  `tests/e2e_config.py` imports them instead of repeating them.

- A box is turned down to zero before it is taken into the zone and faded back up once it has let
  go of its own station. A box switched on plays its last station until the zone takes it over -
  4.7 s of it, measured in the flat on 2026-09-08 and heard - so the mute has to reach it before
  the zone document does, and the order is what the test asserts rather than the fact that the
  volume moved. The level it was on is written to the state file before the box is muted, so a
  service that dies mid-join still knows what to put back; a box whose volume cannot be READ is
  joined at its own level instead, because a level that cannot be read is one that cannot be put
  back and a speaker left silently at zero reads as broken hardware.
- A box that was pressed belongs in the zone from the press, rather than from the moment it says
  what it is playing. Measured over three runs, waiting for the box's own `nowPlayingUpdated` cost
  4.45 s, 3.5 s and 0.17 s - not a constant to design around - and all of it is the box playing
  the station nobody asked for. A wake is now a window (`WAKE_WINDOW_S`, 20 s) rather than a single
  frame, bounded by how long taking a box in can wait for the station's first bytes.

- The dialling window can be measured on the person who dials rather than typed by whoever set the
  house up. Next, previous, next, previous in quick succession starts a calibration; the person
  presses the number keys as they normally would; the window becomes the largest gap between two
  presses plus at least 100 ms, a quarter of the gap where that is more, clamped to the measured
  floor and ceiling. It ends by itself when the pressing stops, and the playing channel restarts
  once at the start and once at the end, which is the only way a flat with no screen can be told.
  The result is written to the state file, so it outlives the run that measured it, and it
  outranks `--dial-window-s`. Fewer than three usable gaps changes nothing and says why. The rule
  reproduces the number the house already carries: the eleven gaps M0 measured have a largest of
  398 ms and it returns exactly the 500 ms floor that was picked by hand from them.

- The thumbs steer what next and previous walk. Thumbs down takes the playing channel out of the
  rotation, thumbs up puts it back, and the channel keeps its number so it is still reached by
  pressing it. The last channel in the rotation is refused rather than taken out, because an empty
  rotation leaves the buttons dead from every channel. It replaces a `favourite` flag on `Channel`
  that was written by nothing and read by nothing (user's decision, 2026-09-07, over leaving the
  thumbs dead until file channels arrive and over letting the mark protect a channel from a
  re-seed).

- Every CLI takes `--json` and `--json-bare`, so all eight are drivable by a machine rather than
  only by a person. `--json` prints an indented envelope, `--json-bare` puts it on one line for
  `jq`, and both move progress and warnings to stderr so stdout carries the envelope and nothing
  else. `extract_protos.py` printed its envelope before this and still does, with or without the
  flag. The exit codes are unchanged and stay the contract: 0 done, 1 it ran and the answer is no,
  2 it could not run.
- `zonemaster/placement.py` holds where each slave sits in the stream: the records, the four
  books, and the join and report arithmetic (REPORT.md S7).
- `zonemaster/zonexml.py` builds every XML document a speaker sees, so the escaping rule is
  applied in one place.
- `tests/test_capture_model.py` pins the vocabulary a capture is read in: the reassembler's
  direction keys, the `msg_type` numbers, the clear-text `msg_typename`, and the two wire states
  the analysers branch on.

- `tests/test_export_public.py`, `tests/test_zonemaster_limits.py`,
  `tests/test_zonemaster_xml_escaping.py` and `tests/test_zonemaster_run_loop.py`.
- `zonemaster.__main__.ZonePort`, and `station_source` / `poll_s` / `run_zone` seams, so the run
  loop is tested rather than replaced.
- `capture_model.Capture` carries a run's packets, master, slave and t0 as one value.
- Every CLI names its exit codes in `--help`, and every `zonemaster` flag has help text.
- The export pipeline is tested end to end. `tools/export_public.py:build` takes the repository to
  export as a parameter (defaulting to this one), so the whole run - clone, `git-filter-repo`, and
  the three refusals that read the result - can be driven against a repository built for the
  purpose. Two tests use it: one runs the pipeline and requires a rewritten history that has lost
  the names AND the commits that only touched a private file, and one holds the check that makes
  the word "clean" mean anything - the detector must fire on the SOURCE, or the export refuses.
  Deleting that check previously left all 166 tests green.
- `tests/test_e2e_speakers.py` runs this repository's parsers over documents the real speakers
  served. The development host cannot reach them, so a stdlib-only probe (`tests/e2e/speaker_probe.py`)
  is shipped to a machine on their LAN and returns one JSON envelope. It is GET-only by
  construction, so it makes no sound and is safe on every speaker including the Lifestyle console.
  Marked `integration`: `make test` skips it, `make testintegration` runs it.
- The house-specific settings those tests need live in a gitignored `.env`, read through
  `lib_layered_config` over the committed defaults in `tests/e2e_defaults.toml`, with
  `.env.example` as the tracked placeholder. Addresses are exactly what the public export strips,
  so they must not sit in a tracked file. The defaults are the safe answers: no host, no speakers,
  nothing audible, so a machine without a `.env` reaches no speaker and the tests skip.

### Fixed

- A join that finished is no longer reported as one that did not. `_fade_one_back_up` took its
  entry out of `_fading` before its last step had run, and in that window the pass saw a mute with
  no fade running and turned the box up itself. The level it wrote was the one the fade was about
  to write, so the room sounded right and the only trace was the rescue's own
  `after a join that did not finish` line - the single signal that a service ever died mid-join,
  spent on a run where nothing had, which is why the listening run of 2026-09-08 first read as if
  the fade had never run at all. The last step is inside the `try` now, so the entry stays until
  the box is back where it was.

- The `bandit` stage of `make test` was measuring nothing. bmk runs
  `bandit -r -c pyproject.toml src/<package>`, and this project kept its package at the repository
  root, so every gate reported that stage green over a path that did not exist. Moving to `src/`
  switched it on and it found five things at once: four log lines reading `select from <origin>`,
  which its hardcoded-SQL rule matches, and the `xml.sax.saxutils` import in `xmlfmt.py`. The log
  lines now say `selection from`, which is this project's own word for the thing, and the import
  carries a one-rule carve with the reason beside it, because `escape`, `quoteattr` and `unescape`
  are pure string functions and nothing here parses XML.

- One press fetches one station. A box that named its own source quickly became a member the
  ordinary way and the pass took it in on the REMEMBERED channel; the number it was still dialling
  completed a moment later naming a different one, and every room was switched over - two stations
  for a press that named one, and a stop plus a 3.5 to 4 s re-buffer in every room already playing.
  Nobody is taken in while a number is open, because until it closes there is no channel to be
  right about.
- A box leaving no longer silences the rooms that stay, and switching a second box on no longer
  restarts the house's stream.
- A box the zone left behind is put back by the pass, on the channel the user chose.
- The pings stop without taking the shutdown's own cancellation with them.

- A data request can no longer stall its own audio through the field that was still read as asked.
  `chunk_size` bounded `byte_count` at both ends and was documented as bounding both int32 fields,
  but the serving loop reached past it for the raw `min_byte_count` and handed that to the ring's
  wait. The ring holds eight megabytes and a request may name two billion, so every request from
  that slave spent the whole 20-second `DATA_WAIT_S` timing out with the bytes it asked for already
  in the ring, and the audio stopped while the master logged a timeout and reported nothing wrong.
  `chunk_plan` now returns both sizes as one record, so the call site has no raw field left to
  reach for.
- A playback state the recovered schema has no name for is logged rather than raised. `state` is an
  int32 from any device that can reach the transport port and only 0 to 6 are named; protobuf's
  `Name` raises `ValueError`, which is not in `ENDS_ONE_CONNECTION`, so one frame carrying a 7 ended
  the read loop with a traceback where a log line belongs.
- The clock's table no longer resets the sync of a speaker already in it. Making room ran on every
  datagram, so at the 512-client cap the least-recently-seen record was dropped whether or not the
  packet that arrived was its own - and it was rebuilt empty a moment later, losing the t1/t3 pair
  the next reply is measured against. A speaker that syncs a little less often than its peers is
  exactly the record at the front, and a spoofed-source flood is exactly when the cap is reached.
- A box that stops answering during its fade back up is logged and left to the next pass, like
  every other call to a speaker in that module. Unguarded, it raised out of a task nothing
  retrieves - its `finally` has already removed it from `_fading`, so not even the stand-down waits
  on it - and asyncio printed a traceback of its own to a logger that is not the service's.
- Seeding the channel list is serialised. It is awaited from two tasks that do not wait for each
  other, the pass and the 30-second registry poll, and reading a box's presets is six sequential
  HTTP GETs with an eight-second timeout each. A poll tick landing inside one of those fetches saw
  an empty list, picked the next box, and whichever radio answered LAST wrote its list over the
  other's, in memory and on disk, with nothing logged. It also decided the house's channels by
  which box replied fastest rather than by which was switched on first, which is the stated rule.
- `sync_report.py` reads the byte-shaped `sync` line the master actually emits. `placement.py`
  gained a trailing `(bytes)` on that line before the tool was written, and the tool's regex was
  built from an excerpt of an older run, so it matched neither form in use - every byte-placed
  run's lines were dropped exactly the way an unrelated line is, which is the silent failure the
  tool exists to end. The parser and the emitter are now pinned to each other by a test that drives
  the real master and requires the real parser to read what it wrote.
- `sync_report.py`'s refusal envelope carries `ok`, like every other CLI here. A caller branching on
  `payload["ok"]` got a `KeyError` from this tool alone, on the failure path it was reading the
  field to detect.

- A box that holds more than one transport channel is no longer taken out of the zone by the one
  that ends. A speaker opens several channels from the same address - measured on 2026-09-07,
  every one of the four boxes reached two live at once during a burst of membership changes - and
  the master's registry was keyed by that address, so an older channel closing removed the live
  one with it. The next station change then skipped that box: Room4 played the stream it had
  been left on until that stream stopped, then fell silent with the new station's name on its
  display, while the master wrote a `slave-state` line about it every second. Channels are now
  forgotten by identity, the newest live one is the one control is sent on, and the box's
  placement is dropped only once it has no channel left rather than on any close.

  What a station change cannot do is reach a channel it does not drive, so a superseded channel
  stays on the station it was told about. If the driven channel later ends, that box is driven on
  a channel the zone has left behind; the master keeps it, because the alternative is a box with
  no control channel at all, and says so in a line naming the box and both streams.

- A port somebody else is holding costs one pass instead of the service. The three ports the
  protocol fixes sit inside the kernel's ephemeral range on the machine this runs on, so an
  outbound connection can be holding one when the zone starts; it killed the service three times
  in a row in the flat, losing the observers, the registry and everything the membership rule had
  learned each time. The pass now gives up and the loop comes back on its own a second later,
  which is an unbounded retry that blocks nothing, and the busy port is reported once per edge
  rather than once per pass.

- A slave's data loop stops with its stream instead of waiting on it for ever. From inside the
  wait, a station that is slow and a stream that has ended look identical, so after a zone was
  dissolved the loops went on logging `waiting on stream` every 20 seconds - 168 lines in 28
  minutes, one loop per box that was a member when the zone ended. The ring now carries the fact:
  it is marked ended, the waiters are woken, and the wait raises rather than timing out, while
  still handing over whatever the ring already holds, because ending is not discarding.

- The service no longer reads its own station change as somebody pressing a preset. A box reports
  what it is PLAYING on the same notification channel, in the same frame, as a pressed preset -
  byte for byte identical in the hardest case, because the preset it names is the channel we just
  sent it. So the master changed the stream, every slave reported it, the service read a press,
  and the master changed the stream: 28 station changes in 45 seconds on the first zone this
  service held in the flat, a new stream each time, audible as constant re-buffering in four
  rooms. It fired on every join, so nothing else about that run could be measured.

  `zonemaster/presses.py` holds the rule the run also measured: a box sends a `userActivityUpdate`
  when a HUMAN touched it, and never for a selection we caused - 7 human selections each with one
  4 to 55 ms behind it, 40 echoes with none, and it holds for the remote, for the buttons on the
  box, and for a press of the preset already playing. A selection is a press only when one FOLLOWS
  it, and a selection nobody confirms is replaced by that box's next selection rather than by a
  clock, which is what makes the window a bound rather than a setting: the run reads identically
  at every window between 60 ms and 200 s.

  A wake and a press are the same frames, so which of the two a selection out of standby is
  decided by what the house is doing (user, 2026-09-07): with nothing playing the box somebody
  switched on chooses the channel, and with the house playing it joins the running channel, as the
  membership rule always said it should.

- `StreamSource.stop` no longer swallows the cancellation of whoever is stopping it. `await task`
  raises `CancelledError` for two reasons that cannot be told apart afterwards - the fetch task
  just cancelled has died, or the CALLER is being cancelled, because a cancel of a task that is
  awaiting another task is delivered THROUGH that child. Read as the first, a stop that landed
  inside a station switch left the service's dialling worker alive after its own cancellation, and
  `ZoneService.run`'s `finally` then waited for that worker for ever: the zone was never dissolved
  and real speakers would have been left in a zone whose master had gone. It waits with
  `asyncio.wait` now, which never re-raises what the awaited task raised.
- The two test doubles drop whoever is still connected when they are stopped. `wait_closed` waits
  for every client transport to be gone, so one connection whose client vanished without closing
  its socket held the service suite's teardown for ever - a hang rather than a red, on one to
  three of every four runs. The orphan is what a cancelled HTTP call leaves behind: measured
  2026-09-07 with `httpx.AsyncClient.is_closed` True and the socket still open.

- `analyze_late_join.py --help` printed `analyze_capture.py`'s description.
- `export_public.py` gave every failure exit 1, so "pass --force" and "a real name reached the
  export" were the same answer; it now separates could-not-run (2) from was-refused (1).
- `zonemaster` reported a fatal error on stdout while option errors went to stderr.
- Both channel servers close their connection and await the cancelled ping loop, and catch the
  `DecodeError` a peer sending gibberish raises instead of dying with an unretrieved exception.
- `capture_zone.py` builds its `Tcpdump` objects inside the try that stops them; one that spawned
  before a later one raised kept running on the speaker.
- A room name was in `research/REPORT.md` and in no redaction rule.

### Changed

- No service option is `required` any more. A setting given in no layer at all is still refused at
  startup with exit 2, the code click gave for a missing option, but the message now names every
  missing setting and the other places it could have been put. The loudness that costs: a
  forgotten option used to fail at parse time and now resolves through the configuration layers
  first, so `config` is the command that says which file answered.

- The package is `soundtouch_zonemaster` and it lives under `src/`; the project is `soundtouch-zonemaster`
  (user, 2026-09-08). It was `zonemaster` at the repository root, distributed as
  `soundtouch-multiroom`. The console scripts follow the distribution, so they are now
  `soundtouch-zonemaster` and `soundtouch-zonemaster-service`, and that is also what the JSON envelope's
  `command` carries. Three names deliberately did not move, because they belong to a running
  house rather than to the source: the install directory `/opt/zonemaster`, the state directory
  `/var/lib/zonemaster`, and the unit `soundtouch-multiroom.service`, whose file lives in the
  service host's own repository. The next deploy therefore has to change that unit's `ExecStart`
  and uninstall the old distribution on the machine first, or it starts cleanly on the old code;
  OPEN-WORK rank 22 holds it open.

- `pyright` is told where `src/soundtouch_zonemaster/pb` is. The generated protobuf modules import each
  other by bare proto path, which `pb/__init__.py` makes work at runtime by putting its own
  directory on `sys.path`, and under the src layout the checker finds them no other way. Measured
  on the move: 429 errors without that entry, 0 with it, and 0 on the pre-move layout, so it is
  the layout change rather than a new defect.

- The channel list is seeded from the presets of the first speaker somebody switches on, and
  `--seed-from` is gone with the name it needed (user, 2026-09-07). Every box in the house was
  measured to carry the same six presets, so naming one decided nothing about the content of the
  list - only which box had to be reachable at the first start. A box is asked for its presets
  once: one with nothing on its keys leaves the chance to the next box switched on. An old unit
  still passing `--seed-from` now fails to start rather than starting with the option ignored.

- The dialling window's default is 800 ms rather than 1 s (user, 2026-09-07). It is twice the
  largest gap the M0 run measured between two deliberately fast presses (398 ms), and the floor of
  500 ms and ceiling of 2 s are unchanged.

- **Breaking for a saved command line:** `analyze_capture.py` and `analyze_late_join.py` take
  `--firmware` REPEATED, once per binary, where they took one flag with many values. argparse
  allowed `nargs="+"` and click has no variadic option, so the form changed with the framework.
- **`capture_zone.py` is no longer stdlib-only.** It carries a PEP 723 header instead, and `uv run
  capture_zone.py` resolves its dependencies on the capture host. Two files now have to reach
  it, `capture_zone.py` and `_click.py` beside it, and it needs `uv` and a route to an index the
  first time.
  None of that is verified from the development host.
- The package takes two more runtime dependencies, `rich-click` and `lib_cli_exit_tools`, which
  changes what an installer pulls.
- `zonemaster/master.py` holds the zone itself. Join placement moved to `placement.py` and XML
  rendering to `zonexml.py`; the placement code moved verbatim, and a real run has not yet judged
  the split by its `sync` line.
- `research/capture_model.py` is measured by the coverage gate. The rest of `research/` is still
  excluded, but per file rather than as a directory, because the reason was always per file.
- `capture_zone.Options.speakers` returns a `Speakers` NamedTuple rather than a bare pair. Every
  call site iterates it or tests membership, so all of them keep working.

- `zonemaster/pb/__init__.py` is described as what it is, hand-written package glue, and the lint,
  type and coverage exclusions name the generated files rather than the whole directory.
- The `PLR0912`/`PLR0913`/`PLR0915` waiver over `research/*.py` and `tools/*.py` is gone; the five
  functions that need it carry their own `noqa`, each saying why.
- `station_from_content_item` is the one place a `<ContentItem>` is read.
