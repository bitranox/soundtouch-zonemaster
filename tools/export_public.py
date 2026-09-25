#!/usr/bin/env python3
"""Build an anonymized export of this repository, and find private names in its tracked files.

The export is a separate clone: every blob and every commit message is run through the private
name list ``tools/public_redactions-rnhome.txt``, the private working files are dropped from the
whole history, and the result is verified - the same detector must FIRE on the source and stay
SILENT on the export, so a redaction that quietly did nothing cannot pass.

The list is gitignored and exists only on the development machine; ``tools/public_redactions.example.txt``
shows its format. ``tracked_offenders`` is the second reader of the same list: the name guard
(``tests/test_no_private_names.py``) asks it which tracked file holds which private literal.

Usage:
    uv run tools/export_public.py --dest /tmp/soundtouch-zonemaster-public

Exit 0 the export is clean, 1 a check refused it, 2 it could not be attempted. The two are
worth telling apart: 1 means a real name or a private file reached the export, 2 means the
run never got that far.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable  # runtime: the Emit alias below is evaluated at import
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import rich_click as click
from _click import current_context, option, run_cli
from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
REDACTIONS = ROOT / "tools" / "public_redactions-rnhome.txt"
"""The private name list. Gitignored: a fresh clone has none, and every reader must say so."""
EXAMPLE = ROOT / "tools" / "public_redactions.example.txt"
"""The tracked example of the list's format, with made-up entries."""
LEGACY_REDACTIONS = "tools/public_redactions.txt"
"""The name the list had while it was tracked; older history holds it there, with every name in it."""
COMMAND = "export_public"

Emit = Callable[[str], None]
"""Where progress prose goes: stdout when a human reads it, stderr when a machine does."""


class OutputMode(StrEnum):
    """How a result is rendered. The two JSON modes differ only in whether they are indented."""

    HUMAN = "human"
    JSON = "json"
    JSON_BARE = "json-bare"


class ExportReport(BaseModel):
    """What one successful export produced."""

    dest: str
    commits: int
    files: int
    redactions: int
    source_hits: int
    unused_rules: list[str] = []


class ExportEnvelope(BaseModel):
    """The machine-readable result this tool prints on success."""

    ok: bool
    command: str
    data: ExportReport
    skipped: list[str] = []


class ErrorEnvelope(BaseModel):
    """The machine-readable result on failure; ``error`` is the class name, to branch on."""

    ok: bool = False
    command: str
    error: str
    message: str


class ExportRefusedError(Exception):
    """A check refused the export (exit 1), as distinct from not being able to run it (exit 2)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# Working files that describe how this house is run or how the work is driven, not the software.
# The records of measurements taken in the house (docs/measurements, docs/plans) are here too: the
# room name is what their sentences are ABOUT, so rewriting it does not make them publishable.
# Published code cites them by name as the provenance of a measured constant.
#
# Paths the source repository lists in its own ``.git/info/exclude`` are added at run time
# (:func:`local_excludes`), so a file kept out of git on the development machine alone is dropped
# from the history as well, without this list having to name it.
PRIVATE_PATHS = (
    "OPEN-WORK.md",
    "handover.md",
    LEGACY_REDACTIONS,
    "docs/measurements",
    "docs/plans",
    "research/REPORT.md",
)
"""Paths the export drops from the history. A FILE or a DIRECTORY: git-filter-repo's ``--path``
takes either, and :func:`is_private` is what keeps the checks that read the result able to as
well."""

# Who the export says wrote it. A commit's author and committer live in the commit object, which no
# text replacement reaches, so a commit made by a machine account publishes that account's name and
# the host in its address. Every identity is collapsed onto this one, the repository owner's.
PUBLISHED_NAME = "Robert Nowotny"
PUBLISHED_EMAIL = "rnowotny1966@gmail.com"
PUBLISHABLE_IDENTITIES = (f"{PUBLISHED_NAME} <{PUBLISHED_EMAIL}>",)

FILTER_REPO = ("uv", "tool", "run", "--from", "git-filter-repo", "git-filter-repo")


class CannotRunError(Exception):
    """The export could not be attempted, as distinct from being refused by a check.

    Bad usage, a rule file that would corrupt the repository or is absent, a git command that
    failed. These exit 2, apart from the checks' exit 1, so a caller can tell "pass --force" from
    "a real name reached the export".
    """


def run(cmd: list[str] | tuple[str, ...], cwd: Path) -> str:
    """Run a command, raising with its output if it fails."""
    done = subprocess.run(cmd, cwd=cwd, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0:
        msg = f"{' '.join(cmd)} failed ({done.returncode}):\n{done.stdout}\n{done.stderr}"
        raise CannotRunError(msg)
    return done.stdout


@dataclass(frozen=True)
class RedactionRule:
    """One ``pattern==>replacement`` rule, split at the boundary that reads the file.

    Parsed into its two fields once, rather than carried as an opaque line that every consumer
    has to re-split: ``secrets()`` needs the pattern alone, git-filter-repo needs the whole line.
    """

    pattern: str
    replacement: str

    @classmethod
    def parse(cls, text: str) -> RedactionRule:
        """One stripped, non-comment line of the redactions file."""
        pattern, _, replacement = text.partition("==>")
        return cls(pattern=pattern, replacement=replacement)

    @property
    def line(self) -> str:
        """The rule as git-filter-repo's --replace-text file wants it back."""
        return f"{self.pattern}==>{self.replacement}"


