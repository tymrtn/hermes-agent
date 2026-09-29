import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from dream_cycle_v3.adapters import (read_github_issues, read_kanban_board,
                                     read_todoist_tasks)
from dream_cycle_v3.adapters.base import AdapterResult, TaskItem
from dream_cycle_v3.adapters.github import ReadOnlyViolation, _assert_read_only
from dream_cycle_v3.adapters.kanban import open_readonly
from dream_cycle_v3.dry_run import SAMPLE_DATA


@pytest.fixture
def kanban_db(tmp_path):
    db = tmp_path / "board" / "kanban.db"
    db.parent.mkdir()
    conn = sqlite3.connect(db)
    conn.executescript((SAMPLE_DATA / "kanban_seed.sql").read_text())
    conn.commit()
    conn.close()
    return db


# -- kanban ---------------------------------------------------------------

def test_kanban_reads_and_maps_states(kanban_db):
    result = read_kanban_board(kanban_db, board_key="sample-board")
    assert result.status == "ok"
    by_ref = {i.ref: i for i in result.items}
    assert by_ref["kanban:sample-board:T-1001"].state == "closed"
    assert by_ref["kanban:sample-board:T-1002"].state == "open"
    assert by_ref["kanban:sample-board:T-1002"].status_raw == "blocked"
    assert len(result.items) == 3


def test_kanban_missing_db_is_typed_unavailable(tmp_path):
    result = read_kanban_board(tmp_path / "nope" / "kanban.db")
    assert result.status == "unavailable"
    assert result.detail == "db_not_found"
    assert result.items == ()


def test_kanban_corrupt_db_is_typed_error(tmp_path):
    junk = tmp_path / "kanban.db"
    junk.write_bytes(b"this is not a sqlite database at all, sorry")
    result = read_kanban_board(junk)
    assert result.status == "error"
    assert "DatabaseError" in result.detail or "failed" in result.detail


def test_kanban_wrong_schema_is_typed_error(tmp_path):
    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE unrelated(x)")
    conn.commit()
    conn.close()
    result = read_kanban_board(db)
    assert result.status == "error"
    assert result.detail == "schema_mismatch:no_tasks_table"


def test_kanban_connection_cannot_write(kanban_db):
    conn = open_readonly(kanban_db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO tasks(id, title, status) VALUES ('X','x','todo')")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM tasks")
    conn.close()


def test_kanban_read_leaves_file_untouched(kanban_db):
    before = hashlib.sha256(kanban_db.read_bytes()).hexdigest()
    read_kanban_board(kanban_db)
    assert hashlib.sha256(kanban_db.read_bytes()).hexdigest() == before


# -- kanban: WAL boards read strictly read-only ------------------------------

def _make_wal_board(tmp_path):
    """A WAL-mode board with NO -wal/-shm sidecars (checkpointed + closed),
    exactly the live layout that broke the readonly open."""
    db = tmp_path / "board" / "kanban.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript((SAMPLE_DATA / "kanban_seed.sql").read_text())
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    for suffix in ("-wal", "-shm"):
        sidecar = db.parent / (db.name + suffix)
        if sidecar.exists():
            sidecar.unlink()
    assert db.read_bytes()[18] == 2, "board must be WAL-mode on disk"
    assert not list(db.parent.glob("kanban.db-*")), "sidecars must be absent"
    return db


def _dir_snapshot(root):
    return sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size)
                  for p in root.rglob("*"))


def test_kanban_wal_board_without_sidecars_reads_ok_and_creates_nothing(
        tmp_path):
    db = _make_wal_board(tmp_path)
    before = _dir_snapshot(db.parent)
    digest = hashlib.sha256(db.read_bytes()).hexdigest()

    result = read_kanban_board(db, board_key="sample-board")

    assert result.status == "ok"
    assert len(result.items) == 3
    assert _dir_snapshot(db.parent) == before, \
        "reading must create/modify no files (no -wal/-shm litter)"
    assert hashlib.sha256(db.read_bytes()).hexdigest() == digest


