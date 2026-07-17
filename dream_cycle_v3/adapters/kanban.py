"""Read-only Kanban SQLite adapter.

Live board layout (verified 2026-07-11 against ~/.hermes/kanban/boards/*/kanban.db):
`tasks(id TEXT, title TEXT, status TEXT, assignee TEXT, completed_at INTEGER, ...)`
with status in {todo, running, blocked, done, archived}.

Read-only is enforced twice at the SQLite layer: the connection is opened with
`mode=ro` (writes fail with 'attempt to write a readonly database') and
`PRAGMA query_only=ON` is set as belt and braces. The live boards directory
is littered with corruption backups, so any open/query failure degrades to a
typed 'error' result instead of raising.

WAL boards need more rules. Reading a WAL database initializes shared
memory on first access; when the `-wal`/`-shm` sidecars are missing, a
`mode=ro` connection either fails outright (older SQLite: 'unable to open
database file') or silently CREATES the missing files (newer SQLite) — and
this adapter must never create files next to a live board. So:

- Both sidecars present: normal `mode=ro` (attaches to the existing shared
  memory, sees fresh WAL rows, creates nothing).
- Both sidecars absent: read a VERIFIED PRIVATE SNAPSHOT COPY — source
  stat and sidecar absence must be identical before and after the bounded
  byte copy (so no writer touched the board mid-copy and no fresh WAL rows
  can be missed), the copy is integrity-checked, and its temp directory is
  removed when the connection closes. The live source is never opened by
  SQLite at all in this path, and never with `immutable=1`.
- Exactly one sidecar present (partial layout): unreadable without
  creating the missing counterpart -> typed error, nothing created.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .base import AdapterResult, TaskItem

ADAPTER_NAME = "kanban"
CLOSED_STATUSES = frozenset({"done", "archived"})
_MAX_ITEMS = 500
_SQLITE_MAGIC = b"SQLite format 3\x00"
_WAL_HEADER_VERSION = 2  # file format read version byte 18 == 2 => WAL
_SNAPSHOT_PREFIX = "dc3-kanban-snapshot-"
_MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024   # live boards are far smaller
_COPY_CHUNK = 1024 * 1024


def _epoch_to_iso(value: object) -> str | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) \
            or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _coerce_text(value: object) -> str:
    """Defensive text coercion for dynamically-typed SQLite columns."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _wal_state(db_path: Path) -> tuple[bool, bool, bool]:
    """(is_wal_header, wal_exists, shm_exists) for the on-disk layout."""
    wal = (db_path.parent / (db_path.name + "-wal")).exists()
    shm = (db_path.parent / (db_path.name + "-shm")).exists()
    try:
        with open(db_path, "rb") as fh:
            header = fh.read(19)
    except OSError:
        return False, wal, shm
    is_wal = (len(header) == 19 and header.startswith(_SQLITE_MAGIC)
              and header[18] == _WAL_HEADER_VERSION)
    return is_wal, wal, shm


