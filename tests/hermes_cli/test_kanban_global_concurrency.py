"""Cross-board concurrency admission regressions for the Kanban dispatcher."""

from __future__ import annotations

import hermes_cli.kanban_db_connect as _reconciled_hermes_cli_kanban_db_connect
import hermes_cli.kanban_db_dispatch as _reconciled_hermes_cli_kanban_db_dispatch
import logging
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.config import DEFAULT_CONFIG


@pytest.fixture
def fleet_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for profile in ("alpha", "beta"):
        (home / "profiles" / profile).mkdir(parents=True)
    for slug in ("board-a", "board-b"):
        kb.create_board(slug=slug, name=slug)
    return home


def _create_tasks(board: str, assignee: str, count: int, *, priority: int = 0) -> list[str]:
    with _reconciled_hermes_cli_kanban_db_connect.connect_closing(board=board) as conn:
        return [
            kb.create_task(
                conn,
                title=f"{board}-{assignee}-{index}",
                assignee=assignee,
                priority=priority - index,
            )
            for index in range(count)
        ]


def _mark_running(board: str, task_ids: list[str]) -> None:
    """Mark tasks running with the claim bookkeeping of a live worker.

    A bare ``status='running'`` row (NULL claim) is orphan-shaped:
    ``reconcile_orphaned_running`` would requeue it on the next dispatch
    tick and dissolve the "workers already up" precondition these tests
    stage. An unexpired foreign-host claim keeps the rows counted as
    running without tripping the crash/stale/pidless reapers.
    """
    expires = int(time.time()) + 3600
    with _reconciled_hermes_cli_kanban_db_connect.connect_closing(board=board) as conn:
        for task_id in task_ids:
            conn.execute(
                "UPDATE tasks SET status = 'running', claim_lock = ?, "
                "claim_expires = ? WHERE id = ?",
                (f"otherhost:{task_id}", expires, task_id),
            )
        conn.commit()


def _corrupt_board_db(board: str) -> Path:
    """Give ``board`` a DB with a valid header but unreadable pages.

    Same shape as the corruption guard's fixture in ``test_kanban_db.py``:
    the cheap header check passes, then SQLite refuses the file. This is what
    a half-written board looks like on a machine that lost power mid-WAL.
    """
    path = kb.kanban_db_path(board=board)
    header = b"SQLite format 3\x00" + b"\x10\x00\x02\x02\x00\x40\x20\x20"
    header += b"\x00\x00\x00\x0c\x00\x00\x23\x46\x00\x00\x00\x00"
    header = header.ljust(100, b"\x00")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + b"definitely not a valid sqlite page \x00\x01\x02\x03" * 64)
    kb._INITIALIZED_PATHS.discard(str(path.resolve()))
    return path


def _running(board: str) -> list[tuple[str, str]]:
    with _reconciled_hermes_cli_kanban_db_connect.connect_closing(board=board) as conn:
        return [
            (row["id"], row["assignee"])
            for row in conn.execute(
                "SELECT id, assignee FROM tasks WHERE status = 'running' "
                "ORDER BY priority DESC, created_at ASC"
            )
        ]


def test_defaults_are_conservative_and_global():
    cfg = DEFAULT_CONFIG["kanban"]
    assert cfg["max_in_progress"] == 4
    assert cfg["max_in_progress_per_profile"] == 2
    assert cfg["max_in_progress_per_profile"] <= cfg["max_in_progress"]


