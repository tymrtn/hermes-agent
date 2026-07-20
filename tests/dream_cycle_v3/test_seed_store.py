"""Phase 4 `seed-store` CLI seam.

Seeds a confined v3 continuity store from operator-reviewed registry and
thread JSON using the existing validated store primitives. Everything is
explicit (--v3-root, --as-of), the store must be owned and inside the
declared root, reruns are no-ops, and the only output is one bounded JSON
status line. No destination promotion, tracker mutation, or ambient
discovery.
"""
import hashlib
import json
import sqlite3

import pytest

from dream_cycle_v3.cli import main
from dream_cycle_v3.dry_run import SAMPLE_DATA
from dream_cycle_v3.store import ContinuityStore

AS_OF = "2026-07-12T08:00:00+00:00"


@pytest.fixture
def env(tmp_path):
    registry = tmp_path / "projects.json"
    registry.write_text(
        (SAMPLE_DATA / "projects.json").read_text(encoding="utf-8"),
        encoding="utf-8")
    threads = tmp_path / "threads.json"
    threads.write_text(
        (SAMPLE_DATA / "threads.json").read_text(encoding="utf-8"),
        encoding="utf-8")
    return {"root": tmp_path / "v3-shadow", "registry": registry,
            "threads": threads}


def seed_args(env, **over):
    args = {"--v3-root": str(env["root"]), "--as-of": AS_OF,
            "--registry": str(env["registry"]),
            "--threads": str(env["threads"])}
    args.update(over)
    argv = ["seed-store"]
    for key, value in args.items():
        if value is not None:
            argv.extend([key, value])
    return argv


def test_seed_store_requires_explicit_v3_root(env, capsys):
    with pytest.raises(SystemExit) as exc:
        main(seed_args(env, **{"--v3-root": None}))
    assert exc.value.code == 2


def test_seed_store_requires_explicit_as_of(env, capsys):
    with pytest.raises(SystemExit) as exc:
        main(seed_args(env, **{"--as-of": None}))
    assert exc.value.code == 2


def test_seed_store_requires_an_operator_reviewed_input(env, capsys):
    rc = main(seed_args(env, **{"--registry": None, "--threads": None}))
    captured = capsys.readouterr()
    assert rc != 0
    assert captured.out == ""
    assert "registry" in captured.err and "threads" in captured.err


def test_seed_store_seeds_canonical_db_with_one_bounded_json_line(env, capsys):
    rc = main(seed_args(env))
    captured = capsys.readouterr()
    assert rc == 0, captured.err

    lines = captured.out.splitlines()
    assert len(lines) == 1, "exactly one bounded status line"
    result = json.loads(lines[0])

    db_path = env["root"] / "continuity.db"
    assert result["db"] == str(db_path)
    assert result["as_of"] == AS_OF
    assert db_path.is_file()

    projects = json.loads(env["registry"].read_text(encoding="utf-8"))
    threads = json.loads(env["threads"].read_text(encoding="utf-8"))
    assert result["registry"]["outcomes"] == {"inserted": len(projects)}
    assert result["threads"]["inserted"] == len(threads)
    assert result["threads"]["exists"] == 0

    # counts only — never row content — in the status result
    blob = lines[0]
    for thread in threads:
        assert thread["title"] not in blob

    with ContinuityStore(db_path, read_only=True) as store:
        counts = store.counts()
        assert counts["projects"] == len(projects)
        assert counts["threads"] == len(threads)
        # no promotion, tracker, or run/candidate side effects
        assert counts["write_receipts"] == 0
        assert counts["candidates"] == 0
        assert counts["adapter_snapshots"] == 0
        assert counts["runs"] == 0


def test_seed_store_is_idempotent(env, capsys):
    assert main(seed_args(env)) == 0
    capsys.readouterr()
    db_path = env["root"] / "continuity.db"
    with ContinuityStore(db_path, read_only=True) as store:
        dump_before = store.dump_canonical()

    rc = main(seed_args(env))
    captured = capsys.readouterr()
    assert rc == 0
    result = json.loads(captured.out)
    projects = json.loads(env["registry"].read_text(encoding="utf-8"))
    threads = json.loads(env["threads"].read_text(encoding="utf-8"))
    assert result["registry"]["outcomes"] == {"unchanged": len(projects)}
    assert result["threads"] == {"path": str(env["threads"]),
                                 "inserted": 0, "exists": len(threads)}

    with ContinuityStore(db_path, read_only=True) as store:
        assert store.dump_canonical() == dump_before


