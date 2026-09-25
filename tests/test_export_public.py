"""Tests for tools/export_public.py, the tool that builds an anonymized copy of a repository.

The tool prints "clean" and exits 0 when its detector finds nothing. That sentence is worth
exactly as much as the detector's ability to say the opposite, so the tests below spend most of
their effort making it fire: on a name written in a case no rule covers, and on a private file
that survives in history after being dropped from the tip.

Every name here is made up (Pantry, Attic, Cellar). The real list is private, gitignored, and read
only by the tests that say so and skip without it.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import export_public as ep

# Resolved once, absolutely: the checks under test ask git itself rather than a stand-in, so a
# machine without git must fail saying that, not fail somewhere further in looking like a defect.
GIT = shutil.which("git")

SYNTHETIC_RULES = "Pantry==>Room1\nPANTRY==>ROOM1\npantry==>room1\nAttic==>Room2\nCellar==>Room3\n"

# The top-level documents that are part of the software and are MEANT to be published. Everything
# else at the top level is a working file and belongs in ep.PRIVATE_PATHS. Naming the published
# side rather than the private one is deliberate: a new working document then fails the test by
# default, where a new private one would otherwise ship by default.
PUBLISHED_ROOT_DOCS = ("README.md", "CHANGELOG.md", "keymappings.md")

# The directories whose documents are part of the software and are meant to be published, named one
# LEAF at a time: a blanket "docs/" would classify every directory somebody adds under it.
PUBLISHED_DOC_DIRS = ("docs/notes/", "docs/systemdesign/", "research/")

needs_private_list = pytest.mark.skipif(
    not ep.REDACTIONS.is_file(),
    reason=f"{ep.REDACTIONS.name} is private and absent here (CI, a fresh clone); see {ep.EXAMPLE.name}",
)


def _git(repo: Path, *args: str) -> bytes:
    """The single place this file starts a process, so the security carve-out stays one line."""
    if GIT is None:
        pytest.skip("git is not installed; these checks ask git itself rather than a stand-in")
    return subprocess.run([GIT, *args], cwd=repo, check=True, capture_output=True).stdout  # noqa: S603 - argv list, literal args


def _repo(path: Path) -> Path:
    """A real git repository, so the git-backed checks run against git."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "t")
    return path


def _commit(repo: Path, name: str, body: str, message: str = "c") -> None:
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _rules_file(tmp_path: Path, text: str = SYNTHETIC_RULES) -> Path:
    path = tmp_path / "rules-rnhome.txt"
    path.write_text(text, encoding="utf-8")
    return path


def _quiet(_: str) -> None:
    """Swallow the progress prose; these tests read the report and the built repo, not the noise."""
    return None


# --- the rules file -----------------------------------------------------------------------------


def test_a_rule_round_trips_through_parse_and_line() -> None:
    rule = ep.RedactionRule.parse("Pantry==>Room1")
    assert (rule.pattern, rule.replacement) == ("Pantry", "Room1")
    assert rule.line == "Pantry==>Room1"


def test_comments_and_blank_lines_are_not_rules() -> None:
    parsed = ep.parse_rules("# a comment\n\n   \nPantry==>Room1\n")
    assert [r.pattern for r in parsed] == ["Pantry"]


def test_a_line_without_the_separator_is_refused() -> None:
    """git-filter-repo would replace such a line with its own marker, corrupting every file."""
    with pytest.raises(ep.CannotRunError, match="plain-hash-line"):
        ep.parse_rules("Pantry==>Room1\n# comment\nplain-hash-line\n")


def test_an_absent_rules_file_is_refused_naming_the_example(tmp_path: Path) -> None:
    """The list is private, so a fresh clone has none; that must say so, not raise from open()."""
    with pytest.raises(ep.CannotRunError, match=ep.EXAMPLE.name):
        ep.rules(tmp_path / "public_redactions-rnhome.txt")


def test_the_shipped_example_parses_into_rules() -> None:
    """The tracked example shows the format, so it has to BE the format."""
    rules = ep.rules(ep.EXAMPLE)
    assert rules, "the example carries at least one rule"
    assert all(rule.pattern and rule.replacement for rule in rules)


