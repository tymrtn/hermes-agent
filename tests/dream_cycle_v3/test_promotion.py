"""Promotion orchestrator: policies -> backup -> write -> proofs -> receipt.

All destinations are fixture homes under tmp_path; the continuity store is a
throwaway tmp database. Nothing touches live profile state.
"""
import copy

import pytest
from .conftest import NOW_ISO, make_manifest_for_run

from dream_cycle_v3.adapters.destinations import (DestinationHomes,
                                                  MemoryDestination,
                                                  restore_backup)
from dream_cycle_v3.canonical import record_key_for
from dream_cycle_v3.errors import CandidateStateError, RetrievalProofError
from dream_cycle_v3.policies import ConflictResolutions
from dream_cycle_v3.promotion import (promote_candidate_via_adapter,
                                      promote_with_homes)
from .test_contracts import VALID_CANDIDATE


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


def ingest(store, manifest, *, n=1, destination="memory:warm",
           subject="python runtime version",
           claim="default python3 is 3.14 via homebrew",
           klass="runtime_memory_warm", status="validated",
           revision=1, conflict_set=(), terms=("python", "homebrew"),
           validation_requirements=(), project_id=None,
           sensitivity_class="normal"):
    candidate = copy.deepcopy(VALID_CANDIDATE)
    candidate["candidate_id"] = f"candidate-{n:016d}"
    candidate["dedupe_key"] = f"dedupe-{n:016d}"
    # Promotable fixtures carry non-transcript evidence: session-backed
    # candidates are metadata-only and quarantine at promotion by design.
    candidate["evidence_refs"] = [{
        "source_type": "file",
        "source_id": f"profile:state/fixture-{n:04d}.md",
        "observed_at": "2026-07-10T21:00:00+00:00",
        "fingerprint": "sha256:" + "a" * 64,
    }]
    candidate["content_revision"] = revision
    candidate["destination"] = destination
    candidate["canonical_subject"] = subject
    candidate["normalized_claim"] = claim
    candidate["class"] = klass
    candidate["project_id"] = project_id
    candidate["sensitivity_class"] = sensitivity_class
    candidate["retrieval_terms"] = list(terms)
    candidate["validation_requirements"] = list(validation_requirements)
    candidate["conflict_set"] = list(conflict_set)
    candidate["status"] = "classified"
    candidate["provenance"]["run_id"] = manifest["run_id"]
    assert store.ingest_candidate(candidate, NOW_ISO) == "inserted"
    store.transition_candidate(candidate["candidate_id"], revision, "routed",
                               reason="seed", now=NOW_ISO)
    if status == "validated":
        store.transition_candidate(candidate["candidate_id"], revision,
                                   "validated", reason="seed", now=NOW_ISO)
    return candidate


def test_promote_end_to_end_and_rerun_noop(store, sample_projects, homes,
                                           tmp_path, seeded):
    candidate = ingest(store, seeded)
    result = promote_with_homes(store, candidate, homes,
                                backup_root=tmp_path / "backups", now=NOW_ISO)
    assert result.outcome == "promoted"
    assert result.receipt_id
    assert result.retrieval_proof.startswith("warm_term_search:")
    row = store.get_candidate(candidate["candidate_id"], 1)
    assert row["status"] == "promoted"
    receipt = store.receipt_for_candidate(candidate["candidate_id"], 1)
    assert receipt["target_revision_before"] is None
    assert receipt["target_revision_after"].startswith("sha256:")
    assert receipt["backup_ref"]
    assert receipt["retrieval_proof"]

    counts = store.counts()
    fact_files = sorted(p.name for p in homes.memory.glob("*.md"))
    again = promote_with_homes(store, candidate, homes,
                               backup_root=tmp_path / "backups", now=NOW_ISO)
    assert again.outcome == "unchanged"
    assert store.counts() == counts
    assert sorted(p.name for p in homes.memory.glob("*.md")) == fact_files


def test_promote_advances_routed_to_validated_first(store, sample_projects,
                                                    homes, tmp_path, seeded):
    candidate = ingest(store, seeded, status="routed")
    result = promote_with_homes(store, candidate, homes,
                                backup_root=tmp_path / "backups", now=NOW_ISO)
    assert result.outcome == "promoted"
    events = [r["payload"] for r in store._conn.execute(
        "SELECT payload FROM events WHERE entity_id = ? AND "
        "event_type = 'candidate_transition'", (candidate["candidate_id"],))]
    assert any('"to":"validated"' in p for p in events)


