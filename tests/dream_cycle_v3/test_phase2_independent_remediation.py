"""Regressions for the 2026-07-12 independent-review blockers.

1. Candidate row binding: the stored (candidate_id, content_revision) row is
   the only promotion authority; a caller object whose content drifts from
   that row is refused fail-closed before any policy, byte, receipt, status,
   routing, or supersession decision.
2. Ingest status invariant: ingest_candidate only accepts legitimate initial
   lifecycle states; routed/validated/promoted/superseded/rejected/expired
   cannot be imported wholesale (a receiptless 'promoted' row in particular).
3. Snapshot/read symlink confinement: every adapter read surface refuses a
   symlinked target or parent component instead of reading bytes outside the
   explicit destination home.

All homes and stores are throwaway tmp_path fixtures; nothing touches live
profile state.
"""
import copy
import json

import pytest
from .conftest import NOW_ISO, make_manifest_for_run

from dream_cycle_v3.adapters.destinations import (DestinationHomes,
                                                  MemoryDestination,
                                                  ProjectDocDestination,
                                                  PromotionRecord,
                                                  SkillDestination,
                                                  _combined_revision,
                                                  load_skill,
                                                  read_project_doc_sections,
                                                  search_warm_memory)
from dream_cycle_v3.canonical import record_key_for
from dream_cycle_v3.errors import (CandidateBindingError, CandidateStateError,
                                   DestinationError)
from dream_cycle_v3.promotion import (load_authoritative_candidate,
                                      promote_candidate_via_adapter,
                                      promote_with_homes)
from .test_contracts import VALID_CANDIDATE
from .test_promotion import ingest


@pytest.fixture
def homes(tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "MEMORY.md").write_text("# Memory index\n", encoding="utf-8")
    return DestinationHomes(memory=memory, skills=tmp_path / "skills",
                            projects=tmp_path / "projects")


@pytest.fixture
def seeded(store, sample_projects):
    manifest = make_manifest_for_run()
    store.record_run(manifest, "/tmp/manifest.json", NOW_ISO)
    for p in sample_projects:
        store.upsert_project(p, NOW_ISO)
    return manifest


def snapshot_home(home):
    if not home.is_dir():
        return {}
    return {p.name: (p.read_bytes() if p.is_file() else "<non-file>")
            for p in home.rglob("*") if not p.is_symlink()}


def make_record(destination="memory:warm", subject="symlink snapshot"):
    return PromotionRecord(
        candidate_id="candidate-0000000000000001", content_revision=1,
        destination=destination,
        record_key=record_key_for(destination, subject), subject=subject,
        claim="safe claim", retrieval_terms=("safe",),
        run_id="run-000000000000000000000000000001")


# -- blocker 1: the stored candidate row is the promotion authority ----------

def test_caller_substituted_content_is_refused_fail_closed(
        store, sample_projects, homes, tmp_path, seeded):
    """Exact review repro: caller swaps subject/claim/terms after ingest."""
    candidate = ingest(store, seeded, n=911, subject="stored safe subject",
                       claim="stored safe claim", terms=("safe",))
    caller = dict(candidate)
    caller["canonical_subject"] = "caller substituted subject"
    caller["normalized_claim"] = "VERBATIM_TRANSCRIPT_CANARY_911"
    caller["retrieval_terms"] = ["VERBATIM_TRANSCRIPT_CANARY_911"]
    before = snapshot_home(homes.memory)
    counts = store.counts()
    with pytest.raises(CandidateBindingError, match="stored"):
        promote_with_homes(store, caller, homes,
                           backup_root=tmp_path / "backups", now=NOW_ISO)
    row = store.get_candidate(candidate["candidate_id"], 1)
    assert row["status"] == "validated"          # no status promotion
    assert store.counts() == counts              # no receipt, no event
    assert snapshot_home(homes.memory) == before  # no destination mutation
    assert not any("CANARY" in p.read_text(encoding="utf-8")
                   for p in homes.memory.rglob("*.md"))


