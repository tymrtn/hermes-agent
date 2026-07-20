"""Migration 2 / write_receipts / promote_candidate / candidate transitions.

Everything here runs against throwaway stores under tmp_path; no live state.
"""
import copy
import json
import sqlite3
from pathlib import Path

import pytest
from .conftest import NOW_ISO, make_manifest_for_run

import dream_cycle_v3.store as store_mod
from dream_cycle_v3.canonical import (record_key_for, sha256_hex,
                                      write_idempotency_key)
from dream_cycle_v3.errors import (CandidateStateError, ContractViolation,
                                   IdempotencyError, StoreError)
from dream_cycle_v3.store import ContinuityStore
from .test_contracts import VALID_CANDIDATE


def seed_candidate(store, sample_projects, *, status="validated",
                   candidate_id="candidate-0000000000000001",
                   dedupe_key="dedupe-0000000000000001",
                   content_revision=1,
                   destination="memory:warm",
                   subject="retriever hook"):
    manifest = make_manifest_for_run()
    store.record_run(manifest, "/tmp/manifest.json", NOW_ISO)
    for p in sample_projects:
        store.upsert_project(p, NOW_ISO)
    candidate = copy.deepcopy(VALID_CANDIDATE)
    candidate["candidate_id"] = candidate_id
    candidate["dedupe_key"] = dedupe_key
    # Promotable fixtures carry non-transcript evidence: the store refuses to
    # promote session-backed candidates (transcript containment).
    candidate["evidence_refs"] = [{
        "source_type": "file",
        "source_id": "profile:state/receipt-fixture.md",
        "observed_at": "2026-07-10T21:00:00+00:00",
        "fingerprint": "sha256:" + "a" * 64,
    }]
    candidate["content_revision"] = content_revision
    candidate["destination"] = destination
    candidate["canonical_subject"] = subject
    candidate["class"] = "runtime_memory_warm"
    candidate["status"] = "classified"
    candidate["provenance"]["run_id"] = manifest["run_id"]
    assert store.ingest_candidate(candidate, NOW_ISO) == "inserted"
    chain = {"classified": [], "routed": ["routed"],
             "validated": ["routed", "validated"]}[status]
    for nxt in chain:
        assert store.transition_candidate(
            candidate_id, content_revision, nxt, reason="test-seed",
            now=NOW_ISO) == "transitioned"
    return manifest, candidate


def make_receipt(candidate, *, record_key=None,
                 receipt_id="receipt-000000000000000001",
                 backup_root: Path):
    record_key = record_key or record_key_for(
        candidate["destination"], candidate["canonical_subject"])
    backup_dir = backup_root / "run" / receipt_id
    backup_dir.mkdir(parents=True, exist_ok=True)
    shadow_home = backup_root / "shadow-memory"
    shadow_home.mkdir(exist_ok=True)
    stat = shadow_home.stat()
    home_identity = {"canonical_path": str(shadow_home.resolve()),
                     "device": stat.st_dev, "inode": stat.st_ino}
    entries = [{"target": "memory/fact.md", "existed": False,
                "backup_file": None, "fingerprint": None}]
    manifest = {"version": 2, "kind": "dc3_filesystem_backup",
                "destination": candidate["destination"],
                "home_identity": home_identity, "entries": entries}
    manifest_bytes = (json.dumps(manifest, sort_keys=True,
                                 separators=(",", ":")) + "\n").encode()
    (backup_dir / "dc3-backup-manifest.json").write_bytes(manifest_bytes)
    return {
        "receipt_id": receipt_id,
        "candidate_id": candidate["candidate_id"],
        "destination": candidate["destination"],
        "adapter": "memory",
        "target_revision_before": None,
        "target_revision_after": "sha256:" + "b" * 64,
        "backup_ref": str(backup_dir.resolve()),
        "written_at": NOW_ISO,
        "read_back_verified": True,
        "retrieval_verified": True,
        "retrieval_proof": "warm reader retrieved record by retrieval terms",
        "rollback_command": None,
        "rollback_metadata": {
            "version": 2,
            "kind": "dc3_filesystem_backup",
            "manifest": "dc3-backup-manifest.json",
            "manifest_fingerprint": "sha256:" + sha256_hex(manifest_bytes),
            "home_identity": home_identity,
            "entries": entries,
        },
        "idempotency_key": write_idempotency_key(
            candidate["destination"], record_key,
            candidate["content_revision"]),
    }, record_key


