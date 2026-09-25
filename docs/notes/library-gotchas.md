# Library and tooling behaviour this repo has been caught by

Small facts about the libraries and tools this project uses, each one paid for once. This file is
only ever appended to and edited, never rewritten wholesale, so an entry cannot go missing without
a diff saying so.

Add one when a gate, a checker or a library surprises you and the surprise was not your code.

## Types and models

- `enum.StrEnum` is a `str` subclass, so `json.dumps` writes a member as its bare value.
- `model_dump_json(indent=2)` is byte-identical to `json.dumps(obj, indent=2)` for a plain model.
- Pydantic wraps a `ValueError` raised inside a validator into a `ValidationError`, but lets any
  OTHER exception type propagate unchanged. That is how a validator carries a distinct exit code.
- Swapping a frozen dataclass for a Pydantic `BaseModel` breaks positional construction at every
  call site, because `BaseModel.__init__` is keyword-only.
- A `dataclass` field using `field(default_factory=dict)` infers as partially unknown under pyright
  strict; a plain class with a typed `__init__` is the way out.

## pyright

- Running `pyright <paths>` REPLACES the config's include list, so a hand-run over a subset reads
  clean while the gate fails. Run bare `pyright`.
- pyright reads `# type: ignore[rule]` as a BARE `# type: ignore`; its own form is
  `# pyright: ignore[rule]`.
- A `cast("X", y)` backed by a `Protocol` is the typed-facade pattern a strict
  data architecture PRESCRIBES, not a violation of it.
- `reportPrivateUsage` fires when a test calls a `_private` method of the class under test. That is
  usually a sign the test should drive the public seam, not that the rule should be silenced.

## protobuf

- `zonemaster/pb/__init__.py` ALIASES its generated modules, so `from .pb.audio import X` does not
  resolve. Use `from .pb import audio`.
- `descriptor_pb2.FileDescriptorProto` exposes no `*_FIELD_NUMBER` constants.
- When a checker floods a protobuf codebase with unknown types, generate the `.pyi` stubs first.

## click and rich-click

- click has NO variadic option: `nargs="+"` becomes a REPEATED flag, so every caller that built the
  old argparse form has to change in the same commit.
- `click.Path` defaults to `readable=True`, so a missing file becomes a click `UsageError` (exit 2,
  prose on stderr, empty stdout) BEFORE any model validator sees it.
- The rich-click decorators are partially typed, one pyright-strict error per `@option`; the
  `_click.py` facade in each runnable area is the answer.

## pytest and coverage

- `pytestmark = pytest.mark.asyncio` at module level warns on every SYNC test in that file.
- `coverage report --fail-under` exits 2, and bmk reports it under a `[pytest] (exit code: 2)`
  label, which reads like a test failure and is not one.
- Moving an import into a `TYPE_CHECKING` block drops that module to 0 percent coverage.
- bmk's `[tool.scripts.test] src-path` defaults to `"src"`; a root-layout repo must set `"."`.

## git and the export

- An IDE with the checkout open (PyCharm) auto-stages newly created files: they appear as `A` or
  `AM` with no `git add`, while edited files stay unstaged.
- git-filter-repo's `--replace-text` rules file has NO comment syntax, so a comment line becomes a
  replacement rule.
- The export's redaction rules are case-SENSITIVE; its detector and the name guard are
  case-INSENSITIVE, on purpose, so they catch a spelling no rule covers.

## AfterTouch, the replacement service in the container

- Its API lives under `/api/setup/` and `/api/mgmt/`, NOT at the paths a person guesses. Eleven
  guessed spellings (`/devices`, `/api/devices`, `/v1/devices`, `/speakers` and the rest) answer
  404, and concluding from that that there is no device list is wrong: `/api/setup/devices` is
  right there, unauthenticated on the loopback, returning JSON with `device_id`, `name`,
  `ip_address`, `mac_address`, `product_code` and `account_id`. The `/api/mgmt/` half needs
  credentials and answers 401.
- The direct instrument for its routes is the binary, not a path sweep: its setup page is compiled
  in, so `strings soundtouch-service | grep -iE "/(device|account|api)[a-zA-Z0-9/_{}-]*"` prints
  every path the page fetches. Its log also prints `[UNHANDLED] GET <path>` for a miss, which tells
  a wrong path from a broken service but says nothing about what the right one is.
- `product_code` distinguishes `Lifestyle` from `SoundTouch`, which is how a console is told from a
  speaker without hard-coding an address.
- Its on-disk tree under the data bind mount is NOT the same answer as the endpoint: it keeps a
  stale `default` account directory from the migration, so a reader of the files sees one speaker
  twice while the endpoint reports it once.
- It finds the speakers by SSDP/UPnP and logs every rejection with its reason, so its log answers
  "did it see this box" without any probing of your own.
- Its stored `Presets.xml` per device is NOT a substitute for asking the box. Measured 2026-09-07:
  five of six speakers had the same six presets stored in the same order, and the sixth had no
  `Presets.xml` at all while the box itself answered `/presets` with all six. Anything seeding a
  list from presets reads the SPEAKER, which is what `station_from_speaker_preset` already does.

## Other

- scapy 2.7 ships `py.typed` and exports `IP`/`TCP`/`UDP` from `scapy.layers.inet`.