def test_secrets_collapses_case_variants_but_rules_keep_them(tmp_path: Path) -> None:
    """One needle per name for the case-insensitive detector; every case for the literal rewrite."""
    rules = ep.rules(_rules_file(tmp_path))
    assert {"Pantry", "PANTRY", "pantry"} <= {rule.pattern for rule in rules}
    lowered = [s.lower() for s in ep.secrets(rules)]
    assert lowered.count("pantry") == 1


@needs_private_list
def test_every_capitalised_word_rule_covers_all_three_cases() -> None:
    """A rule is literal, the detector is not: a case with no rule REFUSES the export.

    A name reaches a tree as prose (Pantry), as an identifier (PANTRY_IP) and as a lowercase path
    or test name (test_pantry_wakes), so a capitalised word in the private list needs all three.
    """
    patterns = {rule.pattern for rule in ep.rules()}
    words = [p for p in patterns if p.isalpha() and p[0].isupper() and p[1:].islower()]
    missing = sorted(case for word in words for case in (word.upper(), word.lower()) if case not in patterns)
    assert not missing, "cases with no rule in the private list: " + ", ".join(missing)


@needs_private_list
@pytest.mark.local_only
def test_the_checkout_s_own_path_has_a_redaction_rule() -> None:
    """A rule naming a path the checkout no longer sits at redacts nothing, and says nothing.

    Renaming or moving the checkout fails here instead of quietly leaving the new path to whatever
    the broader rules happen to catch. Marked local_only because the answer depends on where THIS
    checkout sits, and a CI runner's path is no path the export will ever see.
    """
    patterns = {rule.pattern for rule in ep.rules()}
    assert str(ep.ROOT) in patterns, (
        f"the checkout sits at {ep.ROOT}, which no rule names; add it and keep the old spellings"
    )


@needs_private_list
@pytest.mark.local_only
def test_the_home_of_the_account_that_runs_this_is_redacted_by_some_rule() -> None:
    """Everything this machine writes down while working sits under the account's home directory.

    Marked local_only because the answer depends on which account runs it, which is the point: it
    asks whether the rules cover THIS developer's home, not whether some literal is present.
    """
    home = str(Path.home())
    redacted = home
    for rule in ep.rules():
        redacted = redacted.replace(rule.pattern, rule.replacement)
    assert redacted != home, f"no redaction rule touches {home}, so every path written under it ships"
    assert Path.home().name not in redacted, f"the account name survives the rules: {redacted}"


# --- classification of what a repository holds --------------------------------------------------


def test_every_document_the_export_ships_is_either_published_or_private() -> None:
    """A working file must not reach the export merely because nobody classified it.

    It asks git for every tracked document, so a working record in a directory nobody thought about
    fails here. Enumerating what is PUBLISHED means the next working document fails on the day it
    is created; enumerating what is private would mean it silently ships and the test stays green.
    """
    tracked = _git(ep.ROOT, "ls-files", "-z", "*.md").decode("utf-8").split("\0")
    # ``is_private`` rather than set membership, because a private entry may name a DIRECTORY and
    # then nothing inside it equals the entry.
    unclassified = sorted(
        name
        for name in tracked
        if name
        and not ep.is_private(name)
        and name not in PUBLISHED_ROOT_DOCS
        and not name.startswith(PUBLISHED_DOC_DIRS)
    )
    assert not unclassified, (
        "tracked documents that are in neither PUBLISHED_ROOT_DOCS, a published directory "
        + str(PUBLISHED_DOC_DIRS)
        + ", nor ep.PRIVATE_PATHS: "
        + ", ".join(unclassified)
        + " - decide which, because the export ships anything not named private"
    )


def test_the_locally_excluded_paths_are_read_from_git_info_exclude(tmp_path: Path) -> None:
    """A path this checkout keeps out of git on this machine alone is private by that act.

    Only literal entries count: a glob such as ``*.log`` names no path the drop list could take, and
    a trailing slash names a directory, which the drop list takes without it.
    """
    repo = _repo(tmp_path / "src")
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("# a comment\n\nNOTES.md\n*.log\n.assistant/\ndocs/draft.md\n", encoding="utf-8")
    assert ep.local_excludes(repo) == ("NOTES.md", ".assistant", "docs/draft.md")


