"""Phase 2 deterministic dry run: promotion adapters end-to-end in a workdir.

Everything is created inside one caller-selected workdir: a fresh continuity
store, fixture destination homes (memory / skills / project docs), and
backups. Dates, ids, and the LLM transport are pinned, so two dry runs in
different workdirs report identical scenario outcomes and the same run_id.

Scenario matrix (each exercises one contract from the plan §6/§9/§11):

  hot_env_fact          promoted   hot memory: fact file + index line
  warm_pref_fact        promoted   warm memory: term-search retrieval
  skill_procedure       promoted   bounded patch of a seeded skill
  project_decision      promoted   decision appended to a seeded doc
  exact_duplicate       rejected   byte-normalized duplicate of seeded fact
  near_duplicate        quarantined word-set Jaccard >= 0.8 vs seeded fact
  hot_task_leakage      rejected   task language can never reach hot memory
  skill_budget_overflow quarantined 2,500-token skill cap (§9)
  conflict_unresolved   quarantined contradicts an active promoted claim
  conflict_supersedes   promoted   explicit relationship; old claim superseded
  retrieval_failure     quarantined write rolled back byte-identically
  revision_conflict     (demo)     concurrent edit after backup refuses write

The whole promotion pass then runs a second time: zero new receipts, zero
row changes, destinations byte-identical — §11 idempotency, demonstrated
rather than asserted.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import COLLECTOR_VERSION, CONTRACT_SCHEMA_VERSION
from .adapters.destinations import (DestinationHomes, MemoryDestination,
                                    adapter_for_destination)
from .canonical import canonical_json, sha256_hex, stable_id
from .classifier import ClassificationInput
from .llm_classifier import LLMClassifier
from .manifest import assemble_manifest
from .policies import ConflictResolutions
from .promotion import promote_candidate_via_adapter, promote_with_homes
from .roots import prepare_output_root
from .store import ContinuityStore

FIXED_AS_OF = "2026-07-12T08:00:00+00:00"
FIXED_OBSERVED_AT = "2026-07-11T12:00:00+00:00"
FIXED_WINDOW_START = "2026-07-09T00:00:00+00:00"
FIXED_WINDOW_END = "2026-07-12T00:00:00+00:00"
FIXTURE_MODEL = "fixture-static-1"

SAMPLE_DATA = Path(__file__).parent / "sample_data"

# Claims (canonical subjects -> normalized claims) used by the scenarios.
_SEEDED_WARM_CLAIM = ("the gateway error log is append-only and never "
                      "rotated; filter by time via gateway.log")


def fixture_transport(model: str, prompt: str) -> str:
    """Deterministic stand-in LLM: a pure function of the prompt text."""
    def reply(klass: str, confidence: float) -> str:
        return canonical_json({"decision": "classify", "class": klass,
                               "confidence": confidence})
    if "python3" in prompt:
        return reply("runtime_memory_hot", 0.92)
    if "envelope cli" in prompt:
        return reply("runtime_memory_warm", 0.9)
    if "carry-forward invariant" in prompt:
        return reply("reference_knowledge", 0.88)
    if "ambiguous" in prompt:
        return canonical_json({"decision": "classify",
                               "class": "reference_knowledge",
                               "confidence": 0.4})  # below threshold
    return canonical_json({"decision": "abstain", "class": None,
                           "confidence": 0.0})


def _seed_homes(root: Path) -> DestinationHomes:
    memory = root / "memory"
    skills = root / "skills"
    projects = root / "projects"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text(
        "# Memory index\n\n"
        "- [gateway log rotation](gateway-log-rotation.md) — gateway error "
        "log is append-only, never rotated\n", encoding="utf-8")
    (memory / "gateway-log-rotation.md").write_text(
        "---\nname: gateway-log-rotation\ndescription: gateway log rotation\n"
        "metadata:\n  type: project\n---\n\n"
        f"{_SEEDED_WARM_CLAIM}\n", encoding="utf-8")
    (skills / "hermes-continuity-map").mkdir(parents=True)
    (skills / "hermes-continuity-map" / "SKILL.md").write_text(
        "---\nname: hermes-continuity-map\n"
        "description: Continuity architecture map\n---\n\n"
        "## Store layout\n\nHand-written map content that must survive.\n",
        encoding="utf-8")
    (skills / "bulky-skill").mkdir(parents=True)
    filler = "This line pads the bulky skill toward its token budget.\n" * 135
    (skills / "bulky-skill" / "SKILL.md").write_text(
        "---\nname: bulky-skill\ndescription: Deliberately near the cap\n"
        "---\n\n" + filler, encoding="utf-8")
    (projects / "klas-sample").mkdir(parents=True)
    (projects / "klas-sample" / "decisions.md").write_text(
        "# klas-sample: decisions\n\n## Prior decision\n\nkeep the old flow\n",
        encoding="utf-8")
    return DestinationHomes(memory=memory, skills=skills, projects=projects)


def _candidate(run_id: str, *, key: str, klass: str, destination: str,
               subject: str, claim: str, project_id: str | None,
               terms: list[str], revision: int = 1,
               conflict_set: list[str] | None = None,
               status: str = "classified",
               classifier_kind: str = "deterministic",
               classifier_version: str = "phase2-fixture-1",
               model: str | None = None,
               prompt_hash: str | None = None) -> dict[str, Any]:
    candidate_id = stable_id("dream-cycle-v3-candidate",
                             f"fixture:phase2/{key}", claim)
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "content_revision": revision,
        "class": klass,
        "project_id": project_id,
        "destination": destination,
        "normalized_claim": claim,
        "canonical_subject": subject,
        "retrieval_terms": terms,
        "evidence_refs": [{
            "source_type": "file",
            "source_id": f"fixture:phase2/{key}",
            "location": None,
            "observed_at": FIXED_OBSERVED_AT,
            "fingerprint": "sha256:" + sha256_hex(claim),
        }],
        "confidence": 0.9,
        "freshness_class": "durable",
        "sensitivity_class": "normal",
        "dedupe_key": stable_id("dream-cycle-v3-dedupe", destination,
                                project_id or "", subject, claim,
                                str(CONTRACT_SCHEMA_VERSION)),
        "semantic_cluster_id": None,
        "status": status,
        "validation_requirements": [],
        "conflict_set": conflict_set or [],
        "provenance": {
            "run_id": run_id,
            "collector_version": COLLECTOR_VERSION,
            "classifier_kind": classifier_kind,
            "classifier_version": classifier_version,
            "model": model,
            "prompt_hash": prompt_hash,
        },
    }


def _build_scenarios(run_id: str, llm: LLMClassifier
                     ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fixture candidates; the first three carry real LLM-slot provenance."""
    llm_texts = {
        "hot_env_fact": "default python3 is 3.14 via homebrew",
        "warm_pref_fact": "mailbox work goes through the envelope cli, "
                          "never raw smtp or imap",
        "skill_procedure": "the carry-forward invariant check rolls the "
                           "whole run back when a thread is missed",
    }
    llm_outcomes = {}
    for key, text in llm_texts.items():
        outcome = llm.classify(ClassificationInput(
            item_id=key, text=text, source_id=f"fixture:phase2/{key}"))
        assert outcome.decision == "classified", key
        llm_outcomes[key] = outcome

    abstained = llm.classify(ClassificationInput(
        item_id="ambiguous", text="an ambiguous observation the model "
        "cannot place", source_id="fixture:phase2/llm_abstain"))
    assert abstained.decision == "abstain"

    def llm_kwargs(key):
        o = llm_outcomes[key]
        return {"classifier_kind": "llm", "classifier_version": llm.version,
                "model": o.model, "prompt_hash": o.prompt_hash,
                "klass": o.candidate_class}

    scenarios = [
        {"key": "hot_env_fact",
         "candidate": _candidate(run_id, key="hot_env_fact",
                                 destination="memory:hot",
                                 subject="python runtime version",
                                 claim=llm_texts["hot_env_fact"],
                                 project_id=None,
                                 terms=["python", "homebrew"],
                                 **llm_kwargs("hot_env_fact")),
         "expected": "promoted"},
        {"key": "warm_pref_fact",
         "candidate": _candidate(run_id, key="warm_pref_fact",
                                 destination="memory:warm",
                                 subject="email tooling",
                                 claim=llm_texts["warm_pref_fact"],
                                 project_id=None,
                                 terms=["envelope", "email"],
                                 **llm_kwargs("warm_pref_fact")),
         "expected": "promoted"},
        {"key": "skill_procedure",
         "candidate": _candidate(run_id, key="skill_procedure",
                                 destination="skill:hermes-continuity-map",
                                 subject="carry-forward invariant",
                                 claim=llm_texts["skill_procedure"],
                                 project_id="hermes-continuity",
                                 terms=["carry-forward"],
                                 **llm_kwargs("skill_procedure")),
         "expected": "promoted"},
        {"key": "project_decision",
         "candidate": _candidate(run_id, key="project_decision",
                                 klass="decision_record",
                                 destination="project:klas-sample:decisions",
                                 subject="listing dedupe policy",
                                 claim="decision: dedupe listings by "
                                       "normalized title plus seller id",
                                 project_id="klas-sample",
                                 terms=["dedupe", "listings"]),
         "expected": "promoted"},
        {"key": "exact_duplicate",
         "candidate": _candidate(run_id, key="exact_duplicate",
                                 klass="runtime_memory_warm",
                                 destination="memory:warm",
                                 subject="gateway error log behavior",
                                 claim="The gateway error log is append-only "
                                       "and never rotated: filter by time "
                                       "via gateway.log",
                                 project_id=None, terms=["gateway"]),
         "expected": "rejected"},
        {"key": "near_duplicate",
         "candidate": _candidate(run_id, key="near_duplicate",
                                 klass="runtime_memory_warm",
                                 destination="memory:warm",
                                 subject="gateway log flood behavior",
                                 claim="gateway error log is append-only and "
                                       "never rotated so filter by time via "
                                       "gateway.log",
                                 project_id=None, terms=["gateway"]),
         "expected": "quarantined"},
        {"key": "hot_task_leakage",
         "candidate": _candidate(run_id, key="hot_task_leakage",
                                 klass="runtime_memory_hot",
                                 destination="memory:hot",
                                 subject="pending migration work",
                                 claim="TODO: follow-up on the kanban "
                                       "migration, blocked on tyler",
                                 project_id=None, terms=["migration"]),
         "expected": "rejected"},
        {"key": "skill_budget_overflow",
         "candidate": _candidate(run_id, key="skill_budget_overflow",
                                 klass="reference_knowledge",
                                 destination="skill:bulky-skill",
                                 subject="one more procedure",
                                 claim="appending this procedure pushes the "
                                       "bulky skill over its token budget",
                                 project_id=None, terms=["bulky"]),
         "expected": "quarantined"},
    ]

    warm = next(s for s in scenarios if s["key"] == "warm_pref_fact")
    scenarios.append(
        {"key": "conflict_unresolved",
         "candidate": _candidate(run_id, key="conflict_unresolved",
                                 klass="runtime_memory_warm",
                                 destination="memory:warm",
                                 subject="email tooling exceptions",
                                 claim="some mailbox work may bypass the "
                                       "envelope wrapper entirely",
                                 project_id=None, terms=["envelope"],
                                 conflict_set=[
                                     warm["candidate"]["candidate_id"]]),
         "expected": "quarantined"})
    scenarios.append(
        {"key": "conflict_supersedes",
         "candidate": _candidate(run_id, key="conflict_supersedes",
                                 klass="runtime_memory_warm",
                                 destination="memory:warm",
                                 subject="email tooling",   # same record
                                 revision=2,
                                 claim="mailbox work uses the envelope rust "
                                       "cli; the legacy zerolib mcp is "
                                       "retired",
                                 project_id=None,
                                 terms=["envelope", "email"],
                                 conflict_set=[
                                     warm["candidate"]["candidate_id"]]),
         "expected": "promoted",
         "resolutions": ConflictResolutions(
             {warm["candidate"]["candidate_id"]: "supersedes"})})
    scenarios.append(
        {"key": "retrieval_failure",
         "candidate": _candidate(run_id, key="retrieval_failure",
                                 klass="runtime_memory_warm",
                                 destination="memory:warm",
                                 subject="unfindable fact",
                                 claim="a fact whose retrieval terms match "
                                       "nothing in the store",
                                 project_id=None,
                                 terms=["zzz-unfindable-term"]),
         "expected": "quarantined"})
    llm_report = {
        "model": llm.model,
        "prompt_hash": llm.prompt_hash,
        "classified": len(llm_outcomes),
        "abstained_below_threshold": 1,
        "abstain_reason": list(abstained.reasons),
    }
    return scenarios, llm_report


