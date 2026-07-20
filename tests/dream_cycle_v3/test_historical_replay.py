"""Phase 4 seven-window historical real-source replay gate.

Seven contiguous one-day windows over actual configured read roots, with an
immediate identical rerun per window, producing a summary that proves the
hard invariants — and that labels itself honestly as a historical replay,
not seven elapsed operational days.
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dream_cycle_v3.dry_run import SAMPLE_DATA, _build_sample_kanban_db
from dream_cycle_v3.errors import DreamCycleError
from dream_cycle_v3.runtime import (RuntimeConfig, _publish_replay_summary_once,
                                    run_cycle, run_historical_replay)
from dream_cycle_v3.store import ContinuityStore

START = "2026-07-05"
END = "2026-07-12"          # exclusive: seven one-day windows 07-05 .. 07-11
DAY_DATES = [f"2026-07-{d:02d}" for d in range(5, 12)]


def _mtime(day: str) -> int:
    dt = datetime.fromisoformat(day + "T12:00:00+00:00")
    return int(dt.timestamp())


@pytest.fixture
def env(tmp_path):
    sources = tmp_path / "sources" / "profile"
    sources.mkdir(parents=True)
    for day in DAY_DATES:
        path = sources / "notes" / f"{day}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"- observation recorded on {day}\n", encoding="utf-8")
        os.utime(path, (_mtime(day), _mtime(day)))
    registry = tmp_path / "registry.json"
    registry.write_text(
        (SAMPLE_DATA / "projects.json").read_text(encoding="utf-8"),
        encoding="utf-8")
    threads = tmp_path / "threads.json"
    threads.write_text(
        (SAMPLE_DATA / "threads.json").read_text(encoding="utf-8"),
        encoding="utf-8")
    kanban_db = _build_sample_kanban_db(
        tmp_path / "trackers" / "kanban" / "kanban.db")
    return {
        "tmp": tmp_path,
        "sources": sources,
        "v3_root": tmp_path / "v3-replay",
        "registry": registry,
        "threads": threads,
        "kanban_db": kanban_db,
    }


def replay_kwargs(env, **over):
    base = dict(
        profile="nagatha-test",
        owner="nagatha",
        read_roots={"profile": env["sources"]},
        v3_root=env["v3_root"],
        registry_path=env["registry"],
        threads_path=env["threads"],
        kanban_db=env["kanban_db"],
        kanban_board="sample-board",
        todoist_export=SAMPLE_DATA / "todoist_export.json",
        migrate_v2_roots=("profile",),
        smoke_message="status update on the dream cycle work",
        smoke_expected_project="hermes-continuity",
        smoke_require_thread=True,
    )
    base.update(over)
    return base


def test_seven_window_replay_summary(env):
    result = run_historical_replay(start_date=START, end_date=END,
                                   **replay_kwargs(env))
    summary = result.summary

    assert summary["kind"] == "dream-cycle-v3-historical-replay-summary"
    assert summary["historical_replay"] is True
    assert summary["is_operational_evidence"] is False
    assert "NOT seven elapsed daily operational cycles" in summary["label"]

    windows = summary["windows"]
    assert [w["date"] for w in windows] == DAY_DATES
    assert len({w["run_id"] for w in windows}) == 7
    for w in windows:
        assert w["invariant_ok"] is True
        assert w["rerun_zero_delta"] is True
        assert w["rerun_row_delta"] == {}
        assert w["receipts"] == 0
        assert w["retrieval_smoke"] == "pass"
        assert isinstance(w["sources"], int)
        assert isinstance(w["excluded"], int)

    inv = summary["invariants"]
    assert inv["seven_contiguous_windows"] is True
    assert inv["distinct_run_ids"] is True
    assert inv["all_disposition_invariants_ok"] is True
    assert inv["all_reruns_zero_delta"] is True
    assert inv["zero_write_receipts"] is True
    assert inv["read_roots_unchanged"] is True
    assert inv["zero_live_destination_writes"] is True
    assert inv["zero_task_fields_in_hot_memory"] is True
    assert inv["retrieval_success_rate"] == 1.0

    assert Path(result.summary_path).is_file()
    on_disk = json.loads(Path(result.summary_path).read_text(encoding="utf-8"))
    assert on_disk["kind"] == summary["kind"]

    assert result.ok is True
    assert "\n" not in result.status_line
    assert result.status_line.startswith("dream-cycle-v3 historical-replay ok ")

    # durable state accumulated exactly once per window
    with ContinuityStore(env["v3_root"] / "continuity.db",
                         read_only=True) as store:
        assert store.counts()["runs"] == 7
        assert store.counts()["write_receipts"] == 0
        for day in DAY_DATES:
            n = store._conn.execute(
                "SELECT COUNT(*) AS c FROM thread_dispositions "
                "WHERE disposition_date = ?", (day,)).fetchone()["c"]
            assert n >= 1


def test_replay_requires_exactly_seven_contiguous_days(env):
    with pytest.raises(DreamCycleError):
        run_historical_replay(start_date=START, end_date="2026-07-11",
                              **replay_kwargs(env))
    with pytest.raises(DreamCycleError):
        run_historical_replay(start_date=START, end_date="2026-07-13",
                              **replay_kwargs(env))
    with pytest.raises(DreamCycleError):
        run_historical_replay(start_date="2026-99-01", end_date=END,
                              **replay_kwargs(env))


def test_operational_report_cannot_be_reused_as_replay_evidence(env):
    """The same deterministic run_id cannot cross from operational to replay
    by reusing an authenticated report whose evidence-origin marker differs."""
    kwargs = replay_kwargs(env)
    run_cycle(RuntimeConfig(
        mode="shadow",
        window_start=datetime(2026, 7, 5, tzinfo=timezone.utc),
        window_end=datetime(2026, 7, 6, tzinfo=timezone.utc),
        disposition_date="2026-07-05",
        as_of="2026-07-06T00:00:00+00:00",
        **kwargs))

    with pytest.raises(DreamCycleError, match="evidence-origin marker"):
        run_historical_replay(start_date=START, end_date=END, **kwargs)


def test_failed_operational_report_cannot_be_reused_by_replay(env):
    """Cross-role rejection also applies when the first cycle failed smoke and
    therefore has a report but no completion event/trusted hash."""
    kwargs = replay_kwargs(env, smoke_expected_project="klas-sample")
    failed = run_cycle(RuntimeConfig(
        mode="shadow",
        window_start=datetime(2026, 7, 5, tzinfo=timezone.utc),
        window_end=datetime(2026, 7, 6, tzinfo=timezone.utc),
        disposition_date="2026-07-05",
        as_of="2026-07-06T00:00:00+00:00",
        **kwargs))
    assert failed.ok is False

    with pytest.raises(DreamCycleError, match="evidence-origin marker"):
        run_historical_replay(start_date=START, end_date=END, **kwargs)


def test_replay_records_smoke_failures_without_aborting(env):
    result = run_historical_replay(
        start_date=START, end_date=END,
        **replay_kwargs(env, smoke_expected_project="klas-sample"))
    summary = result.summary
    assert len(summary["windows"]) == 7
    assert all(w["retrieval_smoke"] == "fail" for w in summary["windows"])
    assert summary["invariants"]["retrieval_success_rate"] == 0.0
    assert result.ok is False
    assert result.status_line.startswith("dream-cycle-v3 historical-replay FAIL")


def test_integrity_fingerprint_is_metadata_only_and_skips_symlinks(tmp_path):
    """The replay safety fingerprint must honor symlink policy and never
    open file contents (codex phase-4 finding 5)."""
    from dream_cycle_v3.roots import CollectionRoots
    from dream_cycle_v3.runtime import _read_roots_integrity_fingerprint

    root = tmp_path / "root"
    root.mkdir()
    real = root / "note.md"
    real.write_text("observed\n", encoding="utf-8")
    os.utime(real, (_mtime(START), _mtime(START)))
    outside = tmp_path / "outside-secrets.txt"
    outside.write_text("secret-v1\n", encoding="utf-8")
    (root / "link.txt").symlink_to(outside)

    roots = CollectionRoots.resolve("p", {"r": root})
    fp1 = _read_roots_integrity_fingerprint(roots)
    assert fp1.startswith("sha256:")

    # symlink target content/metadata changes are invisible: the target is
    # outside the root and its bytes were never read
    outside.write_text("secret-v2 changed\n", encoding="utf-8")
    assert _read_roots_integrity_fingerprint(roots) == fp1

    # a real write inside the root is visible via metadata
    real.write_text("observed plus a live write\n", encoding="utf-8")
    assert _read_roots_integrity_fingerprint(roots) != fp1

    # unreadable files are fingerprinted by stat alone (contents never open)
    sealed = root / "sealed.bin"
    sealed.write_bytes(b"x")
    sealed.chmod(0o000)
    try:
        assert _read_roots_integrity_fingerprint(roots).startswith("sha256:")
    finally:
        sealed.chmod(0o600)


def test_replay_reports_destination_evidence_separately(env):
    result = run_historical_replay(start_date=START, end_date=END,
                                   **replay_kwargs(env))
    inv = result.summary["invariants"]
    assert inv["read_roots_unchanged"] is True          # integrity report
    assert inv["all_windows_shadow_mode"] is True       # mode evidence
    assert inv["zero_write_receipts"] is True           # receipt evidence
    assert inv["zero_live_destination_writes"] is True  # derived from above


# -- codex phase-4 fourth review finding 1: replay summary re-attestation ----

def test_exact_rerun_produces_byte_identical_summary_and_dedupes_attestation(
        env):
    """A second full run_historical_replay call over identical inputs must
    retain byte-identical summary bytes and record no second summary
    attestation event."""
    from dream_cycle_v3.runtime import REPLAY_SUMMARY_EVENT_TYPE

    first = run_historical_replay(start_date=START, end_date=END,
                                  **replay_kwargs(env))
    first_bytes = Path(first.summary_path).read_bytes()
    with ContinuityStore(env["v3_root"] / "continuity.db",
                         read_only=True) as store:
        events_after_first = store._conn.execute(
            "SELECT COUNT(*) AS c FROM events WHERE event_type = ?",
            (REPLAY_SUMMARY_EVENT_TYPE,)).fetchone()["c"]
    assert events_after_first == 1

    second = run_historical_replay(start_date=START, end_date=END,
                                   **replay_kwargs(env))
    second_bytes = Path(second.summary_path).read_bytes()
    assert second_bytes == first_bytes, \
        "an exact rerun must retain the byte-identical durable summary"
    with ContinuityStore(env["v3_root"] / "continuity.db",
                         read_only=True) as store:
        events_after_second = store._conn.execute(
            "SELECT COUNT(*) AS c FROM events WHERE event_type = ?",
            (REPLAY_SUMMARY_EVENT_TYPE,)).fetchone()["c"]
    assert events_after_second == 1, \
        "an exact rerun must not create a second attestation event"
    assert second.status_line == first.status_line, \
        ("codex phase-4 final review Low caveat: the retry status line must "
         "derive its receipts figure from the canonical stored summary, not "
         "a retry-local recomputation, so a full exact rerun's status line "
         "is byte-identical to the first run's")


def test_replay_retry_fails_closed_on_edited_summary_bytes(env):
    """A summary file edited on disk after publication must never be
    re-attested by a retry: the retry must fail closed."""
    first = run_historical_replay(start_date=START, end_date=END,
                                  **replay_kwargs(env))
    summary_path = Path(first.summary_path)
    tampered = json.loads(summary_path.read_text(encoding="utf-8"))
    tampered["invariants"]["read_roots_unchanged"] = False
    summary_path.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(DreamCycleError):
        run_historical_replay(start_date=START, end_date=END,
                              **replay_kwargs(env))


# -- codex phase-4 fifth review finding 3: replay-summary attestation TOCTOU -

def test_replay_summary_event_survives_replacement_after_publish_verification(
        env, monkeypatch):
    """An external replacement of the replay summary file in the window
    between `_publish_replay_summary_once` verifying/writing its bytes and
    the summary attestation event being recorded must never become the
    attested hash: the event must stay bound to the bytes actually
    verified/published, not to whatever is on disk afterward."""
    import dream_cycle_v3.runtime as runtime_mod

    original = runtime_mod._publish_replay_summary_once
    tampered_bytes = b'{"tampered": true}'

    def wrapped(summary, reports_dir, *, trusted_hash):
        result = original(summary, reports_dir, trusted_hash=trusted_hash)
        path = result[0] if isinstance(result, tuple) else result
        path.write_bytes(tampered_bytes)  # simulates an external replacement
        return result

    monkeypatch.setattr(runtime_mod, "_publish_replay_summary_once", wrapped)

    result = run_historical_replay(start_date=START, end_date=END,
                                   **replay_kwargs(env))
    assert result.ok

    with ContinuityStore(env["v3_root"] / "continuity.db",
                         read_only=True) as store:
        row = store._conn.execute(
            "SELECT payload FROM events WHERE event_type = ?",
            (runtime_mod.REPLAY_SUMMARY_EVENT_TYPE,)).fetchone()
    payload = json.loads(row["payload"])

    on_disk_hash = hashlib.sha256(
        Path(result.summary_path).read_bytes()).hexdigest()
    assert on_disk_hash == hashlib.sha256(tampered_bytes).hexdigest()
    assert payload["summary_sha256"] != on_disk_hash, \
        "the event must bind to the bytes verified at publish time, not " \
        "to bytes replaced afterward"


# -- codex phase-4 fifth review finding 4: replay retry result/artifact split

def test_replay_retry_returns_canonical_summary_not_diverged_in_memory_copy(
        env):
    """A retry's freshly recomputed in-memory summary can genuinely diverge
    from the already-attested canonical bytes on disk — e.g. retry-local
    carry-forward counts differ because the first run already dispositioned
    the threads the second run now finds already-dispositioned. The retry
    must still return the CANONICAL stored summary in `ReplayResult.summary`
    — never a different in-memory object while `summary_path` keeps
    pointing at the untouched, unchanged artifact."""
    first = run_historical_replay(start_date=START, end_date=END,
                                  **replay_kwargs(env))
    canonical_on_disk = json.loads(
        Path(first.summary_path).read_text(encoding="utf-8"))

    second = run_historical_replay(start_date=START, end_date=END,
                                   **replay_kwargs(env))

    assert Path(second.summary_path).read_bytes() == \
        Path(first.summary_path).read_bytes(), \
        "the durable artifact itself must remain untouched by a retry"
    assert second.summary == canonical_on_disk, \
        "a retry must return the canonical attested summary, not a " \
        "diverged in-memory recomputation"
    assert second.summary == first.summary


def test_replay_retry_status_line_ignores_retry_local_receipts_recompute(
        env, monkeypatch):
    """codex phase-4 final review Low caveat: the status line's `receipts=`
    figure must come from the canonical stored summary (like `windows`,
    `inv`, and `ok` already do), never from a retry-local
    `store.counts()["write_receipts"]` recomputation — a retry-local value
    that diverges from the canonical evidence must never leak into the
    status line."""
    first = run_historical_replay(start_date=START, end_date=END,
                                  **replay_kwargs(env))
    assert "receipts=0" in first.status_line

    original_counts = ContinuityStore.counts

    def poisoned_counts(self):
        counts = dict(original_counts(self))
        counts["write_receipts"] += 999
        return counts

    monkeypatch.setattr(ContinuityStore, "counts", poisoned_counts)
    second = run_historical_replay(start_date=START, end_date=END,
                                   **replay_kwargs(env))

    assert "receipts=999" not in second.status_line
    assert second.status_line == first.status_line, \
        ("a retry-local store.counts() recomputation must never desync the "
         "status line from the canonical stored summary evidence")


# -- codex phase-4 final review: Windows persisted-bytes caveat --------------

_SUMMARY = {"start_date": "2026-07-05", "end_date": "2026-07-12",
           "profile": "nagatha-test", "kind": "dream-cycle-v3-historical-replay-summary"}


def test_publish_replay_summary_once_first_publish_digest_matches_disk_bytes(
        tmp_path):
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True)
    path, digest, canonical = _publish_replay_summary_once(
        _SUMMARY, reports_dir, trusted_hash=None)
    raw = path.read_bytes()
    assert digest == hashlib.sha256(raw).hexdigest()
    assert b"\r\n" not in raw
    assert canonical == _SUMMARY


def test_publish_replay_summary_once_first_publish_never_uses_text_mode(
        tmp_path, monkeypatch):
    def _boom(self, *a, **k):
        raise AssertionError("first publication must not use write_text()")

    monkeypatch.setattr(Path, "write_text", _boom)
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True)
    _publish_replay_summary_once(_SUMMARY, reports_dir, trusted_hash=None)


def test_publish_replay_summary_once_refuses_planted_symlink_at_target(
        tmp_path):
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True)
    evil_target = tmp_path / "outside-reports-dir.json"
    filename = (f"historical-replay-{_SUMMARY['start_date']}_"
               f"{_SUMMARY['end_date']}.json")
    (reports_dir / filename).symlink_to(evil_target)

    with pytest.raises(DreamCycleError):
        _publish_replay_summary_once(_SUMMARY, reports_dir, trusted_hash=None)
    assert not evil_target.exists()