def test_a_repository_without_an_exclude_file_excludes_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "src")
    (repo / ".git" / "info" / "exclude").unlink(missing_ok=True)
    assert ep.local_excludes(repo) == ()


def test_a_private_file_deleted_in_a_later_commit_is_still_found(tmp_path: Path) -> None:
    """A checkout test passes as soon as the tip is clean; history is where the file survives."""
    repo = _repo(tmp_path / "src")
    _commit(repo, "handover.md", "session notes\n")
    _git(repo, "rm", "-q", "handover.md")
    _git(repo, "commit", "-q", "-m", "drop it")
    assert not (repo / "handover.md").exists(), "the tip is clean, which is what makes this a trap"
    assert ep.private_paths_in_history(repo) == ["handover.md"]


def test_a_private_file_that_was_renamed_is_private_under_its_old_name_too(tmp_path: Path) -> None:
    """A path is what the file is called TODAY; the history holds what it used to be called."""
    repo = _repo(tmp_path / "src")
    _commit(repo, "session.md", "session notes for the flat\n")
    _git(repo, "mv", "session.md", "handover.md")
    _git(repo, "commit", "-q", "-m", "name it what it is")
    assert "session.md" in ep.private_paths(repo), "the name the private file used to have is private too"


def test_a_private_file_renamed_away_is_private_under_its_new_name_too(tmp_path: Path) -> None:
    """The same edge read the other way, and this one leaves the content at the TIP."""
    repo = _repo(tmp_path / "src")
    _commit(repo, "handover.md", "session notes for the flat\n")
    _git(repo, "mv", "handover.md", "notes-from-the-session.md")
    _git(repo, "commit", "-q", "-m", "give it a friendlier name")
    assert "notes-from-the-session.md" in ep.private_paths(repo), "a private file stays private when it is renamed"


def test_a_repository_with_no_renames_is_exactly_the_shipped_list(tmp_path: Path) -> None:
    """The control: without a rename edge the answer must not grow, or the closure means nothing."""
    repo = _repo(tmp_path / "src")
    (repo / ".git" / "info" / "exclude").unlink(missing_ok=True)
    _commit(repo, "handover.md", "session notes for the flat\n")
    _commit(repo, "notes.md", "nothing private here\n")
    assert ep.private_paths(repo) == tuple(sorted(ep.PRIVATE_PATHS))


def test_the_export_is_checked_against_the_names_the_source_knew(tmp_path: Path) -> None:
    """Asking the EXPORT for its own aliases would answer the shipped list and nothing else."""
    repo = _repo(tmp_path / "src")
    _commit(repo, "session.md", "session notes for the flat\n")
    assert ep.private_paths_in_history(repo) == [], "the shipped list does not name it, which is the trap"
    assert ep.private_paths_in_history(repo, ("session.md", "handover.md")) == ["session.md"]


def test_a_document_under_a_private_directory_is_found(tmp_path: Path) -> None:
    """A private entry may name a DIRECTORY, and then nothing it holds equals it."""
    repo = _repo(tmp_path / "src")
    _commit(repo, "docs/plans/a-plan.md", "the flat, room by room\n")
    assert ep.private_paths_in_history(repo, ("docs/plans",)) == ["docs/plans/a-plan.md"]
    assert ep.private_paths_in_history(repo, ("docs/plan",)) == [], "a prefix that is not a directory must not match"


# --- the detectors -------------------------------------------------------------------------------


def test_the_detector_can_skip_a_path_that_names_every_literal_by_construction(tmp_path: Path) -> None:
    """The rules file holds every pattern by definition, so counting it made every rule look used."""
    repo = _repo(tmp_path / "src")
    _commit(repo, "rules.txt", "Pantry==>Room1\n")
    assert ep.offenders(repo, ["Pantry"], ep.all_revs(repo)) == {"Pantry": 1}
    assert ep.offenders(repo, ["Pantry"], ep.all_revs(repo), skip=("rules.txt",)) == {}


def test_a_rule_that_fires_nowhere_in_the_source_is_reported() -> None:
    """A rule whose literal no longer matches the tree redacts nothing; the leftover is printed."""
    assert ep.unused(["Pantry", "Attic"], {"Pantry": 3}) == ["Attic"]
    assert ep.unused(["Pantry"], {"Pantry": 1}) == []