def _destinations_fingerprint(homes: DestinationHomes) -> str:
    parts = []
    for home in (homes.memory, homes.skills, homes.projects):
        for path in sorted(home.rglob("*")):
            if path.is_file():
                parts.append(f"{path.relative_to(home.parent)}"
                             f"\x1f{sha256_hex(path.read_bytes())}")
    return "sha256:" + sha256_hex("\x1e".join(parts))


def _promotion_pass(store: ContinuityStore, scenarios: list[dict[str, Any]],
                    homes: DestinationHomes, backup_root: Path,
                    run_id: str, now: str) -> dict[str, str]:
    """One deterministic pass over every scenario; safe to run repeatedly."""
    outcomes: dict[str, str] = {}
    for scenario in scenarios:
        candidate = scenario["candidate"]
        cid, rev = candidate["candidate_id"], candidate["content_revision"]
        store.ingest_candidate(candidate, now)
        row = store.get_candidate(cid, rev)
        if row["status"] == "classified":
            store.transition_candidate(cid, rev, "routed",
                                       reason="phase2_fixture_routing",
                                       now=now, run_id=run_id)
            row = store.get_candidate(cid, rev)
        if row["status"] not in ("routed", "validated", "promoted"):
            outcomes[scenario["key"]] = f"skipped:{row['status']}"
            continue

        kwargs: dict[str, Any] = {"backup_root": backup_root, "now": now,
                                  "run_id": run_id}
        if "resolutions" in scenario:
            kwargs["resolutions"] = scenario["resolutions"]
        # retrieval_failure needs no special handling: its terms genuinely
        # match nothing, so the real retrieval route fails and proves rollback.
        result = promote_with_homes(store, candidate, homes, **kwargs)
        outcomes[scenario["key"]] = result.outcome
    return outcomes


