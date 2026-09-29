import json

import pytest

from dream_cycle_v3.cli import main
from dream_cycle_v3.manifest import load_manifest


def _run_cli(capsys, argv):
    code = main(argv)
    out = capsys.readouterr().out.strip()
    return code, json.loads(out) if out else None


@pytest.fixture(scope="module")
def dry_run_result(tmp_path_factory):
    from dream_cycle_v3.dry_run import execute_dry_run

    workdir = tmp_path_factory.mktemp("dryrun-a")
    return execute_dry_run(workdir)


def test_dry_run_completes_with_invariants(dry_run_result):
    report = dry_run_result
    assert report["invariants"]["every_selected_thread_dispositioned"] is True
    assert report["invariants"]["done_threads_without_closure_proof"] == 0
    assert report["idempotency"]["rerun_run_id_identical"] is True
    assert report["idempotency"]["rerun_row_delta"] == {}
    assert report["idempotency"]["rerun_store_identical"] is True
    assert report["idempotency"]["rerun_all_dispositions_pre_existing"] is True


def test_dry_run_policy_and_adapter_coverage(dry_run_result):
    by_action = dry_run_result["dispositions"]["by_action"]
    assert by_action["close_done"] == 2
    assert by_action["blocked"] == 1
    assert by_action["needs_link"] == 1
    assert by_action["authority_gated"] == 1
    assert by_action["stale_review"] == 1
    assert by_action["continue"] == 2

    adapters = {a["adapter"]: a for a in dry_run_result["adapters"]}
    assert adapters["kanban"]["status"] == "ok"
    assert adapters["todoist"]["status"] == "ok"
    assert adapters["github"]["status"] == "unavailable"

    statuses = dry_run_result["candidates"]["by_status"]
    assert statuses.get("classified", 0) > 0
    assert statuses.get("quarantined", 0) > 0

    routing = dry_run_result["routing"]
    assert routing["external_task_ref"] >= 1     # kanban ref bullet
    assert routing["canonical_path"] >= 1        # state/ docs
    assert routing["alias"] >= 1                 # klas-notes alias line
    assert routing["unresolved"] >= 1            # projectless TODO quarantined


def test_dry_run_artifacts_exist_and_validate(dry_run_result):
    from pathlib import Path

    manifest = load_manifest(Path(dry_run_result["manifest_path"]))
    assert manifest["run_id"] == dry_run_result["run_id"]
    report_file = json.loads(Path(dry_run_result["report_path"]).read_text())
    assert report_file["run_id"] == dry_run_result["run_id"]
    assert report_file["kind"] == "dream-cycle-v3-run-report"
    assert Path(dry_run_result["continuity_db"]).exists()

    # No secret or transcript canaries anywhere in the persisted artifacts:
    # manifest, report, and the raw continuity database bytes.
    blob = Path(dry_run_result["manifest_path"]).read_text() + \
        Path(dry_run_result["report_path"]).read_text()
    db_bytes = Path(dry_run_result["continuity_db"]).read_bytes()
    for canary in ("CANARY_NEVER_IN_MANIFEST", "SAMPLEFAKE",
                   "TRANSCRIPT_CANARY_USER_ZX81",
                   "TRANSCRIPT_CANARY_ASSISTANT_QL72",
                   "TRANSCRIPT_CANARY_JSON_MM45", "TRANSCRIPT_CANARY_NOTES_KP19",
                   "TRANSCRIPT_CANARY_TAIL_RV33"):
        assert canary not in blob, canary
        assert canary.encode() not in db_bytes, canary
    excluded = {e["location"]: e["reason"] for e in manifest["excluded"]}
    # Non-hidden canary (matches the *.env secret pattern like a hidden
    # .env does) so wheel/sdist package-data globs ship it and the installed
    # artifact exercises identical privacy coverage to a source checkout.
    assert excluded["fake-canary.env"].startswith("secret_path:")
    assert excluded["secrets"].startswith("secret_dir:")

    # Session sources are fingerprinted evidence with suppressed excerpts.
    sessions = [s for s in manifest["sources"] if s["source_type"] == "session"]
    assert len(sessions) == 4
    assert all(s["excerpt"] is None for s in sessions)
    assert all(s["excerpt_suppressed"] == "session_transcript" for s in sessions)
    assert all(s["fingerprint"].startswith("sha256:") for s in sessions)


