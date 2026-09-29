import json

import pytest

from .conftest import NOW_ISO, TODAY, make_manifest_for_run
from dream_cycle_v3.carry_forward import CarryForwardPolicy, run_carry_forward
from dream_cycle_v3.errors import DispositionConflictError


@pytest.fixture
def seeded(store, sample_projects, sample_threads):
    manifest = make_manifest_for_run()
    store.record_run(manifest, "/tmp/m.json", NOW_ISO)
    for p in sample_projects:
        store.upsert_project(p, NOW_ISO)
    for t in sample_threads:
        store.open_thread(t, NOW_ISO, run_id=manifest["run_id"])
    # External task state as adapter snapshots: kanban T-1001 closed,
    # T-1002/T-1003 open; todoist 8000000002 closed, 8000000001 open.
    store.record_adapter_snapshot(
        run_id=manifest["run_id"], adapter="kanban", source_locator="sample.db",
        status="ok", detail=None, now=NOW_ISO, items=[
            {"item_id": "T-1001", "ref": "kanban:sample-board:T-1001",
             "state": "closed", "status_raw": "done"},
            {"item_id": "T-1002", "ref": "kanban:sample-board:T-1002",
             "state": "open", "status_raw": "blocked"},
            {"item_id": "T-1003", "ref": "kanban:sample-board:T-1003",
             "state": "open", "status_raw": "todo"},
        ])
    store.record_adapter_snapshot(
        run_id=manifest["run_id"], adapter="todoist", source_locator="export.json",
        status="ok", detail=None, now=NOW_ISO, items=[
            {"item_id": "8000000001", "ref": "todoist:8000000001",
             "state": "open", "status_raw": "active"},
            {"item_id": "8000000002", "ref": "todoist:8000000002",
             "state": "closed", "status_raw": "completed"},
        ])
    # A broken adapter must not contribute closure evidence but must not
    # break the run either.
    store.record_adapter_snapshot(
        run_id=manifest["run_id"], adapter="github", source_locator="octo/repo",
        status="unavailable", detail="gh_cli_not_found", items=[], now=NOW_ISO)
    return manifest


def _actions(store, date):
    return {r["thread_id"]: r for r in store._conn.execute(
        "SELECT * FROM thread_dispositions WHERE disposition_date = ?", (date,))}


def test_policy_matrix_one_disposition_each(store, seeded, sample_threads):
    report = run_carry_forward(store, run_id=seeded["run_id"],
                               disposition_date=TODAY, now=NOW_ISO)
    assert report.invariant_ok
    assert report.selected == len(sample_threads) == 8
    assert report.dispositioned == 8
    assert report.already_dispositioned == 0

    by_thread = {t["thread_id"]: t for t in report.threads}
    expect = {
        "sample-thread-0001-kanban-done": ("close_done", "done"),
        "sample-thread-0002-blocked-future": ("blocked", "blocked"),
        "sample-thread-0003-waiting-elapsed": ("continue", "active"),
        "sample-thread-0004-stale-aged": ("stale_review", "stale"),
        "sample-thread-0005-needs-link": ("needs_link", "observed"),
        "sample-thread-0006-todoist-done": ("close_done", "done"),
        "sample-thread-0007-authority-gated": ("authority_gated", "queued"),
        "sample-thread-0008-active-open": ("continue", "active"),
    }
    for thread_id, (action, state_after) in expect.items():
        assert by_thread[thread_id]["action"] == action, thread_id
        assert by_thread[thread_id]["state_after"] == state_after, thread_id
        assert store.get_thread(thread_id)["state"] == state_after

    rows = _actions(store, TODAY)
    assert len(rows) == 8  # exactly one disposition row per selected thread


def test_done_requires_closure_proof_and_stores_it(store, seeded):
    run_carry_forward(store, run_id=seeded["run_id"], disposition_date=TODAY,
                      now=NOW_ISO)
    row = _actions(store, TODAY)["sample-thread-0001-kanban-done"]
    proof = json.loads(row["closure_proof"])
    assert proof["kind"] == "task_event"
    assert proof["reference"] == "kanban:sample-board:T-1001"
    thread = store.get_thread("sample-thread-0001-kanban-done")
    assert thread["state"] == "done"
    assert json.loads(thread["closure_proof"])["reference"] == proof["reference"]
    zero = store._conn.execute(
        "SELECT COUNT(*) AS c FROM threads WHERE state='done' AND "
        "closure_proof IS NULL").fetchone()["c"]
    assert zero == 0