DRIFT_CASES = [
    ("destination", "memory:hot"),
    ("class", "reference_knowledge"),
    ("project_id", "klas-sample"),
    ("canonical_subject", "caller substituted subject"),
    ("normalized_claim", "caller substituted claim"),
    ("retrieval_terms", ["substituted-term"]),
    ("evidence_refs", [{"source_type": "file",
                        "source_id": "profile:state/forged.md",
                        "observed_at": "2026-07-10T21:00:00+00:00",
                        "fingerprint": "sha256:" + "b" * 64}]),
    ("validation_requirements", ["live_check"]),
    ("conflict_set", ["candidate-0000000000000042"]),
    ("confidence", 0.11),
    ("freshness_class", "ephemeral"),
    ("sensitivity_class", "credential_forbidden"),
    ("dedupe_key", "dedupe-9999999999999999"),
    ("schema_version", 2),
    ("provenance.run_id", "run-999999999999999999999999999999"),
    ("provenance.collector_version", "v999-forged"),
    ("provenance.classifier_kind", "llm"),
    ("provenance.classifier_version", "v999-forged"),
    ("provenance.model", "forged-model"),
    ("provenance.prompt_hash", "sha256:" + "b" * 64),
]


@pytest.mark.parametrize("field,value", DRIFT_CASES,
                         ids=[f for f, _ in DRIFT_CASES])
def test_field_drift_matrix_refuses_every_bound_field(
        store, sample_projects, homes, tmp_path, seeded, field, value):
    candidate = ingest(store, seeded, n=920)
    caller = copy.deepcopy(candidate)
    if field.startswith("provenance."):
        caller["provenance"][field.split(".", 1)[1]] = value
    else:
        caller[field] = value
    before = snapshot_home(homes.memory)
    counts = store.counts()
    with pytest.raises(CandidateBindingError):
        promote_with_homes(store, caller, homes,
                           backup_root=tmp_path / "backups", now=NOW_ISO)
    assert store.get_candidate(
        candidate["candidate_id"], 1)["status"] == "validated"
    assert store.counts() == counts
    assert snapshot_home(homes.memory) == before
    assert not (tmp_path / "backups").exists()   # refused before any backup


def test_direct_adapter_entry_also_binds_to_the_stored_row(
        store, sample_projects, homes, tmp_path, seeded):
    """promote_candidate_via_adapter is the same fail-closed boundary: a
    caller-substituted destination cannot steer adapter routing."""
    candidate = ingest(store, seeded, n=930)
    caller = copy.deepcopy(candidate)
    caller["destination"] = "memory:hot"
    caller["class"] = "runtime_memory_hot"
    adapter = MemoryDestination(homes.memory, "hot")
    counts = store.counts()
    with pytest.raises(CandidateBindingError):
        promote_candidate_via_adapter(store, caller, adapter,
                                      backup_root=tmp_path / "backups",
                                      now=NOW_ISO)
    assert store.counts() == counts
    assert store.get_candidate(
        candidate["candidate_id"], 1)["status"] == "validated"


def test_unknown_candidate_and_bad_revision_are_refused(store, seeded):
    with pytest.raises(Exception, match="unknown candidate"):
        load_authoritative_candidate(
            store, {"candidate_id": "candidate-0000000000000404",
                    "content_revision": 1})
    with pytest.raises(Exception, match="positive"):
        load_authoritative_candidate(
            store, {"candidate_id": "candidate-0000000000000404",
                    "content_revision": 0})


def test_destination_bytes_derive_from_the_stored_row(
        store, sample_projects, homes, tmp_path, seeded):
    """Positive control: an exact caller copy promotes, and every destination
    byte, receipt, and index line carries the stored row's content."""
    candidate = ingest(store, seeded, n=940, subject="stored safe subject",
                       claim="stored safe claim", terms=("safe",))
    result = promote_with_homes(store, dict(candidate), homes,
                                backup_root=tmp_path / "backups", now=NOW_ISO)
    assert result.outcome == "promoted"
    facts = [p for p in homes.memory.glob("*.md") if p.name != "MEMORY.md"]
    assert len(facts) == 1
    text = facts[0].read_text(encoding="utf-8")
    assert "stored safe claim" in text and "stored safe subject" in text
    receipt = store.receipt_for_candidate(candidate["candidate_id"], 1)
    assert receipt is not None
    assert receipt["record_key"] == record_key_for("memory:warm",
                                                   "stored safe subject")


