"""Destination adapters: backup -> bounded diff -> write -> production-
compatible read-back -> retrieval proof, plus rollback/restore.

Every test runs against isolated fixture homes under tmp_path. Nothing here
may name a live profile path; the adapters cannot even construct one on
their own (home is a required constructor argument).
"""
import copy
import shlex

import pytest

from dream_cycle_v3.adapters.destinations import (
    DestinationHomes, MemoryDestination, ProjectDocDestination,
    PromotionRecord, SkillDestination, _combined_revision,
    adapter_for_destination, list_skills, load_skill, parse_frontmatter,
    read_project_doc_sections, restore_backup, search_warm_memory,
    strip_record_region, upsert_record_region)
from dream_cycle_v3.canonical import record_key_for
from dream_cycle_v3.errors import (ConcurrentRevisionError, DestinationError,
                                   DiffBoundError, ReadBackError,
                                   RetrievalProofError)

RUN_ID = "run-000000000000000000000000000001"


def make_record(destination, subject="python runtime version",
                claim="default python3 is 3.14 via homebrew",
                revision=1, terms=("python", "homebrew")):
    return PromotionRecord(
        candidate_id="candidate-0000000000000001",
        content_revision=revision,
        destination=destination,
        record_key=record_key_for(destination, subject),
        subject=subject,
        claim=claim,
        retrieval_terms=tuple(terms),
        run_id=RUN_ID,
    )


@pytest.fixture
def memory_home(tmp_path):
    home = tmp_path / "memory"
    home.mkdir()
    (home / "MEMORY.md").write_text(
        "# Memory index\n\n"
        "- [existing fact](existing-fact.md) — a pre-existing hook\n",
        encoding="utf-8")
    (home / "existing-fact.md").write_text(
        "---\nname: existing-fact\ndescription: existing fact\n"
        "metadata:\n  type: user\n---\n\n"
        "tyler prefers feature-complete commits\n", encoding="utf-8")
    return home


def full_promote_cycle(adapter, record, backup_dir):
    expected = adapter.snapshot_revision(record)
    backup = adapter.backup(record, backup_dir)
    after = adapter.apply_write(record, expected, backup=backup)
    read_back = adapter.read_back(record)
    proof = adapter.retrieval_proof(record)
    return expected, backup, after, read_back, proof


# -- hot memory ------------------------------------------------------------------

def test_hot_memory_write_readback_retrieval(memory_home, tmp_path):
    adapter = MemoryDestination(memory_home, "hot")
    record = make_record("memory:hot")
    expected, backup, after, read_back, proof = full_promote_cycle(
        adapter, record, tmp_path / "backups")
    assert expected is not None and after != expected

    index = (memory_home / "MEMORY.md").read_text(encoding="utf-8")
    # Pre-existing index content untouched, new line present.
    assert "- [existing fact](existing-fact.md) — a pre-existing hook" in index
    assert "python runtime version" in index
    fact = memory_home / adapter.fact_name(record)
    fields, body = parse_frontmatter(fact.read_text(encoding="utf-8"), str(fact))
    assert fields["description"] == "python runtime version"
    assert record.claim in body
    assert "hot_injection:" in proof
    assert "index line" in read_back


def test_hot_memory_revision_update_replaces_not_duplicates(memory_home, tmp_path):
    adapter = MemoryDestination(memory_home, "hot")
    r1 = make_record("memory:hot")
    full_promote_cycle(adapter, r1, tmp_path / "b1")
    r2 = make_record("memory:hot",
                     claim="default python3 is 3.15 via homebrew", revision=2)
    full_promote_cycle(adapter, r2, tmp_path / "b2")
    index = (memory_home / "MEMORY.md").read_text(encoding="utf-8")
    assert index.count("python runtime version") == 1
    body = (memory_home / adapter.fact_name(r2)).read_text(encoding="utf-8")
    assert "3.15" in body and "3.14" not in body
    assert "rev=2" in body


