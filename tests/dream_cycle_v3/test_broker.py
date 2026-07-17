"""Phase 3 canonical broker: bounded thread candidates, lane scoping,
shared provenance, and reference resolution parity for wake + lookup."""
import sqlite3

import pytest

from dream_cycle_v3.broker import (THREAD_CANDIDATE_LIMIT,
                                   collect_project_threads, load_registry,
                                   resolve_project_reference)
from dream_cycle_v3.contracts import parse_iso_datetime
from dream_cycle_v3.kanban_layout import kanban_db_path
from dream_cycle_v3.store import ContinuityStore

from .conftest import NOW_ISO

NOW = parse_iso_datetime(NOW_ISO)


def make_thread(i, *, owner="nagatha", project_id="proj-a", ref=None,
                follow_up=None, state="active"):
    return {
        "schema_version": 1, "thread_id": f"thread-{i:04d}-000000",
        "project_id": project_id, "external_task_ref": ref,
        "link_disposition": "linked" if ref else "needs_link",
        "title": f"thread number {i}", "normalized_next_action": "follow up",
        "owner": owner, "state": state, "opened_from": "carry_forward",
        "evidence_refs": [{"source_type": "file",
                           "source_id": "profile:state/x.md",
                           "fingerprint": f"fp-{i:013d}",
                           "observed_at": NOW_ISO}],
        "last_disposition_date": "2026-07-10",
        "follow_up_after": follow_up,
        "idempotency_key": f"idem-{i:04d}-00000000000",
    }


def make_project(pid):
    return {
        "schema_version": 1, "project_id": pid,
        "canonical_name": f"Project {pid}", "aliases": [f"{pid}-alias"],
        "canonical_paths": [], "repositories": [], "status": "active",
        "owner": "nagatha",
        "task_ssot": {"provider": "kanban", "locator": "sample-board",
                      "write_policy": "read_only"},
        "context_skill_id": None, "memory_policy": "warm_only",
        "sensitivity_policy": "normal", "retrieval_terms": [],
        "registry_version": 1,
        "last_verified_at": "2026-07-10T00:00:00+00:00",
    }


@pytest.fixture
def big_store(tmp_path):
    """40 owned threads across two projects — far more than any lane may
    load or refresh."""
    store_path = tmp_path / "continuity.db"
    with ContinuityStore(store_path) as store:
        store.migrate(NOW_ISO)
        store.upsert_project(make_project("proj-a"), NOW_ISO)
        store.upsert_project(make_project("proj-b"), NOW_ISO)
        for i in range(40):
            store.open_thread(make_thread(
                i,
                project_id="proj-a" if i % 2 == 0 else "proj-b",
                ref=f"kanban:board-{i}:T-{i}" if i % 3 == 0 else None,
                follow_up="2026-07-01T00:00:00+00:00" if i < 6 else None,
            ), NOW_ISO)
    return store_path


def test_candidate_window_bounds_refresh_sweep(big_store):
    # The refresh sweep sees at most the bounded candidate window's refs —
    # never the whole backlog (40 threads, ~13 refs stored).
    with ContinuityStore(big_store, read_only=True) as store:
        snapshots, refresh = collect_project_threads(
            store, now=NOW, owner="nagatha", limit=3,
            kanban_root=None, todoist_export_path=None)
    assert len(snapshots) <= 3
    assert len(refresh.states) <= THREAD_CANDIDATE_LIMIT


def test_project_lane_only_returns_that_project(big_store):
    with ContinuityStore(big_store, read_only=True) as store:
        snapshots, _ = collect_project_threads(
            store, now=NOW, owner="nagatha", project_id="proj-a", limit=10,
            kanban_root=None, todoist_export_path=None)
    assert snapshots
    assert all(s.row["project_id"] == "proj-a" for s in snapshots)