def test_stale_row_object_reruns_stay_idempotent(
        store, sample_projects, homes, tmp_path, seeded):
    """Lifecycle-mutable fields (status) are deliberately unbound: rerunning
    with the original ingest dict is still the documented no-op."""
    candidate = ingest(store, seeded, n=950)
    assert promote_with_homes(store, candidate, homes,
                              backup_root=tmp_path / "backups",
                              now=NOW_ISO).outcome == "promoted"
    counts = store.counts()
    again = promote_with_homes(store, candidate, homes,
                               backup_root=tmp_path / "backups", now=NOW_ISO)
    assert again.outcome == "unchanged"
    assert store.counts() == counts


# -- blocker 2: ingest only accepts legitimate initial states -----------------

def _fresh_candidate(manifest, status, n=991):
    candidate = copy.deepcopy(VALID_CANDIDATE)
    candidate["candidate_id"] = f"candidate-{n:016d}"
    candidate["dedupe_key"] = f"dedupe-{n:016d}"
    candidate["project_id"] = None
    candidate["destination"] = "memory:warm"
    candidate["class"] = "runtime_memory_warm"
    candidate["status"] = status
    candidate["provenance"]["run_id"] = manifest["run_id"]
    return candidate


@pytest.mark.parametrize("status", ["routed", "validated", "promoted",
                                    "superseded", "rejected", "expired"])
def test_ingest_refuses_lifecycle_state_import(store, seeded, status):
    counts = store.counts()
    with pytest.raises(CandidateStateError, match="ingest accepts only"):
        store.ingest_candidate(_fresh_candidate(seeded, status), NOW_ISO)
    after = store.counts()
    assert after == counts                        # zero rows, events, receipts
    assert after["write_receipts"] == 0
    assert store.get_candidate("candidate-0000000000000991", 1) is None


@pytest.mark.parametrize("status", ["observed", "classified", "quarantined"])
def test_ingest_still_accepts_legitimate_initial_states(store, seeded, status):
    candidate = _fresh_candidate(seeded, status)
    assert store.ingest_candidate(candidate, NOW_ISO) == "inserted"
    row = store.get_candidate(candidate["candidate_id"], 1)
    assert row["status"] == status
    assert store.counts()["write_receipts"] == 0


def test_ingest_refusal_leaves_no_receiptless_promoted_row(store, seeded):
    """Exact review repro: status='promoted' at ingest must not persist."""
    candidate = _fresh_candidate(seeded, "promoted", n=992)
    with pytest.raises(CandidateStateError):
        store.ingest_candidate(candidate, NOW_ISO)
    promoted = list(store._conn.execute(
        "SELECT * FROM candidates WHERE status = 'promoted'"))
    assert promoted == []
    assert store.counts()["candidates"] == 0
    assert store.counts()["write_receipts"] == 0


# -- blocker 3: snapshot/read symlink confinement ------------------------------

@pytest.fixture
def outside_secret(tmp_path):
    secret = tmp_path / "outside-secret"
    secret.write_text("OUTSIDE_SECRET_BYTES", encoding="utf-8")
    return secret


def test_memory_snapshot_refuses_symlinked_fact_file(tmp_path, outside_secret):
    """Exact review repro: fact_path is a symlink out of the home."""
    home = tmp_path / "memory"
    home.mkdir()
    adapter = MemoryDestination(home, "warm")
    record = make_record()
    adapter.fact_path(record).symlink_to(outside_secret)
    with pytest.raises(DestinationError, match="symlink"):
        adapter.snapshot_revision(record)


def test_hot_memory_snapshot_refuses_symlinked_index(tmp_path, outside_secret):
    home = tmp_path / "memory"
    home.mkdir()
    (home / "MEMORY.md").symlink_to(outside_secret)
    adapter = MemoryDestination(home, "hot")
    with pytest.raises(DestinationError, match="symlink"):
        adapter.snapshot_revision(make_record(destination="memory:hot"))