def parse_rules(text: str) -> list[RedactionRule]:
    """The redaction rules in ``text``, without the commentary.

    git-filter-repo has no comment syntax in a --replace-text file: EVERY non-empty line is a
    literal, and one without ``==>`` is replaced with its own marker. A file with a bare ``#`` line
    therefore rewrites every ``#`` in the repository, which corrupts markdown and Python alike.
    """
    out: list[RedactionRule] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "==>" not in stripped:
            msg = f"redaction line without '==>' would blank-replace it: {stripped!r}"
            raise CannotRunError(msg)
        out.append(RedactionRule.parse(stripped))
    return out


def rules(path: Path = REDACTIONS) -> list[RedactionRule]:
    """The redaction rules in the private name list, or in the file named instead.

    The list is gitignored, so its absence is the normal state of a fresh clone. That is refused by
    name, pointing at the example, rather than left to surface as a bare open() traceback.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        msg = f"{path} does not exist; it is private to the development machine, copy {EXAMPLE.name} to start one"
        raise CannotRunError(msg) from None
    return parse_rules(text)


def secrets(rules_in: Sequence[RedactionRule] | None = None) -> list[str]:
    """The literals the export must not contain, one per distinct spelling.

    Case variants collapse to a single needle because the detector searches case-insensitively;
    the rules file still needs each case on its own line, because the replacement is literal.
    ``rules_in`` defaults to the private name list.
    """
    distinct: dict[str, str] = {}
    for rule in rules() if rules_in is None else rules_in:
        distinct.setdefault(rule.pattern.lower(), rule.pattern)
    return list(distinct.values())


def is_private(path: str, paths: Collection[str] = PRIVATE_PATHS) -> bool:
    """Whether ``path`` IS one of ``paths`` or sits under one of them.

    An entry may name a directory, and then nothing it holds equals it: a check comparing path
    strings finds no touched path equal to "docs/plans" and reports clean while every plan in it
    shipped. The separator is required, so "docs/plan" does not match "docs/plans/a.md" - a
    prefix that stops mid-name is not a directory.
    """
    return any(path == entry or path.startswith(entry + "/") for entry in paths)


def local_excludes(repo: Path) -> tuple[str, ...]:
    """The literal paths ``repo`` keeps out of git on its own machine, from ``.git/info/exclude``.

    Only literal entries: a glob names no path the drop list could take. A trailing slash marks a
    directory, which git-filter-repo's ``--path`` takes without it.
    """
    try:
        text = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    entries: list[str] = []
    for line in text.splitlines():
        entry = line.strip()
        if not entry or entry.startswith(("#", "!")) or any(ch in entry for ch in "*?["):
            continue
        entries.append(entry.strip("/"))
    return tuple(entries)


def rename_edges(repo: Path) -> list[tuple[str, str]]:
    """Every rename git can see in this history, as ``(old, new)`` pairs.

    ``-M`` is passed although rename detection is git's default for this output, because the
    default is a CONFIG value: a repository or a user with ``diff.renames=false`` would otherwise
    make the export quietly stop following renames, and nothing would say so.

    ``core.quotePath=false`` for the reason the whole file exists: a path with a non-ASCII
    character is printed quoted and escaped by default, so it would not compare equal to the name
    it actually has and the edge would be dropped.
    """
    out = run(["git", "-c", "core.quotePath=false", "log", "--all", "--name-status", "--format=", "-M"], repo)
    edges: list[tuple[str, str]] = []
    for line in out.splitlines():
        status, _, paths = line.partition("\t")
        if not status.startswith("R"):
            continue
        old, tab, new = paths.partition("\t")
        if tab:  # the separator, not the tail: a line without it is not a rename pair
            edges.append((old, new))
    return edges


def private_paths(repo: Path) -> tuple[str, ...]:
    """Every name a private file has ever been known by in ``repo``.

    A path is what a file is called TODAY, and the drop list removes blobs AT those paths. So a
    file renamed into place leaves every earlier commit's content at the old name, and a private
    file renamed AWAY leaves it at the tip under the new one - in both cases somewhere no rule
    names and no check looks. Neither is caught by anything else: the literal detector reads the
    file and finds no house name in it, because a private file is private for what it IS and not
    for carrying a word off a list.

    So the answer is the closure over the rename edges: start from the shipped list and the
    source's local excludes, and pull in the other end of every edge that already has one end in
    it, until it stops growing. Both directions on purpose, because a rename is one edge whichever
    way it is read.
    """
    edges = rename_edges(repo)
    names = {*PRIVATE_PATHS, *local_excludes(repo)}
    growing = True
    while growing:
        growing = False
        for old, new in edges:
            if is_private(old, names) != is_private(new, names):
                names |= {old, new}
                growing = True
    return tuple(sorted(names))


def private_paths_in_history(repo: Path, paths: Collection[str] = PRIVATE_PATHS) -> list[str]:
    """Any private path still reachable anywhere in the history, not merely absent from the tip.

    A checkout-only test passes as soon as the newest commit is clean, which is the one thing
    ``--invert-paths`` gets right even when it has silently skipped an older commit.

    ``paths`` is handed in by the export so that the EXPORT is checked against the names the
    SOURCE knew. Asking the export for its own aliases would answer with the shipped list and
    nothing else: the filter takes the new name out, which takes the rename edge with it, so a
    repository that still holds the old name can no longer say that it was ever the same file.
    """
    touched = set(
        run(["git", "-c", "core.quotePath=false", "log", "--all", "--name-only", "--format="], repo).splitlines()
    )
    return sorted(path for path in touched if path and is_private(path, paths))


def all_revs(repo: Path) -> list[str]:
    """Every commit in the repository, oldest first."""
    return run(["git", "rev-list", "--all"], repo).split()


def offenders(repo: Path, needles: list[str], revs: list[str], *, skip: Sequence[str] = ()) -> dict[str, int]:
    """How often each needle survives anywhere in that repo's history (blobs and messages).

    The search is case-INSENSITIVE while the redaction rules are literal and case-sensitive, so a
    name written in a case no rule covers is reported rather than passed. That asymmetry is the
    point: matching the detector to the rules would make the detector agree with them by
    construction, and it would have nothing left to catch.

    ``skip`` excludes paths that hold every needle by construction - the rules file names all of
    them on its own lines. Counting it gave every rule a guaranteed hit from its own definition, so
    a rule whose literal had stopped matching the tree still reported one and the run still printed
    that every redaction fired. The count means something only with that file out of it.
    """
    pathspec = ["--", ".", *(f":(exclude){path}" for path in skip)] if skip else []
    messages = run(["git", "log", "--all", "--format=%B"], repo).lower()
    found: dict[str, int] = {}
    for needle in needles:
        hits = subprocess.run(
            ["git", "grep", "-c", "-i", "-F", "-e", needle, *revs, *pathspec],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout
        n = len([ln for ln in hits.splitlines() if ln.strip()]) + messages.count(needle.lower())
        if n:
            found[needle] = n
    return found


def _tracked_text(repo: Path, name: str) -> str | None:
    """A tracked file's content as it stands on disk, lowered; None when the working tree lacks it.

    Decoded as latin-1, which maps every byte to one character: a fixture holding raw bytes is read
    whole rather than skipped, and an ASCII literal inside it is found exactly as ``git grep`` would.
    A symbolic link is read as its target, which is what git stores for it.
    """
    path = repo / name
    if path.is_symlink():
        return str(path.readlink()).lower()
    if not path.is_file():
        return None  # deleted in the working tree, or a submodule
    return path.read_bytes().decode("latin-1").lower()


def tracked_offenders(repo: Path, needles: Sequence[str], *, allow: Sequence[str] = ()) -> dict[str, list[str]]:
    """Which TRACKED file, as it stands in the working tree, holds which needle.

    The name guard's question, as opposed to :func:`offenders`, which reads committed history: an
    edit that has not been committed yet is exactly what the guard must stop. Case-insensitive for
    the reason given there. ``allow`` lists tokens that contain a needle and are public by decision
    (a file-name suffix, say); they are removed before the search, so the bare needle beside one is
    still reported.
    """
    names = run(["git", "-c", "core.quotePath=false", "ls-files", "-z"], repo).split("\0")
    lowered = [(needle, needle.lower()) for needle in needles]
    found: dict[str, list[str]] = {}
    for name in sorted(n for n in names if n):
        text = _tracked_text(repo, name)
        if text is None:
            continue
        for token in allow:
            text = text.replace(token.lower(), "")
        hits = [needle for needle, low in lowered if low in text]
        if hits:
            found[name] = hits
    return found


def unused(needles: Sequence[str], found: Mapping[str, int]) -> list[str]:
    """The needles the detector did not find in the source at all.

    Not a refusal: a rule may name a path the checkout was called before, kept on purpose against
    the name coming back. But a rule that fires nowhere is either that, or a literal that has
    quietly stopped matching the text it was written for, and the second one ships the very
    sentence it was meant to remove. Printing the list is what lets a reader tell which they are
    looking at.
    """
    return [needle for needle in needles if needle not in found]


def identities(repo: Path) -> list[str]:
    """Every author and committer identity in that repo's history, deduplicated and sorted."""
    out = run(["git", "log", "--all", "--format=%an <%ae>%n%cn <%ce>"], repo)
    return sorted({line.strip() for line in out.splitlines() if line.strip()})


