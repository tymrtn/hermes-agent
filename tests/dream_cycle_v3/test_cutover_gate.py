"""Phase 4 cutover gate.

Consumes the seven-window historical replay summary plus one current shadow
cycle report and returns pass/fail for the hard invariants. The gate MUST
refuse cutover until seven distinct successful operational dates are
evidenced in durable state (runtime_cycle_completed events stamped with the
runtime's own wall clock) — a same-day historical replay never satisfies
that by itself. The seven-day minimum cannot be lowered by CLI or
programmatic callers.
"""
import copy
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dream_cycle_v3.dry_run import SAMPLE_DATA, _build_sample_kanban_db
from dream_cycle_v3.runtime import (RuntimeConfig, evaluate_cutover_gate,
                                    run_cycle, run_historical_replay)
from dream_cycle_v3.store import ContinuityStore

START = "2026-07-05"
END = "2026-07-12"
DAY_DATES = [f"2026-07-{d:02d}" for d in range(5, 12)]


def _mtime(day: str) -> int:
    return int(datetime.fromisoformat(day + "T12:00:00+00:00").timestamp())


@pytest.fixture
def gated(tmp_path):
    """One replay (7 windows) plus one current shadow cycle in one store."""
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
    v3_root = tmp_path / "v3-shadow"

    shared = dict(
        profile="nagatha-test", owner="nagatha",
        read_roots={"profile": sources}, v3_root=v3_root,
        registry_path=registry, threads_path=threads,
        kanban_db=kanban_db, kanban_board="sample-board",
        todoist_export=SAMPLE_DATA / "todoist_export.json",
        migrate_v2_roots=("profile",),
        smoke_message="status update on the dream cycle work",
        smoke_expected_project="hermes-continuity",
        smoke_require_thread=True,
    )
    replay = run_historical_replay(start_date=START, end_date=END, **shared)
    assert replay.ok, "fixture replay must pass"

    # A distinct current shadow cycle. Replay-owned runs remain permanently
    # labeled as replay evidence; rerunning one through run_cycle must never
    # launder it into elapsed operational evidence.
    shadow = run_cycle(RuntimeConfig(
        mode="shadow",
        window_start=datetime(2026, 7, 12, tzinfo=timezone.utc),
        window_end=datetime(2026, 7, 13, tzinfo=timezone.utc),
        disposition_date="2026-07-12",
        as_of="2026-07-13T00:00:00+00:00",
        **shared))
    assert shadow.ok, "fixture shadow cycle must pass"

    return {
        "tmp": tmp_path,
        "db": v3_root / "continuity.db",
        "summary": replay.summary,
        "summary_path": Path(replay.summary_path),
        "shadow_report": shadow.report,
        "shadow_report_path": Path(shadow.report_path),
        "shared": shared,
    }


def test_gate_refuses_same_day_replay_by_default(gated):
    verdict = evaluate_cutover_gate(
        store_path=gated["db"], replay_store_path=gated["db"],
        replay_summary=gated["summary"],
        shadow_report=gated["shadow_report"])
    assert verdict["kind"] == "dream-cycle-v3-cutover-gate"
    assert verdict["pass"] is False
    assert verdict["checks"]["operational_days"]["ok"] is False
    # everything ran today: exactly one distinct operational date
    assert verdict["operational_days_evidenced"] == 1
    assert verdict["required_operational_days"] == 7
    assert verdict["replay_equivalence_override"] is False
    assert "does not by itself satisfy" in verdict["statement"]
    assert "seven elapsed daily operational cycles" in verdict["statement"]