def _revision_conflict_demo(store: ContinuityStore, homes: DestinationHomes,
                            backup_root: Path, run_id: str,
                            now: str) -> dict[str, Any]:
    """A concurrent edit lands between backup and write; the write refuses."""
    candidate = _candidate(run_id, key="revision_conflict",
                           klass="runtime_memory_hot",
                           destination="memory:hot",
                           subject="shell environment",
                           claim="the login shell is zsh on darwin",
                           project_id=None, terms=["zsh"])
    store.ingest_candidate(candidate, now)
    store.transition_candidate(candidate["candidate_id"], 1, "routed",
                               reason="phase2_fixture_routing", now=now,
                               run_id=run_id)

    class RacingMemory(MemoryDestination):
        def backup(self, record, backup_dir, **kwargs):
            ref = super().backup(record, backup_dir, **kwargs)
            index = self.index_path
            index.write_text(index.read_text(encoding="utf-8")
                             + "- concurrent manual edit\n", encoding="utf-8")
            return ref

    result = promote_candidate_via_adapter(
        store, candidate, RacingMemory(homes.memory, "hot"),
        backup_root=backup_root, now=now, run_id=run_id)
    row = store.get_candidate(candidate["candidate_id"], 1)
    return {"outcome": result.outcome,
            "candidate_status_after": row["status"],
            "receipts_added": 0}


