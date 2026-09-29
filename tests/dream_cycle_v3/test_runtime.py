"""Phase 4 production runtime: explicit-config daily cycle.

Covers: explicit roots/window (no ambient discovery), single-flight lock with
symlink/cross-root refusal, confined owned continuity.db, idempotent
manifest/run/registry/thread recording, read-only tracker snapshots,
quarantine-first v2 migration, mandatory daily disposition invariant,
retrieval smoke against the real Phase 3 wake/lookup brokers, machine
report + exact stdout status, and exact-rerun zero delta.
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

import dream_cycle_v3.runtime as runtime_module
from .conftest import write_tree  # noqa: F401 (package-relative test helper)

from dream_cycle_v3.canonical import stable_id
from dream_cycle_v3.dry_run import SAMPLE_DATA, _build_sample_kanban_db
from dream_cycle_v3.errors import (CarryForwardInvariantError, DreamCycleError,
                                   RootResolutionError, RuntimeLockError,
                                   StoreOwnershipError)
from dream_cycle_v3.runtime import (RuntimeConfig, _publish_cycle_report_once,
                                    run_cycle, runtime_lock)
from dream_cycle_v3.store import ContinuityStore

WINDOW_START = datetime(2026, 7, 11, 0, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 7, 12, 0, 0, tzinfo=timezone.utc)
DATE = "2026-07-11"
AS_OF = "2026-07-12T00:30:00+00:00"
MTIME = int(datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc).timestamp())

SECRET_CANARY = "sk-canary12345678901234567890"
EMAIL_CANARY = "tyler@example.com"


@pytest.fixture
def env(tmp_path):
    sources = tmp_path / "sources" / "profile"
    sources.mkdir(parents=True)
    write_tree(sources, {
        "state/wake-up.md": "## Wake\n- carry the offload thread forward\n",
        "state/loose-threads.md":
            f"- email {EMAIL_CANARY} about the tracker export\n",
        "notes/deploy-notes.md":
            f"Deploy notes with a leaked value: token = \"{SECRET_CANARY}\"\n",
        "secrets/api_tokens.txt": f"OPENAI_API_KEY={SECRET_CANARY}\n",
        "sessions/20260711-session.jsonl":
            '{"role":"user","content":"private transcript text"}\n',
    }, mtime=MTIME)

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
        "v3_root": tmp_path / "v3-shadow",
        "registry": registry,
        "threads": threads,
        "kanban_db": kanban_db,
    }


def make_config(env, **over):
    base = dict(
        profile="nagatha-test",
        owner="nagatha",
        read_roots={"profile": env["sources"]},
        v3_root=env["v3_root"],
        mode="shadow",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        disposition_date=DATE,
        as_of=AS_OF,
        registry_path=env["registry"],
        threads_path=env["threads"],
        kanban_db=env["kanban_db"],
        kanban_board="sample-board",
        todoist_export=SAMPLE_DATA / "todoist_export.json",
        migrate_v2_roots=("profile",),
    )
    base.update(over)
    return RuntimeConfig(**base)


def open_store(env):
    return ContinuityStore(env["v3_root"] / "continuity.db", read_only=True)


# -- end-to-end cycle --------------------------------------------------------

def test_cycle_end_to_end(env):
    result = run_cycle(make_config(env))
    report = result.report

    assert result.ok is True
    assert report["kind"] == "dream-cycle-v3-phase4-cycle-report"
    assert report["mode"] == "shadow"
    assert report["cycle_date"] == DATE

    run_id = report["run_id"]
    assert (env["v3_root"] / "manifests" / f"{run_id}.json").is_file()
    assert Path(result.report_path).is_file()
    on_disk = json.loads(Path(result.report_path).read_text(encoding="utf-8"))
    assert on_disk["run_id"] == run_id

    with open_store(env) as store:
        counts = store.counts()
        assert counts["runs"] == 1
        assert counts["threads"] == 8
        assert counts["write_receipts"] == 0
        # kanban-done thread closed with proof from the recorded snapshot
        done = store.get_thread("sample-thread-0001-kanban-done")
        assert done["state"] == "done"
        assert done["closure_proof"] is not None

    carry = report["carry_forward"]
    assert carry["invariant_ok"] is True
    assert carry["selected"] == carry["dispositioned"] + carry["already_dispositioned"]
    assert report["invariants"]["write_receipts"] == 0
    assert report["invariants"]["promoted_candidates"] == 0
    assert report["invariants"]["task_candidates_in_hot_memory"] == 0

    line = result.status_line
    assert "\n" not in line
    assert line.startswith("dream-cycle-v3 cycle ok mode=shadow ")
    assert f"run={run_id}" in line
    assert "receipts=0" in line


def test_rerun_identical_window_zero_delta(env):
    cfg = make_config(env)
    run_cycle(cfg)
    with open_store(env) as store:
        counts_1, dump_1 = store.counts(), store.dump_canonical()

    result_2 = run_cycle(cfg)
    with open_store(env) as store:
        counts_2, dump_2 = store.counts(), store.dump_canonical()

    assert counts_2 == counts_1
    assert dump_2 == dump_1  # zero candidates/threads/dispositions/events/
    #                          snapshots/receipts added by the exact rerun
    carry = result_2.report["carry_forward"]
    assert carry["already_dispositioned"] == carry["selected"]
    assert carry["dispositioned"] == 0
    assert Path(result_2.report_path).is_file()  # deterministically replaced


# -- v2 migration: quarantine-first only --------------------------------------

def test_v2_migration_is_quarantine_only(env):
    result = run_cycle(make_config(env))
    with open_store(env) as store:
        rows = list(store._conn.execute("SELECT * FROM candidates"))
    assert rows, "v2 migration should quarantine at least one candidate"
    for row in rows:
        assert row["status"] == "quarantined"
        assert row["destination"] == "quarantine:review"
        assert row["class"] == "quarantine"
    assert result.report["v2_migration"]["quarantined_total"] == len(rows)


def test_v2_migration_candidate_identity_is_deterministic(env):
    run_cycle(make_config(env))
    with open_store(env) as store:
        manifest = json.loads(
            (env["v3_root"] / "manifests").glob("*.json").__next__()
            .read_text(encoding="utf-8"))
        by_id = {r["candidate_id"]: r for r in
                 store._conn.execute("SELECT * FROM candidates")}
    source = next(s for s in manifest["sources"]
                  if s["location"] == "state/wake-up.md")
    expected = stable_id("dream-cycle-v3-v2-candidate",
                         source["source_id"], source["fingerprint"])
    assert expected in by_id


def test_v2_migration_session_sources_carry_no_excerpt(env):
    run_cycle(make_config(env))
    with open_store(env) as store:
        rows = list(store._conn.execute("SELECT * FROM candidates"))
    session_rows = [r for r in rows
                    if "sessions/" in json.loads(r["evidence_refs"])[0]["source_id"]]
    assert session_rows, "session source must still be represented"
    for row in session_rows:
        ref = json.loads(row["evidence_refs"])[0]
        assert "excerpt" not in ref or ref["excerpt"] is None
        assert "private transcript text" not in row["normalized_claim"]


def test_v2_migration_excerpts_are_sanitized_and_bounded(env):
    run_cycle(make_config(env))
    with open_store(env) as store:
        rows = list(store._conn.execute("SELECT * FROM candidates"))
    loose = next(r for r in rows if "loose-threads" in r["candidate_id"]
                 or "loose-threads" in
                 json.loads(r["evidence_refs"])[0]["source_id"])
    ref = json.loads(loose["evidence_refs"])[0]
    assert EMAIL_CANARY not in (ref.get("excerpt") or "")
    assert "[email_redacted]" in (ref.get("excerpt") or "")
    assert len(ref.get("excerpt") or "") <= 1000


def test_v2_migration_disabled_produces_no_candidates(env):
    run_cycle(make_config(env, migrate_v2_roots=(),
                          v3_root=env["tmp"] / "v3-nomigrate"))
    with ContinuityStore(env["tmp"] / "v3-nomigrate" / "continuity.db",
                         read_only=True) as store:
        assert store.counts()["candidates"] == 0


# -- secrets / PII containment -------------------------------------------------

def test_tracker_source_locator_sanitized_in_persisted_report(env):
    """The top-level cycle-report `trackers` rows embed the adapter's
    source_locator directly; a credential/email-shaped configured path must
    be sanitized there too, not only in the store-side snapshot (codex
    phase-4 third review finding 2)."""
    tagged_kanban_dir = (env["tmp"] / "trackers" /
                        f"{EMAIL_CANARY}-{SECRET_CANARY}")
    tagged_kanban_dir.mkdir(parents=True)
    tagged_kanban_db = tagged_kanban_dir / "kanban.db"
    tagged_kanban_db.write_bytes(env["kanban_db"].read_bytes())

    result = run_cycle(make_config(env, kanban_db=tagged_kanban_db))
    tracker = next(t for t in result.report["trackers"]
                  if t["adapter"] == "kanban")
    assert EMAIL_CANARY not in tracker["source_locator"]
    assert SECRET_CANARY not in tracker["source_locator"]

    report_bytes = Path(result.report_path).read_bytes()
    assert EMAIL_CANARY.encode() not in report_bytes
    assert SECRET_CANARY.encode() not in report_bytes


def test_secret_and_pii_never_reach_candidates_report_or_stdout(env):
    result = run_cycle(make_config(env))
    report_text = json.dumps(result.report)
    assert SECRET_CANARY not in report_text
    assert EMAIL_CANARY not in report_text
    assert SECRET_CANARY not in result.status_line
    assert EMAIL_CANARY not in result.status_line
    with open_store(env) as store:
        for row in store._conn.execute("SELECT * FROM candidates"):
            blob = json.dumps({k: row[k] for k in row.keys()})
            assert SECRET_CANARY not in blob
            assert EMAIL_CANARY not in blob
    # the secrets directory was pruned by the collector, recorded as excluded
    manifest = json.loads(next(iter(
        (env["v3_root"] / "manifests").glob("*.json")))
        .read_text(encoding="utf-8"))
    reasons = {e["reason"] for e in manifest["excluded"]}
    assert any(r.startswith("secret_dir:") for r in reasons)
    assert all("secrets/api_tokens.txt" not in s["source_id"]
               for s in manifest["sources"])


# -- explicit configuration, no ambient discovery ------------------------------

def test_config_requires_explicit_values(env):
    with pytest.raises(DreamCycleError):
        make_config(env, profile="  ")
    with pytest.raises(DreamCycleError):
        make_config(env, read_roots={})
    with pytest.raises(DreamCycleError):
        make_config(env, window_start=WINDOW_START.replace(tzinfo=None))
    with pytest.raises(DreamCycleError):
        make_config(env, window_start=WINDOW_END, window_end=WINDOW_START)
    with pytest.raises(DreamCycleError):
        make_config(env, disposition_date="2026-99-99")
    with pytest.raises(DreamCycleError):
        make_config(env, as_of="not-a-datetime")
    with pytest.raises(DreamCycleError):
        make_config(env, mode="production")
    with pytest.raises(DreamCycleError):
        make_config(env, migrate_v2_roots=("unknown-root",))
    with pytest.raises(DreamCycleError):
        make_config(env, smoke_expected_project="hermes-continuity")  # no message


def test_output_root_may_not_nest_with_read_roots(env):
    forbidden = env["sources"] / "v3"
    with pytest.raises(RootResolutionError):
        run_cycle(make_config(env, v3_root=forbidden))
    assert not forbidden.exists(), "rejection must not mutate the read root"
    nested_sources = env["tmp"] / "v3-outer" / "sources"
    nested_sources.mkdir(parents=True)
    (nested_sources / "a.md").write_text("x\n", encoding="utf-8")
    os.utime(nested_sources / "a.md", (MTIME, MTIME))
    with pytest.raises(RootResolutionError):
        run_cycle(make_config(env, read_roots={"profile": nested_sources},
                              v3_root=env["tmp"] / "v3-outer"))


# -- single-flight lock ---------------------------------------------------------

def test_lock_is_single_flight(env):
    env["v3_root"].mkdir(parents=True, exist_ok=True)
    with runtime_lock(env["v3_root"].resolve()):
        with pytest.raises(RuntimeLockError):
            run_cycle(make_config(env))


def test_lock_refuses_symlink(env):
    env["v3_root"].mkdir(parents=True, exist_ok=True)
    outside = env["tmp"] / "outside.lock"
    outside.write_text("", encoding="utf-8")
    (env["v3_root"] / "runtime.lock").symlink_to(outside)
    with pytest.raises(RuntimeLockError):
        run_cycle(make_config(env))


def test_store_symlink_outside_root_refused(env):
    env["v3_root"].mkdir(parents=True, exist_ok=True)
    foreign = env["tmp"] / "foreign.db"
    with ContinuityStore(foreign) as store:
        store.migrate(AS_OF)
    (env["v3_root"] / "continuity.db").symlink_to(foreign)
    with pytest.raises(StoreOwnershipError):
        run_cycle(make_config(env))


# -- tracker snapshots ------------------------------------------------------------

def test_tracker_snapshots_recorded_read_only(env):
    result = run_cycle(make_config(env, github_repo="octocat/hello-world",
                                   github_available=False))
    trackers = {t["adapter"]: t for t in result.report["trackers"]}
    assert trackers["kanban"]["status"] == "ok"
    assert trackers["kanban"]["items"] == 3
    assert trackers["todoist"]["status"] == "ok"
    assert trackers["github"]["status"] == "unavailable"
    with open_store(env) as store:
        snaps = store.adapter_snapshots_for_run(result.report["run_id"])
        assert {s["adapter"] for s in snaps} == {"kanban", "todoist", "github"}


# -- carry-forward fails closed -----------------------------------------------------

def test_carry_forward_failure_is_fatal_and_unsuccessful(env, monkeypatch):
    import dream_cycle_v3.runtime as runtime_mod

    def boom(*args, **kwargs):
        raise CarryForwardInvariantError("synthetic invariant failure")

    monkeypatch.setattr(runtime_mod, "run_carry_forward", boom)
    with pytest.raises(CarryForwardInvariantError):
        run_cycle(make_config(env))
    reports_dir = env["v3_root"] / "reports"
    assert not (reports_dir.exists() and list(reports_dir.glob("*.json")))
    with open_store(env) as store:
        events = store._conn.execute(
            "SELECT COUNT(*) AS c FROM events "
            "WHERE event_type = 'runtime_cycle_completed'").fetchone()["c"]
        assert events == 0


# -- success event carries runtime wall-clock operational evidence ------------------

def test_success_event_uses_runtime_wall_clock_not_pinned_as_of(env):
    run_cycle(make_config(env))
    with open_store(env) as store:
        rows = list(store._conn.execute(
            "SELECT * FROM events WHERE event_type = 'runtime_cycle_completed'"))
    assert len(rows) == 1
    created_date = rows[0]["created_at"][:10]
    # operational evidence is the runtime's own clock, never the pinned window
    assert created_date == datetime.now(timezone.utc).date().isoformat()
    assert created_date != DATE
    payload = json.loads(rows[0]["payload"])
    assert payload["cycle_date"] == DATE
    assert payload["mode"] == "shadow"


# -- retrieval smoke gate --------------------------------------------------------------

def test_retrieval_smoke_passes_with_expected_project_and_thread(env):
    result = run_cycle(make_config(
        env,
        smoke_message="status update on the dream cycle work",
        smoke_expected_project="hermes-continuity",
        smoke_require_thread=True))
    smoke = result.report["retrieval_smoke"]
    assert smoke["configured"] is True
    assert smoke["ok"] is True
    assert smoke["wake"]["project_id"] == "hermes-continuity"
    assert smoke["wake"]["threads"] >= 1 or smoke["lookup"]["open_threads"] >= 1
    assert result.ok is True
    assert "smoke=pass" in result.status_line


def test_retrieval_smoke_fails_closed_on_wrong_project(env):
    result = run_cycle(make_config(
        env,
        smoke_message="status update on the dream cycle work",
        smoke_expected_project="klas-sample"))
    assert result.ok is False
    assert result.report["retrieval_smoke"]["ok"] is False
    assert result.status_line.startswith("dream-cycle-v3 cycle FAIL")
    with open_store(env) as store:
        events = store._conn.execute(
            "SELECT COUNT(*) AS c FROM events "
            "WHERE event_type = 'runtime_cycle_completed'").fetchone()["c"]
        assert events == 0  # a failed smoke is not a successful cycle


# -- CLI entry point ----------------------------------------------------------------------

def _cli_args(env, extra=()):
    return [
        "run",
        "--profile", "nagatha-test", "--owner", "nagatha",
        "--root", f"profile={env['sources']}",
        "--shadow", str(env["v3_root"]),
        "--window-start", "2026-07-11T00:00:00+00:00",
        "--window-end", "2026-07-12T00:00:00+00:00",
        "--date", DATE, "--as-of", AS_OF,
        "--registry", str(env["registry"]),
        "--threads", str(env["threads"]),
        "--kanban-db", str(env["kanban_db"]),
        "--kanban-board", "sample-board",
        "--todoist-export", str(SAMPLE_DATA / "todoist_export.json"),
        "--migrate-v2-root", "profile",
        *extra,
    ]


def test_cli_run_prints_one_line_and_exits_zero(env, capsys):
    from dream_cycle_v3.cli import main
    rc = main(_cli_args(env))
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("\n") == 1
    assert out.startswith("dream-cycle-v3 cycle ok mode=shadow ")


def test_cli_run_smoke_failure_exits_nonzero(env, capsys):
    from dream_cycle_v3.cli import main
    rc = main(_cli_args(env, extra=(
        "--smoke-message", "status update on the dream cycle work",
        "--smoke-expect-project", "klas-sample")))
    captured = capsys.readouterr()
    assert rc == 1
    assert "FAIL" in captured.err
    assert SECRET_CANARY not in captured.err


def test_cli_run_requires_exactly_one_output_root(env, capsys):
    from dream_cycle_v3.cli import main
    args = _cli_args(env)
    args.remove("--shadow")
    args.remove(str(env["v3_root"]))
    with pytest.raises(SystemExit):
        main(args)


# -- codex phase-4 remediation: retry idempotency, publication ordering, ------
# -- smoke abstention, bounded reports ----------------------------------------

def test_cli_run_retry_without_pinned_as_of_is_byte_identical(env, capsys):
    """A cron retry re-invokes the identical logical window without --as-of;
    it must reuse deterministic runtime metadata, not a fresh wall clock
    (codex phase-4 finding 4)."""
    from dream_cycle_v3.cli import main
    args = _cli_args(env)
    as_of_at = args.index("--as-of")
    del args[as_of_at:as_of_at + 2]

    assert main(args) == 0
    out1 = capsys.readouterr().out
    manifests = sorted((env["v3_root"] / "manifests").glob("*.json"))
    assert len(manifests) == 1
    manifest_bytes = manifests[0].read_bytes()
    reports = sorted((env["v3_root"] / "reports").glob("*.json"))
    assert len(reports) == 1
    report_bytes = reports[0].read_bytes()
    with ContinuityStore(env["v3_root"] / "continuity.db",
                         read_only=True) as store:
        dump_before = store.dump_canonical()

    rc = main(args)                      # the retry
    out2 = capsys.readouterr().out
    assert rc == 0, "retry must not abort on manifest conflict"
    # same deterministic run id and report path, byte-identical manifest,
    # zero durable delta — the retry adds nothing and rewrites nothing
    run_token = next(t for t in out1.split() if t.startswith("run="))
    assert run_token in out2
    assert out2.startswith("dream-cycle-v3 cycle ok mode=shadow ")
    assert sorted((env["v3_root"] / "manifests").glob("*.json")) == manifests
    assert manifests[0].read_bytes() == manifest_bytes
    assert sorted((env["v3_root"] / "reports").glob("*.json")) == reports
    assert reports[0].read_bytes() == report_bytes, \
        "retry-local deltas must not rewrite deterministic run evidence"
    with ContinuityStore(env["v3_root"] / "continuity.db",
                         read_only=True) as store:
        assert store.dump_canonical() == dump_before


def test_no_success_event_when_report_publication_fails(env, monkeypatch):
    """Operational-day evidence must be ordered after report publication:
    a failed publication cannot count (codex phase-4 finding 3)."""
    import dream_cycle_v3.runtime as runtime_mod

    def failing_write_report(report, reports_dir):
        raise OSError("simulated disk-full during report publication")

    monkeypatch.setattr(runtime_mod, "write_report", failing_write_report)
    with pytest.raises(OSError):
        run_cycle(make_config(env))
    with ContinuityStore(env["v3_root"] / "continuity.db",
                         read_only=True) as store:
        n = store._conn.execute(
            "SELECT COUNT(*) AS c FROM events WHERE event_type = "
            "'runtime_cycle_completed'").fetchone()["c"]
    assert n == 0, "unpublished cycle must not evidence an operational day"


def test_retrieval_smoke_includes_abstention_probe(env):
    result = run_cycle(make_config(
        env,
        smoke_message="status update on the dream cycle work",
        smoke_expected_project="hermes-continuity"))
    smoke = result.report["retrieval_smoke"]
    assert smoke["abstention"]["ok"] is True, \
        "the no-evidence probe must abstain"
    assert smoke["ok"] is True
    assert result.ok is True


def test_retrieval_smoke_fails_over_eager_activation(env):
    """A resolver that activates on a no-evidence message must fail the
    smoke even when the positive probe succeeds (codex phase-4 finding 9)."""
    projects = json.loads(env["registry"].read_text(encoding="utf-8"))
    hijack = json.loads(json.dumps(projects[0]))
    hijack["project_id"] = "over-eager-project"
    hijack["canonical_name"] = "Matches everything"
    hijack["aliases"] = ["abstention probe"]
    hijack["retrieval_terms"] = []
    env["registry"].write_text(json.dumps(projects + [hijack]),
                               encoding="utf-8")

    result = run_cycle(make_config(
        env,
        smoke_message="status update on the dream cycle work",
        smoke_expected_project="hermes-continuity"))
    smoke = result.report["retrieval_smoke"]
    assert smoke["abstention"]["ok"] is False
    assert smoke["ok"] is False
    assert result.ok is False


def test_cycle_report_is_bounded_and_deduplicated(env):
    """Per-thread detail is capped with explicit totals, and the carry
    section appears once (codex phase-4 finding 10)."""
    import dream_cycle_v3.runtime as runtime_mod
    template = json.loads(env["threads"].read_text(encoding="utf-8"))[0]
    many = []
    for i in range(runtime_mod.REPORT_THREADS_CAP + 25):
        t = json.loads(json.dumps(template))
        t["thread_id"] = f"bulk-thread-{i:04d}"
        t["idempotency_key"] = f"bulk-idem-{i:04d}-0123456789abcdef"
        t["external_task_ref"] = None
        t["link_disposition"] = "not_actionable"
        t["state"] = "active"
        many.append(t)
    env["threads"].write_text(json.dumps(many), encoding="utf-8")

    result = run_cycle(make_config(env))
    carry = result.report["carry_forward"]
    assert carry["selected"] > runtime_mod.REPORT_THREADS_CAP
    assert len(carry["threads"]) == runtime_mod.REPORT_THREADS_CAP
    assert carry["threads_total"] == carry["selected"]
    assert carry["threads_truncated"] == \
        carry["selected"] - runtime_mod.REPORT_THREADS_CAP
    run_report_carry = result.report["run_report"]["carry_forward"]
    assert "threads" not in run_report_carry, \
        "the run_report must not duplicate the per-thread list"


# -- codex phase-4 fourth review finding 1: report re-attestation trust gap --

def test_retry_fails_closed_on_edited_existing_report(env):
    """A report edited on disk after publication must never be re-attested
    by a retry: the retry must fail closed, not hash the tampered bytes and
    record them as new valid completion evidence."""
    result = run_cycle(make_config(env))
    assert result.ok
    with open_store(env) as store:
        before = json.loads(store._conn.execute(
            "SELECT payload FROM events WHERE event_type = "
            "'runtime_cycle_completed'").fetchone()["payload"])

    report_path = Path(result.report_path)
    tampered = json.loads(report_path.read_text(encoding="utf-8"))
    tampered["retrieval_smoke"] = {"configured": True, "ok": True}
    tampered["invariants"] = dict(tampered["invariants"], write_receipts=999)
    report_path.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(DreamCycleError):
        run_cycle(make_config(env))

    with open_store(env) as store:
        rows = list(store._conn.execute(
            "SELECT payload FROM events WHERE event_type = "
            "'runtime_cycle_completed'"))
    assert len(rows) == 1, \
        "edited bytes must not produce a second valid completion event"
    assert json.loads(rows[0]["payload"]) == before, \
        "the original trusted report_sha256 must be unchanged"


def test_publish_cycle_report_once_fails_closed_without_trusted_event(
        tmp_path):
    """A pre-existing report file with no prior trusted completion event
    (e.g. a crash between file write and event record, or a planted file)
    must never be accepted as legitimate evidence."""
    v3_root = tmp_path / "v3"
    reports_dir = v3_root / "reports"
    reports_dir.mkdir(parents=True)
    (reports_dir / "orphan-run-id.json").write_text(
        json.dumps({"stray": "pre-existing file, no trusted event"}),
        encoding="utf-8")
    report = {
        "kind": "dream-cycle-v3-phase4-cycle-report",
        "run_id": "orphan-run-id",
        "profile": "nagatha-test",
        "mode": "shadow",
        "historical_replay": False,
        "manifest_fingerprint": "fp-orphan",
    }
    with ContinuityStore(v3_root / "continuity.db") as store:
        store.migrate("2026-07-11T00:00:00+00:00")
        with pytest.raises(DreamCycleError):
            _publish_cycle_report_once(store, report, reports_dir, ok=True)


def test_cross_role_event_with_missing_report_cannot_be_republished(
        tmp_path):
    """A missing report cannot let a replay event's deterministic run_id be
    republished as operational evidence."""
    v3_root = tmp_path / "v3"
    reports_dir = v3_root / "reports"
    reports_dir.mkdir(parents=True)
    now = "2026-07-11T00:00:00+00:00"
    report = {
        "kind": "dream-cycle-v3-phase4-cycle-report",
        "run_id": "cross-role-missing-report",
        "profile": "nagatha-test",
        "mode": "shadow",
        "historical_replay": False,
        "manifest_fingerprint": "fp-cross-role",
    }
    with ContinuityStore(v3_root / "continuity.db") as store:
        store.migrate(now)
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO runs(run_id, profile, window_start, window_end, "
                "collector_version, manifest_fingerprint, manifest_path, "
                "generated_at, recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (report["run_id"], report["profile"], now,
                 "2026-07-12T00:00:00+00:00", "test-collector",
                 report["manifest_fingerprint"], "manifests/cross-role.json",
                 now, now))
            store._emit_event(
                conn, entity_type="run", entity_id=report["run_id"],
                event_type="runtime_cycle_completed",
                payload={"cycle_date": "2026-07-11", "mode": "shadow",
                         "profile": report["profile"],
                         "historical_replay": True,
                         "manifest_fingerprint": report["manifest_fingerprint"],
                         "report_sha256": "a" * 64},
                run_id=report["run_id"], now=now)

        with pytest.raises(DreamCycleError, match="conflicting evidence-origin"):
            _publish_cycle_report_once(store, report, reports_dir, ok=True)
    assert not (reports_dir / f"{report['run_id']}.json").exists()


def test_publish_cycle_report_once_rejects_authenticated_markerless_bytes(
        tmp_path, monkeypatch):
    """Even a matching trusted digest cannot make a markerless report count as
    the expected operational evidence role."""
    v3_root = tmp_path / "v3"
    reports_dir = v3_root / "reports"
    reports_dir.mkdir(parents=True)
    report = {
        "kind": "dream-cycle-v3-phase4-cycle-report",
        "run_id": "markerless-run-id",
        "profile": "nagatha-test",
        "mode": "shadow",
        "historical_replay": False,
        "manifest_fingerprint": "fp-markerless",
    }
    markerless = dict(report)
    markerless.pop("historical_replay")
    raw = (json.dumps(markerless, sort_keys=True, separators=(",", ":"))
           + "\n").encode()
    path = reports_dir / "markerless-run-id.json"
    path.write_bytes(raw)
    monkeypatch.setattr(
        runtime_module, "_trusted_report_hash",
        lambda *args, **kwargs: hashlib.sha256(raw).hexdigest())

    with ContinuityStore(v3_root / "continuity.db") as store:
        store.migrate("2026-07-11T00:00:00+00:00")
        with pytest.raises(DreamCycleError, match="evidence-origin marker"):
            _publish_cycle_report_once(store, report, reports_dir, ok=True)


def test_publish_cycle_report_once_fails_closed_when_trusted_report_missing(
        tmp_path):
    """Trusted completion evidence pointing at a report that is no longer on
    disk must fail closed rather than silently republish a fresh one."""
    v3_root = tmp_path / "v3"
    reports_dir = v3_root / "reports"
    reports_dir.mkdir(parents=True)
    now = "2026-07-11T00:00:00+00:00"
    with ContinuityStore(v3_root / "continuity.db") as store:
        store.migrate(now)
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO runs(run_id, profile, window_start, window_end, "
                "collector_version, manifest_fingerprint, manifest_path, "
                "generated_at, recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                ("orphan-run-id", "nagatha-test",
                 "2026-07-11T00:00:00+00:00", "2026-07-12T00:00:00+00:00",
                 "test-collector", "fp-orphan",
                 "manifests/orphan-run-id.json", now, now))
            store._emit_event(
                conn, entity_type="run", entity_id="orphan-run-id",
                event_type="runtime_cycle_completed",
                payload={"cycle_date": "2026-07-11", "mode": "shadow",
                         "profile": "nagatha-test",
                         "historical_replay": False,
                         "manifest_fingerprint": "fp-orphan",
                         "report_sha256": "d" * 64},
                run_id="orphan-run-id", now=now)
        report = {
            "kind": "dream-cycle-v3-phase4-cycle-report",
            "run_id": "orphan-run-id",
            "profile": "nagatha-test",
            "mode": "shadow",
            "historical_replay": False,
            "manifest_fingerprint": "fp-orphan",
        }
        with pytest.raises(DreamCycleError):
            _publish_cycle_report_once(store, report, reports_dir, ok=True)


# -- codex phase-4 final review: Windows persisted-bytes caveat --------------

def test_publish_cycle_report_once_first_publish_digest_matches_disk_bytes(
        tmp_path):
    """The digest `_publish_cycle_report_once` returns on first publication
    must equal sha256 of the exact bytes now on disk, with no CRLF: text-mode
    write_text() would let native Windows newline translation desync the
    two."""
    v3_root = tmp_path / "v3"
    reports_dir = v3_root / "reports"
    reports_dir.mkdir(parents=True)
    report = {
        "kind": "dream-cycle-v3-phase4-cycle-report",
        "run_id": "run-bytes-0001",
        "profile": "nagatha-test",
        "mode": "shadow",
        "historical_replay": False,
        "manifest_fingerprint": "fp-bytes",
    }
    with ContinuityStore(v3_root / "continuity.db") as store:
        store.migrate("2026-07-11T00:00:00+00:00")
        path, digest = _publish_cycle_report_once(
            store, report, reports_dir, ok=True)
    raw = path.read_bytes()
    assert digest == hashlib.sha256(raw).hexdigest()
    assert b"\r\n" not in raw


def test_publish_cycle_report_once_first_publish_never_uses_text_mode(
        tmp_path, monkeypatch):
    def _boom(self, *a, **k):
        raise AssertionError("first publication must not use write_text()")

    monkeypatch.setattr(Path, "write_text", _boom)
    v3_root = tmp_path / "v3"
    reports_dir = v3_root / "reports"
    reports_dir.mkdir(parents=True)
    report = {
        "kind": "dream-cycle-v3-phase4-cycle-report",
        "run_id": "run-bytes-0002",
        "profile": "nagatha-test",
        "mode": "shadow",
        "historical_replay": False,
        "manifest_fingerprint": "fp-bytes",
    }
    with ContinuityStore(v3_root / "continuity.db") as store:
        store.migrate("2026-07-11T00:00:00+00:00")
        _publish_cycle_report_once(store, report, reports_dir, ok=True)


def test_publish_cycle_report_once_refuses_planted_symlink_at_target(
        tmp_path):
    """A symlink planted at the report path before first publication must
    never be followed and written through."""
    v3_root = tmp_path / "v3"
    reports_dir = v3_root / "reports"
    reports_dir.mkdir(parents=True)
    evil_target = tmp_path / "outside-reports-dir.json"
    (reports_dir / "run-bytes-0003.json").symlink_to(evil_target)
    report = {
        "kind": "dream-cycle-v3-phase4-cycle-report",
        "run_id": "run-bytes-0003",
        "profile": "nagatha-test",
        "mode": "shadow",
        "historical_replay": False,
        "manifest_fingerprint": "fp-bytes",
    }
    with ContinuityStore(v3_root / "continuity.db") as store:
        store.migrate("2026-07-11T00:00:00+00:00")
        with pytest.raises(DreamCycleError):
            _publish_cycle_report_once(store, report, reports_dir, ok=True)
    assert not evil_target.exists()


# -- codex phase-4 fifth review finding 1: cycle-report attestation TOCTOU --

def test_event_report_sha256_survives_replacement_after_publish_verification(
        env, monkeypatch):
    """An external replacement of the cycle report file in the window
    between `_publish_cycle_report_once` verifying/writing its bytes and
    `run_cycle` recording the completion event must never become the
    attested hash: the event must stay bound to the bytes that were
    actually verified/published, not to whatever is on disk afterward."""
    import dream_cycle_v3.runtime as runtime_mod

    original = runtime_mod._publish_cycle_report_once
    tampered_bytes = b'{"tampered": true}'

    def wrapped(store, report, reports_dir, *, ok):
        result = original(store, report, reports_dir, ok=ok)
        path = result[0] if isinstance(result, tuple) else result
        path.write_bytes(tampered_bytes)  # simulates an external replacement
        return result

    monkeypatch.setattr(runtime_mod, "_publish_cycle_report_once", wrapped)

    result = run_cycle(make_config(env))
    assert result.ok

    with open_store(env) as store:
        payload = json.loads(store._conn.execute(
            "SELECT payload FROM events WHERE event_type = "
            "'runtime_cycle_completed'").fetchone()["payload"])

    on_disk_hash = hashlib.sha256(
        Path(result.report_path).read_bytes()).hexdigest()
    assert on_disk_hash == hashlib.sha256(tampered_bytes).hexdigest()
    assert payload["report_sha256"] != on_disk_hash, \
        "the event must bind to the bytes verified at publish time, not " \
        "to bytes replaced afterward"