def _snapshot_temp_dirs():
    import tempfile
    return set(Path(tempfile.gettempdir()).glob("dc3-kanban-snapshot-*"))


def test_kanban_wal_board_reads_ok_when_sidecars_cannot_be_created(tmp_path):
    """The live failure shape: readonly WAL shared-memory init cannot
    create sidecars (here: unwritable board dir). With no -wal on disk the
    verified private snapshot copy is exact and must succeed."""
    db = _make_wal_board(tmp_path)
    db.parent.chmod(0o500)
    try:
        result = read_kanban_board(db, board_key="sample-board")
        listing = sorted(p.name for p in db.parent.iterdir())
    finally:
        db.parent.chmod(0o700)
    assert result.status == "ok"
    assert len(result.items) == 3
    assert listing == ["kanban.db"]


def test_kanban_snapshot_temp_is_cleaned_on_close(tmp_path):
    db = _make_wal_board(tmp_path)
    leaked_before = _snapshot_temp_dirs()
    result = read_kanban_board(db, board_key="sample-board")
    assert result.status == "ok"
    assert _snapshot_temp_dirs() == leaked_before, \
        "snapshot temp dirs must be removed when the connection closes"


def test_kanban_sidecar_appearing_during_copy_is_a_typed_error(tmp_path,
                                                               monkeypatch):
    """Race: a writer commits (creating -wal) while the snapshot copy is in
    flight. The after-copy stability check must refuse the snapshot with a
    typed error and clean its temp dir — never serve possibly-stale rows."""
    import dream_cycle_v3.adapters.kanban as kanban_mod
    db = _make_wal_board(tmp_path)
    real_copy = kanban_mod._copy_snapshot

    def racing_copy(src, dst):
        real_copy(src, dst)
        (db.parent / "kanban.db-wal").write_bytes(b"simulated writer commit")

    monkeypatch.setattr(kanban_mod, "_copy_snapshot", racing_copy)
    leaked_before = _snapshot_temp_dirs()
    result = read_kanban_board(db, board_key="sample-board")
    assert result.status == "error"
    assert result.items == ()
    assert _snapshot_temp_dirs() == leaked_before


def test_kanban_partial_sidecar_layouts_are_typed_errors_without_writes(
        tmp_path):
    """-wal without -shm (or vice versa) cannot be read without creating
    the missing counterpart: refuse with a typed error, create nothing."""
    for present, absent in (("-wal", "-shm"), ("-shm", "-wal")):
        db = _make_wal_board(tmp_path / present.strip("-"))
        (db.parent / (db.name + present)).write_bytes(b"leftover")
        result = read_kanban_board(db, board_key="sample-board")
        assert result.status == "error"
        assert "sidecar" in (result.detail or "")
        assert not (db.parent / (db.name + absent)).exists(), \
            f"reading must not create {absent}"


def test_kanban_titles_and_assignees_are_sanitized_in_payload(tmp_path):
    """Tracker snapshots persist only sanitized text: secret-shaped titles
    are withheld, emails redacted; refs/state/updated_at stay exact."""
    db = tmp_path / "board" / "kanban.db"
    db.parent.mkdir()
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE tasks (id TEXT, title TEXT, status TEXT, "
                 "assignee TEXT, completed_at INTEGER)")
    secret_title = "rotate deploy key ghp_0123456789abcdef0123456789abcdef0123"
    conn.execute("INSERT INTO tasks VALUES ('T-1', ?, 'todo', "
                 "'alice@example.com', 1783684800)", (secret_title,))
    conn.commit()
    conn.close()

    result = read_kanban_board(db, board_key="b")
    assert result.status == "ok"
    payload = json.dumps(result.items_payload())
    assert "ghp_0123456789abcdef" not in payload
    assert "alice@example.com" not in payload
    item = result.items_payload()[0]
    assert item["ref"] == "kanban:b:T-1"          # exact ref retained
    assert item["state"] == "open"
    assert item["updated_at"] is not None