def test_seed_store_refuses_db_outside_the_declared_root(env, tmp_path,
                                                         capsys):
    foreign = tmp_path / "elsewhere" / "continuity.db"
    rc = main(seed_args(env, **{"--db": str(foreign)}))
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert not foreign.exists()


def test_seed_store_refuses_symlinked_store_path(env, tmp_path, capsys):
    env["root"].mkdir()
    target = tmp_path / "outside.db"
    link = env["root"] / "continuity.db"
    link.symlink_to(target)
    rc = main(seed_args(env))
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "symlink" in captured.err
    assert not target.exists()


def test_seed_store_refuses_dotdot_escape_to_nonexistent_target(env, tmp_path,
                                                                capsys):
    """Lexical '..' plus a nonexistent target must not bypass confinement
    (codex phase-4 finding 1)."""
    escape = tmp_path / "v3-shadow" / ".." / "escaped.db"
    rc = main(seed_args(env, **{"--db": str(escape)}))
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert not (tmp_path / "escaped.db").exists()


def test_seed_store_refuses_nested_dotdot_escape(env, tmp_path, capsys):
    escape = tmp_path / "v3-shadow" / "sub" / ".." / ".." / "escaped.db"
    rc = main(seed_args(env, **{"--db": str(escape)}))
    assert rc == 2
    assert not (tmp_path / "escaped.db").exists()


def test_seed_store_thread_content_drift_fails_without_partial_writes(
        env, capsys):
    """CAS: a changed existing thread must fail the whole batch (codex
    phase-4 finding 11)."""
    assert main(seed_args(env)) == 0
    capsys.readouterr()
    db_path = env["root"] / "continuity.db"
    with ContinuityStore(db_path, read_only=True) as store:
        dump_before = store.dump_canonical()

    threads = json.loads(env["threads"].read_text(encoding="utf-8"))
    # `opened_from` is persisted but was omitted by the original CAS check.
    threads[-1]["opened_from"] = "operator-rewrote-provenance"
    new_thread = dict(threads[0])
    new_thread["thread_id"] = "thread-batch-first"
    new_thread["idempotency_key"] = "idem-batch-first"
    env["threads"].write_text(json.dumps([new_thread] + threads),
                              encoding="utf-8")

    rc = main(seed_args(env))
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "drift" in captured.err
    with ContinuityStore(db_path, read_only=True) as store:
        assert store.dump_canonical() == dump_before, \
            "drift failure must leave the store untouched (no partial batch)"


def test_seed_store_project_content_drift_fails_without_partial_writes(
        env, capsys):
    assert main(seed_args(env)) == 0
    capsys.readouterr()
    db_path = env["root"] / "continuity.db"
    with ContinuityStore(db_path, read_only=True) as store:
        dump_before = store.dump_canonical()

    projects = json.loads(env["registry"].read_text(encoding="utf-8"))
    projects[0]["canonical_name"] = "renamed project"
    projects[0]["registry_version"] = projects[0]["registry_version"] + 1
    env["registry"].write_text(json.dumps(projects), encoding="utf-8")

    rc = main(seed_args(env))
    captured = capsys.readouterr()
    assert rc == 2
    assert "drift" in captured.err
    with ContinuityStore(db_path, read_only=True) as store:
        assert store.dump_canonical() == dump_before


def test_seed_store_validates_whole_batch_before_any_write(env, capsys):
    """One invalid row anywhere fails validation before the first write."""
    threads = json.loads(env["threads"].read_text(encoding="utf-8"))
    invalid = {"thread_id": "thread-invalid"}  # misses required contract keys
    env["threads"].write_text(json.dumps(threads + [invalid]),
                              encoding="utf-8")
    rc = main(seed_args(env))
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    db_path = env["root"] / "continuity.db"
    if db_path.exists():
        with ContinuityStore(db_path, read_only=True) as store:
            counts = store.counts()
            assert counts["threads"] == 0, "no partial batch may be written"
            assert counts["projects"] == 0


def test_seed_store_refuses_foreign_sqlite_database(env, capsys):
    env["root"].mkdir()
    foreign = env["root"] / "continuity.db"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE tasks (id TEXT)")
    conn.commit()
    conn.close()
    before = hashlib.sha256(foreign.read_bytes()).hexdigest()

    rc = main(seed_args(env))
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "application_id" in captured.err
    assert hashlib.sha256(foreign.read_bytes()).hexdigest() == before