def test_hot_memory_read_back_fails_on_missing_index_line(memory_home, tmp_path):
    adapter = MemoryDestination(memory_home, "hot")
    record = make_record("memory:hot")
    expected = adapter.snapshot_revision(record)
    backup = adapter.backup(record, tmp_path / "b")
    adapter.apply_write(record, expected, backup=backup)
    # Sabotage: production index loses the line -> read-back must fail.
    (memory_home / "MEMORY.md").write_text("# Memory index\n", encoding="utf-8")
    with pytest.raises(ReadBackError, match="no index line"):
        adapter.read_back(record)
    with pytest.raises(RetrievalProofError):
        adapter.retrieval_proof(record)


# -- warm memory -----------------------------------------------------------------

def test_warm_memory_no_index_line_term_search_retrieval(memory_home, tmp_path):
    adapter = MemoryDestination(memory_home, "warm")
    record = make_record("memory:warm")
    _, _, _, read_back, proof = full_promote_cycle(
        adapter, record, tmp_path / "backups")
    index = (memory_home / "MEMORY.md").read_text(encoding="utf-8")
    assert "python runtime version" not in index
    assert proof.startswith("warm_term_search:")
    hits = search_warm_memory(memory_home, ["homebrew"])
    assert any(p.name == adapter.fact_name(record) for p, _ in hits)


def test_warm_memory_retrieval_fails_without_matching_terms(memory_home, tmp_path):
    adapter = MemoryDestination(memory_home, "warm")
    record = make_record("memory:warm", terms=("zebra-unfindable-term",))
    expected = adapter.snapshot_revision(record)
    backup = adapter.backup(record, tmp_path / "b")
    adapter.apply_write(record, expected, backup=backup)
    with pytest.raises(RetrievalProofError, match="did not return"):
        adapter.retrieval_proof(record)


# -- rollback / restore ------------------------------------------------------------

def test_rollback_restores_byte_identical_state(memory_home, tmp_path):
    adapter = MemoryDestination(memory_home, "hot")
    record = make_record("memory:hot")
    before_index = (memory_home / "MEMORY.md").read_bytes()
    expected = adapter.snapshot_revision(record)
    backup = adapter.backup(record, tmp_path / "backups")
    adapter.apply_write(record, expected, backup=backup)
    assert (memory_home / "MEMORY.md").read_bytes() != before_index
    assert (memory_home / adapter.fact_name(record)).exists()

    restore_backup(backup)
    assert (memory_home / "MEMORY.md").read_bytes() == before_index
    # The fact file did not exist before the write; restore removes it.
    assert not (memory_home / adapter.fact_name(record)).exists()
    assert adapter.snapshot_revision(record) == expected


def test_rollback_command_is_recorded(memory_home, tmp_path):
    adapter = MemoryDestination(memory_home, "hot")
    record = make_record("memory:hot")
    backup = adapter.backup(record, tmp_path / "backups")
    cmd = backup.rollback_command()
    assert cmd.startswith("cp ") and "MEMORY.md" in cmd
    assert "rm -f " in cmd  # the not-yet-existing fact file
    assert backup.rollback_metadata()["entries"]


def test_rollback_command_quotes_single_quote_paths(tmp_path):
    home = tmp_path / "quote'home"
    home.mkdir()
    (home / "MEMORY.md").write_text("# Memory index\n", encoding="utf-8")
    adapter = MemoryDestination(home, "hot")
    backup = adapter.backup(make_record("memory:hot"), tmp_path / "backups")
    command = backup.rollback_command()
    assert shlex.quote(str(home / "MEMORY.md")) in command
    assert "'\"'\"'" in command


# -- concurrency / bounded diff ------------------------------------------------------