def test_historical_replay_events_never_count_as_operational_days(gated):
    """Authenticated replay reports/events prove the pipeline, not elapsed
    operations; only the fixture's separate current cycle may count."""
    import dream_cycle_v3.runtime as runtime_mod

    replay_run_ids = {row["run_id"] for row in gated["summary"]["windows"]}
    with ContinuityStore(gated["db"], read_only=True) as store:
        rows = store._conn.execute(
            "SELECT e.run_id, e.payload FROM events e WHERE e.event_type = ?",
            (runtime_mod.CYCLE_EVENT_TYPE,)).fetchall()
    replay_payloads = [json.loads(row["payload"]) for row in rows
                       if row["run_id"] in replay_run_ids]
    assert replay_payloads
    assert all(row["historical_replay"] is True for row in replay_payloads)

    for run_id in replay_run_ids:
        report = json.loads(
            (gated["db"].parent / "reports" / f"{run_id}.json")
            .read_text(encoding="utf-8"))
        assert report["historical_replay"] is True

    assert runtime_mod._distinct_operational_dates(
        gated["db"], profile="nagatha-test") == 1


def test_gate_ignores_replay_equivalence_override(gated):
    verdict = evaluate_cutover_gate(
        store_path=gated["db"], replay_store_path=gated["db"],
        replay_summary=gated["summary"],
        shadow_report=gated["shadow_report"],
        accept_replay_as_operational=True)
    assert verdict["pass"] is False
    assert verdict["replay_equivalence_override"] is False
    assert verdict["checks"]["operational_days"]["ok"] is False
    assert "does not by itself satisfy" in verdict["statement"]


def test_gate_cannot_lower_required_operational_days(gated):
    verdict = evaluate_cutover_gate(
        store_path=gated["db"], replay_store_path=gated["db"],
        replay_summary=gated["summary"],
        shadow_report=gated["shadow_report"], required_operational_days=1)
    assert verdict["pass"] is False
    assert verdict["required_operational_days"] == 7
    assert verdict["checks"]["operational_days"]["ok"] is False


def _emit_cycle_events(db, days, *, run_ids=None, mode="shadow",
                       profile="nagatha-test"):
    """Backdated runtime_cycle_completed events, optionally linked to real
    recorded runs (run_ids=None emits unlinked synthetic events)."""
    with ContinuityStore(db) as store:
        recorded = [r["run_id"] for r in store._conn.execute(
            "SELECT run_id FROM runs ORDER BY run_id")]
        for i, day in enumerate(days):
            run_id = (recorded[i % len(recorded)]
                      if run_ids == "recorded" else run_ids)
            with store.transaction() as conn:
                store._emit_event(
                    conn, entity_type="run",
                    entity_id=run_id or f"synthetic-run-{i}",
                    event_type="runtime_cycle_completed",
                    payload={"cycle_date": day, "mode": mode,
                             "profile": profile, "synthetic": i},
                    run_id=run_id, now=f"{day}T03:00:00+00:00")


def test_gate_never_counts_hashless_fabricated_linked_events(gated):
    """A fabricated payload-only event linked to a real recorded run_id, but
    carrying no report_sha256 (or one that does not authenticate against the
    real published report), must not count as an elapsed operational day —
    only genuine hash-verified completion evidence may (codex phase-4 fourth
    review finding 1)."""
    _emit_cycle_events(gated["db"], DAY_DATES, run_ids="recorded")
    verdict = evaluate_cutover_gate(
        store_path=gated["db"], replay_store_path=gated["db"],
        replay_summary=gated["summary"],
        shadow_report=gated["shadow_report"])
    assert verdict["operational_days_evidenced"] == 1  # today's real cycles
    assert verdict["checks"]["operational_days"]["ok"] is False
    assert verdict["pass"] is False