def test_caps_count_running_workers_across_all_dispatched_boards(fleet_home):
    board_a_alpha = _create_tasks("board-a", "alpha", 6, priority=100)
    _create_tasks("board-b", "alpha", 6, priority=100)
    _create_tasks("board-b", "beta", 6, priority=10)
    scope = ["board-a", "board-b"]

    with _reconciled_hermes_cli_kanban_db_connect.connect_closing(board="board-a") as conn:
        first = _reconciled_hermes_cli_kanban_db_dispatch.dispatch_once(
            conn,
            board="board-a",
            admission_boards=scope,
            max_in_progress=4,
            max_in_progress_per_profile=2,
            spawn_fn=lambda *_args, **_kwargs: os.getpid(),
        )

    assert [task_id for task_id, _who, _workspace in first.spawned] == board_a_alpha[:2]
    assert _running("board-a") == [(board_a_alpha[0], "alpha"), (board_a_alpha[1], "alpha")]

    with _reconciled_hermes_cli_kanban_db_connect.connect_closing(board="board-b") as conn:
        second = _reconciled_hermes_cli_kanban_db_dispatch.dispatch_once(
            conn,
            board="board-b",
            admission_boards=scope,
            max_in_progress=4,
            max_in_progress_per_profile=2,
            spawn_fn=lambda *_args, **_kwargs: os.getpid(),
        )

    assert [who for _task_id, who, _workspace in second.spawned] == ["beta", "beta"]
    assert len([item for item in second.skipped_per_profile_capped if item[1] == "alpha"]) == 6
    assert len(_running("board-a")) + len(_running("board-b")) == 4
    assert _running("board-a") == [(board_a_alpha[0], "alpha"), (board_a_alpha[1], "alpha")]

    with _reconciled_hermes_cli_kanban_db_connect.connect_closing(board="board-b") as conn:
        restart_tick = _reconciled_hermes_cli_kanban_db_dispatch.dispatch_once(
            conn,
            board="board-b",
            admission_boards=scope,
            max_in_progress=4,
            max_in_progress_per_profile=2,
            spawn_fn=lambda *_args, **_kwargs: os.getpid(),
        )
    assert restart_tick.spawned == []
    assert len(_running("board-a")) + len(_running("board-b")) == 4


def test_global_admission_lock_prevents_cross_board_overclaim_race(fleet_home):
    _create_tasks("board-a", "alpha", 1)
    _create_tasks("board-b", "beta", 1)
    scope = ["board-a", "board-b"]
    start = threading.Barrier(2)
    results: dict[str, _reconciled_hermes_cli_kanban_db_dispatch.DispatchResult] = {}

    def run(board: str) -> None:
        with _reconciled_hermes_cli_kanban_db_connect.connect_closing(board=board) as conn:
            start.wait(timeout=2)
            results[board] = _reconciled_hermes_cli_kanban_db_dispatch.dispatch_once(
                conn,
                board=board,
                admission_boards=scope,
                max_in_progress=1,
                max_in_progress_per_profile=1,
                spawn_fn=lambda *_args, **_kwargs: (time.sleep(0.2), os.getpid())[1],
            )

    threads = [threading.Thread(target=run, args=(board,)) for board in scope]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()

    assert sum(len(result.spawned) for result in results.values()) == 1
    assert sum(result.skipped_admission_locked for result in results.values()) == 1
    assert len(_running("board-a")) + len(_running("board-b")) == 1


def test_cap_is_a_ceiling_when_no_cross_board_count_was_supplied(fleet_home):
    """With 3 workers already up and a cap of 4, one more may start — not 4.

    ``max_spawn`` is clamped to the *absolute* ceiling and the spawn loop
    compares ``running + spawned`` against it, so the running count has to be
    real. ``dispatch_once`` always supplies a cross-board pre-count, but the
    inner tick is also reached without one (the fallback when DB-path
    resolution fails); starting that loop from zero admits a full cap on top
    of the workers already running.
    """
    ids = _create_tasks("board-a", "alpha", 8, priority=100)
    _mark_running("board-a", ids[:3])

    with _reconciled_hermes_cli_kanban_db_connect.connect_closing(board="board-a") as conn:
        res = _reconciled_hermes_cli_kanban_db_dispatch._dispatch_once_locked(
            conn,
            board="board-a",
            max_in_progress=4,
            spawn_fn=lambda *_args, **_kwargs: os.getpid(),
        )

    assert len(res.spawned) == 1
    assert len(_running("board-a")) == 4