def test_skill_snapshot_refuses_symlinked_dir_and_file(tmp_path,
                                                       outside_secret):
    skills = tmp_path / "skills"
    skills.mkdir()
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "SKILL.md").write_text("---\nname: x\n---\n")
    (skills / "linked-skill").symlink_to(outside_dir)
    record = make_record(destination="skill:linked-skill")
    with pytest.raises(DestinationError, match="symlink"):
        SkillDestination(skills, "linked-skill").snapshot_revision(record)
    real = skills / "real-skill"
    real.mkdir()
    (real / "SKILL.md").symlink_to(outside_secret)
    record = make_record(destination="skill:real-skill")
    with pytest.raises(DestinationError, match="symlink"):
        SkillDestination(skills, "real-skill").snapshot_revision(record)


def test_project_doc_snapshot_refuses_symlinked_doc(tmp_path, outside_secret):
    projects = tmp_path / "projects"
    (projects / "proj").mkdir(parents=True)
    (projects / "proj" / "decisions.md").symlink_to(outside_secret)
    record = make_record(destination="project:proj:decisions")
    with pytest.raises(DestinationError, match="symlink"):
        ProjectDocDestination(projects, "proj",
                              "decisions").snapshot_revision(record)


def test_combined_revision_never_fingerprints_through_a_symlink(
        tmp_path, outside_secret):
    link = tmp_path / "linked.md"
    link.symlink_to(outside_secret)
    with pytest.raises(DestinationError, match="symlink"):
        _combined_revision([link])


def test_sibling_read_surfaces_refuse_symlinks(tmp_path, outside_secret):
    """existing_records / warm search / skill loader / doc reader / budget
    context all reject a planted symlink instead of reading through it."""
    home = tmp_path / "memory"
    home.mkdir()
    (home / "planted.md").symlink_to(outside_secret)
    adapter = MemoryDestination(home, "warm")
    with pytest.raises(DestinationError, match="symlink"):
        adapter.existing_records()
    with pytest.raises(DestinationError, match="symlink"):
        search_warm_memory(home, ["secret"])

    hot_home = tmp_path / "memory-hot"
    hot_home.mkdir()
    (hot_home / "USER.md").symlink_to(outside_secret)
    with pytest.raises(DestinationError, match="symlink"):
        MemoryDestination(hot_home, "hot").budget_context(
            make_record(destination="memory:hot"))

    skills = tmp_path / "skills"
    (skills / "sk").mkdir(parents=True)
    (skills / "sk" / "SKILL.md").symlink_to(outside_secret)
    with pytest.raises(DestinationError, match="symlink"):
        load_skill(skills, "sk")

    projects = tmp_path / "projects"
    (projects / "proj").mkdir(parents=True)
    (projects / "proj" / "notes.md").symlink_to(outside_secret)
    with pytest.raises(DestinationError, match="symlink"):
        read_project_doc_sections(projects, "proj", "notes")


def test_promotion_against_symlinked_target_fails_closed(
        store, sample_projects, homes, tmp_path, seeded, outside_secret):
    """End to end: a symlinked fact target refuses before any byte moves —
    no receipt, no status change, the outside file untouched, the symlink
    never replaced."""
    candidate = ingest(store, seeded, n=960, subject="symlinked target",
                       claim="this claim must never reach the secret",
                       terms=("symlinked",))
    adapter = MemoryDestination(homes.memory, "warm")
    row = store.get_candidate(candidate["candidate_id"], 1)
    from dream_cycle_v3.promotion import build_record
    fact = adapter.fact_path(build_record(row))
    fact.symlink_to(outside_secret)
    counts = store.counts()
    with pytest.raises(DestinationError, match="symlink"):
        promote_with_homes(store, candidate, homes,
                           backup_root=tmp_path / "backups", now=NOW_ISO)
    assert outside_secret.read_text(encoding="utf-8") == "OUTSIDE_SECRET_BYTES"
    assert fact.is_symlink()
    assert store.counts()["write_receipts"] == 0
    assert store.get_candidate(
        candidate["candidate_id"], 1)["status"] == "validated"
    assert store.counts() == counts