def _connect(db_path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro" + ("&immutable=1" if immutable else "")
    conn = sqlite3.connect(uri, uri=True, timeout=5,
                           factory=_SnapshotConnection)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        # Force the lazy open now: WAL shared-memory initialization (and
        # corruption detection) happens on first read, so without this a
        # bad open would surface later as a misleading query failure.
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    except BaseException:
        conn.close()
        raise
    return conn


class _SnapshotConnection(sqlite3.Connection):
    """Connection that removes its private snapshot dir on close."""

    snapshot_dir: str | None = None

    def close(self) -> None:  # noqa: D102 - inherited semantics
        try:
            super().close()
        finally:
            directory, self.snapshot_dir = self.snapshot_dir, None
            if directory:
                shutil.rmtree(directory, ignore_errors=True)


def _copy_snapshot(src: Path, dst: Path) -> None:
    """Bounded streaming byte copy of the board file (never via SQLite)."""
    copied = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            chunk = fin.read(_COPY_CHUNK)
            if not chunk:
                break
            copied += len(chunk)
            if copied > _MAX_SNAPSHOT_BYTES:
                raise sqlite3.OperationalError(
                    f"kanban snapshot exceeds {_MAX_SNAPSHOT_BYTES} bytes; "
                    "refusing unbounded copy")
            fout.write(chunk)
        fout.flush()
        os.fsync(fout.fileno())


def _source_signature(db_path: Path) -> tuple:
    before = db_path.stat()
    _, wal, shm = _wal_state(db_path)
    after = db_path.stat()
    before_id = (before.st_dev, before.st_ino, before.st_size,
                 before.st_mtime_ns)
    after_id = (after.st_dev, after.st_ino, after.st_size,
                after.st_mtime_ns)
    if before_id != after_id:
        raise sqlite3.OperationalError(
            "kanban board changed while measuring snapshot boundary")
    return (*after_id, wal, shm)


def _open_via_snapshot(db_path: Path) -> sqlite3.Connection:
    """Verified private snapshot copy for a WAL db with absent sidecars.

    The stat+sidecar signature must be identical before and after the byte
    copy: any change means a writer was active (fresh rows could exist in
    a new WAL), so the snapshot is discarded with a typed error. The copy
    itself is verified (schema read + quick integrity check) before use.
    """
    before = _source_signature(db_path)
    if before[4] or before[5]:
        raise sqlite3.OperationalError(
            "kanban WAL sidecar appeared before snapshot; board is active")
    tmp_dir = tempfile.mkdtemp(prefix=_SNAPSHOT_PREFIX)
    try:
        copy_path = Path(tmp_dir) / db_path.name
        _copy_snapshot(db_path, copy_path)
        after = _source_signature(db_path)
        if after != before:
            raise sqlite3.OperationalError(
                "kanban board changed during snapshot copy (WAL sidecar or "
                "stat drift); refusing possibly-stale snapshot")
        # verify the private copy before serving reads from it; immutable
        # is safe here because nothing can write our private copy
        conn = _connect(copy_path, immutable=True)
        try:
            row = conn.execute("PRAGMA quick_check(1)").fetchone()
            if row is None or row[0] != "ok":
                raise sqlite3.DatabaseError(
                    f"kanban snapshot failed integrity check: "
                    f"{row[0] if row else 'no result'}")
        except BaseException:
            conn.close()
            raise
        conn.snapshot_dir = tmp_dir
        return conn
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def open_readonly(db_path: Path) -> sqlite3.Connection:
    is_wal, wal, shm = _wal_state(db_path)
    if not is_wal:
        return _connect(db_path)
    if wal and shm:
        # Never attach SQLite to the live WAL board. A writer can unlink both
        # sidecars between this observation and sqlite3.connect(), allowing a
        # nominal mode=ro open to create replacements beside the live board.
        # Fail closed and retry after checkpoint instead.
        raise sqlite3.OperationalError(
            "kanban WAL is active; retry after checkpoint")
    if wal or shm:
        raise sqlite3.OperationalError(
            "kanban WAL board has a partial sidecar layout; reading it "
            "could create the missing sidecar next to the live board")
    return _open_via_snapshot(db_path)


def read_kanban_task_project(db_path: str | Path, item_id: str
                             ) -> tuple[str, str | None]:
    """Canonical task->project resolution (Phase 3 activation evidence).

    Returns (status, project_id): ('ok', <id or None>) when the task row was
    read (project_id may legitimately be NULL, and is absent entirely on
    boards created before the column existed), ('missing', None) when the
    task is not on the board, ('error', None) when the board is unreadable.
    Read-only exactly like read_kanban_board.
    """
    path = Path(db_path).expanduser()
    try:
        is_file = path.is_file()
    except OSError:
        return "error", None
    if not is_file:
        return "error", None
    try:
        conn = open_readonly(path)
    except (sqlite3.Error, OSError):
        return "error", None
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        if "id" not in cols:
            return "error", None
        select = ("SELECT id, project_id FROM tasks WHERE id = ?"
                  if "project_id" in cols
                  else "SELECT id, NULL AS project_id FROM tasks WHERE id = ?")
        row = conn.execute(select, (str(item_id),)).fetchone()
        if row is None:
            return "missing", None
        project_id = row["project_id"]
        return "ok", (str(project_id) if project_id else None)
    except (sqlite3.Error, OSError):
        return "error", None
    finally:
        conn.close()


def read_kanban_task_states(db_path: str | Path, item_ids: list[str], *,
                            board_key: str | None = None
                            ) -> tuple[str, dict[str, str]]:
    """Targeted read of exactly *item_ids* -> ('ok', {ref: open|closed}).

    Freshness for a referenced task must never depend on how many other
    tasks the board holds, so this queries the referenced ids directly
    instead of a bounded board listing. Ids absent from the board are
    simply absent from the map (the caller keeps them inconclusive/stale).
    ('error', {}) when the board is unreadable. Read-only exactly like
    read_kanban_board.
    """
    path = Path(db_path).expanduser()
    board = board_key or path.parent.name or path.stem
    try:
        is_file = path.is_file()
    except OSError:
        return "error", {}
    if not is_file:
        return "error", {}
    try:
        conn = open_readonly(path)
    except (sqlite3.Error, OSError):
        return "error", {}
    try:
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "tasks" not in tables:
            return "error", {}
        states: dict[str, str] = {}
        ids = [str(i) for i in item_ids]
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT id, status FROM tasks WHERE id IN ({placeholders})",
                chunk).fetchall()
            for r in rows:
                state = ("closed" if (r["status"] or "") in CLOSED_STATUSES
                         else "open")
                states[f"kanban:{board}:{r['id']}"] = state
        return "ok", states
    except (sqlite3.Error, OSError):
        return "error", {}
    finally:
        conn.close()