def test_migration_2_upgrades_existing_v1_store(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    # Materialize a genuine v1-only store, as if created before Phase 2.
    monkeypatch.setattr(store_mod, "_MIGRATIONS", store_mod._MIGRATIONS[:1])
    monkeypatch.setattr(store_mod, "STORE_SCHEMA_TARGET", 1)
    with ContinuityStore(path) as store:
        assert store.migrate(NOW_ISO) == [1]
        manifest = make_manifest_for_run()
        store.record_run(manifest, "/tmp/m.json", NOW_ISO)
    monkeypatch.undo()
    with ContinuityStore(path) as store:
        assert store.migrate("2026-07-12T00:00:00+00:00") == [2, 3, 4]
        assert store.schema_version() == 4
        # Phase 1 data survives the upgrade.
        assert store.counts()["runs"] == 1
        assert store.counts()["write_receipts"] == 0


def test_promote_happy_path_is_atomic_and_idempotent(store, sample_projects):
    manifest, candidate = seed_candidate(store, sample_projects)
    receipt, record_key = make_receipt(candidate, backup_root=store.backup_root)
    assert store.promote_candidate(
        candidate["candidate_id"], receipt, record_key=record_key,
        now=NOW_ISO, content_revision=1,
        run_id=manifest["run_id"]) == "inserted"

    row = store.get_candidate(candidate["candidate_id"], 1)
    assert row["status"] == "promoted"
    stored = store.receipt_for_candidate(candidate["candidate_id"], 1)
    assert stored["idempotency_key"] == receipt["idempotency_key"]
    assert stored["record_key"] == record_key
    assert stored["backup_ref"] == receipt["backup_ref"]
    events = [r["event_type"] for r in store._conn.execute(
        "SELECT event_type FROM events WHERE entity_id = ?",
        (candidate["candidate_id"],))]
    assert "candidate_promoted" in events

    counts = store.counts()
    # Identical re-promotion is a no-op, not a duplicate.
    assert store.promote_candidate(
        candidate["candidate_id"], receipt, record_key=record_key,
        now=NOW_ISO, content_revision=1,
        run_id=manifest["run_id"]) == "unchanged"
    assert store.counts() == counts


def test_promote_requires_validated_state(store, sample_projects):
    _, candidate = seed_candidate(store, sample_projects, status="classified")
    receipt, record_key = make_receipt(candidate, backup_root=store.backup_root)
    with pytest.raises(CandidateStateError, match="only 'validated'"):
        store.promote_candidate(candidate["candidate_id"], receipt,
                                record_key=record_key, now=NOW_ISO,
                                content_revision=1)
    assert store.get_candidate(candidate["candidate_id"], 1)["status"] == "classified"
    assert store.counts()["write_receipts"] == 0


def test_promote_rejects_contract_invalid_receipt(store, sample_projects):
    _, candidate = seed_candidate(store, sample_projects)
    receipt, record_key = make_receipt(candidate, backup_root=store.backup_root)
    broken = {k: v for k, v in receipt.items() if k != "backup_ref"}
    with pytest.raises(ContractViolation):
        store.promote_candidate(candidate["candidate_id"], broken,
                                record_key=record_key, now=NOW_ISO,
                                content_revision=1)
    unverified = dict(receipt, read_back_verified=False)
    with pytest.raises(ContractViolation, match="literal true"):
        store.promote_candidate(candidate["candidate_id"], unverified,
                                record_key=record_key, now=NOW_ISO,
                                content_revision=1)
    assert store.counts()["write_receipts"] == 0
    assert store.get_candidate(candidate["candidate_id"], 1)["status"] == "validated"


def test_store_boundary_requires_concrete_proof_and_constrained_backup(
        store, sample_projects):
    _, candidate = seed_candidate(store, sample_projects)
    receipt, record_key = make_receipt(candidate, backup_root=store.backup_root)
    for key in ("retrieval_proof", "rollback_metadata"):
        broken = dict(receipt)
        broken.pop(key)
        with pytest.raises(ContractViolation, match=key):
            store.promote_candidate(candidate["candidate_id"], broken,
                                    record_key=record_key, now=NOW_ISO,
                                content_revision=1)

    outside = dict(receipt, backup_ref=str((store.path.parent / "outside").resolve()))
    with pytest.raises(ContractViolation, match="outside"):
        store.promote_candidate(candidate["candidate_id"], outside,
                                record_key=record_key, now=NOW_ISO,
                                content_revision=1)
    assert store.counts()["write_receipts"] == 0


def test_store_persists_structured_rollback_metadata(store, sample_projects):
    _, candidate = seed_candidate(store, sample_projects)
    receipt, record_key = make_receipt(candidate, backup_root=store.backup_root)
    assert store.promote_candidate(candidate["candidate_id"], receipt,
                                   record_key=record_key, now=NOW_ISO,
                                content_revision=1) == "inserted"
    stored = store.receipt_for_candidate(candidate["candidate_id"], 1)
    assert json.loads(stored["rollback_metadata"])["kind"] == "dc3_filesystem_backup"


def test_promote_rejects_forged_idempotency_key(store, sample_projects):
    _, candidate = seed_candidate(store, sample_projects)
    receipt, record_key = make_receipt(candidate, backup_root=store.backup_root)
    forged = dict(receipt, idempotency_key="f" * 32)
    with pytest.raises(ContractViolation, match="design §11"):
        store.promote_candidate(candidate["candidate_id"], forged,
                                record_key=record_key, now=NOW_ISO,
                                content_revision=1)
    assert store.counts()["write_receipts"] == 0


def test_promote_rejects_mismatched_candidate_and_destination(store, sample_projects):
    _, candidate = seed_candidate(store, sample_projects)
    receipt, record_key = make_receipt(candidate, backup_root=store.backup_root)
    with pytest.raises(ContractViolation, match="does not match candidate"):
        store.promote_candidate("candidate-0000000000000099", receipt,
                                record_key=record_key, now=NOW_ISO,
                                content_revision=1)
    wrong_dest = dict(receipt, destination="memory:hot")
    with pytest.raises(ContractViolation, match="destination"):
        store.promote_candidate(candidate["candidate_id"], wrong_dest,
                                record_key=record_key, now=NOW_ISO,
                                content_revision=1)
    assert store.counts()["write_receipts"] == 0


def test_promote_unknown_candidate_fails_loud(store, sample_projects):
    _, candidate = seed_candidate(store, sample_projects)
    ghost = copy.deepcopy(candidate)
    ghost["candidate_id"] = "candidate-0000000000000042"
    receipt, record_key = make_receipt(ghost, backup_root=store.backup_root)
    with pytest.raises(StoreError, match="unknown candidate"):
        store.promote_candidate(ghost["candidate_id"], receipt,
                                record_key=record_key, now=NOW_ISO,
                                content_revision=1)


def test_same_record_revision_cannot_be_written_twice(store, sample_projects):
    """Database-enforced §11 idempotency: two different candidates aiming the
    same (destination, record identity, content revision) — the second
    receipt is refused by UNIQUE and its candidate stays unpromoted."""
    manifest, first = seed_candidate(store, sample_projects)
    receipt1, record_key = make_receipt(first, backup_root=store.backup_root)
    assert store.promote_candidate(first["candidate_id"], receipt1,
                                   record_key=record_key, now=NOW_ISO,
                                content_revision=1) == "inserted"

    rival = copy.deepcopy(first)
    rival["candidate_id"] = "candidate-0000000000000002"
    rival["dedupe_key"] = "dedupe-0000000000000002"
    rival["normalized_claim"] = "a different claim about the same subject"
    rival["status"] = "classified"
    assert store.ingest_candidate(rival, NOW_ISO) == "inserted"
    store.transition_candidate(rival["candidate_id"], 1, "routed",
                               reason="t", now=NOW_ISO)
    store.transition_candidate(rival["candidate_id"], 1, "validated",
                               reason="t", now=NOW_ISO)
    receipt2, _ = make_receipt(rival, record_key=record_key,
                               receipt_id="receipt-000000000000000002",
                               backup_root=store.backup_root)
    with pytest.raises(IdempotencyError, match="conflicts with an existing"):
        store.promote_candidate(rival["candidate_id"], receipt2,
                                record_key=record_key, now=NOW_ISO,
                                content_revision=1)
    # Atomicity: the failed promotion left the rival unpromoted.
    assert store.get_candidate(rival["candidate_id"], 1)["status"] == "validated"
    assert store.counts()["write_receipts"] == 1


def test_already_promoted_with_different_receipt_raises(store, sample_projects):
    manifest, candidate = seed_candidate(store, sample_projects)
    receipt, record_key = make_receipt(candidate, backup_root=store.backup_root)
    store.promote_candidate(candidate["candidate_id"], receipt,
                            record_key=record_key, now=NOW_ISO,
                                content_revision=1)
    different = dict(receipt, target_revision_after="sha256:" + "c" * 64)
    with pytest.raises(IdempotencyError, match="different receipt"):
        store.promote_candidate(candidate["candidate_id"], different,
                                record_key=record_key, now=NOW_ISO,
                                content_revision=1)


def test_write_receipts_are_append_only(store, sample_projects):
    manifest, candidate = seed_candidate(store, sample_projects)
    receipt, record_key = make_receipt(candidate, backup_root=store.backup_root)
    store.promote_candidate(candidate["candidate_id"], receipt,
                            record_key=record_key, now=NOW_ISO,
                                content_revision=1)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute(
            "UPDATE write_receipts SET backup_ref = 'tampered'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("DELETE FROM write_receipts")


def test_transition_state_machine(store, sample_projects):
    _, candidate = seed_candidate(store, sample_projects, status="classified")
    cid = candidate["candidate_id"]
    # Legal chain with audit events.
    assert store.transition_candidate(cid, 1, "routed", reason="routing proof",
                                      now=NOW_ISO) == "transitioned"
    assert store.transition_candidate(cid, 1, "validated",
                                      reason="policies passed",
                                      now=NOW_ISO) == "transitioned"
    transitions = [r for r in store._conn.execute(
        "SELECT payload FROM events WHERE entity_id = ? AND "
        "event_type = 'candidate_transition'", (cid,))]
    assert len(transitions) >= 2
    # Same-status transition is explicitly a no-op.
    assert store.transition_candidate(cid, 1, "validated", reason="again",
                                      now=NOW_ISO) == "unchanged"
    # Skipping states is illegal.
    with pytest.raises(CandidateStateError, match="illegal transition"):
        store.transition_candidate(cid, 1, "classified", reason="rewind",
                                   now=NOW_ISO)
    # 'promoted' is unreachable without a receipt.
    with pytest.raises(CandidateStateError, match="promote_candidate"):
        store.transition_candidate(cid, 1, "promoted", reason="shortcut",
                                   now=NOW_ISO)
    # Terminal states accept nothing further.
    store.transition_candidate(cid, 1, "rejected", reason="test", now=NOW_ISO)
    with pytest.raises(CandidateStateError, match="illegal transition"):
        store.transition_candidate(cid, 1, "validated", reason="undo",
                                   now=NOW_ISO)


def test_quarantine_and_cluster_assignment(store, sample_projects):
    _, candidate = seed_candidate(store, sample_projects, status="classified")
    cid = candidate["candidate_id"]
    assert store.transition_candidate(
        cid, 1, "quarantined", reason="near_duplicate", now=NOW_ISO,
        semantic_cluster_id="cluster-0001") == "transitioned"
    row = store.get_candidate(cid, 1)
    assert row["status"] == "quarantined"
    assert row["semantic_cluster_id"] == "cluster-0001"