def test_identical_rerun_is_a_no_op(store, seeded):
    first = run_carry_forward(store, run_id=seeded["run_id"],
                              disposition_date=TODAY, now=NOW_ISO)
    counts = store.counts()
    dump = store.dump_canonical()
    second = run_carry_forward(store, run_id=seeded["run_id"],
                               disposition_date=TODAY, now=NOW_ISO)
    assert second.invariant_ok
    assert second.dispositioned == 0
    # Two threads closed in the first pass leave the nonterminal selection.
    assert second.selected == first.selected - 2
    assert second.already_dispositioned == second.selected
    assert store.counts() == counts
    assert store.dump_canonical() == dump


def test_next_day_produces_new_dispositions(store, seeded):
    run_carry_forward(store, run_id=seeded["run_id"], disposition_date=TODAY,
                      now=NOW_ISO)
    day2 = "2026-07-12"
    report = run_carry_forward(store, run_id=seeded["run_id"],
                               disposition_date=day2, now=NOW_ISO)
    assert report.invariant_ok
    assert report.selected == report.dispositioned == 6
    rows = _actions(store, day2)
    # Yesterday's stale thread is re-reviewed, not silently resurrected.
    assert rows["sample-thread-0004-stale-aged"]["action"] == "stale_review"
    # The revisited waiting thread now carries forward as active.
    assert rows["sample-thread-0003-waiting-elapsed"]["action"] == "continue"


def test_blocked_and_waiting_dispositions_carry_blocker(store, seeded):
    run_carry_forward(store, run_id=seeded["run_id"], disposition_date=TODAY,
                      now=NOW_ISO)
    row = _actions(store, TODAY)["sample-thread-0002-blocked-future"]
    assert row["action"] == "blocked"
    assert row["blocker"] == "tyler decision on offload approach"
    assert row["follow_up_after"] == "2026-07-14T09:00:00+00:00"


def test_project_scoping_selects_subset(store, seeded):
    report = run_carry_forward(store, run_id=seeded["run_id"],
                               disposition_date=TODAY, now=NOW_ISO,
                               project_ids=["klas-sample"])
    assert report.selected == 2
    assert set(_actions(store, TODAY)) == {"sample-thread-0006-todoist-done",
                                           "sample-thread-0007-authority-gated"}


def test_conflicting_manual_disposition_is_loud(store, seeded):
    store.record_disposition(
        thread_id="sample-thread-0008-active-open", disposition_date=TODAY,
        run_id=seeded["run_id"], action="defer", reason="manually deferred",
        state_after="active", now=NOW_ISO)
    # Carry-forward accepts the existing disposition (exactly-one preserved).
    report = run_carry_forward(store, run_id=seeded["run_id"],
                               disposition_date=TODAY, now=NOW_ISO)
    assert report.invariant_ok
    assert report.already_dispositioned == 1
    # But a second, different manual disposition is refused.
    with pytest.raises(DispositionConflictError):
        store.record_disposition(
            thread_id="sample-thread-0008-active-open", disposition_date=TODAY,
            run_id=seeded["run_id"], action="continue", reason="rewrite attempt",
            state_after="active", now=NOW_ISO)


def test_stale_policy_is_configurable(store, seeded):
    report = run_carry_forward(store, run_id=seeded["run_id"],
                               disposition_date=TODAY, now=NOW_ISO,
                               policy=CarryForwardPolicy(stale_after_days=365))
    by_thread = {t["thread_id"]: t for t in report.threads}
    assert by_thread["sample-thread-0004-stale-aged"]["action"] == "continue"