# ---------------------------------------------------------------------------
# A corrupt neighbour must fail admission closed without corrupting this board
# ---------------------------------------------------------------------------


def test_corrupt_secondary_board_fails_admission_closed(
    fleet_home, caplog
):
    """Unknown neighbour workers must never be treated as zero.

    The gateway wraps each tick in ``except ... _is_corrupt_board_db_error``
    and quarantines *the board it was ticking*. If a corrupt neighbour's
    error escapes, board-a is misidentified as corrupt. Admission therefore
    returns a structured fail-closed result naming the real culprit while
    leaving board-a's ready work untouched.
    """
    ready = _create_tasks("board-a", "alpha", 2, priority=100)
    _corrupt_board_db("board-b")

    with caplog.at_level(logging.ERROR):
        with _reconciled_hermes_cli_kanban_db_connect.connect_closing(board="board-a") as conn:
            res = _reconciled_hermes_cli_kanban_db_dispatch.dispatch_once(
                conn,
                board="board-a",
                admission_boards=["board-a", "board-b"],
                max_in_progress=4,
                max_in_progress_per_profile=2,
                spawn_fn=lambda *_args, **_kwargs: os.getpid(),
            )

    assert res.spawned == []
    assert res.skipped_uncounted_admission is True
    assert res.uncounted_admission_boards == ["board-b"]
    with _reconciled_hermes_cli_kanban_db_connect.connect_closing(board="board-a") as conn:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE status = 'ready' ORDER BY created_at, id"
        ).fetchall()
    # The contract is that every ready card survives admission failure.  Task
    # creation timestamps have one-second resolution, so ordering tied rows by
    # random task id is intentionally not an insertion-order guarantee.
    assert {row["id"] for row in rows} == set(ready)
    assert any(
        "board-b" in record.getMessage()
        for record in caplog.records
        if record.levelname == "ERROR"
    )


class _BrokenConn:
    """Stand-in for a connection whose own board DB went bad mid-tick."""

    def execute(self, *_args, **_kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")


def test_unreadable_current_board_still_raises_for_the_caller(fleet_home):
    """The current board's own failure must propagate, not be excluded.

    The other half of the previous test: swallowing *every* unreadable board
    would rob the gateway of the signal it quarantines on, and the dispatcher
    would count zero running workers on a board it cannot read.
    """
    with pytest.raises(sqlite3.DatabaseError):
        _reconciled_hermes_cli_kanban_db_dispatch._count_running_across_boards(
            _BrokenConn(),
            current_board="board-a",
            admission_boards=["board-a", "board-b"],
        )


# ---------------------------------------------------------------------------
# Board aliases: one DB file counted once
# ---------------------------------------------------------------------------


def test_admission_counting_dedupes_boards_pinned_to_one_db_by_env(
    fleet_home, monkeypatch
):
    """``HERMES_KANBAN_DB`` collapses every slug onto one file.

    Counting per slug then multiplies the same running workers by the number
    of boards in scope, and the fleet locks itself out at a fraction of the
    configured cap.
    """
    ids = _create_tasks("board-a", "alpha", 6, priority=100)
    _mark_running("board-a", ids[:2])
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path(board="board-a")))
    scope = ["board-a", "board-b"]

    with _reconciled_hermes_cli_kanban_db_connect.connect_closing(board="board-a") as conn:
        total, by_profile, uncounted = _reconciled_hermes_cli_kanban_db_dispatch._count_running_across_boards(
            conn, current_board="board-a", admission_boards=scope
        )
        assert (total, by_profile, uncounted) == (2, {"alpha": 2}, [])

        res = _reconciled_hermes_cli_kanban_db_dispatch.dispatch_once(
            conn,
            board="board-a",
            admission_boards=scope,
            max_in_progress=4,
            max_in_progress_per_profile=6,
            spawn_fn=lambda *_args, **_kwargs: os.getpid(),
        )

    # 2 running + 2 admitted == the cap. Double-counting would have read 4
    # already-running workers and spawned nothing.
    assert len(res.spawned) == 2
    assert len(_running("board-a")) == 4