def read_kanban_board(db_path: str | Path, *, board_key: str | None = None,
                      max_items: int = _MAX_ITEMS) -> AdapterResult:
    path = Path(db_path).expanduser()
    board = board_key or path.parent.name or path.stem
    locator = str(path)
    try:
        exists = path.exists()
    except OSError as exc:
        return AdapterResult.error(ADAPTER_NAME, locator,
                                   f"precheck_failed:{type(exc).__name__}:{exc}")
    if not exists:
        return AdapterResult.unavailable(ADAPTER_NAME, locator, "db_not_found")
    try:
        is_file = path.is_file()
    except OSError as exc:
        return AdapterResult.error(ADAPTER_NAME, locator,
                                   f"precheck_failed:{type(exc).__name__}:{exc}")
    if not is_file:
        return AdapterResult.unavailable(ADAPTER_NAME, locator, "not_a_file")
    try:
        conn = open_readonly(path)
    except (sqlite3.Error, OSError) as exc:
        return AdapterResult.error(ADAPTER_NAME, locator,
                                   f"open_failed:{type(exc).__name__}:{exc}")
    try:
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "tasks" not in tables:
            return AdapterResult.error(ADAPTER_NAME, locator,
                                       "schema_mismatch:no_tasks_table")
        rows = conn.execute(
            "SELECT id, title, status, assignee, completed_at FROM tasks "
            "ORDER BY id LIMIT ?", (max_items,)).fetchall()
        items = []
        for r in rows:
            item_id = _coerce_text(r["id"])
            status = _coerce_text(r["status"])
            items.append(TaskItem(
                item_id=item_id,
                ref=f"kanban:{board}:{item_id}",
                title=_coerce_text(r["title"]),
                state="closed" if status in CLOSED_STATUSES else "open",
                status_raw=status or "unknown",
                assignee=_coerce_text(r["assignee"]) or None,
                updated_at=_epoch_to_iso(r["completed_at"]),
            ))
        return AdapterResult.ok(ADAPTER_NAME, locator, items)
    except (sqlite3.Error, OSError, ValueError, TypeError,
            OverflowError) as exc:
        # malformed rows/values become a typed error, never an aborted cycle
        return AdapterResult.error(ADAPTER_NAME, locator,
                                   f"query_failed:{type(exc).__name__}:{exc}")
    finally:
        conn.close()