def test_the_detector_finds_a_name_written_in_a_case_no_rule_covers(tmp_path: Path) -> None:
    """The whole point: a rule is literal and case-sensitive, so the detector must not be."""
    repo = _repo(tmp_path / "src")
    _commit(repo, "notes.md", "the capture lives in 2026-09-05-cellar-pantry\n")
    found = ep.offenders(repo, ["Cellar"], ep.all_revs(repo))
    assert found == {"Cellar": 1}, "a lowercase spelling must still be reported"


def test_the_detector_reads_commit_messages_too(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "src")
    _commit(repo, "a.txt", "nothing here\n", message="move the Pantry speaker")
    assert ep.offenders(repo, ["Pantry"], ep.all_revs(repo)) == {"Pantry": 1}


def test_the_detector_stays_silent_on_a_clean_repository(tmp_path: Path) -> None:
    """The positive control's partner: it must be able to answer 'nothing here' as well."""
    repo = _repo(tmp_path / "src")
    _commit(repo, "a.txt", "Room1 and Room3 only\n")
    assert ep.offenders(repo, ["Pantry", "Cellar"], ep.all_revs(repo)) == {}


def test_the_detector_takes_a_needle_that_starts_with_a_dash(tmp_path: Path) -> None:
    """A literal is a pattern, never an option: git grep must not read ``-pantry`` as a flag."""
    repo = _repo(tmp_path / "src")
    _commit(repo, "a.txt", "the box is called x-pantry here\n")
    assert ep.offenders(repo, ["-pantry"], ep.all_revs(repo)) == {"-pantry": 1}


