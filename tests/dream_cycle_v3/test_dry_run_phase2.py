"""Phase 2 dry run: deterministic scenario matrix, idempotent rerun, CLI."""
import json

from dream_cycle_v3.cli import main
from dream_cycle_v3.dry_run_phase2 import execute_phase2_dry_run

EXPECTED_SCENARIOS = {
    "hot_env_fact": "promoted",
    "warm_pref_fact": "promoted",
    "skill_procedure": "promoted",
    "project_decision": "promoted",
    "exact_duplicate": "rejected",
    "near_duplicate": "quarantined",
    "hot_task_leakage": "rejected",
    "skill_budget_overflow": "quarantined",
    "conflict_unresolved": "quarantined",
    "conflict_supersedes": "promoted",
    "retrieval_failure": "quarantined",
}


def core(report):
    return {k: v for k, v in report.items()
            if k not in ("workdir", "continuity_db", "report_path")}


def test_phase2_dry_run_matrix_and_invariants(tmp_path):
    report = execute_phase2_dry_run(tmp_path / "run")
    assert report["scenarios"] == EXPECTED_SCENARIOS
    assert report["invariants"]["every_scenario_matched_expected"] is True
    assert report["invariants"]["promoted_without_full_receipt"] == 0
    assert report["idempotency"]["rerun_row_delta"] == {}
    assert report["idempotency"]["rerun_store_identical"] is True
    assert report["idempotency"]["rerun_destinations_identical"] is True
    assert report["revision_conflict_demo"]["outcome"] == "revision_conflict"
    assert report["revision_conflict_demo"]["candidate_status_after"] == "validated"
    # LLM slot exercised with mandatory provenance.
    assert report["llm"]["classified"] == 3
    assert report["llm"]["model"] == "fixture-static-1"
    assert report["llm"]["prompt_hash"].startswith("sha256:")
    # Every second-pass promotion is a no-op, never a duplicate write.
    assert all(v in ("unchanged",) or v.startswith("skipped:")
               for v in report["second_pass"].values())


def test_phase2_dry_run_is_identical_across_workdirs(tmp_path):
    a = execute_phase2_dry_run(tmp_path / "a")
    b = execute_phase2_dry_run(tmp_path / "b")
    assert core(a) == core(b)
    assert a["run_id"] == b["run_id"]
    # The persisted report files are byte-identical too.
    ra = (tmp_path / "a" / "reports").glob("phase2-*.json")
    rb = (tmp_path / "b" / "reports").glob("phase2-*.json")
    assert next(iter(ra)).read_bytes() == next(iter(rb)).read_bytes()


def test_phase2_dry_run_destination_content(tmp_path):
    report = execute_phase2_dry_run(tmp_path / "run")
    dest = tmp_path / "run" / "destinations"
    index = (dest / "memory" / "MEMORY.md").read_text(encoding="utf-8")
    # Seeded line survives; the hot fact was added; leakage never landed.
    assert "gateway log rotation" in index
    assert "python runtime version" in index
    assert "TODO" not in index
    assert "kanban migration" not in index
    # Superseded warm claim was replaced by revision 2.
    warm_facts = [p.read_text(encoding="utf-8")
                  for p in (dest / "memory").glob("email-tooling-*.md")]
    assert len(warm_facts) == 1
    assert "zerolib mcp is retired" in warm_facts[0]
    assert "rev=2" in warm_facts[0]
    # Skill patched, hand-written content intact, budget-capped skill intact.
    skill = (dest / "skills" / "hermes-continuity-map" / "SKILL.md").read_text(
        encoding="utf-8")
    assert "Hand-written map content that must survive." in skill
    assert "carry-forward invariant" in skill
    bulky = (dest / "skills" / "bulky-skill" / "SKILL.md").read_text(
        encoding="utf-8")
    assert "one more procedure" not in bulky
    # Rolled-back retrieval failure left no fact file behind.
    assert not list((dest / "memory").glob("unfindable-fact-*.md"))
    # Decision doc gained the section, prior content preserved.
    decisions = (dest / "projects" / "klas-sample" / "decisions.md").read_text(
        encoding="utf-8")
    assert "keep the old flow" in decisions
    assert "listing dedupe policy" in decisions


def test_phase2_cli_exit_code_and_json(tmp_path, capsys):
    assert main(["dry-run-phase2", "--workdir", str(tmp_path / "cli")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["scenarios"] == EXPECTED_SCENARIOS
    assert out["receipts"] == 5