def test_due_only_lane_excludes_undated_threads(big_store):
    with ContinuityStore(big_store, read_only=True) as store:
        snapshots, _ = collect_project_threads(
            store, now=NOW, owner="nagatha", due_only=True, limit=10,
            kanban_root=None, todoist_export_path=None,
            candidate_limit=6)
    # Only the 6 threads with an elapsed follow-up qualify; undated threads
    # never ride the global lane.
    assert 0 < len(snapshots) <= 6
    assert all(s.row["follow_up_after"] for s in snapshots)


def test_owner_filter_is_sql_level(big_store):
    with ContinuityStore(big_store, read_only=True) as store:
        snapshots, _ = collect_project_threads(
            store, now=NOW, owner="somebody-else", limit=10,
            kanban_root=None, todoist_export_path=None)
    assert snapshots == []


def test_snapshots_carry_wake_shaped_provenance(big_store, tmp_path):
    with ContinuityStore(big_store, read_only=True) as store:
        snapshots, _ = collect_project_threads(
            store, now=NOW, owner="nagatha", project_id="proj-a", limit=10,
            kanban_root=tmp_path / "no-such-root",
            todoist_export_path=None)
    with_ref = [s for s in snapshots if s.row["external_task_ref"]]
    without_ref = [s for s in snapshots if not s.row["external_task_ref"]]
    assert all(s.tracker_state == "stale" and s.stale for s in with_ref)
    assert all(s.status_source == "no_task_ref" and not s.stale
               for s in without_ref)


def test_resolve_project_reference_matching(big_store):
    with ContinuityStore(big_store, read_only=True) as store:
        registry = load_registry(store)
    record, reason = resolve_project_reference(registry, "proj-a")
    assert reason == "ok" and record["project_id"] == "proj-a"
    record, reason = resolve_project_reference(registry, "proj-b-alias")
    assert reason == "ok" and record["project_id"] == "proj-b"
    record, reason = resolve_project_reference(registry, "Project proj-a")
    assert reason == "ok" and record["project_id"] == "proj-a"
    record, reason = resolve_project_reference(registry, "nope")
    assert record is None and reason == "unknown_project"


def test_registry_includes_context_skill_id(tmp_path):
    store_path = tmp_path / "continuity.db"
    project = make_project("proj-skill")
    project["context_skill_id"] = "ops/deploy-runbook"
    with ContinuityStore(store_path) as store:
        store.migrate(NOW_ISO)
        store.upsert_project(project, NOW_ISO)
        registry = load_registry(store)
    assert registry[0]["context_skill_id"] == "ops/deploy-runbook"


# -- kanban layout ------------------------------------------------------------

def test_kanban_db_path_layout(tmp_path):
    root = tmp_path / "hermes-root"
    assert kanban_db_path(root, "default") == root / "kanban.db"
    assert (kanban_db_path(root, "my-board")
            == root / "kanban" / "boards" / "my-board" / "kanban.db")


@pytest.mark.parametrize("bad", ["", "..", "a/b", "/abs", "UPPER", "-lead",
                                 "_lead", "x" * 65, None, 7])
def test_kanban_db_path_rejects_malformed_boards(tmp_path, bad):
    assert kanban_db_path(tmp_path, bad) is None


# -- post-verification finding 11: candidate ordering ---------------------------

def _seeded_store(tmp_path):
    store_path = tmp_path / "continuity.db"
    store = ContinuityStore(store_path)
    store.migrate(NOW_ISO)
    store.upsert_project(make_project("proj-a"), NOW_ISO)
    return store_path, store


def test_nondue_threads_surface_most_recent_first(tmp_path):
    """The broker's contract is due-first, then MOST recently updated —
    never the oldest rows."""
    store_path, store = _seeded_store(tmp_path)
    with store:
        for i, ts in enumerate(["2026-07-01T00:00:00+00:00",
                                "2026-07-05T00:00:00+00:00",
                                "2026-07-09T00:00:00+00:00"]):
            store.open_thread(make_thread(i), ts)
    with ContinuityStore(store_path, read_only=True) as store:
        snapshots, _ = collect_project_threads(
            store, now=NOW, owner="nagatha", project_id="proj-a", limit=2,
            kanban_root=None, todoist_export_path=None)
    assert [s.row["thread_id"] for s in snapshots] == [
        "thread-0002-000000", "thread-0001-000000"]