def test_task_item_sanitizes_every_external_string_before_persistence():
    item = TaskItem(
        item_id="alice@example.com", ref="github:alice@example.com#1",
        title="ordinary title", state="open",
        status_raw="Authorization: Bearer sk-secretvalue1234567890",
        assignee="bob@example.com",
        updated_at="2026-07-13T00:00:00+00:00",
        url="https://example.invalid/task?token="
            "ghp_abcdefghijklmnopqrstuvwxyz")
    payload = json.dumps(item.to_dict())
    for secret in ("alice@example.com", "bob@example.com",
                   "sk-secretvalue1234567890",
                   "ghp_abcdefghijklmnopqrstuvwxyz"):
        assert secret not in payload


def test_kanban_snapshot_oserror_is_typed_and_does_not_escape(
        tmp_path, monkeypatch):
    import dream_cycle_v3.adapters.kanban as kanban_mod
    db = _make_wal_board(tmp_path)

    def denied(_path):
        raise OSError("simulated snapshot permission failure")

    monkeypatch.setattr(kanban_mod, "_open_via_snapshot", denied)
    result = read_kanban_board(db, board_key="sample-board")
    assert result.status == "error"
    assert "OSError" in (result.detail or "")


def test_kanban_malformed_values_do_not_abort(tmp_path):
    """Extreme timestamps and BLOB text fields must map to a usable row or
    a typed error — never an uncaught exception aborting the cycle."""
    db = tmp_path / "board" / "kanban.db"
    db.parent.mkdir()
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE tasks (id TEXT, title, status, assignee, "
                 "completed_at)")
    conn.execute("INSERT INTO tasks VALUES ('T-1', ?, 'todo', NULL, ?)",
                 (b"\xff\xfe blob title", 2**62))
    conn.execute("INSERT INTO tasks VALUES ('T-2', 'fine', ?, ?, -5)",
                 (b"todo", b"bob"))
    conn.commit()
    conn.close()

    result = read_kanban_board(db, board_key="b")   # must not raise
    assert result.status in ("ok", "error")
    if result.status == "ok":
        by_id = {i.item_id: i for i in result.items}
        assert by_id["T-1"].updated_at is None      # overflow clamped
        json.dumps(result.items_payload())          # serializable payload


def test_kanban_active_wal_with_sidecars_fails_typed_without_live_writes(
        tmp_path):
    """An active WAL cannot be safely attached after a sidecar pre-check:
    the writer may unlink both files before sqlite opens and a nominally
    readonly connection can recreate them. Fail typed and retry later rather
    than serve stale rows or touch the live board."""
    db = tmp_path / "board" / "kanban.db"
    db.parent.mkdir()
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.executescript((SAMPLE_DATA / "kanban_seed.sql").read_text())
    writer.commit()
    writer.execute("INSERT INTO tasks (id, title, status, assignee, "
                   "completed_at) VALUES ('T-2000', 'fresh in wal', 'todo', "
                   "NULL, NULL)")
    writer.commit()  # lives in the WAL; not yet checkpointed into the db
    try:
        assert (db.parent / "kanban.db-wal").exists()
        before = _dir_snapshot(db.parent)
        result = read_kanban_board(db, board_key="sample-board")
        assert result.status == "error"
        assert "WAL is active" in (result.detail or "")
        assert _dir_snapshot(db.parent) == before
    finally:
        writer.close()


def test_kanban_no_immutable_fallback_when_a_wal_sidecar_exists(tmp_path):
    """A present -wal may carry fresh rows; if the readonly open fails the
    adapter must degrade to a typed error, never to a possibly-stale
    immutable read — and the forced schema read surfaces it at open time."""
    db = _make_wal_board(tmp_path)
    (db.parent / "kanban.db-wal").write_bytes(b"")
    db.parent.chmod(0o500)
    try:
        result = read_kanban_board(db, board_key="sample-board")
    finally:
        db.parent.chmod(0o700)
    assert result.status == "error"
    assert result.detail.startswith("open_failed:")
    assert result.items == ()