def test_dry_run_db_candidates_are_transcript_free(dry_run_result):
    import sqlite3

    conn = sqlite3.connect(f"file:{dry_run_result['continuity_db']}?mode=ro",
                           uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT normalized_claim, evidence_refs, validation_requirements, "
        "status FROM candidates").fetchall()
    conn.close()
    assert rows
    stubs = 0
    for row in rows:
        assert "user:" not in row["normalized_claim"]
        assert "assistant:" not in row["normalized_claim"]
        for ref in json.loads(row["evidence_refs"]):
            excerpt = ref.get("excerpt")
            if ref["source_type"] == "session":
                assert excerpt is None or excerpt == ""
        if "transcript_policy" in row["validation_requirements"]:
            stubs += 1
            assert row["status"] == "quarantined"
    assert stubs == 4  # one metadata-only stub per session source


def test_dry_run_is_reproducible_across_workdirs(dry_run_result, tmp_path):
    from dream_cycle_v3.dry_run import execute_dry_run

    second = execute_dry_run(tmp_path / "dryrun-b")
    assert second["run_id"] == dry_run_result["run_id"]
    assert second["manifest_fingerprint"] == dry_run_result["manifest_fingerprint"]
    assert second["store_counts"] == dry_run_result["store_counts"]
    assert second["dispositions"] == dry_run_result["dispositions"]


def test_cli_dry_run_and_downstream_commands(tmp_path, capsys):
    workdir = tmp_path / "cli-dryrun"
    code, summary = _run_cli(capsys, ["dry-run", "--workdir", str(workdir)])
    assert code == 0
    assert summary["idempotency"]["rerun_store_identical"] is True

    code, validation = _run_cli(
        capsys, ["validate-manifest", "--manifest", summary["manifest_path"]])
    assert code == 0 and validation == {"valid": True, "errors": []}

    code, report = _run_cli(capsys, [
        "report", "--db", summary["continuity_db"], "--run-id", summary["run_id"]])
    assert code == 0
    assert report["store_counts"]["thread_dispositions"] == 8

    # carry-forward for the same date via CLI: pure no-op, still invariant-true.
    code, carry = _run_cli(capsys, [
        "carry-forward", "--db", summary["continuity_db"],
        "--v3-root", str(workdir),
        "--run-id", summary["run_id"], "--date", "2026-07-11",
        "--as-of", "2026-07-11T09:00:00+00:00"])
    assert code == 0
    assert carry["invariant_ok"] is True
    assert carry["dispositioned"] == 0

    # Impossible calendar date is refused before any write (exit 2).
    code = main(["carry-forward", "--db", summary["continuity_db"],
                 "--v3-root", str(workdir),
                 "--run-id", summary["run_id"], "--date", "2026-99-99",
                 "--as-of", "2026-07-11T09:00:00+00:00"])
    captured = capsys.readouterr()
    assert code == 2
    assert "ContractViolation" in captured.err


def test_cli_collect_requires_explicit_roots(tmp_path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    (src / "note.md").write_text("hello\n")
    out = tmp_path / "out"
    code, summary = _run_cli(capsys, [
        "collect", "--profile", "cli-test", "--root", f"profile={src}",
        "--out", str(out),
        "--window-start", "2020-01-01T00:00:00+00:00",
        "--window-end", "2030-01-01T00:00:00+00:00",
        "--as-of", "2026-07-11T08:00:00+00:00"])
    assert code == 0
    assert summary["sources"] == 1

    # 'Z' timestamps are valid CLI input on every supported interpreter and
    # produce the same run over the same window regardless of suffix form.
    code, z_summary = _run_cli(capsys, [
        "collect", "--profile", "cli-test", "--root", f"profile={src}",
        "--out", str(out),
        "--window-start", "2020-01-01T00:00:00Z",
        "--window-end", "2030-01-01T00:00:00Z",
        "--as-of", "2026-07-11T08:00:00Z"])
    assert code == 0
    assert z_summary["run_id"] == summary["run_id"]

    code, _ = _run_cli(capsys, ["init-db", "--db", str(tmp_path / "v3" / "c.db"),
                                "--v3-root", str(tmp_path / "v3")])
    assert code == 0

    # Nested output root is refused loudly (exit 2, typed error on stderr).
    code = main(["collect", "--profile", "cli-test",
                 "--root", f"profile={src}", "--out", str(src / "out"),
                 "--window-start", "2020-01-01T00:00:00+00:00",
                 "--window-end", "2030-01-01T00:00:00+00:00"])
    captured = capsys.readouterr()
    assert code == 2
    assert "RootResolutionError" in captured.err