def unpublishable_identities(repo: Path, allowed: Collection[str] = PUBLISHABLE_IDENTITIES) -> list[str]:
    """The identities in that repo's history that are not the published one.

    A commit made by a machine account carries that account's name, and an address naming the
    host it ran on, in the commit object itself. No blob and no commit message holds either
    string, so only this check can see it.
    """
    return [identity for identity in identities(repo) if identity not in allowed]


def refuse_unless_clean(
    *, damaged: str, after: Mapping[str, int], survivors: Sequence[str], identities: Sequence[str] = ()
) -> None:
    """The four refusals standing between this repository and a public one, in one place.

    Separated from the pipeline because inside it they cannot be reached: making any of them fire
    needs git-filter-repo to have done its job WRONG, which no test can arrange. Here they take the
    observations as arguments, and the tests can hand them each answer, including the one the real
    pipeline must never produce.

    The order is deliberate and is the order of how badly the reader is being misled. A blanked
    rule (``damaged``) means the redaction ran and destroyed content while reporting success, so it
    is named before the count of what it missed. ``identities`` comes last because it is the
    narrowest: one field of one commit's metadata rather than any of the content.
    """
    if damaged.strip():
        raise ExportRefusedError("a redaction rule was applied as a bare literal", damaged.strip())
    if after:
        detail = "; ".join(f"{needle}: {n} hits remain" for needle, n in sorted(after.items()))
        raise ExportRefusedError("redaction incomplete", detail)
    if survivors:
        raise ExportRefusedError("private files still present", ", ".join(survivors))
    if identities:
        raise ExportRefusedError("a commit identity that is not publishable", ", ".join(identities))


