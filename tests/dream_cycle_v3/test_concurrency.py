"""Concurrent callers get typed idempotent outcomes; carry-forward is atomic."""
import json
import threading

import pytest

from .conftest import NOW_ISO, TODAY, make_manifest_for_run
from dream_cycle_v3.carry_forward import run_carry_forward
from dream_cycle_v3.collect import CollectionBounds, collect_to_manifest
from dream_cycle_v3.errors import CarryForwardInvariantError
from dream_cycle_v3.roots import CollectionRoots
from dream_cycle_v3.store import ContinuityStore

from .conftest import AS_OF, WINDOW_END, WINDOW_START, write_tree


def _seed(db_path, sample_projects, sample_threads):
    manifest = make_manifest_for_run()
    with ContinuityStore(db_path) as store:
        store.migrate(NOW_ISO)
        store.record_run(manifest, "/tmp/m.json", NOW_ISO)
        for p in sample_projects:
            store.upsert_project(p, NOW_ISO)
        for t in sample_threads:
            store.open_thread(t, NOW_ISO)
    return manifest


def _hammer(db_path, n_threads, fn):
    """Run fn(store, index) from n threads at once; collect outcomes/errors."""
    barrier = threading.Barrier(n_threads)
    outcomes: list = [None] * n_threads
    errors: list = [None] * n_threads

    def work(i):
        store = ContinuityStore(db_path)
        try:
            barrier.wait()
            outcomes[i] = fn(store, i)
        except Exception as exc:  # noqa: BLE001 — recorded for assertions
            errors[i] = exc
        finally:
            store.close()

    threads = [threading.Thread(target=work, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return outcomes, errors


def test_concurrent_dispositions_yield_one_insert(tmp_path, sample_projects,
                                                  sample_threads):
    db = tmp_path / "c.db"
    manifest = _seed(db, sample_projects, sample_threads)
    thread_id = sample_threads[7]["thread_id"]

    def record(store, _i):
        return store.record_disposition(
            thread_id=thread_id, disposition_date=TODAY,
            run_id=manifest["run_id"], action="continue", reason="carried",
            state_after="active", now=NOW_ISO)

    outcomes, errors = _hammer(db, 8, record)
    assert errors == [None] * 8
    assert sorted(outcomes) == ["inserted"] + ["unchanged"] * 7
    with ContinuityStore(db, read_only=True) as store:
        rows = store._conn.execute(
            "SELECT COUNT(*) AS c FROM thread_dispositions WHERE thread_id=?",
            (thread_id,)).fetchone()
        assert rows["c"] == 1


def test_concurrent_record_run_and_candidates(tmp_path, sample_projects,
                                              sample_threads):
    from .test_contracts import VALID_CANDIDATE

    db = tmp_path / "c.db"
    manifest = _seed(db, sample_projects, sample_threads)

    outcomes, errors = _hammer(
        db, 6, lambda store, _i: store.record_run(manifest, "/tmp/m.json", NOW_ISO))
    assert errors == [None] * 6
    assert all(o == "unchanged" for o in outcomes)

    candidate = json.loads(json.dumps(VALID_CANDIDATE))
    candidate["provenance"]["run_id"] = manifest["run_id"]
    outcomes, errors = _hammer(
        db, 6, lambda store, _i: store.ingest_candidate(candidate, NOW_ISO))
    assert errors == [None] * 6
    assert sorted(outcomes) == ["inserted"] + ["unchanged"] * 5
    with ContinuityStore(db, read_only=True) as store:
        assert store.counts()["candidates"] == 1


def test_concurrent_carry_forward_runs_agree(tmp_path, sample_projects,
                                             sample_threads):
    db = tmp_path / "c.db"
    manifest = _seed(db, sample_projects, sample_threads)

    def carry(store, _i):
        report = run_carry_forward(store, run_id=manifest["run_id"],
                                   disposition_date=TODAY, now=NOW_ISO)
        return (report.dispositioned, report.already_dispositioned,
                report.invariant_ok)

    outcomes, errors = _hammer(db, 2, carry)
    assert errors == [None, None]
    assert all(ok for _, _, ok in outcomes)
    # One run wrote all 8; the other found them pre-existing (order-free check).
    assert sorted(o[0] for o in outcomes) == [0, 8]
    with ContinuityStore(db, read_only=True) as store:
        assert store.counts()["thread_dispositions"] == 8


def test_carry_forward_rolls_back_wholesale_on_failure(store, sample_projects,
                                                       sample_threads,
                                                       monkeypatch):
    manifest = make_manifest_for_run()
    store.record_run(manifest, "/tmp/m.json", NOW_ISO)
    for p in sample_projects:
        store.upsert_project(p, NOW_ISO)
    for t in sample_threads:
        store.open_thread(t, NOW_ISO)
    counts_before = store.counts()
    states_before = {t["thread_id"]: t["state"]
                     for t in store.select_nonterminal_threads()}

    real = store.record_disposition
    calls = {"n": 0}

    def failing(**kwargs):
        calls["n"] += 1
        if calls["n"] == 6:
            raise RuntimeError("simulated mid-run failure")
        return real(**kwargs)

    monkeypatch.setattr(store, "record_disposition", failing)
    with pytest.raises(RuntimeError, match="simulated"):
        run_carry_forward(store, run_id=manifest["run_id"],
                          disposition_date=TODAY, now=NOW_ISO)

    # Everything the first five calls wrote must be gone: one atomic unit.
    assert store.counts() == counts_before
    assert store._conn.execute(
        "SELECT COUNT(*) AS c FROM thread_dispositions WHERE disposition_date=?",
        (TODAY,)).fetchone()["c"] == 0
    states_after = {t["thread_id"]: t["state"]
                    for t in store.select_nonterminal_threads()}
    assert states_after == states_before
    assert not store._conn.in_transaction

    # A clean rerun afterwards succeeds fully.
    monkeypatch.setattr(store, "record_disposition", real)
    report = run_carry_forward(store, run_id=manifest["run_id"],
                               disposition_date=TODAY, now=NOW_ISO)
    assert report.invariant_ok and report.dispositioned == len(sample_threads)


def test_invariant_violation_rolls_back(store, sample_projects, sample_threads,
                                        monkeypatch):
    manifest = make_manifest_for_run()
    store.record_run(manifest, "/tmp/m.json", NOW_ISO)
    for p in sample_projects:
        store.upsert_project(p, NOW_ISO)
    for t in sample_threads:
        store.open_thread(t, NOW_ISO)
    counts_before = store.counts()

    real = store.record_disposition

    def skipping(**kwargs):
        if kwargs["thread_id"] == sample_threads[4]["thread_id"]:
            return "unchanged"  # lie: nothing written for this thread
        return real(**kwargs)

    monkeypatch.setattr(store, "record_disposition", skipping)
    with pytest.raises(CarryForwardInvariantError):
        run_carry_forward(store, run_id=manifest["run_id"],
                          disposition_date=TODAY, now=NOW_ISO)
    assert store.counts() == counts_before


def test_concurrent_manifest_writers_never_overwrite(tmp_path):
    root = write_tree(tmp_path / "src", {"note.md": "concurrent publish target\n"})
    roots = CollectionRoots.resolve("race-profile", {"profile": root})
    out = tmp_path / "out"

    barrier = threading.Barrier(6)
    results: list = [None] * 6

    def publish(i):
        barrier.wait()
        try:
            _, path = collect_to_manifest(roots, out, window_start=WINDOW_START,
                                          window_end=WINDOW_END,
                                          bounds=CollectionBounds(),
                                          generated_at=AS_OF)
            results[i] = path
        except Exception as exc:  # noqa: BLE001
            results[i] = exc

    threads = [threading.Thread(target=publish, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    paths = {r for r in results if not isinstance(r, Exception)}
    assert not any(isinstance(r, Exception) for r in results), results
    assert len(paths) == 1
    manifests = list((out / "manifests").iterdir())
    assert [p.name for p in manifests] == [paths.pop().name]  # no stray temp files