def test_distinct_operational_dates_counts_genuine_backdated_hash_events(
        gated, monkeypatch):
    """Seven distinct wall-clock dates of GENUINE hash-verified shadow runs
    (real reports, real report_sha256, only the completion event's wall
    clock is simulated to stand in for seven real elapsed days) satisfy the
    operational-days requirement; fabricated payload-only events never do."""
    import dream_cycle_v3.runtime as runtime_mod

    def backdated_record(store, *, run_id, payload):
        with store.transaction() as conn:
            store._emit_event(
                conn, entity_type="run", entity_id=run_id,
                event_type=runtime_mod.CYCLE_EVENT_TYPE, payload=payload,
                run_id=run_id, now=f"{payload['cycle_date']}T03:00:00+00:00")

    monkeypatch.setattr(runtime_mod, "_record_success_event",
                        backdated_record)

    # smoke is irrelevant to operational-day accounting, and the broker
    # abstains as stale this far past the fixture's registry timestamps;
    # disable it so only the hash-authentication path is under test.
    shared = dict(gated["shared"], smoke_message=None,
                 smoke_expected_project=None, smoke_require_thread=False)
    for i in range(7):
        window_start = datetime(2026, 9, 1 + i, tzinfo=timezone.utc)
        window_end = datetime(2026, 9, 2 + i, tzinfo=timezone.utc)
        disposition_date = window_start.date().isoformat()
        result = run_cycle(RuntimeConfig(
            mode="shadow", window_start=window_start, window_end=window_end,
            disposition_date=disposition_date, as_of=window_end.isoformat(),
            **shared))
        assert result.ok, f"backdated fixture cycle {i} must succeed"

    evidenced = runtime_mod._distinct_operational_dates(
        gated["db"], profile="nagatha-test")
    assert evidenced >= 7

    verdict = evaluate_cutover_gate(
        store_path=gated["db"], replay_store_path=gated["db"],
        replay_summary=gated["summary"],
        shadow_report=gated["shadow_report"])
    assert verdict["required_operational_days"] == 7
    assert verdict["checks"]["operational_days"]["ok"] is True
    assert verdict["pass"] is True


# -- codex phase-4 fifth review finding 2: one run/hash evidencing 7 days ----

def test_distinct_operational_dates_never_lets_one_run_evidence_seven_days(
        gated):
    """One genuine run/report can evidence at most ONE operational day, no
    matter how many completion events reference it. A forged extra event
    that replays the SAME already-attested run_id/report_sha256 payload
    under distinct entity_ids and backdated wall clocks (the read-only
    probe's technique — seven events, one real run/report hash) must not
    inflate the distinct-date count (codex phase-4 fifth review finding 2)."""
    import dream_cycle_v3.runtime as runtime_mod

    run_id = gated["shadow_report"]["run_id"]
    with ContinuityStore(gated["db"]) as store:
        row = store._conn.execute(
            "SELECT payload FROM events WHERE event_type = ? AND run_id = ?",
            (runtime_mod.CYCLE_EVENT_TYPE, run_id)).fetchone()
        payload = json.loads(row["payload"])
        for i, day in enumerate(DAY_DATES):
            with store.transaction() as conn:
                store._emit_event(
                    conn, entity_type="run", entity_id=f"replayed-evidence:{i}",
                    event_type=runtime_mod.CYCLE_EVENT_TYPE, payload=payload,
                    run_id=run_id, now=f"{day}T03:00:00+00:00")

    evidenced = runtime_mod._distinct_operational_dates(
        gated["db"], profile="nagatha-test")
    assert evidenced == 1, \
        "one genuine run/report hash must never evidence seven days"

    verdict = evaluate_cutover_gate(
        store_path=gated["db"], replay_store_path=gated["db"],
        replay_summary=gated["summary"], shadow_report=gated["shadow_report"])
    assert verdict["checks"]["operational_days"]["ok"] is False
    assert verdict["pass"] is False


def test_gate_never_counts_unlinked_synthetic_events(gated):
    """run_id=None events (or events whose run was never recorded) are
    fabrications, not operational evidence (codex phase-4 finding 3)."""
    _emit_cycle_events(gated["db"], DAY_DATES, run_ids=None)
    verdict = evaluate_cutover_gate(
        store_path=gated["db"], replay_store_path=gated["db"],
        replay_summary=gated["summary"],
        shadow_report=gated["shadow_report"])
    assert verdict["operational_days_evidenced"] == 1  # today's real cycles
    assert verdict["checks"]["operational_days"]["ok"] is False
    assert verdict["pass"] is False