def test_concurrent_edit_after_backup_is_refused(memory_home, tmp_path):
    adapter = MemoryDestination(memory_home, "hot")
    record = make_record("memory:hot")
    expected = adapter.snapshot_revision(record)
    backup = adapter.backup(record, tmp_path / "backups")
    # Someone edits the index between backup and write.
    index = memory_home / "MEMORY.md"
    tampered = index.read_text(encoding="utf-8") + "- manual edit\n"
    index.write_text(tampered, encoding="utf-8")
    with pytest.raises(ConcurrentRevisionError, match="revision changed"):
        adapter.apply_write(record, expected, backup=backup)
    # Nothing was written: the concurrent edit stands, no fact file appeared.
    assert index.read_text(encoding="utf-8") == tampered
    assert not (memory_home / adapter.fact_name(record)).exists()


def test_fact_file_owned_by_other_record_is_never_overwritten(memory_home, tmp_path):
    adapter = MemoryDestination(memory_home, "warm")
    record = make_record("memory:warm")
    # A foreign file occupies the record's slot.
    (memory_home / adapter.fact_name(record)).write_text(
        "unrelated content that dc3 does not own\n", encoding="utf-8")
    expected = adapter.snapshot_revision(record)
    backup = adapter.backup(record, tmp_path / "backups")
    with pytest.raises(DiffBoundError, match="not owned by record"):
        adapter.apply_write(record, expected, backup=backup)


def test_bounded_diff_rejects_out_of_region_changes():
    old = ("# doc\n\nuntouchable prose\n\n"
           "<!-- dc3:begin " + "a" * 32 + " rev=1 -->\nbody\n"
           "<!-- dc3:end " + "a" * 32 + " -->\n")
    # A malicious/buggy render that also edits the prose outside its region.
    region = ("<!-- dc3:begin " + "a" * 32 + " rev=2 -->\nnew body\n"
              "<!-- dc3:end " + "a" * 32 + " -->\n")
    new = upsert_record_region(old, "a" * 32, region).replace(
        "untouchable prose", "vandalized prose")
    from dream_cycle_v3.adapters.destinations import assert_bounded_diff
    with pytest.raises(DiffBoundError, match="outside\\s+its own region"):
        assert_bounded_diff(old, new, "a" * 32)
    assert strip_record_region(old, "a" * 32) == "# doc\n\nuntouchable prose\n\n"


# -- skills ---------------------------------------------------------------------

@pytest.fixture
def skills_home(tmp_path):
    home = tmp_path / "skills"
    (home / "hermes-continuity-map").mkdir(parents=True)
    (home / "hermes-continuity-map" / "SKILL.md").write_text(
        "---\nname: hermes-continuity-map\n"
        "description: Continuity architecture map\n---\n\n"
        "## Existing section\n\nHand-written content that must survive.\n",
        encoding="utf-8")
    return home


def test_skill_patch_preserves_existing_content(skills_home, tmp_path):
    adapter = SkillDestination(skills_home, "hermes-continuity-map")
    record = make_record("skill:hermes-continuity-map",
                         subject="store ownership guard",
                         claim="writable opens require the v3 application_id "
                               "or a fresh path")
    _, _, _, read_back, proof = full_promote_cycle(
        adapter, record, tmp_path / "backups")
    fields, body = load_skill(skills_home, "hermes-continuity-map")
    assert fields["description"] == "Continuity architecture map"
    assert "Hand-written content that must survive." in body
    assert "## store ownership guard" in body
    assert proof.startswith("skill_lookup:")
    assert "frontmatter" in read_back


def test_skill_created_when_absent_with_loader_valid_frontmatter(tmp_path):
    home = tmp_path / "skills"
    adapter = SkillDestination(home, "new-procedure")
    record = make_record("skill:new-procedure", subject="restart recovery",
                         claim="check the restart breaker before kickstart")
    expected, _, _, _, _ = full_promote_cycle(adapter, record,
                                              tmp_path / "backups")
    assert expected is None  # fresh destination
    assert list_skills(home) == ["new-procedure"]
    fields, _ = load_skill(home, "new-procedure")
    assert fields["name"] == "new-procedure"