def test_admission_counting_dedupes_symlinked_board_directories(fleet_home):
    """Dedup keys off the resolved path, so a symlinked board is one board."""
    ids = _create_tasks("board-a", "alpha", 6, priority=100)
    _mark_running("board-a", ids[:2])
    alias_dir = kb.board_dir("board-a").parent / "board-alias"
    alias_dir.symlink_to(kb.board_dir("board-a"), target_is_directory=True)

    with _reconciled_hermes_cli_kanban_db_connect.connect_closing(board="board-a") as conn:
        total, by_profile, uncounted = _reconciled_hermes_cli_kanban_db_dispatch._count_running_across_boards(
            conn,
            current_board="board-a",
            admission_boards=["board-a", "board-alias", "board-b"],
        )

    assert (total, by_profile, uncounted) == (2, {"alpha": 2}, [])


# ---------------------------------------------------------------------------
# Admission-only boards: nothing else ever reaps them
# ---------------------------------------------------------------------------


# A pid the test never spawns. ``_pid_alive`` is stubbed to recognise only this
# process, so this stands for a worker the kernel has already reclaimed.
_DEAD_PID = 4_000_001


def _plant_worker(board: str, task_id: str, *, pid: int) -> None:
    """Claim ``task_id`` on ``board`` and pin it to ``pid``, as a spawn would."""
    with _reconciled_hermes_cli_kanban_db_connect.connect_closing(board=board) as conn:
        assert kb.claim_task(conn, task_id) is not None
        _reconciled_hermes_cli_kanban_db_dispatch._set_worker_pid(conn, task_id, pid)


def _plant_pidless_claim(board: str, task_id: str, *, expired: bool) -> None:
    """Claim ``task_id`` and never stamp a worker PID.

    What the row looks like when a dispatcher dies between ``claim_task``
    and ``_set_worker_pid``: ``running``, holding a claim, with no PID for
    the crash reaper to check liveness against. ``expired`` backdates the
    claim past its TTL, which is the only thing separating debris from a
    claim whose worker is a second away from being stamped.
    """
    with _reconciled_hermes_cli_kanban_db_connect.connect_closing(board=board) as conn:
        assert kb.claim_task(conn, task_id) is not None
        assert kb.get_task(conn, task_id).worker_pid is None
        if expired:
            conn.execute(
                "UPDATE tasks SET claim_expires = ? WHERE id = ?",
                (int(time.time()) - 60, task_id),
            )
            conn.commit()


def test_crashed_worker_on_an_admission_only_board_stops_holding_a_cap_slot(
    fleet_home, monkeypatch
):
    """board-b is counted but never dispatched, so nothing else reaps it.

    Its worker is gone — reboot, OOM kill, ``kill -9`` — and the row stayed
    ``running``. Only boards in ``kanban.dispatch_boards`` run crash
    detection, so that row is counted against the machine-global cap on
    every future tick, forever: the fleet permanently runs one worker short
    of ``max_in_progress``, and at a cap of 1 it stops dispatching entirely.
    """
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(_reconciled_hermes_cli_kanban_db_dispatch, "_pid_alive", lambda pid: int(pid) == os.getpid())
    stale = _create_tasks("board-b", "alpha", 1)[0]
    _plant_worker("board-b", stale, pid=_DEAD_PID)
    ready = _create_tasks("board-a", "alpha", 1, priority=100)

    with _reconciled_hermes_cli_kanban_db_connect.connect_closing(board="board-a") as conn:
        res = _reconciled_hermes_cli_kanban_db_dispatch.dispatch_once(
            conn,
            board="board-a",
            admission_boards=["board-a", "board-b"],
            max_in_progress=1,
            spawn_fn=lambda *_args, **_kwargs: os.getpid(),
        )

    assert [task_id for task_id, _who, _ws in res.spawned] == ready
    with _reconciled_hermes_cli_kanban_db_connect.connect_closing(board="board-b") as conn:
        assert kb.get_task(conn, stale).status == "ready"
        # Reclaimed through the ordinary crash path, not a bare status flip:
        # the board keeps its crash event, run outcome and failure accounting.
        assert "crashed" in {event.kind for event in kb.list_events(conn, stale)}