def test_gate_never_counts_wrong_mode_or_profile_events(gated):
    _emit_cycle_events(gated["db"], DAY_DATES[:4], run_ids="recorded",
                       mode="live")
    _emit_cycle_events(gated["db"], DAY_DATES[4:], run_ids="recorded",
                       profile="some-other-profile")
    verdict = evaluate_cutover_gate(
        store_path=gated["db"], replay_store_path=gated["db"],
        replay_summary=gated["summary"],
        shadow_report=gated["shadow_report"])
    assert verdict["operational_days_evidenced"] == 1
    assert verdict["checks"]["operational_days"]["ok"] is False
    assert verdict["pass"] is False


def test_gate_fails_on_profile_disagreement(gated):
    tampered = copy.deepcopy(gated["shadow_report"])
    tampered["profile"] = "someone-else"
    verdict = evaluate_cutover_gate(
        store_path=gated["db"], replay_store_path=gated["db"],
        replay_summary=gated["summary"],
        shadow_report=tampered,
        accept_replay_as_operational=True)
    assert verdict["checks"]["profile_agreement"]["ok"] is False
    assert verdict["pass"] is False


def test_gate_fails_when_shadow_report_run_is_not_recorded(gated):
    """A shadow report whose run_id was never recorded in this store is
    stale or fabricated and must be refused."""
    tampered = copy.deepcopy(gated["shadow_report"])
    tampered["run_id"] = "never-recorded-run"
    verdict = evaluate_cutover_gate(
        store_path=gated["db"], replay_store_path=gated["db"],
        replay_summary=gated["summary"],
        shadow_report=tampered,
        accept_replay_as_operational=True)
    assert verdict["checks"]["shadow_cycle_report"]["ok"] is False
    assert verdict["pass"] is False


@pytest.mark.parametrize("field,value", [
    ("manifest_fingerprint", "fabricated-fingerprint"),
    ("window", {"start": "2020-01-01T00:00:00+00:00",
                "end": "2020-01-02T00:00:00+00:00"}),
])
def test_gate_links_shadow_report_to_recorded_manifest_and_window(
        gated, field, value):
    tampered = copy.deepcopy(gated["shadow_report"])
    tampered[field] = value
    verdict = evaluate_cutover_gate(
        store_path=gated["db"], replay_store_path=gated["db"],
        replay_summary=gated["summary"],
        shadow_report=tampered, accept_replay_as_operational=True)
    assert verdict["checks"]["shadow_cycle_report"]["ok"] is False
    assert verdict["pass"] is False


def test_gate_rejects_empty_assertion_only_replay_windows(gated):
    tampered = copy.deepcopy(gated["summary"])
    tampered["windows"] = [{} for _ in range(7)]
    verdict = evaluate_cutover_gate(
        store_path=gated["db"], replay_store_path=gated["db"],
        replay_summary=tampered,
        shadow_report=gated["shadow_report"],
        accept_replay_as_operational=True)
    assert verdict["checks"]["replay_summary_shape"]["ok"] is False
    assert verdict["pass"] is False


def test_gate_does_not_trust_payload_profile_over_recorded_run_profile(gated):
    """A payload claiming Nagatha cannot make a foreign-profile run count."""
    with ContinuityStore(gated["db"]) as store:
        row = store._conn.execute(
            "SELECT run_id FROM runs WHERE run_id != ? ORDER BY run_id LIMIT 1",
            (gated["shadow_report"]["run_id"],)).fetchone()
        assert row is not None
        run_id = row["run_id"]
        with store.transaction() as conn:
            conn.execute("UPDATE runs SET profile='foreign-profile' "
                         "WHERE run_id=?", (run_id,))
            for i, day in enumerate(DAY_DATES):
                store._emit_event(
                    conn, entity_type="run", entity_id=run_id,
                    event_type="runtime_cycle_completed",
                    payload={"mode": "shadow", "profile": "nagatha-test",
                             "synthetic": f"foreign-{i}"},
                    run_id=run_id, now=f"{day}T04:00:00+00:00")
    verdict = evaluate_cutover_gate(
        store_path=gated["db"], replay_store_path=gated["db"],
        replay_summary=gated["summary"],
        shadow_report=gated["shadow_report"])
    assert verdict["operational_days_evidenced"] == 1
    assert verdict["checks"]["operational_days"]["ok"] is False


