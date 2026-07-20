"""End-to-end dry run over packaged sample data.

Everything is written inside one caller-selected workdir; nothing outside it
is touched. Defaults are pinned (window, as-of, date, mtimes) so two dry runs
in fresh workdirs produce the same run_id — the dry run demonstrates the
determinism contract rather than merely asserting it. It also executes an
identical second pass and reports the row delta, which must be zero.

Pipeline: copy sample tree -> collect (immutable manifest) -> migrate store ->
record run -> register projects -> adapter snapshots (sample kanban DB built
inside the workdir, todoist export fixture, GitHub deliberately unavailable)
-> deterministic classification (abstain -> quarantine) -> seed threads ->
carry-forward -> rerun everything -> machine-readable report.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import CONTRACT_SCHEMA_VERSION
from .adapters import read_github_issues, read_kanban_board, read_todoist_tasks
from .canonical import stable_id
from .carry_forward import CarryForwardPolicy, run_carry_forward
from .classifier import ClassificationInput, ClassifierPipeline, classify_or_quarantine
from .collect import CollectionBounds, collect_to_manifest
from .contracts import parse_iso_datetime
from .report import build_run_report, write_report
from .roots import CollectionRoots, prepare_output_root
from .routing import RoutingDecision, route_observation
from .store import ContinuityStore

SAMPLE_DATA = Path(__file__).parent / "sample_data"

# Pinned so the dry run is reproducible by default; flags may override date.
FIXED_WINDOW_START = "2026-07-09T00:00:00+00:00"
FIXED_WINDOW_END = "2026-07-12T00:00:00+00:00"
FIXED_AS_OF = "2026-07-11T08:00:00+00:00"
FIXED_DATE = "2026-07-11"
FIXED_SOURCE_MTIME = 1783684800  # 2026-07-10T12:00:00Z, inside the window

_DESTINATIONS = {
    "task_thread": "ledger:threads",
    "decision_record": "project:decisions",
    "runtime_memory_warm": "memory:warm",
    "runtime_memory_hot": "memory:hot",
    "reference_knowledge": "knowledge:reference",
    "project_context": "project:context",
    "ephemeral": "expiry:queue",
}
_FRESHNESS = {
    "task_thread": "days",
    "decision_record": "durable",
    "runtime_memory_warm": "durable",
    "runtime_memory_hot": "durable",
    "reference_knowledge": "durable",
    "project_context": "months",
    "ephemeral": "ephemeral",
}
_VALIDATION = {
    "task_thread": ["task_ssot_link"],
    "decision_record": ["explicit_decision_evidence"],
    "runtime_memory_warm": ["explicit_user_statement_or_repeated_behavior"],
    "runtime_memory_hot": ["explicit_user_statement_or_repeated_behavior"],
    "reference_knowledge": ["reproduced_failure_and_verified_fix"],
    "project_context": ["registry_match"],
    "ephemeral": [],
}

_MIN_OBSERVATION_CHARS = 12
_MAX_OBSERVATIONS_PER_SOURCE = 20


def _normalize_claim(line: str) -> str:
    return " ".join(line.split())[:4000]


def observations_from_manifest(manifest: dict[str, Any]) -> list[ClassificationInput]:
    """Deterministic observation extraction from manifest excerpts."""
    observations: list[ClassificationInput] = []
    for source in manifest["sources"]:
        excerpt = source["excerpt"]
        if not excerpt:
            continue
        taken = 0
        for line_no, line in enumerate(excerpt.splitlines()):
            line = line.strip().lstrip("-* ").strip()
            if len(line) < _MIN_OBSERVATION_CHARS:
                continue
            if taken >= _MAX_OBSERVATIONS_PER_SOURCE:
                break
            taken += 1
            observations.append(ClassificationInput(
                item_id=stable_id("dream-cycle-v3-observation",
                                  source["source_id"], str(line_no)),
                text=line,
                source_id=source["source_id"],
            ))
    return observations


def _destination_for(klass: str, project_id: str | None) -> str:
    base = _DESTINATIONS[klass]
    if klass in ("decision_record", "project_context") and project_id:
        return f"project:{project_id}:{base.split(':', 1)[1]}"
    return base


def _build_candidate(*, manifest: dict[str, Any], source: dict[str, Any],
                     source_ref: str, klass: str, status: str, destination: str,
                     project_id: str | None, claim: str, excerpt: str | None,
                     confidence: float, freshness: str,
                     validation: list[str], classifier_kind: str,
                     classifier_version: str, model: str | None = None,
                     prompt_hash: str | None = None) -> dict[str, Any]:
    subject = claim.lower()[:120]
    evidence: dict[str, Any] = {
        "source_type": source["source_type"],
        "source_id": source["source_id"],
        "location": source["location"],
        "observed_at": source["mtime_utc"],
        "fingerprint": source["fingerprint"],
    }
    if excerpt is not None:
        evidence["excerpt"] = excerpt[:1000]
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "candidate_id": stable_id("dream-cycle-v3-candidate", source_ref, claim),
        "content_revision": 1,
        "class": klass,
        "project_id": project_id,
        "destination": destination,
        "normalized_claim": claim,
        "canonical_subject": subject,
        "retrieval_terms": [],
        "evidence_refs": [evidence],
        "confidence": confidence,
        "freshness_class": freshness,
        "sensitivity_class": "normal",
        "dedupe_key": stable_id("dream-cycle-v3-dedupe", destination,
                                project_id or "", subject, claim,
                                str(CONTRACT_SCHEMA_VERSION)),
        "semantic_cluster_id": None,
        "status": status,
        "validation_requirements": validation,
        "conflict_set": [],
        "provenance": {
            "run_id": manifest["run_id"],
            "collector_version": manifest["collector_version"],
            "classifier_kind": classifier_kind,
            "classifier_version": classifier_version,
            "model": model,
            "prompt_hash": prompt_hash,
        },
    }


def candidates_from_outcomes(manifest: dict[str, Any],
                             observations: list[ClassificationInput],
                             classified: list, quarantined: list,
                             registry: list[dict[str, Any]],
                             ) -> list[tuple[dict[str, Any], RoutingDecision | None]]:
    """Build candidates with deterministic routing; never a default project.

    Classified observations are routed by explicit task ref -> canonical path
    -> alias. Routed items become 'classified' candidates carrying the routing
    proof; ambiguous/unresolved items are quarantined with no project. All
    observations here come from non-session excerpts — the collector never
    excerpts transcripts — so raw claim text is transcript-free by construction.
    """
    by_id = {o.item_id: o for o in observations}
    sources = {s["source_id"]: s for s in manifest["sources"]}
    out: list[tuple[dict[str, Any], RoutingDecision | None]] = []
    for outcome in list(classified) + list(quarantined):
        obs = by_id[outcome.item_id]
        source = sources[obs.source_id]
        claim = _normalize_claim(obs.text)
        if outcome.decision == "abstain":
            out.append((_build_candidate(
                manifest=manifest, source=source, source_ref=obs.source_id,
                klass="quarantine", status="quarantined",
                destination="quarantine", project_id=None, claim=claim,
                excerpt=obs.text, confidence=outcome.confidence,
                freshness="ephemeral", validation=["human_review"],
                classifier_kind=outcome.classifier_kind,
                classifier_version=outcome.classifier_version,
                model=outcome.model, prompt_hash=outcome.prompt_hash), None))
            continue

        routing = route_observation(text=obs.text, source_id=obs.source_id,
                                    registry=registry)
        klass = outcome.candidate_class
        if routing.routed:
            out.append((_build_candidate(
                manifest=manifest, source=source, source_ref=obs.source_id,
                klass=klass, status="classified",
                destination=_destination_for(klass, routing.project_id),
                project_id=routing.project_id, claim=claim, excerpt=obs.text,
                confidence=outcome.confidence, freshness=_FRESHNESS[klass],
                validation=_VALIDATION[klass],
                classifier_kind=outcome.classifier_kind,
                classifier_version=outcome.classifier_version,
                model=outcome.model, prompt_hash=outcome.prompt_hash), routing))
        else:
            # Routing abstained: quarantine, never a default project (§5).
            out.append((_build_candidate(
                manifest=manifest, source=source, source_ref=obs.source_id,
                klass=klass, status="quarantined", destination="quarantine",
                project_id=None, claim=claim, excerpt=obs.text,
                confidence=outcome.confidence, freshness="ephemeral",
                validation=[f"routing_{routing.method}_review"],
                classifier_kind=outcome.classifier_kind,
                classifier_version=outcome.classifier_version,
                model=outcome.model, prompt_hash=outcome.prompt_hash), routing))
    return out


def session_stub_candidates(manifest: dict[str, Any]
                            ) -> list[tuple[dict[str, Any], RoutingDecision | None]]:
    """Metadata-only quarantine entries for transcript sources.

    Session content is withheld under the transcript policy, so each session
    source is represented by a stub whose claim references only the stable
    source id and fingerprint — quarantine without raw content.
    """
    out: list[tuple[dict[str, Any], RoutingDecision | None]] = []
    for source in manifest["sources"]:
        if source["excerpt_suppressed"] != "session_transcript":
            continue
        claim = (f"session source {source['source_id']} "
                 f"({source['fingerprint'][:23]}) withheld under transcript "
                 "policy; awaiting runtime semantic classification")
        out.append((_build_candidate(
            manifest=manifest, source=source, source_ref=source["source_id"],
            klass="quarantine", status="quarantined", destination="quarantine",
            project_id=None, claim=claim, excerpt=None, confidence=0.0,
            freshness="ephemeral",
            validation=["human_review", "transcript_policy"],
            classifier_kind="deterministic",
            classifier_version="transcript-policy-1"), None))
    return out


def _build_sample_kanban_db(target: Path) -> Path:
    """Materialize the sample board inside the workdir from the seed SQL."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return target
    seed = (SAMPLE_DATA / "kanban_seed.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(target)
    try:
        conn.executescript(seed)
        conn.commit()
    finally:
        conn.close()
    return target


def _copy_sample_tree(dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(SAMPLE_DATA / "profile", dest)
    for path in sorted(dest.rglob("*")):
        os.utime(path, (FIXED_SOURCE_MTIME, FIXED_SOURCE_MTIME))
    os.utime(dest, (FIXED_SOURCE_MTIME, FIXED_SOURCE_MTIME))


def _ingest_pass(store: ContinuityStore, manifest: dict[str, Any],
                 candidates: list[tuple[dict[str, Any], RoutingDecision | None]],
                 threads: list[dict[str, Any]],
                 adapter_results: list, manifest_path: Path,
                 now: str) -> dict[str, int]:
    outcomes: dict[str, int] = {}

    def bump(key: str) -> None:
        outcomes[key] = outcomes.get(key, 0) + 1

    bump(f"run_{store.record_run(manifest, str(manifest_path), now)}")
    for result in adapter_results:
        status = store.record_adapter_snapshot(
            run_id=manifest["run_id"], adapter=result.adapter,
            source_locator=result.source_locator, status=result.status,
            detail=result.detail, items=result.items_payload(), now=now)
        bump(f"snapshot_{status}")
    for candidate, routing in candidates:
        outcome = store.ingest_candidate(
            candidate, now, routing=routing.to_payload() if routing else None)
        bump(f"candidate_{outcome}")
    for thread in threads:
        bump(f"thread_{store.open_thread(thread, now, run_id=manifest['run_id'])}")
    return outcomes


def execute_dry_run(workdir: str | Path, *, date: str = FIXED_DATE,
                    as_of: str = FIXED_AS_OF, keep_workdir: bool = True
                    ) -> dict[str, Any]:
    del keep_workdir  # caller-owned; retained for CLI symmetry
    work = prepare_output_root(workdir)
    sources_root = work / "sources" / "profile"
    _copy_sample_tree(sources_root)
    kanban_db = _build_sample_kanban_db(work / "sources" / "kanban" / "kanban.db")

    roots = CollectionRoots.resolve("dream-cycle-v3-sample",
                                    {"profile": sources_root})
    window_start = datetime.fromisoformat(FIXED_WINDOW_START)
    window_end = datetime.fromisoformat(FIXED_WINDOW_END)
    generated_at = parse_iso_datetime(as_of)
    out_dir = work / "continuity"

    manifest, manifest_path = collect_to_manifest(
        roots, out_dir, window_start=window_start, window_end=window_end,
        bounds=CollectionBounds(), generated_at=generated_at)

    projects = json.loads((SAMPLE_DATA / "projects.json").read_text(encoding="utf-8"))
    threads = json.loads((SAMPLE_DATA / "threads.json").read_text(encoding="utf-8"))

    adapter_results = [
        read_kanban_board(kanban_db, board_key="sample-board"),
        read_todoist_tasks(export_path=SAMPLE_DATA / "todoist_export.json"),
        # Deliberately unavailable: a dry run must not touch the network.
        read_github_issues("octocat/hello-world", gh_available=False),
    ]

    observations = observations_from_manifest(manifest)
    classified, quarantined = classify_or_quarantine(
        observations, ClassifierPipeline())
    candidates = candidates_from_outcomes(
        manifest, observations, classified, quarantined,
        registry=projects) + session_stub_candidates(manifest)

    routing_summary: dict[str, int] = {}
    for _, routing in candidates:
        key = routing.method if routing is not None else "no_routing_attempted"
        routing_summary[key] = routing_summary.get(key, 0) + 1

    db_path = out_dir / "continuity.db"
    with ContinuityStore(db_path) as store:
        store.migrate(as_of)
        for project in projects:
            store.upsert_project(project, as_of)
        first_pass = _ingest_pass(store, manifest, candidates, threads,
                                  adapter_results, manifest_path, as_of)
        carry_1 = run_carry_forward(store, run_id=manifest["run_id"],
                                    disposition_date=date, now=as_of,
                                    policy=CarryForwardPolicy())
        counts_before = store.counts()
        dump_before = store.dump_canonical()

        # Identical second pass: collect again, re-ingest, re-carry-forward.
        manifest_2, _ = collect_to_manifest(
            roots, out_dir, window_start=window_start, window_end=window_end,
            bounds=CollectionBounds(), generated_at=generated_at)
        if manifest_2["run_id"] != manifest["run_id"]:
            raise AssertionError("rerun produced a different run_id")
        second_pass = _ingest_pass(store, manifest_2, candidates, threads,
                                   adapter_results, manifest_path, as_of)
        carry_2 = run_carry_forward(store, run_id=manifest["run_id"],
                                    disposition_date=date, now=as_of,
                                    policy=CarryForwardPolicy())
        counts_after = store.counts()
        dump_after = store.dump_canonical()

        delta = {t: counts_after[t] - counts_before[t] for t in counts_before
                 if counts_after[t] != counts_before[t]}
        idempotency = {
            "rerun_run_id_identical": True,
            "rerun_row_delta": delta,
            "rerun_store_identical": dump_before == dump_after,
            "rerun_all_dispositions_pre_existing":
                carry_2.already_dispositioned == carry_2.selected,
            "first_pass": dict(sorted(first_pass.items())),
            "second_pass": dict(sorted(second_pass.items())),
        }
        report = build_run_report(store, manifest["run_id"], generated_at=as_of,
                                  carry_forward=carry_1.to_dict(),
                                  idempotency=idempotency,
                                  routing=dict(sorted(routing_summary.items())))
        report_path = write_report(report, work / "reports")

    report["report_path"] = str(report_path)
    report["workdir"] = str(work)
    report["manifest_path"] = str(manifest_path)
    report["continuity_db"] = str(db_path)
    return report