# -- github -----------------------------------------------------------------

def _gh_runner(payload, returncode=0, stderr=""):
    calls = []

    def runner(argv):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, returncode,
                                           stdout=json.dumps(payload),
                                           stderr=stderr)

    return runner, calls


def test_github_parses_and_stays_read_only():
    payload = [
        {"number": 7, "title": "Fix flaky test", "state": "OPEN",
         "updatedAt": "2026-07-10T00:00:00Z",
         "url": "https://github.com/o/r/issues/7", "assignees": []},
        {"number": 3, "title": "Ship it", "state": "CLOSED",
         "updatedAt": "2026-07-09T00:00:00Z",
         "url": "https://github.com/o/r/issues/3",
         "assignees": [{"login": "octocat"}]},
    ]
    runner, calls = _gh_runner(payload)
    result = read_github_issues("octo/repo", runner=runner, gh_available=True)
    assert result.status == "ok"
    assert {i.ref: i.state for i in result.items} == {
        "github:octo/repo#7": "open", "github:octo/repo#3": "closed"}
    argv = calls[0]
    assert argv[:3] == ["gh", "issue", "list"]
    assert "--repo" in argv
    _assert_read_only(argv)  # the exact argv used passes the guard


def test_github_missing_cli_is_unavailable():
    result = read_github_issues("octo/repo", gh_available=False)
    assert result.status == "unavailable"
    assert result.detail == "gh_cli_not_found"


def test_github_bad_locator_rejected_before_any_execution():
    runner, calls = _gh_runner([])
    result = read_github_issues("--flag-injection", runner=runner,
                                gh_available=True)
    assert result.status == "unavailable"
    assert result.detail == "invalid_repo_locator"
    assert calls == []


def test_github_nonzero_exit_is_unavailable():
    runner, _ = _gh_runner([], returncode=4, stderr="auth required\n")
    result = read_github_issues("octo/repo", runner=runner, gh_available=True)
    assert result.status == "unavailable"
    assert result.detail.startswith("gh_exit_4:")


def test_github_garbage_output_is_typed_error():
    def runner(argv):
        return subprocess.CompletedProcess(argv, 0, stdout="not json", stderr="")

    result = read_github_issues("octo/repo", runner=runner, gh_available=True)
    assert result.status == "error"
    assert result.detail.startswith("gh_output_parse_failed")


@pytest.mark.parametrize("state", ["UNKNOWN", "MERGED", "Draft", "", None])
def test_github_unmapped_state_is_typed_error_never_closure(state):
    payload = [
        {"number": 7, "title": "fine", "state": "OPEN",
         "updatedAt": "2026-07-10T00:00:00Z", "url": "u", "assignees": []},
        {"number": 9, "title": "weird", "state": state,
         "updatedAt": "2026-07-10T00:00:00Z", "url": "u", "assignees": []},
    ]
    runner, _ = _gh_runner(payload)
    result = read_github_issues("octo/repo", runner=runner, gh_available=True)
    assert result.status == "error"
    assert result.detail.startswith("unknown_issue_state:")
    assert result.items == ()  # nothing usable, so nothing can prove closure


@pytest.mark.parametrize("argv", [
    ["gh", "issue", "create", "--repo", "o/r"],
    ["gh", "pr", "merge", "7"],
    ["gh", "issue", "list", "--repo", "o/r", "delete"],
    ["gh", "api", "repos/o/r/issues", "--method", "POST"],
    ["gh", "api", "repos/o/r/issues", "-f", "title=x"],
    ["rm", "-rf", "everything"],
])
def test_read_only_guard_rejects_mutation_shapes(argv):
    with pytest.raises(ReadOnlyViolation):
        _assert_read_only(argv)