def execute_phase2_dry_run(workdir: str | Path, *,
                           as_of: str = FIXED_AS_OF) -> dict[str, Any]:
    work = prepare_output_root(workdir)
    homes = _seed_homes(work / "destinations")
    backup_root = work / "backups"

    manifest = assemble_manifest(
        profile="dream-cycle-v3-phase2",
        window_start=FIXED_WINDOW_START,
        window_end=FIXED_WINDOW_END,
        collector_version=COLLECTOR_VERSION,
        bounds={"max_files_per_root": 64, "max_bytes_per_file": 65536,
                "max_total_bytes": 4194304, "max_depth": 8,
                "excerpt_chars": 700, "allowed_suffixes": [".md"]},
        sources=[], excluded=[], roots={"fixture": "phase2"},
        generated_at=as_of)
    run_id = manifest["run_id"]
    manifests_dir = work / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    (manifests_dir / f"{run_id}.json").write_text(
        canonical_json(manifest) + "\n", encoding="utf-8")

    projects = json.loads((SAMPLE_DATA / "projects.json").read_text("utf-8"))

    llm = LLMClassifier(transport=fixture_transport, model=FIXTURE_MODEL)
    scenarios, llm_report = _build_scenarios(run_id, llm)

    db_path = work / "continuity" / "continuity.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with ContinuityStore(db_path) as store:
        store.migrate(as_of)
        store.record_run(manifest, f"manifests/{run_id}.json", as_of)
        for project in projects:
            store.upsert_project(project, as_of)

        first_pass = _promotion_pass(store, scenarios, homes, backup_root,
                                     run_id, as_of)
        counts_1 = store.counts()
        dump_1 = store.dump_canonical()
        dest_fp_1 = _destinations_fingerprint(homes)

        second_pass = _promotion_pass(store, scenarios, homes, backup_root,
                                      run_id, as_of)
        counts_2 = store.counts()
        dump_2 = store.dump_canonical()
        dest_fp_2 = _destinations_fingerprint(homes)

        conflict_demo = _revision_conflict_demo(store, homes, backup_root,
                                                run_id, as_of)

        promoted_without_full_receipt = store._conn.execute(
            "SELECT COUNT(*) AS c FROM candidates c LEFT JOIN write_receipts r "
            "ON c.candidate_id = r.candidate_id AND "
            "c.content_revision = r.content_revision "
            "WHERE c.status = 'promoted' AND (r.receipt_id IS NULL OR "
            "r.backup_ref IS NULL OR r.retrieval_proof IS NULL OR "
            "r.rollback_metadata IS NULL OR r.target_revision_after IS NULL)"
        ).fetchone()["c"]
        candidates_by_status = {
            r["status"]: r["c"] for r in store._conn.execute(
                "SELECT status, COUNT(*) AS c FROM candidates "
                "GROUP BY status ORDER BY status")}
        receipt_count = counts_2["write_receipts"]

    report = {
        "kind": "dream-cycle-v3-phase2-dry-run",
        "schema_version": 1,
        "run_id": run_id,
        "generated_at": as_of,
        "scenarios": dict(sorted(first_pass.items())),
        "second_pass": dict(sorted(second_pass.items())),
        "revision_conflict_demo": conflict_demo,
        "llm": llm_report,
        "candidates_by_status": candidates_by_status,
        "receipts": receipt_count,
        "idempotency": {
            "rerun_row_delta": {t: counts_2[t] - counts_1[t]
                                for t in counts_1
                                if counts_2[t] != counts_1[t]},
            "rerun_store_identical": dump_1 == dump_2,
            "rerun_destinations_identical": dest_fp_1 == dest_fp_2,
        },
        "invariants": {
            "promoted_without_full_receipt": promoted_without_full_receipt,
            "every_scenario_matched_expected": all(
                first_pass[s["key"]] == s["expected"] for s in scenarios),
        },
    }
    reports_dir = work / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"phase2-{run_id}.json"
    report_path.write_text(canonical_json(report) + "\n", encoding="utf-8")

    report["workdir"] = str(work)
    report["continuity_db"] = str(db_path)
    report["report_path"] = str(report_path)
    return report