def test_policy_reject_leaves_destination_untouched(store, sample_projects,
                                                    homes, tmp_path, seeded):
    candidate = ingest(store, seeded, destination="memory:hot",
                       klass="runtime_memory_hot",
                       claim="TODO: follow-up on the migration tomorrow")
    before = (homes.memory / "MEMORY.md").read_bytes()
    result = promote_with_homes(store, candidate, homes,
                                backup_root=tmp_path / "backups", now=NOW_ISO)
    assert result.outcome == "rejected"
    assert result.reason == "hot_memory_task_leakage"
    assert store.get_candidate(candidate["candidate_id"], 1)["status"] == "rejected"
    assert (homes.memory / "MEMORY.md").read_bytes() == before
    assert store.counts()["write_receipts"] == 0


def test_exact_duplicate_rejected_near_duplicate_quarantined(
        store, sample_projects, homes, tmp_path, seeded):
    first = ingest(store, seeded, n=1)
    assert promote_with_homes(store, first, homes,
                              backup_root=tmp_path / "backups",
                              now=NOW_ISO).outcome == "promoted"
    # Exact text, different subject -> different record, same claim.
    dup = ingest(store, seeded, n=2, subject="python version fact",
                 claim="Default python3 is 3.14 via Homebrew!")
    result = promote_with_homes(store, dup, homes,
                                backup_root=tmp_path / "backups", now=NOW_ISO)
    assert result.outcome == "rejected"
    assert result.reason == "exact_duplicate"

    near = ingest(store, seeded, n=3, subject="python default runtime",
                  claim="default python3 is 3.14 via homebrew, installed")
    result = promote_with_homes(store, near, homes,
                                backup_root=tmp_path / "backups", now=NOW_ISO)
    assert result.outcome == "quarantined"
    assert result.reason == "near_duplicate"
    row = store.get_candidate(near["candidate_id"], 1)
    assert row["status"] == "quarantined"
    assert row["semantic_cluster_id"] == record_key_for(
        "memory:warm", "python runtime version")


def test_unresolved_conflict_quarantines_supersedes_promotes(
        store, sample_projects, homes, tmp_path, seeded):
    old = ingest(store, seeded, n=1)
    promote_with_homes(store, old, homes, backup_root=tmp_path / "backups",
                       now=NOW_ISO)
    challenger = ingest(store, seeded, n=2, revision=2,
                        claim="default python3 is 3.15 via homebrew now",
                        conflict_set=(old["candidate_id"],))
    result = promote_with_homes(store, challenger, homes,
                                backup_root=tmp_path / "backups", now=NOW_ISO)
    assert result.outcome == "quarantined"
    assert result.reason == "unresolved_conflict"

    # With an explicit reviewed relationship the same content revision 2
    # promotes and the old claim is superseded.
    store.transition_candidate(challenger["candidate_id"], 2, "validated",
                               reason="review resolved conflict", now=NOW_ISO)
    result = promote_with_homes(
        store, challenger, homes, backup_root=tmp_path / "backups",
        now=NOW_ISO,
        resolutions=ConflictResolutions({old["candidate_id"]: "supersedes"}))
    assert result.outcome == "promoted"
    assert result.superseded == (old["candidate_id"],)
    assert store.get_candidate(old["candidate_id"], 1)["status"] == "superseded"


def test_failed_retrieval_proof_rolls_back_and_quarantines(
        store, sample_projects, homes, tmp_path, seeded):
    candidate = ingest(store, seeded)
    before_files = {p.name: p.read_bytes() for p in homes.memory.glob("*.md")}

    def failing_prober(adapter, record):
        raise RetrievalProofError("fixture: retrieval route is broken")

    result = promote_with_homes(store, candidate, homes,
                                backup_root=tmp_path / "backups", now=NOW_ISO,
                                retrieval_prober=failing_prober)
    assert result.outcome == "quarantined"
    assert result.reason == "retrieval_proof_failed"
    assert result.rolled_back is True
    after_files = {p.name: p.read_bytes() for p in homes.memory.glob("*.md")}
    assert after_files == before_files  # byte-identical restore
    assert store.get_candidate(candidate["candidate_id"], 1)["status"] == "quarantined"
    assert store.counts()["write_receipts"] == 0