def test_skill_read_back_rejects_malformed_frontmatter(skills_home, tmp_path):
    adapter = SkillDestination(skills_home, "hermes-continuity-map")
    record = make_record("skill:hermes-continuity-map",
                         subject="store ownership guard", claim="a claim")
    expected = adapter.snapshot_revision(record)
    backup = adapter.backup(record, tmp_path / "b")
    adapter.apply_write(record, expected, backup=backup)
    path = skills_home / "hermes-continuity-map" / "SKILL.md"
    path.write_text(path.read_text(encoding="utf-8").replace("---\n", "", 1),
                    encoding="utf-8")
    with pytest.raises(ReadBackError, match="frontmatter"):
        adapter.read_back(record)


def test_invalid_skill_id_refused():
    with pytest.raises(DestinationError, match="invalid skill_id"):
        SkillDestination("/tmp/x", "../escape")


# -- project docs -----------------------------------------------------------------

def test_project_doc_append_and_section_lookup(tmp_path):
    home = tmp_path / "projects"
    (home / "klas-sample").mkdir(parents=True)
    (home / "klas-sample" / "decisions.md").write_text(
        "# klas-sample: decisions\n\n## Prior decision\n\nkeep the old flow\n",
        encoding="utf-8")
    adapter = ProjectDocDestination(home, "klas-sample", "decisions")
    record = make_record("project:klas-sample:decisions",
                         subject="listing dedupe policy",
                         claim="decision: dedupe listings by normalized title "
                               "plus seller id")
    _, _, _, read_back, proof = full_promote_cycle(adapter, record,
                                                   tmp_path / "backups")
    sections = read_project_doc_sections(home, "klas-sample", "decisions")
    headings = [h for h, _ in sections]
    assert "Prior decision" in headings and "listing dedupe policy" in headings
    assert proof.startswith("project_doc_lookup:")
    text = (home / "klas-sample" / "decisions.md").read_text(encoding="utf-8")
    assert "keep the old flow" in text


def test_project_doc_created_when_absent(tmp_path):
    adapter = ProjectDocDestination(tmp_path / "projects", "newproj", "context")
    record = make_record("project:newproj:context", subject="objective",
                         claim="ship the marketplace onboarding")
    full_promote_cycle(adapter, record, tmp_path / "backups")
    sections = read_project_doc_sections(tmp_path / "projects", "newproj",
                                         "context")
    assert sections and sections[0][0] == "objective"


# -- factory / isolation ---------------------------------------------------------

def test_adapter_factory_routing(tmp_path):
    homes = DestinationHomes(memory=tmp_path / "m", skills=tmp_path / "s",
                             projects=tmp_path / "p")
    assert isinstance(adapter_for_destination("memory:hot", homes),
                      MemoryDestination)
    assert isinstance(adapter_for_destination("skill:foo", homes),
                      SkillDestination)
    assert isinstance(
        adapter_for_destination("project:klas-sample:decisions", homes),
        ProjectDocDestination)
    for bad in ("quarantine", "ledger:threads", "memory:cold", "project:x"):
        with pytest.raises(DestinationError, match="no destination adapter"):
            adapter_for_destination(bad, homes)


def test_all_writes_stay_inside_fixture_home(memory_home, tmp_path):
    adapter = MemoryDestination(memory_home, "hot")
    record = make_record("memory:hot")
    for path in adapter.target_paths(record):
        assert path.is_relative_to(memory_home)
    rendered = adapter.render(record)
    assert all(p.is_relative_to(memory_home) for p in rendered)


def test_combined_revision_none_only_when_all_absent(tmp_path):
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    assert _combined_revision([a, b]) is None
    a.write_text("content", encoding="utf-8")
    rev1 = _combined_revision([a, b])
    assert rev1 is not None
    b.write_text("more", encoding="utf-8")
    assert _combined_revision([a, b]) != rev1