def test_counting_an_admission_only_board_leaves_a_live_worker_running(
    fleet_home, monkeypatch
):
    """A worker that is still alive is counted and left completely alone.

    The other half of the reconcile: it may only touch rows whose worker is
    demonstrably gone. Counting a neighbour board must not open it for
    writing at all while every worker on it is healthy — reaping a live
    worker's row would hand its task to a second worker.
    """
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(_reconciled_hermes_cli_kanban_db_dispatch, "_pid_alive", lambda pid: int(pid) == os.getpid())
    live = _create_tasks("board-b", "alpha", 1)[0]
    _plant_worker("board-b", live, pid=os.getpid())

    def _refuse(*_args, **_kwargs):
        raise AssertionError("counting a healthy board must not write to it")

    monkeypatch.setattr(_reconciled_hermes_cli_kanban_db_dispatch, "detect_crashed_workers", _refuse)

    with _reconciled_hermes_cli_kanban_db_connect.connect_closing(board="board-a") as conn:
        counted = _reconciled_hermes_cli_kanban_db_dispatch._count_running_across_boards(
            conn, current_board="board-a", admission_boards=["board-a", "board-b"]
        )

    assert counted == (1, {"alpha": 1}, [])
    assert _running("board-b") == [(live, "alpha")]


def test_stale_pidless_claim_on_an_admission_only_board_releases_its_cap_slot(
    fleet_home,
):
    """A claim abandoned before its PID was stamped is invisible to the reaper.

    ``detect_crashed_workers`` selects ``worker_pid IS NOT NULL`` — liveness
    is a question about a PID — so a dispatcher killed between ``claim_task``
    and ``_set_worker_pid`` leaves a ``running`` row that nothing on an
    admission-only board can reap. Its TTL is the only evidence available,
    and until it is honoured the row spends a machine-global cap slot for
    good: at a cap of 1 the whole fleet stops dispatching.
    """
    abandoned = _create_tasks("board-b", "alpha", 1)[0]
    _plant_pidless_claim("board-b", abandoned, expired=True)
    ready = _create_tasks("board-a", "alpha", 1, priority=100)

    with _reconciled_hermes_cli_kanban_db_connect.connect_closing(board="board-a") as conn:
        res = _reconciled_hermes_cli_kanban_db_dispatch.dispatch_once(
            conn,
            board="board-a",
            admission_boards=["board-a", "board-b"],
            max_in_progress=1,
            spawn_fn=lambda *_args, **_kwargs: os.getpid(),
        )

    assert [task_id for task_id, _who, _ws in res.spawned] == ready
    with _reconciled_hermes_cli_kanban_db_connect.connect_closing(board="board-b") as conn:
        released = kb.get_task(conn, abandoned)
        assert released.status == "ready"
        assert (released.claim_lock, released.claim_expires) == (None, None)
        # Released through the ordinary TTL path, so the board keeps the
        # ``reclaimed`` event and run outcome its own dispatcher would write.
        assert "reclaimed" in {
            event.kind for event in kb.list_events(conn, abandoned)
        }