def build(dest: Path, *, force: bool, say: Emit, source: Path = ROOT, rules_file: Path = REDACTIONS) -> ExportReport:
    """Build the export at ``dest`` and verify it, or raise.

    ``say`` takes the progress prose. In a machine-readable mode it goes to stderr, so the
    parsed stream on stdout holds the envelope and nothing else.

    ``source`` is the repository to export, and defaults to this one. It is a parameter so that
    the pipeline below - the clone, the rewrite, and the three refusals that guard it - can be
    run end to end against a repository built for the purpose. ``rules_file`` is the name list,
    the private one unless a test hands in its own.
    """
    if dest.exists():
        if not force:
            msg = f"{dest} exists; pass --force to replace it"
            raise CannotRunError(msg)
        shutil.rmtree(dest)

    rule_set = rules(rules_file)
    needles = secrets(rule_set)
    # The list names every needle by construction, under its current name and the one it had while
    # tracked; counting it would give every rule a hit from its own definition.
    before = offenders(source, needles, all_revs(source), skip=(str(REDACTIONS.relative_to(ROOT)), LEGACY_REDACTIONS))
    if not before:
        msg = "the detector found nothing in the SOURCE repo, so it cannot be trusted here"
        raise CannotRunError(msg)
    source_hits = sum(before.values())
    say(f"source carries {source_hits} hits across {len(before)} of {len(needles)} redactions")
    leftovers = unused(needles, before)
    if leftovers:
        say(f"{len(leftovers)} rules fire nowhere in the source: {', '.join(leftovers)}")

    say(f"cloning into {dest}")
    run(["git", "clone", "--no-local", "--quiet", str(source), str(dest)], source)

    # Every name each private file has ever had, not the shipped list: a file renamed at any point
    # keeps its content at the other name, where the drop list would not reach it and no check
    # would look for it.
    private = private_paths(source)
    drops: list[str] = []
    for path in private:
        drops += ["--path", path]
    run([*FILTER_REPO, "--force", "--invert-paths", *drops], dest)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write("\n".join(rule.line for rule in rule_set) + "\n")
        expressions = fh.name
    run(
        [
            *FILTER_REPO,
            "--force",
            "--replace-text",
            expressions,
            "--replace-message",
            expressions,
            # The one channel a text rule cannot reach: the author and committer of every commit.
            "--name-callback",
            f'return b"{PUBLISHED_NAME}"',
            "--email-callback",
            f'return b"{PUBLISHED_EMAIL}"',
        ],
        dest,
    )
    Path(expressions).unlink()

    dest_revs = all_revs(dest)

    # filter-repo's own marker: if it appears, a rule was read as a bare literal and blanked it.
    marker = "*" * 3 + "REMOVED" + "*" * 3
    damaged = subprocess.run(
        ["git", "grep", "-l", "-F", marker, *dest_revs],
        cwd=dest,
        check=False,
        capture_output=True,
        text=True,
    ).stdout
    refuse_unless_clean(
        damaged=damaged,
        after=offenders(dest, needles, dest_revs),
        survivors=private_paths_in_history(dest, private),
        identities=unpublishable_identities(dest),
    )

    files = len(run(["git", "ls-files"], dest).splitlines())
    return ExportReport(
        dest=str(dest),
        commits=len(dest_revs),
        files=files,
        redactions=len(needles),
        source_hits=source_hits,
        unused_rules=leftovers,
    )


