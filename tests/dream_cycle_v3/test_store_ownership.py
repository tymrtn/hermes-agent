"""Adversarial tests: the store must refuse to touch anything it does not own."""
import hashlib
import sqlite3

import pytest

from .conftest import NOW_ISO
from dream_cycle_v3.cli import main
from dream_cycle_v3.dry_run import SAMPLE_DATA
from dream_cycle_v3.errors import StoreError, StoreOwnershipError
from dream_cycle_v3.store import (V3_APPLICATION_ID, ContinuityStore,
                                  inspect_store_identity)


@pytest.fixture
def kanban_db(tmp_path):
    """A task-SSOT database — the thing that must never be mutated."""
    db = tmp_path / "boards" / "hermes" / "kanban.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.executescript((SAMPLE_DATA / "kanban_seed.sql").read_text())
    conn.commit()
    conn.close()
    return db


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_kanban_db_is_refused_and_untouched(kanban_db):
    before = _sha(kanban_db)
    with pytest.raises(StoreOwnershipError, match="task/Kanban"):
        ContinuityStore(kanban_db)
    with pytest.raises(StoreOwnershipError):
        ContinuityStore(kanban_db, read_only=True)
    assert _sha(kanban_db) == before
    assert not kanban_db.with_name("kanban.db-wal").exists()
    assert not kanban_db.with_name("kanban.db-shm").exists()


def test_arbitrary_sqlite_db_is_refused_and_untouched(tmp_path):
    db = tmp_path / "random.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    before = _sha(db)
    with pytest.raises(StoreOwnershipError, match="application_id"):
        ContinuityStore(db)
    assert _sha(db) == before


def test_non_sqlite_file_is_refused_and_untouched(tmp_path):
    f = tmp_path / "notes.db"
    f.write_bytes(b"just some bytes, definitely not a database header....." * 3)
    before = _sha(f)
    with pytest.raises(StoreOwnershipError, match="not a SQLite database"):
        ContinuityStore(f)
    assert _sha(f) == before


def test_directory_path_is_refused(tmp_path):
    with pytest.raises(StoreOwnershipError, match="not a regular file"):
        ContinuityStore(tmp_path)


def test_fresh_store_is_stamped_and_reopens(tmp_path):
    path = tmp_path / "continuity.db"
    assert inspect_store_identity(path) == "fresh"
    with ContinuityStore(path) as store:
        store.migrate(NOW_ISO)
        app_id = store._conn.execute("PRAGMA application_id").fetchone()[0]
        assert app_id == V3_APPLICATION_ID
    assert inspect_store_identity(path) == "owned"
    with ContinuityStore(path) as store:          # writable reopen
        assert store.schema_version() == 4
    with ContinuityStore(path, read_only=True) as store:  # read-only reopen
        assert store.schema_version() == 4
        with pytest.raises(sqlite3.OperationalError):
            store._conn.execute("INSERT INTO schema_migrations VALUES (9,'x','y')")


def test_empty_file_counts_as_fresh(tmp_path):
    path = tmp_path / "empty.db"
    path.touch()
    assert inspect_store_identity(path) == "fresh"
    with ContinuityStore(path) as store:
        store.migrate(NOW_ISO)


def test_read_only_open_requires_existing_store(tmp_path):
    with pytest.raises(StoreError, match="no continuity store"):
        ContinuityStore(tmp_path / "missing.db", read_only=True)


def test_cli_refuses_kanban_db_inside_v3_root(tmp_path, kanban_db, capsys):
    # Even placed inside the declared root, a foreign DB is refused by identity.
    root = kanban_db.parent.parent  # boards/
    before = _sha(kanban_db)
    code = main(["init-db", "--db", str(kanban_db), "--v3-root", str(root)])
    err = capsys.readouterr().err
    assert code == 2
    assert "StoreOwnershipError" in err
    assert _sha(kanban_db) == before

    code = main(["carry-forward", "--db", str(kanban_db), "--v3-root", str(root),
                 "--run-id", "0" * 32, "--date", "2026-07-11",
                 "--as-of", "2026-07-11T08:00:00+00:00"])
    assert code == 2
    assert "StoreOwnershipError" in capsys.readouterr().err
    assert _sha(kanban_db) == before


def test_cli_refuses_db_outside_v3_root(tmp_path, capsys):
    outside = tmp_path / "elsewhere" / "c.db"
    outside.parent.mkdir()
    root = tmp_path / "v3-root"
    code = main(["init-db", "--db", str(outside), "--v3-root", str(root)])
    err = capsys.readouterr().err
    assert code == 2
    assert "RootResolutionError" in err
    assert not outside.exists()