def test_read_only_guard_accepts_get_api():
    _assert_read_only(["gh", "api", "repos/o/r/issues", "--method", "GET"])


# -- todoist -----------------------------------------------------------------

def test_todoist_export_ok():
    result = read_todoist_tasks(export_path=SAMPLE_DATA / "todoist_export.json")
    assert result.status == "ok"
    states = {i.ref: i.state for i in result.items}
    assert states == {"todoist:8000000001": "open", "todoist:8000000002": "closed"}


def test_todoist_sync_export_shape(tmp_path):
    export = tmp_path / "sync.json"
    export.write_text(json.dumps({"items": [
        {"id": 1, "content": "legacy shape", "checked": 1}]}))
    result = read_todoist_tasks(export_path=export)
    assert result.status == "ok"
    assert result.items[0].state == "closed"


def test_todoist_missing_export_is_unavailable(tmp_path):
    result = read_todoist_tasks(export_path=tmp_path / "gone.json")
    assert result.status == "unavailable"
    assert result.detail == "export_not_found"


def test_todoist_malformed_export_is_typed_error(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    result = read_todoist_tasks(export_path=bad)
    assert result.status == "error"
    assert result.detail.startswith("export_parse_failed")

    wrong_shape = tmp_path / "shape.json"
    wrong_shape.write_text(json.dumps({"tasks": []}))
    result = read_todoist_tasks(export_path=wrong_shape)
    assert result.status == "error"


def test_todoist_unconfigured_is_unavailable():
    result = read_todoist_tasks()
    assert result.status == "unavailable"
    assert result.detail == "no_export_path_and_no_api_token"


def test_todoist_api_uses_injected_get_only():
    seen = {}

    def fake_get(url, headers):
        seen["url"] = url
        seen["auth"] = headers.get("Authorization", "")
        return json.dumps([{"id": "9", "content": "from api",
                            "is_completed": False}])

    result = read_todoist_tasks(api_token="FAKE-TOKEN", http_get=fake_get)
    assert result.status == "ok"
    assert seen["url"].endswith("/rest/v2/tasks")
    assert seen["auth"] == "Bearer FAKE-TOKEN"


def test_todoist_api_network_failure_is_unavailable():
    def failing_get(url, headers):
        raise TimeoutError("no route to host")

    result = read_todoist_tasks(api_token="FAKE-TOKEN", http_get=failing_get)
    assert result.status == "unavailable"
    assert result.detail.startswith("api_unreachable")


# -- result type invariants ---------------------------------------------------

def test_adapter_result_type_invariants():
    with pytest.raises(ValueError):
        AdapterResult(adapter="x", source_locator="y", status="weird")
    with pytest.raises(ValueError):
        AdapterResult(adapter="x", source_locator="y", status="unavailable")
    with pytest.raises(ValueError):
        TaskItem(item_id="1", ref="r", title="t", state="half-done",
                 status_raw="x")
    ok = AdapterResult.ok("x", "y", [TaskItem(item_id="1", ref="b", title="t",
                                              state="open", status_raw="todo"),
                                     TaskItem(item_id="2", ref="a", title="t",
                                              state="open", status_raw="todo")])
    assert [i.ref for i in ok.items] == ["a", "b"]
    assert ok.fingerprint.startswith("sha256:")


# -- post-verification findings 2 and 10 --------------------------------------

def test_kanban_task_states_targeted_read(kanban_db):
    """Targeted per-id state read: referenced tasks resolve regardless of
    board size (never bounded by a list window)."""
    from dream_cycle_v3.adapters.kanban import read_kanban_task_states
    status, states = read_kanban_task_states(
        kanban_db, ["T-1001", "T-1003", "T-9999"], board_key="sample-board")
    assert status == "ok"
    assert states["kanban:sample-board:T-1001"] == "closed"
    assert states["kanban:sample-board:T-1003"] == "open"
    assert "kanban:sample-board:T-9999" not in states


def test_kanban_task_states_unreadable_board_is_error(tmp_path):
    from dream_cycle_v3.adapters.kanban import read_kanban_task_states
    status, states = read_kanban_task_states(
        tmp_path / "missing.db", ["T-1"], board_key="b")
    assert status == "error" and states == {}


# -- codex phase-4 third review finding 3: OSError from precheck itself ------
# The three public readers must not let an OSError raised directly out of
# their exists()/is_file() precheck escape uncaught: that would abort the
# whole cycle instead of degrading to a typed adapter result. Existing
# injection tests only reach the deeper snapshot helpers; these patch
# Path.exists/Path.is_file so the injection begins at the precheck itself.

def test_kanban_board_precheck_exists_oserror_is_typed(kanban_db, monkeypatch):
    def raiser(self):
        raise OSError("simulated stat failure during exists() precheck")
    monkeypatch.setattr(Path, "exists", raiser)
    result = read_kanban_board(kanban_db)
    assert result.status == "error"
    assert "precheck_failed" in result.detail
    assert result.items == ()


def test_kanban_board_precheck_is_file_oserror_is_typed(kanban_db, monkeypatch):
    def raiser(self):
        raise OSError("simulated stat failure during is_file() precheck")
    monkeypatch.setattr(Path, "is_file", raiser)
    result = read_kanban_board(kanban_db)
    assert result.status == "error"
    assert "precheck_failed" in result.detail
    assert result.items == ()


def test_kanban_task_project_precheck_oserror_is_typed(kanban_db, monkeypatch):
    from dream_cycle_v3.adapters.kanban import read_kanban_task_project

    def raiser(self):
        raise OSError("simulated stat failure during is_file() precheck")
    monkeypatch.setattr(Path, "is_file", raiser)
    status, project_id = read_kanban_task_project(kanban_db, "T-1001")
    assert status == "error"
    assert project_id is None


def test_kanban_task_states_precheck_oserror_is_typed(kanban_db, monkeypatch):
    from dream_cycle_v3.adapters.kanban import read_kanban_task_states

    def raiser(self):
        raise OSError("simulated stat failure during is_file() precheck")
    monkeypatch.setattr(Path, "is_file", raiser)
    status, states = read_kanban_task_states(
        kanban_db, ["T-1001"], board_key="sample-board")
    assert status == "error"
    assert states == {}


def test_todoist_confined_export_refuses_symlink(tmp_path):
    """A per-profile export must ride the same symlink confinement as
    stores, project docs, and skills when a confine home is declared."""
    home = tmp_path / "profile-home"
    cont = home / "dream-cycle-v3"
    cont.mkdir(parents=True)
    export = cont / "todoist_export.json"
    export.symlink_to(SAMPLE_DATA / "todoist_export.json")
    result = read_todoist_tasks(export_path=export, confine_home=home)
    assert result.status == "unavailable"
    assert "confin" in (result.detail or "")
    assert not result.items


def test_todoist_confined_export_regular_file_reads(tmp_path):
    home = tmp_path / "profile-home"
    cont = home / "dream-cycle-v3"
    cont.mkdir(parents=True)
    export = cont / "todoist_export.json"
    export.write_text((SAMPLE_DATA / "todoist_export.json").read_text(
        encoding="utf-8"), encoding="utf-8")
    result = read_todoist_tasks(export_path=export, confine_home=home)
    assert result.status == "ok" and result.items


def test_todoist_confined_export_outside_home_refused(tmp_path):
    home = tmp_path / "profile-home"
    home.mkdir()
    result = read_todoist_tasks(export_path=SAMPLE_DATA / "todoist_export.json",
                                confine_home=home)
    assert result.status == "unavailable"
    assert "confin" in (result.detail or "")
