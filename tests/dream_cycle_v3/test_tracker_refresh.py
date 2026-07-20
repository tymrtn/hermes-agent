"""Phase 3 task-SSOT refresh matrix: kanban/todoist/github, each through
closed / open / missing / stale / outage, plus truthful lookup freshness.

Review finding 6 regression coverage. Everything here is read-only; a stale
or missing tracker never closes a thread and never writes anywhere.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dream_cycle_v3.store import ContinuityStore
from dream_cycle_v3.tracker_refresh import (SNAPSHOT_FRESH_DAYS, refresh_refs)
from dream_cycle_v3.wake import WakeInputs, build_wake_packet

from .conftest import NOW_ISO, make_manifest_for_run

NOW = datetime.fromisoformat(NOW_ISO)
RUN_MANIFEST = make_manifest_for_run(profile="nagatha")


def _manifest_with_window_end(window_end: str) -> dict:
    from dream_cycle_v3 import COLLECTOR_VERSION
    from dream_cycle_v3.manifest import assemble_manifest
    return assemble_manifest(
        profile="nagatha",
        window_start="2026-05-29T00:00:00+00:00",
        window_end=window_end,
        collector_version=COLLECTOR_VERSION,
        bounds={"max_files_per_root": 64, "max_bytes_per_file": 65536,
                "max_total_bytes": 4194304, "max_depth": 8,
                "excerpt_chars": 700, "allowed_suffixes": [".md"]},
        sources=[], excluded=[], roots={"profile": "/tmp/example"},
        generated_at=NOW_ISO,
    )


def seed_store(tmp_path, *, snapshots=(), run_window_end=None):
    store_path = tmp_path / "continuity.db"
    manifest = dict(RUN_MANIFEST)
    if run_window_end:
        manifest = _manifest_with_window_end(run_window_end)
    with ContinuityStore(store_path) as store:
        store.migrate(NOW_ISO)
        store.record_run(manifest, "manifest.json", NOW_ISO)
        for adapter, items in snapshots:
            store.record_adapter_snapshot(
                run_id=manifest["run_id"], adapter=adapter,
                source_locator=f"{adapter}-source", status="ok", detail=None,
                items=items, now=NOW_ISO)
    return store_path


def seed_board(tmp_path, board="sample-board"):
    """Real shared-root layout: named boards under <root>/kanban/boards/,
    the special default board at <root>/kanban.db."""
    root = tmp_path / "hermes-root"
    from dream_cycle_v3.dry_run import SAMPLE_DATA
    if board == "default":
        root.mkdir(parents=True, exist_ok=True)
        db = root / "kanban.db"
    else:
        board_dir = root / "kanban" / "boards" / board
        board_dir.mkdir(parents=True)
        db = board_dir / "kanban.db"
    conn = sqlite3.connect(db)
    conn.executescript((SAMPLE_DATA / "kanban_seed.sql").read_text())
    conn.commit()
    conn.close()
    return root


def refresh(refs, *, store_path, kanban_root=None, export=None):
    with ContinuityStore(store_path, read_only=True) as store:
        return refresh_refs(list(refs), kanban_root=kanban_root,
                            todoist_export_path=export, store=store, now=NOW)


# -- kanban -------------------------------------------------------------------

def test_kanban_closed_open_missing_outage(tmp_path):
    store_path = seed_store(tmp_path)
    boards = seed_board(tmp_path)
    result = refresh(["kanban:sample-board:T-1001",     # done on board
                      "kanban:sample-board:T-1003",     # todo on board
                      "kanban:sample-board:T-9999"],    # not on board
                     store_path=store_path, kanban_root=boards)
    assert result.state_of("kanban:sample-board:T-1001") == "closed"
    assert result.state_of("kanban:sample-board:T-1003") == "open"
    assert result.state_of("kanban:sample-board:T-9999") == "stale"
    assert not result.outage

    outage = refresh(["kanban:sample-board:T-1003"], store_path=store_path,
                     kanban_root=tmp_path / "nonexistent")
    assert outage.state_of("kanban:sample-board:T-1003") == "stale"
    assert outage.outage
    assert any("kanban" in w for w in outage.warnings)


def test_default_board_refreshes_at_shared_root(tmp_path):
    # The default board's DB lives at <root>/kanban.db (back-compat), not
    # under kanban/boards/default/ — the layout helper must find it there.
    store_path = seed_store(tmp_path)
    root = seed_board(tmp_path, board="default")
    result = refresh(["kanban:default:T-1001", "kanban:default:T-1003"],
                     store_path=store_path, kanban_root=root)
    assert result.state_of("kanban:default:T-1001") == "closed"
    assert result.state_of("kanban:default:T-1003") == "open"
    assert not result.outage


def test_ambient_kanban_env_never_redirects_refresh(tmp_path, monkeypatch):
    # HERMES_KANBAN_DB pins a worker's CURRENT board; refresh for an
    # explicit ref on a different board must ignore it entirely.
    store_path = seed_store(tmp_path)
    root = seed_board(tmp_path)
    impostor = tmp_path / "impostor.db"
    conn = sqlite3.connect(impostor)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, "
                 "status TEXT, assignee TEXT, completed_at INTEGER)")
    conn.execute("INSERT INTO tasks VALUES ('T-1003', 'impostor', 'done', "
                 "'x', 1783600000)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(impostor))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "elsewhere"))
    result = refresh(["kanban:sample-board:T-1003"], store_path=store_path,
                     kanban_root=root)
    # Real board says todo/open; the env-pinned impostor said done.
    assert result.state_of("kanban:sample-board:T-1003") == "open"


# -- todoist ------------------------------------------------------------------

def test_todoist_live_export_closed_open_missing(tmp_path):
    from dream_cycle_v3.dry_run import SAMPLE_DATA
    store_path = seed_store(tmp_path)
    export = SAMPLE_DATA / "todoist_export.json"
    result = refresh(["todoist:8000000002",     # completed in export
                      "todoist:8000000001",     # active in export
                      "todoist:9999999999"],    # missing
                     store_path=store_path, export=export)
    assert result.state_of("todoist:8000000002") == "closed"
    assert result.state_of("todoist:8000000001") == "open"
    assert result.state_of("todoist:9999999999") == "stale"


def test_todoist_corrupt_export_is_outage(tmp_path):
    store_path = seed_store(tmp_path)
    bad = tmp_path / "todoist_export.json"
    bad.write_text("{corrupt", encoding="utf-8")
    result = refresh(["todoist:8000000001"], store_path=store_path,
                     export=bad)
    assert result.state_of("todoist:8000000001") == "stale"
    assert result.outage
    assert any("todoist" in w for w in result.warnings)


def test_todoist_fresh_snapshot_when_unconfigured(tmp_path):
    items = [{"ref": "todoist:8000000002", "state": "closed"},
             {"ref": "todoist:8000000001", "state": "open"}]
    store_path = seed_store(tmp_path, snapshots=[("todoist", items)])
    result = refresh(["todoist:8000000002", "todoist:8000000001"],
                     store_path=store_path)
    assert result.state_of("todoist:8000000002") == "closed"
    assert result.state_of("todoist:8000000001") == "open"
    st = result.states["todoist:8000000001"]
    assert st.source == "snapshot" and st.age_days is not None


def test_todoist_old_snapshot_is_stale_with_age_warning(tmp_path):
    items = [{"ref": "todoist:8000000002", "state": "closed"}]
    old_end = "2026-06-01T00:00:00+00:00"     # >> SNAPSHOT_FRESH_DAYS ago
    store_path = seed_store(tmp_path, snapshots=[("todoist", items)],
                            run_window_end=old_end)
    result = refresh(["todoist:8000000002"], store_path=store_path)
    # An old snapshot can neither prove open nor closed: stale, NOT closed.
    assert result.state_of("todoist:8000000002") == "stale"
    assert any("aged" in w and "todoist" in w for w in result.warnings)


def test_todoist_no_source_at_all_is_explicit_stale(tmp_path):
    store_path = seed_store(tmp_path)
    result = refresh(["todoist:8000000001"], store_path=store_path)
    assert result.state_of("todoist:8000000001") == "stale"
    assert result.outage
    assert any("todoist" in w and "stale" in w for w in result.warnings)


# -- github -------------------------------------------------------------------

def test_github_snapshot_states(tmp_path):
    items = [{"ref": "github:owner/repo#7", "state": "closed"},
             {"ref": "github:owner/repo#8", "state": "open"}]
    store_path = seed_store(tmp_path, snapshots=[("github", items)])
    result = refresh(["github:owner/repo#7", "github:owner/repo#8",
                      "github:owner/repo#9"], store_path=store_path)
    assert result.state_of("github:owner/repo#7") == "closed"
    assert result.state_of("github:owner/repo#8") == "open"
    assert result.state_of("github:owner/repo#9") == "stale"


def test_github_without_snapshot_is_explicit_stale(tmp_path):
    store_path = seed_store(tmp_path)
    result = refresh(["github:owner/repo#7"], store_path=store_path)
    assert result.state_of("github:owner/repo#7") == "stale"
    assert result.outage
    assert any("github" in w for w in result.warnings)


# -- wake integration: the review's exact repro --------------------------------

def make_thread(thread_id, ref, title):
    return {
        "schema_version": 1, "thread_id": thread_id,
        "project_id": "hermes-continuity", "external_task_ref": ref,
        "link_disposition": "linked" if ref else "needs_link",
        "title": title, "normalized_next_action": "follow up",
        "owner": "nagatha", "state": "active", "opened_from": "carry_forward",
        "evidence_refs": [{"source_type": "file",
                           "source_id": "profile:state/x.md",
                           "fingerprint": "fp-0000000000001",
                           "observed_at": NOW_ISO}],
        "last_disposition_date": "2026-07-10",
        # Due at NOW so the thread is eligible for the global due lane
        # (no project activates in these repros).
        "follow_up_after": "2026-07-10T00:00:00+00:00",
        "idempotency_key": f"idem-{thread_id}-0000000",
    }


PROJECT = {
    "schema_version": 1, "project_id": "hermes-continuity",
    "canonical_name": "Hermes continuity", "aliases": [],
    "canonical_paths": [], "repositories": [], "status": "active",
    "owner": "nagatha",
    "task_ssot": {"provider": "todoist", "locator": None,
                  "write_policy": "read_only"},
    "context_skill_id": None, "memory_policy": "warm_only",
    "sensitivity_policy": "normal", "retrieval_terms": [],
    "registry_version": 1, "last_verified_at": "2026-07-10T00:00:00+00:00",
}


def test_wake_drops_todoist_completed_thread_with_live_export(tmp_path):
    """The review's fixture repro: a thread whose todoist export marks it
    completed must NOT be injected as active with tracker_stale=False."""
    from dream_cycle_v3.dry_run import SAMPLE_DATA
    store_path = tmp_path / "continuity.db"
    with ContinuityStore(store_path) as store:
        store.migrate(NOW_ISO)
        store.upsert_project(PROJECT, NOW_ISO)
        store.open_thread(make_thread("todo-thread-0001-0000000",
                                      "todoist:8000000002",
                                      "Duplicate listing follow-up"), NOW_ISO)
    packet = build_wake_packet(
        store_path=store_path, projects_home=None, kanban_root=None,
        todoist_export_path=SAMPLE_DATA / "todoist_export.json",
        inputs=WakeInputs(profile="nagatha", owner="nagatha", now=NOW_ISO))
    assert "todo-thread-0001-0000000" not in packet.thread_ids
    assert "Duplicate listing follow-up" not in packet.text


def test_wake_marks_todoist_thread_stale_without_source(tmp_path):
    store_path = tmp_path / "continuity.db"
    with ContinuityStore(store_path) as store:
        store.migrate(NOW_ISO)
        store.upsert_project(PROJECT, NOW_ISO)
        store.open_thread(make_thread("todo-thread-0002-0000000",
                                      "todoist:8000000002",
                                      "Needs status check"), NOW_ISO)
    packet = build_wake_packet(
        store_path=store_path, projects_home=None, kanban_root=None,
        inputs=WakeInputs(profile="nagatha", owner="nagatha", now=NOW_ISO))
    assert "todo-thread-0002-0000000" in packet.thread_ids
    assert packet.tracker_stale is True
    assert "status stale" in packet.text or "may be stale" in packet.text


# -- lookup freshness truthfulness ---------------------------------------------

def test_lookup_reports_freshness_truthfully(tmp_path):
    from dream_cycle_v3.lookup import continuity_lookup
    from dream_cycle_v3.dry_run import SAMPLE_DATA
    store_path = tmp_path / "continuity.db"
    with ContinuityStore(store_path) as store:
        store.migrate(NOW_ISO)
        store.upsert_project(PROJECT, NOW_ISO)
        store.open_thread(make_thread("todo-thread-0003-0000000",
                                      "todoist:8000000002", "Check me"),
                          NOW_ISO)

    # No tracker source: stored status is labelled stored/stale, not live.
    stored = continuity_lookup(store_path=store_path,
                               thread_id="todo-thread-0003-0000000")
    assert stored["status_source"] == "stored_continuity"
    assert stored["tracker_state"] == "stale"

    # Live export: refresh says the tracker closed it.
    live = continuity_lookup(store_path=store_path,
                             thread_id="todo-thread-0003-0000000",
                             todoist_export_path=SAMPLE_DATA /
                             "todoist_export.json")
    assert live["status_source"] == "tracker_live"
    assert live["tracker_state"] == "closed"


# -- post-verification finding 10: freshness beyond the 500-row window ---------

def test_kanban_closed_task_beyond_list_window_reads_closed(tmp_path):
    """A closed task whose row sorts past any bounded board-list window must
    still refresh as closed — freshness for a referenced task can never
    depend on how many other tasks the board holds."""
    store_path = seed_store(tmp_path)
    root = tmp_path / "hermes-root"
    board_dir = root / "kanban" / "boards" / "big-board"
    board_dir.mkdir(parents=True)
    conn = sqlite3.connect(board_dir / "kanban.db")
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, "
                 "status TEXT, assignee TEXT, completed_at INTEGER)")
    conn.executemany(
        "INSERT INTO tasks VALUES (?, ?, 'todo', 'x', NULL)",
        [(f"A-{i:04d}", f"filler {i}") for i in range(600)])
    conn.execute("INSERT INTO tasks VALUES ('Z-9999', 'late closed', 'done', "
                 "'x', 1783600000)")
    conn.commit()
    conn.close()
    result = refresh(["kanban:big-board:Z-9999", "kanban:big-board:A-0500",
                      "kanban:big-board:NOPE"],
                     store_path=store_path, kanban_root=root)
    assert result.state_of("kanban:big-board:Z-9999") == "closed"
    assert result.state_of("kanban:big-board:A-0500") == "open"
    # A genuinely absent ref stays inconclusive — visible but stale.
    assert result.state_of("kanban:big-board:NOPE") == "stale"
    assert not result.outage


# -- post-verification finding 2: per-profile todoist export confinement -------

def test_todoist_symlinked_export_refused_under_confinement(tmp_path):
    """A symlinked per-profile export is refused (stale + outage), never
    read through — same boundary as stores/project docs/skills."""
    from dream_cycle_v3.dry_run import SAMPLE_DATA
    store_path = seed_store(tmp_path)
    home = tmp_path / "profile-home"
    (home / "dream-cycle-v3").mkdir(parents=True)
    export = home / "dream-cycle-v3" / "todoist_export.json"
    export.symlink_to(SAMPLE_DATA / "todoist_export.json")
    with ContinuityStore(store_path, read_only=True) as store:
        result = refresh_refs(["todoist:8000000002"], kanban_root=None,
                              todoist_export_path=export, store=store,
                              now=NOW, todoist_confine_home=home)
    assert result.state_of("todoist:8000000002") == "stale"
    assert result.outage


def test_wake_confine_root_refuses_symlinked_todoist_export(tmp_path):
    """End-to-end: with confine_root set, a symlinked export can never close
    (drop) a thread — the completed state behind the link is unreadable."""
    from dream_cycle_v3.dry_run import SAMPLE_DATA
    home = tmp_path / "profile-home"
    cont = home / "dream-cycle-v3"
    cont.mkdir(parents=True)
    store_path = cont / "continuity.db"
    with ContinuityStore(store_path) as store:
        store.migrate(NOW_ISO)
        store.upsert_project(PROJECT, NOW_ISO)
        store.open_thread(make_thread("conf-thread-0001-0000000",
                                      "todoist:8000000002",
                                      "Confined follow-up"), NOW_ISO)
    export = cont / "todoist_export.json"
    export.symlink_to(SAMPLE_DATA / "todoist_export.json")
    packet = build_wake_packet(
        store_path=store_path, projects_home=None, kanban_root=None,
        todoist_export_path=export, confine_root=home,
        inputs=WakeInputs(profile="nagatha", owner="nagatha", now=NOW_ISO))
    # The export marks the task completed; reading through the link would
    # drop the thread. Confinement keeps it visible and stale instead.
    assert "conf-thread-0001-0000000" in packet.thread_ids
    assert packet.tracker_stale is True