def test_future_dated_rows_never_outrank_due_or_recent(tmp_path):
    """A future follow-up is not due: it must neither crowd the bounded SQL
    window nor surface ahead of due rows or recently-updated undated rows."""
    store_path, store = _seeded_store(tmp_path)
    with store:
        # Five future-dated, long-untouched threads.
        for i in range(5):
            store.open_thread(
                make_thread(100 + i, follow_up="2026-08-01T00:00:00+00:00"),
                "2026-06-01T00:00:00+00:00")
        # One genuinely due thread.
        store.open_thread(
            make_thread(200, follow_up="2026-07-01T00:00:00+00:00"),
            "2026-06-15T00:00:00+00:00")
        # Two undated threads, recently updated.
        store.open_thread(make_thread(300), "2026-07-09T00:00:00+00:00")
        store.open_thread(make_thread(301), "2026-07-10T00:00:00+00:00")
    with ContinuityStore(store_path, read_only=True) as store:
        snapshots, _ = collect_project_threads(
            store, now=NOW, owner="nagatha", project_id="proj-a", limit=3,
            kanban_root=None, todoist_export_path=None, candidate_limit=4)
    assert [s.row["thread_id"] for s in snapshots] == [
        "thread-0200-000000", "thread-0301-000000", "thread-0300-000000"]


# -- re-review blocker 6: chronological ordering across timezone offsets --------

def test_due_selection_is_chronological_across_offsets(tmp_path):
    """The contract accepts any valid ISO offset, but the SQL pre-LIMIT
    window compared raw timestamp text: a due date written as
    2026-07-11T09:00:00+05:00 (04:00Z, an hour due at NOW=08:00Z) read as
    lexically greater than now's +00:00 form, so the row was classified
    not-due, deprioritized, and cut by the candidate window entirely."""
    store_path, store = _seeded_store(tmp_path)
    with store:
        # B: due at 04:00Z expressed with a +05:00 offset; long-untouched.
        store.open_thread(
            make_thread(400, follow_up="2026-07-11T09:00:00+05:00"),
            "2026-07-01T00:00:00+00:00")
        # A: due at 06:00Z (UTC form) — chronologically LATER than B.
        store.open_thread(
            make_thread(401, follow_up="2026-07-11T06:00:00+00:00"),
            "2026-07-01T01:00:00+00:00")
        # Two undated, recently-updated rows that crowd a lexical window.
        store.open_thread(make_thread(402), "2026-07-11T07:00:00+00:00")
        store.open_thread(make_thread(403), "2026-07-11T07:30:00+00:00")
    with ContinuityStore(store_path, read_only=True) as store:
        snapshots, _ = collect_project_threads(
            store, now=NOW, owner="nagatha", project_id="proj-a", limit=2,
            kanban_root=None, todoist_export_path=None, candidate_limit=3)
    # Earliest-due first regardless of how the offset was spelled.
    assert [s.row["thread_id"] for s in snapshots] == [
        "thread-0400-000000", "thread-0401-000000"]


def test_updated_recency_is_chronological_across_offsets(tmp_path):
    """updated_at recency must compare instants, not strings: a row updated
    at 04:00Z written as 2026-07-11T09:00:00+05:00 lexically outranked a
    row updated at 05:00Z written in UTC."""
    store_path, store = _seeded_store(tmp_path)
    with store:
        store.open_thread(make_thread(500), "2026-07-11T09:00:00+05:00")
        store.open_thread(make_thread(501), "2026-07-11T05:00:00+00:00")
    with ContinuityStore(store_path, read_only=True) as store:
        snapshots, _ = collect_project_threads(
            store, now=NOW, owner="nagatha", project_id="proj-a", limit=2,
            kanban_root=None, todoist_export_path=None)
    # 501 (05:00Z) is more recent than 500 (04:00Z).
    assert [s.row["thread_id"] for s in snapshots] == [
        "thread-0501-000000", "thread-0500-000000"]