def _render(report: ExportReport, mode: OutputMode) -> None:
    """Print the success result in the requested shape."""
    if mode is OutputMode.HUMAN:
        print(
            f"clean: {report.commits} commits, {report.files} files, "
            f"none of the {report.redactions} redacted literals anywhere"
        )
        if report.unused_rules:
            print(
                f"note: {len(report.unused_rules)} rules fire nowhere in the source: {', '.join(report.unused_rules)}"
            )
        print(f"review it with:  git -C {report.dest} log --stat | less")
        return
    envelope = ExportEnvelope(ok=True, command=COMMAND, data=report)
    print(envelope.model_dump_json(indent=2 if mode is OutputMode.JSON else None))


def _render_error(exc: Exception, mode: OutputMode) -> None:
    """Print the failure in the requested shape; prose goes to stderr, JSON stays on stdout."""
    if mode is OutputMode.HUMAN:
        print(f"error: {exc}", file=sys.stderr)
        return
    envelope = ErrorEnvelope(command=COMMAND, error=type(exc).__name__, message=str(exc))
    print(envelope.model_dump_json(indent=2 if mode is OutputMode.JSON else None))


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@option("--dest", required=True, type=click.Path(path_type=Path), help="where to build the export (must not exist)")
@option("--force", is_flag=True, help="remove --dest first if it exists")
@option("--json", "as_json", is_flag=True, help="print a JSON envelope instead of prose")
@option("--json-bare", "as_json_bare", is_flag=True, help="as --json, on one line for jq")
def cli(*, dest: Path, force: bool, as_json: bool, as_json_bare: bool) -> None:
    """Build the redacted public export of this repository and verify it."""
    mode = OutputMode.JSON_BARE if as_json_bare else (OutputMode.JSON if as_json else OutputMode.HUMAN)
    # Progress is a diagnostic, never part of the parsed stream (it goes to stderr in JSON modes).
    say: Emit = print if mode is OutputMode.HUMAN else partial(print, file=sys.stderr)
    ctx = current_context()
    try:
        report = build(dest, force=force, say=say)
    except ExportRefusedError as exc:
        _render_error(exc, mode)
        ctx.exit(1)
    except CannotRunError as exc:
        _render_error(exc, mode)
        ctx.exit(2)
    _render(report, mode)
    ctx.exit(0)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; ``lib_cli_exit_tools`` maps an unexpected exception onto an exit code."""
    return run_cli(cli, argv=argv, prog_name="export_public")


if __name__ == "__main__":
    raise SystemExit(main())
