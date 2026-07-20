import copy
import sqlite3

import pytest

from .conftest import NOW_ISO, make_manifest_for_run
from dream_cycle_v3.errors import (ContractViolation, DispositionConflictError,
                                   IdempotencyError, StoreError)
from dream_cycle_v3.store import ContinuityStore

PROOF = {"kind": "task_event", "reference": "kanban:sample-board:T-1001",
         "verified_at": NOW_ISO}


def _seeded(store, sample_projects, sample_threads):
    manifest = make_manifest_for_run()
    store.record_run(manifest, "/tmp/manifests/x.json", NOW_ISO)
    for p in sample_projects:
        store.upsert_project(p, NOW_ISO)
    for t in sample_threads:
        store.open_thread(t, NOW_ISO, run_id=manifest["run_id"])
    return manifest


def test_migrations_are_idempotent(tmp_path):
    with ContinuityStore(tmp_path / "db.sqlite") as store:
        assert store.migrate(NOW_ISO) == [1, 2, 3, 4]
        assert store.migrate(NOW_ISO) == []
        assert store.schema_version() == 4


def test_migration_timestamp_is_parameterized(tmp_path):
    with ContinuityStore(tmp_path / "db.sqlite") as store:
        store.migrate("2026-01-02T03:04:05+00:00")
        row = store._conn.execute(
            "SELECT applied_at FROM schema_migrations WHERE version = 1").fetchone()
        assert row["applied_at"] == "2026-01-02T03:04:05+00:00"
        with pytest.raises(StoreError):
            ContinuityStore(tmp_path / "db2.sqlite").migrate("not a datetime")