def test_proof_failure_can_leave_candidate_validated_by_policy(
        store, sample_projects, homes, tmp_path, seeded):
    candidate = ingest(store, seeded)

    def failing_prober(adapter, record):
        raise RetrievalProofError("fixture failure")

    result = promote_with_homes(store, candidate, homes,
                                backup_root=tmp_path / "backups", now=NOW_ISO,
                                retrieval_prober=failing_prober,
                                quarantine_on_proof_failure=False)
    assert result.outcome == "proof_failed"
    assert result.rolled_back is True
    assert store.get_candidate(candidate["candidate_id"], 1)["status"] == "validated"


def test_concurrent_edit_between_backup_and_write(store, sample_projects,
                                                  homes, tmp_path, seeded):
    candidate = ingest(store, seeded, destination="memory:hot",
                       klass="runtime_memory_hot")

    class RacingMemory(MemoryDestination):
        def backup(self, record, backup_dir, **kwargs):
            ref = super().backup(record, backup_dir, **kwargs)
            index = self.index_path
            index.write_text(index.read_text(encoding="utf-8")
                             + "- concurrent manual edit\n", encoding="utf-8")
            return ref

    adapter = RacingMemory(homes.memory, "hot")
    result = promote_candidate_via_adapter(
        store, candidate, adapter, backup_root=tmp_path / "backups",
        now=NOW_ISO)
    assert result.outcome == "revision_conflict"
    # The concurrent edit survives; candidate stays validated for retry.
    text = (homes.memory / "MEMORY.md").read_text(encoding="utf-8")
    assert "concurrent manual edit" in text
    assert store.get_candidate(candidate["candidate_id"], 1)["status"] == "validated"
    assert store.counts()["write_receipts"] == 0


def test_db_idempotency_refusal_rolls_destination_back(
        store, sample_projects, homes, tmp_path, seeded):
    """Same record identity + revision from two candidates: the second write
    is refused by the database and the destination is rolled back."""
    first = ingest(store, seeded, n=1)
    promote_with_homes(store, first, homes, backup_root=tmp_path / "backups",
                       now=NOW_ISO)
    # Different claim, low similarity, but same subject+destination+revision.
    rival = ingest(store, seeded, n=2,
                   claim="an entirely dissimilar statement about gateway "
                         "restart storms and launchd kickstart behavior")
    before = {p.name: p.read_bytes() for p in homes.memory.glob("*.md")}
    result = promote_with_homes(store, rival, homes,
                                backup_root=tmp_path / "backups", now=NOW_ISO)
    assert result.outcome == "quarantined"
    assert result.reason == "db_idempotency_refused"
    assert result.rolled_back is True
    after = {p.name: p.read_bytes() for p in homes.memory.glob("*.md")}
    assert after == before
    assert store.counts()["write_receipts"] == 1


def test_promotion_requires_promotable_status(store, sample_projects, homes,
                                              tmp_path, seeded):
    candidate = ingest(store, seeded)
    store.transition_candidate(candidate["candidate_id"], 1, "rejected",
                               reason="test", now=NOW_ISO)
    with pytest.raises(CandidateStateError, match="promotion accepts"):
        promote_with_homes(store, candidate, homes,
                           backup_root=tmp_path / "backups", now=NOW_ISO)


def test_skill_and_project_doc_end_to_end(store, sample_projects, homes,
                                          tmp_path, seeded):
    skill = ingest(store, seeded, n=5, destination="skill:hermes-continuity-map",
                   klass="reference_knowledge", subject="store ownership guard",
                   claim="writable opens require the v3 application_id",
                   terms=("ownership",))
    decision = ingest(store, seeded, n=6,
                      destination="project:klas-sample:decisions",
                      klass="decision_record", subject="listing dedupe policy",
                      claim="decision: dedupe listings by normalized title",
                      terms=("dedupe",))
    r1 = promote_with_homes(store, skill, homes,
                            backup_root=tmp_path / "backups", now=NOW_ISO)
    r2 = promote_with_homes(store, decision, homes,
                            backup_root=tmp_path / "backups", now=NOW_ISO)
    assert (r1.outcome, r2.outcome) == ("promoted", "promoted")
    assert (homes.skills / "hermes-continuity-map" / "SKILL.md").is_file()
    assert (homes.projects / "klas-sample" / "decisions.md").is_file()
    assert store.counts()["write_receipts"] == 2