def test_gate_fails_on_broken_replay_invariants_even_with_override(gated):
    tampered = copy.deepcopy(gated["summary"])
    tampered["invariants"]["all_reruns_zero_delta"] = False
    verdict = evaluate_cutover_gate(
        store_path=gated["db"], replay_store_path=gated["db"],
        replay_summary=tampered,
        shadow_report=gated["shadow_report"],
        accept_replay_as_operational=True)
    assert verdict["pass"] is False
    assert verdict["checks"]["replay_invariants"]["ok"] is False


def test_gate_fails_on_low_retrieval_rate(gated):
    tampered = copy.deepcopy(gated["summary"])
    tampered["invariants"]["retrieval_success_rate"] = 0.5
    verdict = evaluate_cutover_gate(
        store_path=gated["db"], replay_store_path=gated["db"],
        replay_summary=tampered,
        shadow_report=gated["shadow_report"],
        accept_replay_as_operational=True)
    assert verdict["pass"] is False
    assert verdict["checks"]["retrieval_success_rate"]["ok"] is False


def test_gate_fails_on_wrong_document_shapes(gated):
    verdict = evaluate_cutover_gate(
        store_path=gated["db"], replay_store_path=gated["db"],
        replay_summary={"kind": "something-else"},
        shadow_report=gated["shadow_report"],
        accept_replay_as_operational=True)
    assert verdict["pass"] is False
    assert verdict["checks"]["replay_summary_shape"]["ok"] is False

    verdict = evaluate_cutover_gate(
        store_path=gated["db"], replay_store_path=gated["db"],
        replay_summary=gated["summary"],
        shadow_report={"kind": "not-a-cycle-report"},
        accept_replay_as_operational=True)
    assert verdict["pass"] is False
    assert verdict["checks"]["shadow_cycle_report"]["ok"] is False


def test_cli_cutover_gate(gated, capsys):
    from dream_cycle_v3.cli import main
    args = ["cutover-gate",
            "--db", str(gated["db"]),
            "--replay-summary", str(gated["summary_path"]),
            "--shadow-report", str(gated["shadow_report_path"])]
    rc = main(args)
    out = capsys.readouterr().out
    verdict = json.loads(out)
    assert rc == 1
    assert verdict["pass"] is False

    with pytest.raises(SystemExit) as exc:
        main(args + ["--accept-replay-as-operational"])
    assert exc.value.code == 2
    assert "unrecognized arguments: --accept-replay-as-operational" in \
        capsys.readouterr().err


# -- codex phase-4 third review finding 1: cryptographic evidence linkage ----
# Replay rows must be joined to real recorded runs (not shape-checked JSON),
# and the shadow report's invariants/smoke must come from a hash-verified
# canonical report, never a caller-supplied dict or an edited file.

def test_gate_rejects_fabricated_replay_window_run_id(gated):
    """A shape-valid window row whose run_id was never recorded in the
    replay store is a fabrication, not evidence."""
    tampered = copy.deepcopy(gated["summary"])
    tampered["windows"][0]["run_id"] = "fabricated-run-id-not-recorded"
    verdict = evaluate_cutover_gate(
        store_path=gated["db"], replay_store_path=gated["db"],
        replay_summary=tampered,
        shadow_report=gated["shadow_report"],
        accept_replay_as_operational=True)
    # the OLD shape-only check still finds seven distinct, well-typed rows
    assert verdict["checks"]["replay_summary_shape"]["ok"] is True
    assert verdict["checks"]["replay_windows_linked_to_store"]["ok"] is False
    assert verdict["pass"] is False


def test_gate_rejects_replay_window_claim_mismatch_for_a_real_run(gated):
    """A window row pointing at a REAL recorded run, but lying about its
    outcome (here: sources count), must not pass — the claim is checked
    against that run's own hash-verified canonical report."""
    tampered = copy.deepcopy(gated["summary"])
    tampered["windows"][0]["sources"] += 1
    verdict = evaluate_cutover_gate(
        store_path=gated["db"], replay_store_path=gated["db"],
        replay_summary=tampered,
        shadow_report=gated["shadow_report"],
        accept_replay_as_operational=True)
    assert verdict["checks"]["replay_windows_linked_to_store"]["ok"] is False
    assert verdict["pass"] is False