@pytest.mark.parametrize("bad_date", ["2026-99-99", "2026-02-30", "2026-13-01"])
def test_impossible_dates_rejected_before_any_write(store, seeded, bad_date):
    from dream_cycle_v3.errors import ContractViolation

    counts = store.counts()
    with pytest.raises(ContractViolation, match="not a valid calendar date"):
        run_carry_forward(store, run_id=seeded["run_id"],
                          disposition_date=bad_date, now=NOW_ISO)
    assert store.counts() == counts  # zero partial writes

    with pytest.raises(ContractViolation, match="not a valid calendar date"):
        store.record_disposition(
            thread_id="sample-thread-0008-active-open",
            disposition_date=bad_date, run_id=seeded["run_id"],
            action="continue", reason="x", state_after="active", now=NOW_ISO)
    with pytest.raises(ContractViolation, match="ISO-8601"):
        run_carry_forward(store, run_id=seeded["run_id"],
                          disposition_date=TODAY, now="2026-02-30T10:00:00+00:00")
    assert store.counts() == counts


def test_follow_up_comparison_is_semantic_not_lexical(store, seeded):
    # 23:00Z on the disposition date has elapsed at date granularity even
    # though a naive string slice would agree; the point is that the value is
    # PARSED — corrupt data cannot silently compare.
    store._conn.execute(
        "UPDATE threads SET follow_up_after = '2026-07-11T23:00:00Z' "
        "WHERE thread_id = 'sample-thread-0002-blocked-future'")
    report = run_carry_forward(store, run_id=seeded["run_id"],
                               disposition_date=TODAY, now=NOW_ISO)
    by_thread = {t["thread_id"]: t for t in report.threads}
    assert by_thread["sample-thread-0002-blocked-future"]["action"] == "continue"


def test_corrupt_follow_up_fails_loud_and_rolls_back(store, seeded):
    from dream_cycle_v3.errors import ContractViolation

    # Simulate legacy bad data written before semantic validation existed.
    store._conn.execute(
        "UPDATE threads SET follow_up_after = '2026-99-99T00:00:00Z' "
        "WHERE thread_id = 'sample-thread-0002-blocked-future'")
    counts = store.counts()
    with pytest.raises(ContractViolation, match="invalid follow_up_after"):
        run_carry_forward(store, run_id=seeded["run_id"],
                          disposition_date=TODAY, now=NOW_ISO)
    # The whole run rolled back: not even earlier threads kept dispositions.
    assert store.counts() == counts
    assert store._conn.execute(
        "SELECT COUNT(*) AS c FROM thread_dispositions WHERE disposition_date=?",
        (TODAY,)).fetchone()["c"] == 0


def test_timezone_less_follow_up_fails_loud_and_rolls_back(store, seeded):
    from dream_cycle_v3.errors import ContractViolation

    store._conn.execute(
        "UPDATE threads SET follow_up_after = '2026-07-11T23:00:00' "
        "WHERE thread_id = 'sample-thread-0002-blocked-future'")
    counts = store.counts()
    with pytest.raises(ContractViolation, match="invalid follow_up_after"):
        run_carry_forward(store, run_id=seeded["run_id"],
                          disposition_date=TODAY, now=NOW_ISO)
    assert store.counts() == counts
    assert store._conn.execute(
        "SELECT COUNT(*) AS c FROM thread_dispositions WHERE disposition_date=?",
        (TODAY,)).fetchone()["c"] == 0


def test_error_snapshot_cannot_prove_closure(store, sample_projects,
                                             sample_threads):
    from .conftest import make_manifest_for_run

    manifest = make_manifest_for_run()
    store.record_run(manifest, "/tmp/m.json", NOW_ISO)
    for p in sample_projects:
        store.upsert_project(p, NOW_ISO)
    thread = sample_threads[7]  # linked to kanban:sample-board:T-1003
    store.open_thread(thread, NOW_ISO)
    # An errored adapter snapshot claiming closure must be ignored wholesale.
    store.record_adapter_snapshot(
        run_id=manifest["run_id"], adapter="kanban", source_locator="sample.db",
        status="error", detail="unknown_issue_state:UNKNOWN:#9", now=NOW_ISO,
        items=[{"item_id": "T-1003", "ref": "kanban:sample-board:T-1003",
                "state": "closed", "status_raw": "UNKNOWN"}])
    report = run_carry_forward(store, run_id=manifest["run_id"],
                               disposition_date=TODAY, now=NOW_ISO)
    assert report.actions == {"continue": 1}
    assert store.get_thread(thread["thread_id"])["state"] == "active"