def test_the_working_tree_detector_names_file_and_needle(tmp_path: Path) -> None:
    """The name guard's question: which TRACKED file, as it stands on disk, holds which literal."""
    repo = _repo(tmp_path / "src")
    _commit(repo, "a.md", "nothing here\n")
    _commit(repo, "sub/b.py", "x = 1\n")
    (repo / "a.md").write_text("an uncommitted edit names the PANTRY\n", encoding="utf-8")
    (repo / "sub" / "b.py").write_text("attic = 1  # and the cellar\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("Pantry is fine here: git does not carry this file\n", encoding="utf-8")
    found = ep.tracked_offenders(repo, ["Pantry", "Attic", "Cellar", "Garage"])
    assert found == {"a.md": ["Pantry"], "sub/b.py": ["Attic", "Cellar"]}


def test_the_working_tree_detector_stays_silent_on_a_clean_tree(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "src")
    _commit(repo, "a.md", "Room1 and Room2\n")
    assert ep.tracked_offenders(repo, ["Pantry", "Attic"]) == {}


def test_the_working_tree_detector_lets_an_allowed_token_through(tmp_path: Path) -> None:
    """A token that CONTAINS a needle can be public by decision, such as a file-name suffix.

    Only that token is let through: the bare needle beside it is still reported.
    """
    repo = _repo(tmp_path / "src")
    _commit(repo, "a.md", "private files end in -pantry.toml\n")
    _commit(repo, "b.md", "private files end in -pantry.toml, and the pantry box is loud\n")
    found = ep.tracked_offenders(repo, ["pantry"], allow=("-pantry",))
    assert found == {"b.md": ["pantry"]}


def test_the_working_tree_detector_reads_bytes_a_text_decoder_would_refuse(tmp_path: Path) -> None:
    """A fixture holding raw bytes can still carry a name in clear; decoding must not skip it."""
    repo = _repo(tmp_path / "src")
    (repo / "blob.bin").write_bytes(b"\xff\xfe\x00Pantry\x00\xff")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c")
    assert ep.tracked_offenders(repo, ["pantry"]) == {"blob.bin": ["pantry"]}


@pytest.mark.skipif(sys.platform == "win32", reason="git for Windows checks links in as plain files by default")
def test_the_working_tree_detector_reads_a_symlink_as_its_target(tmp_path: Path) -> None:
    """git stores a symbolic link as the path it points at, so that path is what gets published."""
    repo = _repo(tmp_path / "src")
    (repo / "link").symlink_to("/srv/pantry/music")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c")
    assert ep.tracked_offenders(repo, ["pantry"]) == {"link": ["pantry"]}


def test_the_working_tree_detector_skips_a_tracked_file_deleted_on_disk(tmp_path: Path) -> None:
    """A deletion not yet committed leaves nothing on disk to read; it is not an error."""
    repo = _repo(tmp_path / "src")
    _commit(repo, "gone.md", "the Pantry\n")
    (repo / "gone.md").unlink()
    assert ep.tracked_offenders(repo, ["pantry"]) == {}


def test_an_unpublishable_commit_identity_is_reported(tmp_path: Path) -> None:
    """A commit carries its author and committer inside the commit object, which no text rule reads."""
    repo = _repo(tmp_path / "machine")
    _git(repo, "config", "user.name", "builder")
    _git(repo, "config", "user.email", "builder@some-container.local.invalid")
    _commit(repo, "a.txt", "nothing private in the text at all\n")
    assert ep.unpublishable_identities(repo) == ["builder <builder@some-container.local.invalid>"]


def test_a_publishable_commit_identity_is_accepted(tmp_path: Path) -> None:
    """The control: the check has to be able to answer "this one is fine" as well."""
    repo = _repo(tmp_path / "human")
    _commit(repo, "a.txt", "text\n")
    assert ep.unpublishable_identities(repo, allowed=("t <t@example.invalid>",)) == []


# --- the refusals --------------------------------------------------------------------------------


def test_a_clean_result_is_let_through() -> None:
    """The control. Without it the tests below pass against a function that always raises."""
    ep.refuse_unless_clean(damaged="", after={}, survivors=[])


def test_a_rule_applied_as_a_bare_literal_refuses_the_export() -> None:
    """The worst of them: the redaction RAN, destroyed content, and reported success."""
    with pytest.raises(ep.ExportRefusedError, match="bare literal") as excinfo:
        ep.refuse_unless_clean(damaged="notes.md\nREADME.md\n", after={}, survivors=[])
    assert "notes.md" in excinfo.value.detail, "the refusal has to name the files, or nobody can act on it"


def test_a_name_that_survived_the_rewrite_refuses_the_export() -> None:
    with pytest.raises(ep.ExportRefusedError, match="redaction incomplete") as excinfo:
        ep.refuse_unless_clean(damaged="", after={"Pantry": 3, "Attic": 1}, survivors=[])
    assert "Pantry: 3 hits remain" in excinfo.value.detail
    assert "Attic: 1 hits remain" in excinfo.value.detail


def test_a_private_file_still_in_the_history_refuses_the_export() -> None:
    """Dropped from the tip is not dropped from the history, which is what a clone carries."""
    with pytest.raises(ep.ExportRefusedError, match="private files still present") as excinfo:
        ep.refuse_unless_clean(damaged="", after={}, survivors=["NOTES.md", "handover.md"])
    assert "handover.md" in excinfo.value.detail


def test_an_identity_that_survived_the_rewrite_refuses_the_export() -> None:
    """The narrowest refusal: one field of metadata rather than any content."""
    with pytest.raises(ep.ExportRefusedError, match="commit identity") as excinfo:
        ep.refuse_unless_clean(damaged="", after={}, survivors=[], identities=["builder <builder@box.local.invalid>"])
    assert "builder@box.local.invalid" in excinfo.value.detail, "the refusal must name what it found"


def test_a_blanked_rule_is_named_before_the_names_it_missed() -> None:
    """A run that blanked a rule AND left names standing must say the rule was blanked first."""
    with pytest.raises(ep.ExportRefusedError, match="bare literal"):
        ep.refuse_unless_clean(damaged="notes.md\n", after={"Pantry": 1}, survivors=["handover.md"])


# --- the pipeline --------------------------------------------------------------------------------


def test_the_export_refuses_a_source_its_detector_finds_nothing_in(tmp_path: Path) -> None:
    """A detector that cannot fire on the source cannot be believed when it stays silent on the export."""
    repo = _repo(tmp_path / "src")
    _commit(repo, "a.txt", "Room1 and Room3 only\n")
    with pytest.raises(ep.CannotRunError, match="cannot be trusted"):
        ep.build(tmp_path / "public", force=False, say=_quiet, source=repo, rules_file=_rules_file(tmp_path))


@pytest.mark.local_only
def test_the_whole_pipeline_rewrites_the_history_and_drops_the_private_files(tmp_path: Path) -> None:
    """The export end to end: clone, rewrite, and the refusals that read the result.

    Marked local_only because it shells out to git-filter-repo through ``uv tool run``, which
    fetches the tool the first time.
    """
    repo = _repo(tmp_path / "src")
    _commit(repo, "notes.md", "the Pantry speaker feeds the Attic one\n", message="wire up the Cellar")
    _commit(repo, "handover.md", "session notes for the flat\n")
    _git(repo, "rm", "-q", "handover.md")
    _git(repo, "commit", "-q", "-m", "drop the handover")
    rules_file = _rules_file(tmp_path)

    dest = tmp_path / "public"
    report = ep.build(dest, force=False, say=_quiet, source=repo, rules_file=rules_file)

    # git-filter-repo drops a commit that the path filter emptied, so the two commits that only
    # touched handover.md go with it rather than staying behind empty.
    assert report.commits == 1, "the commits that only touched the private file are pruned, not emptied"
    assert report.source_hits >= 3, "Pantry, Attic and Cellar all stand in the source"
    needles = ep.secrets(ep.rules(rules_file))
    assert ep.offenders(dest, needles, ep.all_revs(dest)) == {}, "no private name survives anywhere"
    assert ep.private_paths_in_history(dest) == [], "handover.md is gone from the history, not just the tip"
    assert (dest / "notes.md").read_text(encoding="utf-8") == "the Room1 speaker feeds the Room2 one\n"
    messages = ep.run(["git", "log", "--all", "--format=%B"], dest)
    assert "Room3" in messages, "a commit MESSAGE is rewritten too, not only the blobs"
    assert "Cellar" not in messages

    # The source commits as `t <t@example.invalid>`, which is nobody's published identity: the
    # export collapses every author and committer onto the repository owner.
    assert ep.identities(repo) == ["t <t@example.invalid>"], "the source is what it says it is"
    assert ep.identities(dest) == list(ep.PUBLISHABLE_IDENTITIES), "the export carries one identity"
    assert ep.unpublishable_identities(dest) == []


@pytest.mark.local_only
def test_the_export_drops_a_private_file_under_every_name_it_ever_had(tmp_path: Path) -> None:
    """A private file renamed into place ships under its old name unless the drop list follows it.

    The literal detector finds no name in it, because a private file is private for what it IS,
    and the private-path check asks for the shipped names only. Both would answer clean.
    """
    repo = _repo(tmp_path / "src")
    _commit(repo, "notes.md", "the Pantry speaker feeds the Attic one\n", message="wire up the Cellar")
    _commit(repo, "session.md", "session notes for the flat\n")
    _git(repo, "mv", "session.md", "handover.md")
    _git(repo, "commit", "-q", "-m", "name it what it is")

    dest = tmp_path / "public"
    ep.build(dest, force=False, say=_quiet, source=repo, rules_file=_rules_file(tmp_path))

    shipped = ep.run(["git", "log", "--all", "--name-only", "--format="], dest).split()
    assert "session.md" not in shipped, "the private file shipped under the name it used to have"
    assert not (dest / "session.md").exists()
    assert (dest / "notes.md").exists(), "and the public file is still there, so the drop was not indiscriminate"


@pytest.mark.local_only
def test_the_export_drops_a_path_the_source_excludes_locally(tmp_path: Path) -> None:
    """A file listed in the source's .git/info/exclude but committed in its history is dropped."""
    repo = _repo(tmp_path / "src")
    _commit(repo, "notes.md", "the Pantry speaker\n")
    _commit(repo, "AGENT.md", "instructions for the assistant\n")
    (repo / ".git" / "info" / "exclude").write_text("AGENT.md\n", encoding="utf-8")

    dest = tmp_path / "public"
    ep.build(dest, force=False, say=_quiet, source=repo, rules_file=_rules_file(tmp_path))

    shipped = ep.run(["git", "log", "--all", "--name-only", "--format="], dest).split()
    assert "AGENT.md" not in shipped
    assert (dest / "notes.md").exists()