def test_gate_fails_closed_on_missing_replay_store(gated):
    """A missing/mismatched replay store must fail the verdict, not crash."""
    verdict = evaluate_cutover_gate(
        store_path=gated["db"],
        replay_store_path=gated["tmp"] / "no-such-root" / "continuity.db",
        replay_summary=gated["summary"],
        shadow_report=gated["shadow_report"],
        accept_replay_as_operational=True)
    assert verdict["checks"]["replay_windows_linked_to_store"]["ok"] is False
    assert verdict["checks"]["replay_store_current_invariants"]["ok"] is False
    assert verdict["pass"] is False


def test_gate_rejects_edited_shadow_report_dict_claiming_false_smoke(gated):
    """A shadow_report dict can keep every DB-linked field correct (run_id,
    manifest_fingerprint, window, profile) and still lie about invariants/
    smoke — those must come only from the hash-verified canonical report on
    disk, never the caller's dict."""
    shared = dict(gated["shared"], smoke_message=None,
                 smoke_expected_project=None, smoke_require_thread=False)
    off_root = gated["tmp"] / "v3-smoke-off"
    real = run_cycle(RuntimeConfig(
        mode="shadow",
        window_start=datetime(2026, 7, 20, tzinfo=timezone.utc),
        window_end=datetime(2026, 7, 21, tzinfo=timezone.utc),
        disposition_date="2026-07-20",
        as_of="2026-07-21T00:00:00+00:00",
        v3_root=off_root,
        **{k: v for k, v in shared.items() if k != "v3_root"}))
    assert real.ok
    assert real.report["retrieval_smoke"]["configured"] is False

    lie = copy.deepcopy(real.report)
    lie["retrieval_smoke"] = {"configured": True, "ok": True}

    verdict = evaluate_cutover_gate(
        store_path=off_root / "continuity.db", replay_store_path=gated["db"],
        replay_summary=gated["summary"],
        shadow_report=lie,
        accept_replay_as_operational=True)
    assert verdict["checks"]["shadow_report_authenticity"]["ok"] is True
    assert verdict["checks"]["shadow_cycle_report"]["ok"] is False
    assert verdict["pass"] is False


def test_gate_rejects_report_file_tampered_after_publication(gated):
    """Editing the on-disk report file after publication changes its bytes'
    sha256, so even the UNMODIFIED, originally-correct shadow_report dict
    must fail: disk bytes checked against the event's recorded hash are the
    source of truth, not the caller's claim."""
    tampered_on_disk = copy.deepcopy(gated["shadow_report"])
    tampered_on_disk["retrieval_smoke"]["wake"]["threads"] += 1
    gated["shadow_report_path"].write_text(
        json.dumps(tampered_on_disk), encoding="utf-8")

    verdict = evaluate_cutover_gate(
        store_path=gated["db"], replay_store_path=gated["db"],
        replay_summary=gated["summary"],
        shadow_report=gated["shadow_report"],  # untouched, originally-correct
        accept_replay_as_operational=True)
    assert verdict["checks"]["shadow_report_authenticity"]["ok"] is False
    assert verdict["checks"]["shadow_cycle_report"]["ok"] is False
    assert verdict["pass"] is False


def test_cli_cutover_gate_refuses_symlinked_replay_store(gated, tmp_path):
    """The CLI derives the replay store from --replay-summary's location;
    it must refuse a symlinked store rather than verify through it."""
    from dream_cycle_v3.cli import main

    fake_root = tmp_path / "fake-v3-root"
    (fake_root / "reports").mkdir(parents=True)
    summary_copy = fake_root / "reports" / gated["summary_path"].name
    summary_copy.write_text(
        gated["summary_path"].read_text(encoding="utf-8"), encoding="utf-8")
    (fake_root / "continuity.db").symlink_to(gated["db"])

    args = ["cutover-gate",
            "--db", str(gated["db"]),
            "--replay-summary", str(summary_copy),
            "--shadow-report", str(gated["shadow_report_path"])]
    rc = main(args)
    assert rc == 2