def test_fresh_pidless_claim_on_an_admission_only_board_is_counted_and_kept(
    fleet_home, monkeypatch
):
    """A claim inside its TTL has no PID *yet* — it is a worker, not debris.

    The other half of the TTL release: every claim is pidless for the window
    between ``claim_task`` and ``_set_worker_pid``, so reaping on
    pidlessness alone would release live workers' rows straight back to
    ``ready`` and hand each task to a second worker. Only the expired ones
    are debris; a fresh one stays counted against the cap and untouched.
    """
    fresh = _create_tasks("board-b", "alpha", 1)[0]
    _plant_pidless_claim("board-b", fresh, expired=False)

    def _refuse(*_args, **_kwargs):
        raise AssertionError("a claim still inside its TTL must not be reaped")

    monkeypatch.setattr(kb, "release_stale_claims", _refuse)
    monkeypatch.setattr(_reconciled_hermes_cli_kanban_db_dispatch, "detect_crashed_workers", _refuse)

    with _reconciled_hermes_cli_kanban_db_connect.connect_closing(board="board-a") as conn:
        counted = _reconciled_hermes_cli_kanban_db_dispatch._count_running_across_boards(
            conn, current_board="board-a", admission_boards=["board-a", "board-b"]
        )

    assert counted == (1, {"alpha": 1}, [])
    with _reconciled_hermes_cli_kanban_db_connect.connect_closing(board="board-b") as conn:
        kept = kb.get_task(conn, fresh)
        assert kept.status == "running"
        assert kept.claim_lock and kept.claim_expires
        assert "reclaimed" not in {event.kind for event in kb.list_events(conn, fresh)}


# ---------------------------------------------------------------------------
# Counting must never bring a board into existence
# ---------------------------------------------------------------------------


def test_admission_counting_never_creates_a_board_it_only_counts(fleet_home):
    """A typo in ``kanban.dispatch_boards`` must not become a real board.

    The admission scope is a union of every board on disk with the
    configured scope, so it legitimately names boards that do not exist:
    typos, boards that were archived, boards that were deleted. Opening
    those with ``connect()`` creates the directory, the DB and the schema
    for each one on every tick, and the typo then shows up in ``hermes
    kanban boards`` indistinguishable from a real board.
    """
    ids = _create_tasks("board-a", "alpha", 2)
    _mark_running("board-a", ids[:1])
    ghost_db = kb.kanban_db_path(board="board-ghost")

    with _reconciled_hermes_cli_kanban_db_connect.connect_closing(board="board-a") as conn:
        total, by_profile, uncounted = _reconciled_hermes_cli_kanban_db_dispatch._count_running_across_boards(
            conn,
            current_board="board-a",
            admission_boards=["board-a", "board-ghost"],
        )

    # A board with no DB holds no workers — zero, and not a read failure.
    assert (total, by_profile, uncounted) == (1, {"alpha": 1}, [])
    assert not ghost_db.exists()
    assert not ghost_db.parent.exists()
    assert "board-ghost" not in {board["slug"] for board in kb.list_boards()}


def test_dispatch_does_not_resurrect_a_board_deleted_out_from_under_the_scope(
    fleet_home,
):
    """A board deleted while still named in the scope counts as zero.

    End-to-end through ``dispatch_once``: the tick has to keep working and
    leave the deleted board deleted, rather than re-creating an empty
    board-b on every tick for as long as the stale config entry survives.
    """
    ready = _create_tasks("board-a", "alpha", 1, priority=100)
    board_b = kb.board_dir("board-b")
    kb._INITIALIZED_PATHS.discard(str(kb.kanban_db_path(board="board-b").resolve()))
    shutil.rmtree(board_b)

    with _reconciled_hermes_cli_kanban_db_connect.connect_closing(board="board-a") as conn:
        res = _reconciled_hermes_cli_kanban_db_dispatch.dispatch_once(
            conn,
            board="board-a",
            admission_boards=["board-a", "board-b"],
            max_in_progress=2,
            spawn_fn=lambda *_args, **_kwargs: os.getpid(),
        )

    assert [task_id for task_id, _who, _ws in res.spawned] == ready
    assert res.uncounted_admission_boards == []
    assert not board_b.exists()