def test_read_only_store_query_creates_no_wal_sidecars(tmp_path):
    path = tmp_path / "continuity.db"
    with ContinuityStore(path) as store:
        store.migrate(NOW_ISO)
    for suffix in ("-wal", "-shm"):
        sidecar = tmp_path / (path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()

    with ContinuityStore(path, read_only=True) as store:
        assert store.schema_version() == 4
    assert not (tmp_path / "continuity.db-wal").exists()
    assert not (tmp_path / "continuity.db-shm").exists()


def test_record_run_validates_manifest_before_persistence(store):
    manifest = make_manifest_for_run()
    assert store.record_run(manifest, "/tmp/m.json", NOW_ISO) == "inserted"
    assert store.record_run(manifest, "/tmp/m.json", NOW_ISO) == "unchanged"

    # Forged fingerprint: rejected by validation, nothing persisted.
    from dream_cycle_v3.errors import ManifestValidationError

    forged = dict(manifest, manifest_fingerprint="sha256:" + "f" * 64)
    with pytest.raises(ManifestValidationError):
        store.record_run(forged, "/tmp/m.json", NOW_ISO)
    with pytest.raises(ManifestValidationError):
        store.record_run({"run_id": "0" * 32}, "/tmp/m.json", NOW_ISO)
    assert store.counts()["runs"] == 1

    # Valid manifest, same run_id, different core: typed idempotency conflict.
    conflicting = make_manifest_for_run(max_depth=9)
    assert conflicting["run_id"] == manifest["run_id"]
    assert conflicting["manifest_fingerprint"] != manifest["manifest_fingerprint"]
    with pytest.raises(IdempotencyError):
        store.record_run(conflicting, "/tmp/m.json", NOW_ISO)
    assert store.counts()["runs"] == 1


def test_record_run_rejects_forged_manifest_into_empty_store(store):
    from dream_cycle_v3.errors import ManifestValidationError

    manifest = make_manifest_for_run()
    forged = dict(manifest, manifest_fingerprint="sha256:" + "0" * 64)
    with pytest.raises(ManifestValidationError):
        store.record_run(forged, "/tmp/m.json", NOW_ISO)
    assert store.counts()["runs"] == 0
    assert store.counts()["events"] == 0


def test_project_upsert_versioning(store, sample_projects):
    project = copy.deepcopy(sample_projects[0])
    assert store.upsert_project(project, NOW_ISO) == "inserted"
    assert store.upsert_project(project, NOW_ISO) == "unchanged"

    drifted = copy.deepcopy(project)
    drifted["canonical_name"] = "renamed without version bump"
    with pytest.raises(IdempotencyError):
        store.upsert_project(drifted, NOW_ISO)

    bumped = copy.deepcopy(drifted)
    bumped["registry_version"] = 2
    assert store.upsert_project(bumped, NOW_ISO) == "updated"

    invalid = copy.deepcopy(project)
    invalid["status"] = "zombie"
    with pytest.raises(ContractViolation):
        store.upsert_project(invalid, NOW_ISO)


def test_candidate_dedupe_and_rerun_idempotency(store, sample_projects):
    from .test_contracts import VALID_CANDIDATE

    manifest = make_manifest_for_run()
    store.record_run(manifest, "/tmp/m.json", NOW_ISO)
    store.upsert_project(sample_projects[0], NOW_ISO)

    candidate = copy.deepcopy(VALID_CANDIDATE)
    candidate["provenance"]["run_id"] = manifest["run_id"]
    assert store.ingest_candidate(candidate, NOW_ISO) == "inserted"
    assert store.ingest_candidate(candidate, NOW_ISO) == "unchanged"

    rival = copy.deepcopy(candidate)
    rival["candidate_id"] = "candidate-0000000000000002"  # same dedupe_key
    assert store.ingest_candidate(rival, NOW_ISO) == "duplicate_rejected"

    counts = store.counts()
    assert counts["candidates"] == 1

    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO candidates(candidate_id, content_revision, class, "
            "destination, normalized_claim, canonical_subject, evidence_refs, "
            "confidence, freshness_class, sensitivity_class, dedupe_key, status, "
            "run_id, collector_version, classifier_kind, classifier_version, "
            "content_fingerprint, created_at) VALUES "
            "('x-0000000000000009', 1, 'task_thread', 'd', 'c', 's', '[]', 0.5, "
            "'days', 'normal', ?, 'observed', ?, 'v', 'deterministic', 'v', 'fp', ?)",
            (candidate["dedupe_key"], manifest["run_id"], NOW_ISO))


def test_thread_open_is_idempotent(store, sample_projects, sample_threads):
    _seeded(store, sample_projects, sample_threads)
    assert store.counts()["threads"] == len(sample_threads)
    for t in sample_threads:
        assert store.open_thread(t, NOW_ISO) == "exists"
    assert store.counts()["threads"] == len(sample_threads)

    invalid = copy.deepcopy(sample_threads[0])
    invalid["thread_id"] = "sample-thread-0099-invalid-done"
    invalid["idempotency_key"] = "sample-idem-0099-invalid-done"
    invalid["state"] = "done"  # no closure proof
    with pytest.raises(ContractViolation):
        store.open_thread(invalid, NOW_ISO)


def test_db_checks_enforce_contract_without_python(store, sample_projects,
                                                   sample_threads):
    _seeded(store, sample_projects, sample_threads)
    base = ("INSERT INTO threads(thread_id, project_id, link_disposition, title, "
            "normalized_next_action, owner, state, opened_from, evidence_refs, "
            "last_disposition_date, idempotency_key, created_at, updated_at, "
            "blocked_by, follow_up_after, closure_proof) "
            "VALUES (?, 'hermes-continuity', 'not_actionable', 't', 'n', 'o', ?, "
            "'test', '[]', '2026-07-10', ?, ?, ?, ?, ?, ?)")
    with pytest.raises(sqlite3.IntegrityError):  # done without proof
        store._conn.execute(base, ("raw-thread-0001-xxxxxxxx", "done",
                                   "raw-idem-0001-xxxxxxxx", NOW_ISO, NOW_ISO,
                                   None, None, None))
    with pytest.raises(sqlite3.IntegrityError):  # blocked without blocker
        store._conn.execute(base, ("raw-thread-0002-xxxxxxxx", "blocked",
                                   "raw-idem-0002-xxxxxxxx", NOW_ISO, NOW_ISO,
                                   None, None, None))


def test_dispositions_unique_per_day_and_append_only(store, sample_projects,
                                                     sample_threads):
    manifest = _seeded(store, sample_projects, sample_threads)
    thread_id = sample_threads[7]["thread_id"]

    assert store.record_disposition(
        thread_id=thread_id, disposition_date="2026-07-11",
        run_id=manifest["run_id"], action="continue", reason="carried",
        state_after="active", now=NOW_ISO) == "inserted"
    # Identical re-present: no-op.
    assert store.record_disposition(
        thread_id=thread_id, disposition_date="2026-07-11",
        run_id=manifest["run_id"], action="continue", reason="carried",
        state_after="active", now=NOW_ISO) == "unchanged"
    # Different same-day disposition: loud conflict.
    with pytest.raises(DispositionConflictError):
        store.record_disposition(
            thread_id=thread_id, disposition_date="2026-07-11",
            run_id=manifest["run_id"], action="defer", reason="changed my mind",
            state_after="active", now=NOW_ISO)

    row = store.get_disposition(thread_id, "2026-07-11")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute(
            "UPDATE thread_dispositions SET reason='rewritten' WHERE disposition_id=?",
            (row["disposition_id"],))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute(
            "DELETE FROM thread_dispositions WHERE disposition_id=?",
            (row["disposition_id"],))
    with pytest.raises(sqlite3.IntegrityError):  # UNIQUE(thread_id, date)
        store._conn.execute(
            "INSERT INTO thread_dispositions(disposition_id, thread_id, "
            "disposition_date, run_id, action, reason, state_before, state_after, "
            "created_at) VALUES ('other-id-000000001', ?, '2026-07-11', ?, "
            "'continue', 'dup', 'active', 'active', ?)",
            (thread_id, manifest["run_id"], NOW_ISO))


def test_disposition_validation_gates(store, sample_projects, sample_threads):
    manifest = _seeded(store, sample_projects, sample_threads)
    thread_id = sample_threads[0]["thread_id"]
    kwargs = dict(thread_id=thread_id, disposition_date="2026-07-11",
                  run_id=manifest["run_id"], now=NOW_ISO)
    with pytest.raises(ContractViolation, match="closure_proof"):
        store.record_disposition(action="close_done", reason="no proof",
                                 state_after="done", **kwargs)
    with pytest.raises(ContractViolation, match="blocker and follow_up_after"):
        store.record_disposition(action="blocked", reason="no blocker",
                                 state_after="blocked", **kwargs)
    with pytest.raises(ContractViolation, match="unknown action"):
        store.record_disposition(action="procrastinate", reason="x",
                                 state_after="active", **kwargs)
    bad_proof = {"kind": "vibes", "reference": "x", "verified_at": NOW_ISO}
    with pytest.raises(ContractViolation):
        store.record_disposition(action="close_done", reason="bad proof",
                                 state_after="done", closure_proof=bad_proof,
                                 **kwargs)
    assert store.record_disposition(action="close_done", reason="external done",
                                    state_after="done", closure_proof=PROOF,
                                    **kwargs) == "inserted"
    assert store.get_thread(thread_id)["state"] == "done"


def test_disposition_follow_up_must_be_semantically_valid(store, sample_projects,
                                                          sample_threads):
    manifest = _seeded(store, sample_projects, sample_threads)
    counts = store.counts()
    kwargs = dict(thread_id=sample_threads[7]["thread_id"],
                  disposition_date="2026-07-11", run_id=manifest["run_id"],
                  now=NOW_ISO)
    for bad in ("2026-99-99T00:00:00Z", "2026-02-30T00:00:00+00:00", "soonish"):
        with pytest.raises(ContractViolation, match="follow_up_after"):
            store.record_disposition(action="blocked", reason="x",
                                     state_after="blocked", blocker="someone",
                                     follow_up_after=bad, **kwargs)
    assert store.counts() == counts  # nothing persisted

    # A valid 'Z' timestamp is accepted and stored.
    assert store.record_disposition(
        action="blocked", reason="parked", state_after="blocked",
        blocker="someone", follow_up_after="2026-07-14T09:00:00Z",
        **kwargs) == "inserted"


def test_events_append_only_and_keyed(store, sample_projects, sample_threads):
    _seeded(store, sample_projects, sample_threads)
    row = store._conn.execute("SELECT * FROM events LIMIT 1").fetchone()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("UPDATE events SET payload='{}' WHERE event_seq=?",
                            (row["event_seq"],))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("DELETE FROM events WHERE event_seq=?",
                            (row["event_seq"],))


def test_adapter_snapshot_idempotency(store):
    manifest = make_manifest_for_run()
    store.record_run(manifest, "/tmp/m.json", NOW_ISO)
    kwargs = dict(run_id=manifest["run_id"], adapter="kanban",
                  source_locator="/tmp/board.db", status="ok", detail=None,
                  items=[{"ref": "kanban:x:1", "state": "open"}], now=NOW_ISO)
    assert store.record_adapter_snapshot(**kwargs) == "inserted"
    assert store.record_adapter_snapshot(**kwargs) == "unchanged"
    with pytest.raises(IdempotencyError):
        store.record_adapter_snapshot(**{**kwargs, "items": []})


def test_full_rerun_adds_zero_rows(store, sample_projects, sample_threads):
    manifest = _seeded(store, sample_projects, sample_threads)
    before_counts = store.counts()
    before_dump = store.dump_canonical()

    store.record_run(manifest, "/tmp/manifests/x.json", NOW_ISO)
    for p in sample_projects:
        store.upsert_project(p, NOW_ISO)
    for t in sample_threads:
        store.open_thread(t, NOW_ISO, run_id=manifest["run_id"])

    assert store.counts() == before_counts
    assert store.dump_canonical() == before_dump


def test_project_id_grammar_enforced_by_schema(tmp_path):
    """Post-verification finding 1: the read paths build filesystem paths
    from stored project ids, so the schema itself must refuse ids outside
    the registry grammar — even for writes that bypass the contract layer
    (raw SQL, foreign tooling)."""
    cols = ("project_id, canonical_name, status, owner, task_provider, "
            "memory_policy, sensitivity_policy, registry_version, "
            "last_verified_at, content_fingerprint")

    with ContinuityStore(tmp_path / "db.sqlite") as store:
        store.migrate(NOW_ISO)

        def _insert(pid):
            store._conn.execute(
                f"INSERT INTO projects ({cols}) VALUES (?, 'X', 'active', "
                "'owner', 'none', 'warm_only', 'normal', 1, ?, 'fp')",
                (pid, NOW_ISO))

        for evil in ("../outside", "evil/relative", "UPPER", "x",
                     "a" * 65, ".hidden"):
            with pytest.raises(sqlite3.IntegrityError):
                _insert(evil)
        _insert("fine-id")
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute(
                "UPDATE projects SET project_id = '../x' "
                "WHERE project_id = 'fine-id'")


def test_project_id_grammar_rejects_null(tmp_path):
    """Re-review blocker 1: projects is a rowid table, so its TEXT PRIMARY
    KEY accepts NULL, and a plain ``WHEN NOT (...)`` guard evaluates to NULL
    (not true) for a NULL id under SQL three-valued logic — the row sailed
    through and the read paths later built filesystem paths from it. The
    schema guard must reject NULL explicitly, on INSERT and UPDATE alike."""
    cols = ("project_id, canonical_name, status, owner, task_provider, "
            "memory_policy, sensitivity_policy, registry_version, "
            "last_verified_at, content_fingerprint")

    with ContinuityStore(tmp_path / "db.sqlite") as store:
        store.migrate(NOW_ISO)

        def _insert(pid):
            store._conn.execute(
                f"INSERT INTO projects ({cols}) VALUES (?, 'X', 'active', "
                "'owner', 'none', 'warm_only', 'normal', 1, ?, 'fp')",
                (pid, NOW_ISO))

        with pytest.raises(sqlite3.IntegrityError):
            _insert(None)
        assert store._conn.execute(
            "SELECT COUNT(*) FROM projects").fetchone()[0] == 0
        _insert("fine-id")
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute(
                "UPDATE projects SET project_id = NULL "
                "WHERE project_id = 'fine-id'")
        assert store._conn.execute(
            "SELECT project_id FROM projects").fetchone()[0] == "fine-id"