# -- codex phase-4 fourth review finding 1: replay summary re-attestation ----
# rerun_zero_delta/rerun_row_delta/read_roots_unchanged/aggregate invariants
# must be authenticated against a hash-verified canonical summary published
# under the replay root, never trusted from editable assertion-only JSON.

def test_gate_rejects_edited_replay_summary_dict(gated):
    """A caller-supplied replay_summary dict can re-use every genuinely
    linked per-window run_id and still lie about a rerun/read-roots claim;
    that must fail the new authenticity check even though the per-window
    linked-to-store check has nothing to compare it against."""
    tampered = copy.deepcopy(gated["summary"])
    tampered["windows"][0]["rerun_zero_delta"] = False
    verdict = evaluate_cutover_gate(
        store_path=gated["db"], replay_store_path=gated["db"],
        replay_summary=tampered,
        shadow_report=gated["shadow_report"],
        accept_replay_as_operational=True)
    assert verdict["checks"]["replay_summary_authenticity"]["ok"] is False
    assert verdict["pass"] is False


def test_gate_rejects_replay_summary_file_tampered_after_publication(gated):
    """Editing the on-disk replay summary file after publication must be
    caught even when the caller passes the ORIGINAL, untouched summary dict:
    disk bytes checked against the recorded event hash are authoritative,
    not the caller's claim."""
    tampered_on_disk = copy.deepcopy(gated["summary"])
    tampered_on_disk["invariants"]["read_roots_unchanged"] = False
    gated["summary_path"].write_text(json.dumps(tampered_on_disk),
                                     encoding="utf-8")

    verdict = evaluate_cutover_gate(
        store_path=gated["db"], replay_store_path=gated["db"],
        replay_summary=gated["summary"],  # untouched, originally-correct
        shadow_report=gated["shadow_report"],
        accept_replay_as_operational=True)
    assert verdict["checks"]["replay_summary_authenticity"]["ok"] is False
    assert verdict["pass"] is False


def test_gate_accepts_genuine_unmodified_replay_summary(gated):
    """The control case: an untouched, genuinely-published summary must
    authenticate cleanly."""
    verdict = evaluate_cutover_gate(
        store_path=gated["db"], replay_store_path=gated["db"],
        replay_summary=gated["summary"],
        shadow_report=gated["shadow_report"],
        accept_replay_as_operational=True)
    assert verdict["checks"]["replay_summary_authenticity"]["ok"] is True
    assert verdict["checks"]["replay_invariants"]["ok"] is True


# -- codex phase-4 fourth review finding 2: malformed replay rows must not --
# -- raise ---------------------------------------------------------------

def test_gate_handles_non_dict_replay_window_rows_without_crashing(gated):
    """A malformed replay summary with non-dict window rows must yield a
    structured failing verdict, never raise an AttributeError."""
    tampered = copy.deepcopy(gated["summary"])
    tampered["windows"] = ["not-a-dict"] * 7
    verdict = evaluate_cutover_gate(
        store_path=gated["db"], replay_store_path=gated["db"],
        replay_summary=tampered,
        shadow_report=gated["shadow_report"],
        accept_replay_as_operational=True)
    assert verdict["checks"]["replay_summary_shape"]["ok"] is False
    assert verdict["pass"] is False


def test_gate_handles_mixed_non_dict_replay_window_rows_without_crashing(
        gated):
    tampered = copy.deepcopy(gated["summary"])
    tampered["windows"] = [tampered["windows"][0], None, 42, [], "x",
                           {"date": "2026-07-05"}, True]
    verdict = evaluate_cutover_gate(
        store_path=gated["db"], replay_store_path=gated["db"],
        replay_summary=tampered,
        shadow_report=gated["shadow_report"],
        accept_replay_as_operational=True)
    assert verdict["checks"]["replay_summary_shape"]["ok"] is False
    assert verdict["pass"] is False
